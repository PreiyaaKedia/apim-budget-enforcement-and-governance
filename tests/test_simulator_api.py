from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from local_simulator import create_simulator_app


def test_local_simulator_http_flow(tmp_path: Path) -> None:
    client = TestClient(create_simulator_app(tmp_path / "state.json"))
    price = {
        "id": "model-a",
        "deployment": "model-a",
        "model": "*",
        "modelVersion": "*",
        "region": "local",
        "deploymentType": "Simulation",
        "currency": "USD",
        "unit": "usdPerMillionTokens",
        "inputUsdPerMillion": "5",
        "cacheWriteUsdPerMillion": "6.25",
        "cacheReadUsdPerMillion": "0.5",
        "outputUsdPerMillion": "25",
        "maxOutputTokens": 32768,
        "effectiveFrom": "2026-09-01T00:00:00Z",
        "effectiveTo": None,
        "source": "test",
    }

    assert client.put("/api/prices/model-a", json=price).status_code == 200
    assert client.put("/api/users/alice", json={"limitUsd": "0.01"}).status_code == 200
    response = client.post(
        "/api/simulations",
        json={
            "userId": "alice",
            "deployment": "model-a",
            "requestedMaxOutputTokens": 1000,
        },
    )

    assert response.status_code == 200
    assert response.json()["allowed"] is False
    assert response.json()["reason"] == "budget_exceeded"
    assert client.post("/api/users/alice/reset").status_code == 200
    assert client.get("/api/state").json()["users"]["alice"]["remainingUsd"] == "0.010000"


def test_local_simulator_serves_workbench(tmp_path: Path) -> None:
    client = TestClient(create_simulator_app(tmp_path / "state.json"))

    page = client.get("/")

    assert page.status_code == 200
    assert "Cost Budget Lab" in page.text
    assert client.get("/assets/styles.css").status_code == 200
    assert client.get("/assets/app.js").status_code == 200


def test_live_admin_proxy_sets_team_per_user_budget(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    price = {
        "id": "model-a",
        "deployment": "model-a",
        "model": "*",
        "modelVersion": "*",
        "region": "global",
        "deploymentType": "GlobalStandard",
        "currency": "USD",
        "unit": "usdPerMillionTokens",
        "inputUsdPerMillion": "5",
        "cacheWriteUsdPerMillion": "6.25",
        "cacheReadUsdPerMillion": "0.5",
        "outputUsdPerMillion": "25",
        "maxOutputTokens": 32768,
        "effectiveFrom": "2026-09-01T00:00:00Z",
        "effectiveTo": None,
        "source": "test",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/admin/budget/teams/Team.Engineering" and request.method == "PUT":
            return httpx.Response(
                200,
                json={
                    "teamRole": "Team.Engineering",
                    "perUserLimitUsd": "100.00",
                },
            )
        if request.url.path == "/v1/admin/budget/teams" and request.method == "GET":
            return httpx.Response(200, json=[{
                "teamRole": "Team.Engineering",
                "perUserLimitUsd": "100.00",
            }])
        if request.url.path == "/v1/admin/budget/metrics" and request.method == "GET":
            return httpx.Response(200, json={
                "period": "2026-09",
                "teams": [{
                    "teamRole": "Team.Engineering",
                    "period": "2026-09",
                    "activeUsers": 2,
                    "allocatedUsd": "200.00",
                    "spentUsd": "42.00",
                    "reservedUsd": "8.00",
                    "remainingUsd": "150.00",
                }],
                "users": [{
                    "userKey": "user:tenant:111",
                    "userEmail": "one@example.com",
                    "userName": "One",
                    "teamRole": "Team.Engineering",
                    "period": "2026-09",
                    "allocatedUsd": "100.00",
                    "spentUsd": "42.00",
                    "reservedUsd": "8.00",
                    "remainingUsd": "50.00",
                }],
            })
        if request.url.path == "/v1/admin/prices" and request.method == "GET":
            return httpx.Response(200, json=[price])
        if request.url.path == "/v1/admin/prices/model-a" and request.method == "PUT":
            return httpx.Response(200, json=price)
        if request.url.path == "/v1/admin/prices/model-a/model-a" and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    client = TestClient(
        create_simulator_app(
            tmp_path / "state.json",
            cost_api_url="https://cost.example",
            transport=httpx.MockTransport(handler),
        )
    )

    policy = client.put(
        "/api/admin/budget/teams/Team.Engineering",
        json={"perUserLimitUsd": "100.00"},
    )
    price_update = client.put("/api/prices/model-a", json=price)
    state = client.get("/api/state").json()
    price_delete = client.delete("/api/prices/model-a")

    assert policy.status_code == 200
    assert price_update.status_code == 200
    assert price_delete.status_code == 204
    assert state["backendMode"] == "live"
    assert state["prices"]["model-a"]["outputUsdPerMillion"] == "25"
    assert state["teams"]["Team.Engineering"]["perUserLimitUsd"] == "100.00"
    assert state["budgetMetrics"]["teams"][0]["spentUsd"] == "42.00"
    assert state["budgetMetrics"]["users"][0]["userEmail"] == "one@example.com"
    assert any(request.url.path == "/v1/admin/budget/teams/Team.Engineering" for request in requests)


def test_live_admin_reports_backend_upgrade_required_for_legacy_api(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/admin/prices":
            return httpx.Response(200, json=[])
        if request.url.path == "/v1/admin/budget/teams":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(404)

    client = TestClient(
        create_simulator_app(
            tmp_path / "state.json",
            cost_api_url="https://legacy-cost.example",
            transport=httpx.MockTransport(handler),
        )
    )

    response = client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["backendMode"] == "live"
    assert response.json()["backendCompatible"] is False
    assert "Deploy the latest cost API" in response.json()["backendError"]


def test_dashboard_analytics_are_proxied_server_side(tmp_path: Path) -> None:
    class AnalyticsStub:
        def dashboard(self, **filters: object) -> dict[str, object]:
            return {
                "available": True,
                "filtersReceived": filters,
                "summary": {"requests": 7, "tokens": 4200, "spendUsd": 0.42},
            }

    client = TestClient(
        create_simulator_app(
            tmp_path / "state.json",
            analytics_provider=AnalyticsStub(),
        )
    )

    response = client.get(
        "/api/analytics",
        params={"days": 14, "team": "Team.Engineering", "app_id": "client-a", "model": "gpt-4.1"},
    )

    assert response.status_code == 200
    assert response.json()["summary"]["requests"] == 7
    assert response.json()["filtersReceived"] == {
        "days": 14,
        "team": "Team.Engineering",
        "app": "client-a",
        "model": "gpt-4.1",
    }


def test_dashboard_reports_unconfigured_analytics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LOG_ANALYTICS_WORKSPACE_ID", raising=False)
    client = TestClient(create_simulator_app(tmp_path / "state.json"))

    response = client.get("/api/analytics?days=7")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["rangeDays"] == 7
    assert "not configured" in response.json()["reason"]