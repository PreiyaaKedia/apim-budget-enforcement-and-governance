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


def test_live_admin_proxy_sets_email_override_and_probes_real_ledger(tmp_path: Path) -> None:
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
        if request.url.path == "/v1/admin/budget/users/alex@contoso.com":
            return httpx.Response(
                200,
                json={
                    "callerKey": "email:alex@contoso.com",
                    "period": "2026-09",
                    "limitUsd": "0.10",
                    "spentUsd": "0.02",
                    "reservedUsd": "0",
                    "remainingUsd": "0.08",
                },
            )
        if request.url.path == "/v1/admin/budget/probe":
            return httpx.Response(200, json={"allowed": False, "amountUsd": "0.09", "remainingUsd": "0.08", "reason": "budget_exceeded"})
        if request.url.path == "/v1/admin/budget/default":
            return httpx.Response(200, json={"limitUsd": "1.00"})
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

    override = client.put("/api/admin/budget/users/alex@contoso.com", json={"limitUsd": "0.10"})
    probe = client.post("/api/admin/budget/probe", json={"email": "alex@contoso.com", "amountUsd": "0.09"})
    price_update = client.put("/api/prices/model-a", json=price)
    state = client.get("/api/state").json()
    price_delete = client.delete("/api/prices/model-a")

    assert override.status_code == 200
    assert probe.json()["allowed"] is False
    assert price_update.status_code == 200
    assert price_delete.status_code == 204
    assert state["backendMode"] == "live"
    assert state["prices"]["model-a"]["outputUsdPerMillion"] == "25"
    assert state["users"]["alex@contoso.com"]["spentUsd"] == "0.02"
    assert any(request.url.raw_path == b"/v1/admin/budget/users/alex%40contoso.com" for request in requests)