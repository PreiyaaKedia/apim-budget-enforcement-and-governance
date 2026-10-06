from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from local_simulator import _verify_microsoft_id_token, create_simulator_app, hash_owner_password


def microsoft_client(tmp_path: Path, monkeypatch, email: str = "test@example.com") -> TestClient:
    monkeypatch.setenv("CONSOLE_ENTRA_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CONSOLE_ENTRA_TENANT_ID", "test-tenant-id")
    return TestClient(create_simulator_app(
        tmp_path / "state.json",
        allowed_email_domains={"example.com"},
        owner_session_secret="test-session-secret-at-least-32-bytes-long",
        owner_cookie_secure=False,
        entra_token_verifier=lambda _token: {
            "oid": "employee-object-id",
            "name": "Test User",
            "preferred_username": email,
        },
    ))


def test_microsoft_token_verifier_accepts_multitenant_issuer() -> None:
    tenant_id = "11111111-2222-3333-4444-555555555555"
    client_id = "console-client-id"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "employee-object-id",
            "tid": tenant_id,
            "aud": client_id,
            "iss": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=key.public_key())
    )

    claims = _verify_microsoft_id_token(token, client_id, jwks_client)

    assert claims["tid"] == tenant_id
    assert claims["aud"] == client_id


def test_microsoft_token_verifier_rejects_wrong_audience() -> None:
    tenant_id = "11111111-2222-3333-4444-555555555555"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": "employee-object-id",
            "tid": tenant_id,
            "aud": "another-client",
            "iss": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=key.public_key())
    )

    with pytest.raises(jwt.InvalidAudienceError):
        _verify_microsoft_id_token(token, "console-client-id", jwks_client)


def test_local_simulator_http_flow(tmp_path: Path) -> None:
    client = TestClient(create_simulator_app(tmp_path / "state.json", console_auth_disabled=True))
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
    client = TestClient(create_simulator_app(tmp_path / "state.json", console_auth_disabled=True))

    page = client.get("/")

    assert page.status_code == 200
    assert "Cost Budget Lab" in page.text
    assert "Sign in as owner" in page.text
    assert "Sign in with Microsoft" in page.text
    assert "msal-browser.min.js?v=4.28.1" in page.text
    assert 'name="cost-console-ui-version"' in page.text
    assert 'content="2026-09-30.1"' in page.text
    assert page.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert page.headers["clear-site-data"] == '"cache"'
    assert page.headers["expires"] == "0"
    assert page.headers["x-cost-console-release"] == "2026-09-30.1"
    styles = client.get("/assets/styles.css")
    script = client.get("/assets/app.js")
    assert styles.status_code == 200
    assert script.status_code == 200
    assert styles.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert script.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"


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
            console_auth_disabled=True,
            remote_token_provider=lambda: "managed-identity-token",
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
    assert all(request.headers["Authorization"] == "Bearer managed-identity-token" for request in requests)


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
            console_auth_disabled=True,
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
            console_auth_disabled=True,
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
    client = TestClient(create_simulator_app(tmp_path / "state.json", console_auth_disabled=True))

    response = client.get("/api/analytics?days=7")

    assert response.status_code == 200
    assert response.json()["available"] is False
    assert response.json()["rangeDays"] == 7
    assert "not configured" in response.json()["reason"]


def test_console_requires_authenticated_principal(tmp_path: Path) -> None:
    client = TestClient(create_simulator_app(tmp_path / "state.json", allowed_email_domains={"example.com"}))

    assert client.get("/api/session").status_code == 401
    assert client.get("/api/state").status_code == 401
    assert client.get("/health").status_code == 200


def test_employee_can_self_provision_as_member_but_cannot_mutate(tmp_path: Path, monkeypatch) -> None:
    client = microsoft_client(tmp_path, monkeypatch)

    config = client.get("/api/auth/microsoft/config")
    login = client.post("/api/auth/microsoft", json={"id_token": "test-id-token"})
    session = client.get("/api/session")
    state = client.get("/api/state")
    mutation = client.put(
        "/api/users/alice",
        json={"limitUsd": "1.00"},
    )

    assert config.status_code == 200
    assert config.json() == {
        "clientId": "test-client-id",
        "authority": "https://login.microsoftonline.com/test-tenant-id",
        "scopes": ["openid", "profile", "email"],
    }
    assert login.status_code == 200
    assert session.status_code == 200
    assert session.json()["isAdmin"] is False
    assert state.status_code == 200
    assert mutation.status_code == 403


def test_owner_can_log_in_and_mutate_console_state(tmp_path: Path) -> None:
    client = TestClient(create_simulator_app(
        tmp_path / "state.json",
        allowed_email_domains={"example.com"},
        owner_username="owner@example.com",
        owner_password_hash=hash_owner_password("correct horse", salt=b"0123456789abcdef"),
        owner_session_secret="test-session-secret-at-least-32-bytes-long",
        owner_cookie_secure=False,
    ))

    login = client.post(
        "/api/auth/login",
        json={"username": "owner@example.com", "password": "correct horse"},
    )

    session = client.get("/api/session")
    mutation = client.put(
        "/api/users/alice",
        json={"limitUsd": "1.00"},
    )

    assert login.status_code == 200
    assert session.status_code == 200
    assert session.json()["isAdmin"] is True
    assert mutation.status_code == 200

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/session").status_code == 401


def test_console_rejects_employee_outside_allowed_domain(tmp_path: Path, monkeypatch) -> None:
    client = microsoft_client(tmp_path, monkeypatch, "user@outside.example")

    response = client.post("/api/auth/microsoft", json={"id_token": "test-id-token"})

    assert response.status_code == 403


def test_console_rejects_invalid_owner_password(tmp_path: Path) -> None:
    client = TestClient(create_simulator_app(
        tmp_path / "state.json",
        owner_username="owner@example.com",
        owner_password_hash=hash_owner_password("correct horse", salt=b"0123456789abcdef"),
        owner_session_secret="test-session-secret-at-least-32-bytes-long",
        owner_cookie_secure=False,
    ))

    response = client.post(
        "/api/auth/login",
        json={"username": "owner@example.com", "password": "wrong"},
    )

    assert response.status_code == 401


def test_console_rejects_invalid_microsoft_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_ENTRA_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CONSOLE_ENTRA_TENANT_ID", "test-tenant-id")
    client = TestClient(create_simulator_app(
        tmp_path / "state.json",
        allowed_email_domains={"example.com"},
        owner_session_secret="test-session-secret-at-least-32-bytes-long",
        owner_cookie_secure=False,
        entra_token_verifier=lambda _token: (_ for _ in ()).throw(jwt.InvalidTokenError("invalid token")),
    ))

    response = client.post("/api/auth/microsoft", json={"id_token": "invalid"})

    assert response.status_code == 401