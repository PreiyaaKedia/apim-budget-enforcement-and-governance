from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from analytics import EMPTY_DASHBOARD, configured_analytics
from app.models import BudgetProbeRequest, PriceDocument, TeamBudgetUpdate
from app.simulator import BudgetUpdate, SimulationRequest, SimulationResponse, SimulatorStore


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "simulator_web"
LOGGER = logging.getLogger(__name__)
OWNER_COOKIE = "budget_console_owner"
OWNER_SESSION_ISSUER = "cost-budget-console"
MICROSOFT_JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"


@dataclass(frozen=True)
class ConsolePrincipal:
    name: str
    email: str
    roles: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return "owner" in self.roles


class OwnerLogin(BaseModel):
    username: str
    password: str


class MicrosoftLogin(BaseModel):
    id_token: str


def hash_owner_password(password: str, salt: bytes | None = None) -> str:
    iterations = 310_000
    password_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), password_salt, iterations)
    return "$".join((
        "pbkdf2_sha256",
        str(iterations),
        base64.urlsafe_b64encode(password_salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    ))


def _verify_owner_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        padding = lambda value: value + "=" * (-len(value) % 4)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.urlsafe_b64decode(padding(salt)),
            int(iterations),
        )
        return hmac.compare_digest(
            base64.urlsafe_b64encode(actual).decode().rstrip("="),
            expected,
        )
    except (ValueError, TypeError):
        return False


class BearerTokenAuth(httpx.Auth):
    def __init__(self, token_provider: Callable[[], str]) -> None:
        self.token_provider = token_provider

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self.token_provider()}"
        yield request


def create_simulator_app(
    state_path: Path | None = None,
    cost_api_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
    analytics_provider: Any | None = None,
    console_auth_disabled: bool | None = None,
    remote_token_provider: Callable[[], str] | None = None,
    allowed_email_domains: set[str] | None = None,
    owner_username: str | None = None,
    owner_password_hash: str | None = None,
    owner_session_secret: str | None = None,
    owner_cookie_secure: bool | None = None,
    entra_token_verifier: Callable[[str], dict[str, Any]] | None = None,
) -> FastAPI:
    store = SimulatorStore(
        state_path or ROOT / ".local" / "simulator-state.json",
        ROOT / "prices.example.json",
    )
    api = FastAPI(title="Cost Budget Lab", version="1.0.0")
    remote_url = cost_api_url or os.environ.get("COST_ENFORCEMENT_URL", "")
    remote_scope = os.environ.get("COST_ENFORCEMENT_SCOPE", "").strip()
    auth_disabled = console_auth_disabled
    if auth_disabled is None:
        auth_disabled = os.environ.get("CONSOLE_AUTH_DISABLED", "false").lower() == "true"
    allowed_domains = allowed_email_domains
    if allowed_domains is None:
        allowed_domains = {
            domain.strip().lower()
            for domain in os.environ.get("CONSOLE_ALLOWED_EMAIL_DOMAINS", "").split(",")
            if domain.strip()
        }
    configured_owner = (owner_username or os.environ.get("CONSOLE_OWNER_USERNAME", "")).strip()
    configured_owner_hash = owner_password_hash or os.environ.get("CONSOLE_OWNER_PASSWORD_HASH", "")
    session_secret = owner_session_secret or os.environ.get("CONSOLE_OWNER_SESSION_SECRET", "")
    entra_client_id = os.environ.get("CONSOLE_ENTRA_CLIENT_ID", "").strip()
    entra_tenant_id = os.environ.get("CONSOLE_ENTRA_TENANT_ID", "").strip()
    jwks_client = jwt.PyJWKClient(MICROSOFT_JWKS_URL, cache_keys=True)
    secure_owner_cookie = owner_cookie_secure
    if secure_owner_cookie is None:
        secure_owner_cookie = os.environ.get("CONSOLE_OWNER_COOKIE_SECURE", "true").lower() == "true"
    token_provider = remote_token_provider
    if remote_url and remote_scope and token_provider is None:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(process_timeout=60)
        token_provider = lambda: credential.get_token(remote_scope).token
    remote = httpx.Client(
        base_url=remote_url.rstrip("/"),
        auth=BearerTokenAuth(token_provider) if token_provider else None,
        timeout=10,
        transport=transport,
    ) if remote_url else None
    analytics = analytics_provider if analytics_provider is not None else configured_analytics()
    api.mount("/assets", StaticFiles(directory=WEB_ROOT, check_dir=False), name="assets")

    @api.middleware("http")
    async def disable_console_asset_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        if request.url.path == "/":
            response.headers["Clear-Site-Data"] = '"cache"'
            response.headers["X-Cost-Console-Release"] = "2026-09-30.1"
        return response

    def current_principal(request: Request) -> ConsolePrincipal:
        if auth_disabled:
            return ConsolePrincipal(
                name="Local Administrator",
                email="local@localhost",
                roles=frozenset({"owner", "member"}),
            )
        session_token = request.cookies.get(OWNER_COOKIE, "")
        if session_token and session_secret:
            try:
                claims = jwt.decode(
                    session_token,
                    session_secret,
                    algorithms=["HS256"],
                    issuer=OWNER_SESSION_ISSUER,
                )
                role = str(claims.get("role", ""))
                if role not in {"owner", "member"}:
                    raise jwt.InvalidTokenError("session role is invalid")
                if role == "owner" and claims.get("sub") != configured_owner:
                    raise jwt.InvalidTokenError("owner does not match configuration")
                return ConsolePrincipal(
                    name=str(claims.get("name", "")),
                    email=str(claims.get("email", claims.get("sub", ""))),
                    roles=frozenset({"member", role}),
                )
            except jwt.PyJWTError:
                pass
        raise HTTPException(status_code=401, detail="authenticated session is missing")

    def require_reader(principal: ConsolePrincipal = Depends(current_principal)) -> ConsolePrincipal:
        return principal

    def require_admin(principal: ConsolePrincipal = Depends(current_principal)) -> ConsolePrincipal:
        if not principal.is_admin:
            raise HTTPException(status_code=403, detail="owner role is required")
        return principal

    @api.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            WEB_ROOT / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Clear-Site-Data": '"cache"',
                "Expires": "0",
                "Pragma": "no-cache",
                "X-Cost-Console-Release": "2026-09-30.1",
            },
        )

    @api.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/api/auth/login")
    def owner_login(body: OwnerLogin, response: Response) -> dict[str, bool]:
        valid = (
            configured_owner
            and configured_owner_hash
            and session_secret
            and hmac.compare_digest(body.username.casefold(), configured_owner.casefold())
            and _verify_owner_password(body.password, configured_owner_hash)
        )
        if not valid:
            raise HTTPException(status_code=401, detail="username or password is incorrect")
        _set_session_cookie(
            response,
            session_secret,
            secure_owner_cookie,
            subject=configured_owner,
            email=configured_owner,
            name="Budget Owner",
            role="owner",
        )
        return {"authenticated": True}

    @api.get("/api/auth/microsoft/config", include_in_schema=False)
    def microsoft_config() -> dict[str, Any]:
        if not entra_client_id or not entra_tenant_id:
            raise HTTPException(status_code=503, detail="Microsoft sign-in is not configured")
        return {
            "clientId": entra_client_id,
            "authority": f"https://login.microsoftonline.com/{entra_tenant_id}",
            "scopes": ["openid", "profile", "email"],
        }

    @api.post("/api/auth/microsoft")
    def microsoft_login(body: MicrosoftLogin, response: Response) -> dict[str, bool]:
        if not entra_client_id or not session_secret:
            raise HTTPException(status_code=503, detail="Microsoft sign-in is not configured")
        try:
            identity = (
                entra_token_verifier(body.id_token)
                if entra_token_verifier
                else _verify_microsoft_id_token(body.id_token, entra_client_id, jwks_client)
            )
            email = str(identity.get("preferred_username") or identity.get("email") or "").lower()
            if not email:
                raise HTTPException(status_code=401, detail="Microsoft identity does not contain an email address")
            domain = email.rsplit("@", 1)[-1] if "@" in email else ""
            if not allowed_domains or domain not in allowed_domains:
                raise HTTPException(status_code=403, detail="account is not in an allowed employee domain")
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="Microsoft identity token is invalid") from exc
        _set_session_cookie(
            response,
            session_secret,
            secure_owner_cookie,
            subject=str(identity.get("oid") or identity.get("sub") or email),
            email=email,
            name=str(identity.get("name") or email),
            role="member",
        )
        return {"authenticated": True}

    @api.post("/api/auth/logout", status_code=204)
    def logout(response: Response) -> Response:
        response.delete_cookie(OWNER_COOKIE, path="/", secure=secure_owner_cookie, samesite="lax")
        response.status_code = 204
        return response

    @api.get("/api/session")
    def session(principal: ConsolePrincipal = Depends(require_reader)) -> dict[str, Any]:
        return {
            "name": principal.name,
            "email": principal.email,
            "roles": sorted(principal.roles),
            "isAdmin": principal.is_admin,
        }

    @api.get("/api/state")
    def state(_principal: ConsolePrincipal = Depends(require_reader)) -> dict:
        snapshot = store.snapshot()
        snapshot["backendMode"] = "live" if remote else "simulation"
        snapshot["backendCompatible"] = True
        snapshot["backendError"] = ""
        snapshot["defaultLimitUsd"] = None
        snapshot["teams"] = {}
        snapshot["budgetMetrics"] = {"period": "", "teams": [], "users": []}
        if remote:
            prices = _remote_json(remote, "GET", "/v1/admin/prices")
            snapshot["prices"] = {price["deployment"]: price for price in prices}
            try:
                policies = _remote_json(remote, "GET", "/v1/admin/budget/teams")
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                policies = []
                snapshot["backendCompatible"] = False
                snapshot["backendError"] = (
                    "Deploy the latest cost API revision; the configured backend "
                    "does not provide team-budget endpoints."
                )
            snapshot["teams"] = {
                policy["teamRole"]: policy
                for policy in policies
            }
            if snapshot["backendCompatible"]:
                snapshot["budgetMetrics"] = _remote_json(remote, "GET", "/v1/admin/budget/metrics")
            snapshot["users"] = {}
            snapshot["history"] = []
        for user in snapshot["users"].values():
            if remote:
                live = _remote_json(remote, "GET", f"/v1/admin/budget/users/{quote(user['userId'], safe='')}")
                user.update(live)
            else:
                user["remainingUsd"] = format(
                    max(Decimal("0"), Decimal(user["limitUsd"]) - Decimal(user["spentUsd"])),
                    "f",
                )
        return snapshot

    @api.get("/api/analytics")
    def dashboard_analytics(
        days: int = 30,
        team: str = "",
        app_id: str = "",
        model: str = "",
        _principal: ConsolePrincipal = Depends(require_reader),
    ) -> dict[str, Any]:
        if analytics is None:
            return {**EMPTY_DASHBOARD, "rangeDays": min(max(days, 1), 90)}
        try:
            return analytics.dashboard(days=days, team=team, app=app_id, model=model)
        except Exception as exc:
            LOGGER.warning("Azure Monitor dashboard query failed: %s", exc)
            return {
                **EMPTY_DASHBOARD,
                "rangeDays": min(max(days, 1), 90),
                "reason": "Azure Monitor query unavailable. Verify Azure sign-in, configuration, and Reader access.",
            }

    @api.put("/api/admin/budget/teams/{team_role}")
    def set_live_team(
        team_role: str,
        update: TeamBudgetUpdate,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        if remote is None:
            raise HTTPException(status_code=409, detail="COST_ENFORCEMENT_URL is not configured")
        return _remote_json(
            remote,
            "PUT",
            f"/v1/admin/budget/teams/{quote(team_role, safe='')}",
            update.model_dump(by_alias=True, mode="json"),
        )

    @api.put("/api/admin/budget/default")
    def set_live_default(
        update: BudgetUpdate,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        if remote is None:
            raise HTTPException(status_code=409, detail="COST_ENFORCEMENT_URL is not configured")
        return _remote_json(remote, "PUT", "/v1/admin/budget/default", {"limitUsd": str(update.limit_usd)})

    @api.put("/api/admin/budget/users/{email}")
    def set_live_user(
        email: str,
        update: BudgetUpdate,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        if remote is None:
            raise HTTPException(status_code=409, detail="COST_ENFORCEMENT_URL is not configured")
        result = _remote_json(
            remote,
            "PUT",
            f"/v1/admin/budget/users/{quote(email, safe='')}",
            {"email": email, "limitUsd": str(update.limit_usd)},
        )
        store.set_budget(email.lower(), update.limit_usd)
        return result

    @api.post("/api/admin/budget/probe")
    def probe_live_budget(
        body: BudgetProbeRequest,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        if remote is None:
            raise HTTPException(status_code=409, detail="COST_ENFORCEMENT_URL is not configured")
        return _remote_json(
            remote,
            "POST",
            "/v1/admin/budget/probe",
            body.model_dump(by_alias=True, mode="json"),
        )

    @api.post("/api/admin/reservations/reconcile")
    def reconcile_expired_reservations(
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> dict[str, Any]:
        if remote is None:
            raise HTTPException(status_code=409, detail="COST_ENFORCEMENT_URL is not configured")
        return _remote_json(remote, "POST", "/v1/admin/reservations/reconcile")

    @api.put("/api/users/{user_id}")
    def set_budget(
        user_id: str,
        update: BudgetUpdate,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> dict[str, str]:
        return store.set_budget(user_id, update.limit_usd)

    @api.post("/api/users/{user_id}/reset")
    def reset_budget(
        user_id: str,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> dict[str, str]:
        try:
            return store.reset_budget(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.put("/api/prices/{deployment}")
    def upsert_price(
        deployment: str,
        price: PriceDocument,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> dict:
        if price.deployment != deployment:
            raise HTTPException(status_code=400, detail="deployment path and body must match")
        if remote:
            return _remote_json(
                remote,
                "PUT",
                f"/v1/admin/prices/{quote(deployment, safe='')}",
                price.model_dump(by_alias=True, mode="json", exclude_none=True),
            )
        return store.upsert_price(price)

    @api.delete("/api/prices/{deployment}", status_code=204, response_class=Response)
    def delete_price(
        deployment: str,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> Response:
        if remote:
            prices = _remote_json(remote, "GET", "/v1/admin/prices")
            match = next((price for price in prices if price["deployment"] == deployment), None)
            if match is None:
                raise HTTPException(status_code=404, detail=f"unknown deployment: {deployment}")
            _remote_json(
                remote,
                "DELETE",
                f"/v1/admin/prices/{quote(deployment, safe='')}/{quote(match['id'], safe='')}",
            )
            return Response(status_code=204)
        try:
            store.delete_price(deployment)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(status_code=204)

    @api.post("/api/simulations", response_model=SimulationResponse, response_model_by_alias=True)
    def simulate(
        request: SimulationRequest,
        _principal: ConsolePrincipal = Depends(require_admin),
    ) -> SimulationResponse:
        try:
            return store.simulate(request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return api


def _remote_json(
    client: httpx.Client,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    try:
        response = client.request(method, path, json=body)
        response.raise_for_status()
        return None if response.status_code == 204 else response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except ValueError:
            detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"cost enforcement API unavailable: {exc}") from exc


def _verify_microsoft_id_token(
    token: str,
    client_id: str,
    jwks_client: jwt.PyJWKClient,
) -> dict[str, Any]:
    unverified = jwt.decode(
        token,
        options={
            "verify_signature": False,
            "verify_aud": False,
            "verify_exp": False,
        },
    )
    tenant_id = str(unverified.get("tid", ""))
    if not tenant_id:
        raise jwt.InvalidTokenError("Microsoft identity token does not contain a tenant")
    signing_key = jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        options={"require": ["exp", "iat", "aud", "iss", "tid"]},
    )


def _set_session_cookie(
    response: Response,
    session_secret: str,
    secure: bool,
    *,
    subject: str,
    email: str,
    name: str,
    role: str,
) -> None:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": subject,
            "email": email,
            "name": name,
            "role": role,
            "iss": OWNER_SESSION_ISSUER,
            "iat": now,
            "exp": now + timedelta(hours=8),
        },
        session_secret,
        algorithm="HS256",
    )
    response.set_cookie(
        OWNER_COOKIE,
        token,
        max_age=8 * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


app = create_simulator_app()


if __name__ == "__main__":
    uvicorn.run(
        "local_simulator:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8010")),
        reload=False,
    )