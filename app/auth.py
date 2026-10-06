from __future__ import annotations

import json
import os

import jwt
from jwt import PyJWKClient


class EntraAuthMiddleware:
    def __init__(self, app) -> None:
        self.app = app
        self.tenant_id = os.environ.get("TENANT_ID", "")
        self.audience = os.environ.get("AUDIENCE", "")
        self.allowed_client_id = os.environ.get("ALLOWED_CLIENT_ID", "")
        self.admin_allowed_client_id = os.environ.get("ADMIN_ALLOWED_CLIENT_ID", "")
        self.admin_allowed_principal_id = os.environ.get("ADMIN_ALLOWED_PRINCIPAL_ID", "")
        self.admin_required_role = os.environ.get("ADMIN_REQUIRED_ROLE", "CostEnforcement.Admin")
        self.admin_auth_disabled = os.environ.get("ADMIN_AUTH_DISABLED", "false").lower() == "true"
        self.disabled = os.environ.get("AUTH_DISABLED", "false").lower() == "true"
        self.issuers = {
            f"https://login.microsoftonline.com/{self.tenant_id}/v2.0",
            f"https://sts.windows.net/{self.tenant_id}/",
        }
        self.jwks = PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"
        ) if self.tenant_id else None

    async def __call__(self, scope, receive, send) -> None:
        is_admin = scope.get("path", "").startswith("/v1/admin/")
        if scope["type"] != "http" or scope.get("path") == "/health" or self.disabled or (is_admin and self.admin_auth_disabled):
            await self.app(scope, receive, send)
            return
        audience = self.audience
        if not all((self.tenant_id, audience, self.jwks)):
            await self._error(send, 503, "authentication is not configured")
            return
        if is_admin and not (self.admin_allowed_client_id or self.admin_allowed_principal_id):
            await self._error(send, 503, "admin authentication is not configured")
            return
        if not is_admin and not self.allowed_client_id:
            await self._error(send, 503, "authentication is not configured")
            return

        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            await self._error(send, 401, "missing bearer token")
            return
        try:
            token = authorization.removeprefix("Bearer ")
            signing_key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=audience,
                options={"require": ["exp", "aud", "iss"], "verify_iss": False},
            )
            if claims.get("iss") not in self.issuers:
                raise jwt.InvalidIssuerError("unexpected issuer")
            caller_client_id = claims.get("azp", claims.get("appid"))
            if is_admin:
                roles = claims.get("roles", [])
                if isinstance(roles, str):
                    roles = [roles]
                if self.admin_required_role not in roles:
                    raise jwt.InvalidTokenError("required admin role is missing")
                if self.admin_allowed_client_id and caller_client_id != self.admin_allowed_client_id:
                    raise jwt.InvalidTokenError("admin calling application is not allowed")
                if self.admin_allowed_principal_id and claims.get("oid") != self.admin_allowed_principal_id:
                    raise jwt.InvalidTokenError("admin calling principal is not allowed")
            elif caller_client_id != self.allowed_client_id:
                raise jwt.InvalidTokenError("calling application is not allowed")
        except jwt.PyJWTError:
            await self._error(send, 401, "invalid bearer token")
            return
        scope.setdefault("state", {})["claims"] = claims
        await self.app(scope, receive, send)

    @staticmethod
    async def _error(send, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})