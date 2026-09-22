from unittest.mock import Mock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth import EntraAuthMiddleware


def authenticated_client(monkeypatch, claims: dict) -> TestClient:
    monkeypatch.setenv("TENANT_ID", "tenant-id")
    monkeypatch.setenv("AUDIENCE", "cost-api")
    monkeypatch.setenv("ALLOWED_CLIENT_ID", "apim-mi")
    api = FastAPI()

    @api.get("/v1/reservations")
    def reservations(request: Request) -> dict:
        return request.scope["state"]["claims"]

    @api.get("/v1/admin/budget/default")
    def admin_budget() -> dict:
        return {"limitUsd": "1.00"}

    middleware = EntraAuthMiddleware(api)
    middleware.jwks = Mock()
    middleware.jwks.get_signing_key_from_jwt.return_value.key = "key"
    client = TestClient(middleware)
    patcher = patch("app.auth.jwt.decode", return_value=claims)
    patcher.start()
    client._decode_patcher = patcher
    return client


def test_reservation_accepts_apim_managed_identity(monkeypatch) -> None:
    client = authenticated_client(
        monkeypatch,
        {
            "iss": "https://login.microsoftonline.com/tenant-id/v2.0",
            "azp": "apim-mi",
            "tid": "tenant-id",
            "oid": "user-id",
        },
    )

    try:
        response = client.get("/v1/reservations", headers={"Authorization": "Bearer token"})
    finally:
        client._decode_patcher.stop()

    assert response.status_code == 200
    assert response.json()["oid"] == "user-id"


def test_admin_route_can_be_explicitly_unprotected_for_prototype(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_AUTH_DISABLED", "true")
    client = authenticated_client(
        monkeypatch,
        {},
    )

    try:
        response = client.get("/v1/admin/budget/default")
    finally:
        client._decode_patcher.stop()

    assert response.status_code == 200


def test_admin_route_accepts_shared_key_without_user_token(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    client = authenticated_client(monkeypatch, {})

    try:
        response = client.get("/v1/admin/budget/default", headers={"X-Admin-Key": "test-admin-key"})
    finally:
        client._decode_patcher.stop()

    assert response.status_code == 200