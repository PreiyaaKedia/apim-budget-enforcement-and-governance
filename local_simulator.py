from __future__ import annotations

import os
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from analytics import EMPTY_DASHBOARD, configured_analytics
from app.models import BudgetProbeRequest, PriceDocument, TeamBudgetUpdate
from app.simulator import BudgetUpdate, SimulationRequest, SimulationResponse, SimulatorStore


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "simulator_web"
LOGGER = logging.getLogger(__name__)


def create_simulator_app(
    state_path: Path | None = None,
    cost_api_url: str | None = None,
    admin_api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    analytics_provider: Any | None = None,
) -> FastAPI:
    store = SimulatorStore(
        state_path or ROOT / ".local" / "simulator-state.json",
        ROOT / "prices.example.json",
    )
    api = FastAPI(title="Cost Budget Lab", version="1.0.0")
    remote_url = cost_api_url or os.environ.get("COST_ENFORCEMENT_URL", "")
    remote_key = admin_api_key or os.environ.get("ADMIN_API_KEY", "")
    remote = httpx.Client(
        base_url=remote_url.rstrip("/"),
        headers={"X-Admin-Key": remote_key} if remote_key else {},
        timeout=10,
        transport=transport,
    ) if remote_url else None
    analytics = analytics_provider if analytics_provider is not None else configured_analytics()
    api.mount("/assets", StaticFiles(directory=WEB_ROOT, check_dir=False), name="assets")

    @api.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html")

    @api.get("/api/state")
    def state() -> dict:
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
    def set_live_team(team_role: str, update: TeamBudgetUpdate) -> dict[str, Any]:
        if remote is None:
            raise HTTPException(status_code=409, detail="COST_ENFORCEMENT_URL is not configured")
        return _remote_json(
            remote,
            "PUT",
            f"/v1/admin/budget/teams/{quote(team_role, safe='')}",
            update.model_dump(by_alias=True, mode="json"),
        )

    @api.put("/api/admin/budget/default")
    def set_live_default(update: BudgetUpdate) -> dict[str, Any]:
        if remote is None:
            raise HTTPException(status_code=409, detail="COST_ENFORCEMENT_URL is not configured")
        return _remote_json(remote, "PUT", "/v1/admin/budget/default", {"limitUsd": str(update.limit_usd)})

    @api.put("/api/admin/budget/users/{email}")
    def set_live_user(email: str, update: BudgetUpdate) -> dict[str, Any]:
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
    def probe_live_budget(body: BudgetProbeRequest) -> dict[str, Any]:
        if remote is None:
            raise HTTPException(status_code=409, detail="COST_ENFORCEMENT_URL is not configured")
        return _remote_json(
            remote,
            "POST",
            "/v1/admin/budget/probe",
            body.model_dump(by_alias=True, mode="json"),
        )

    @api.put("/api/users/{user_id}")
    def set_budget(user_id: str, update: BudgetUpdate) -> dict[str, str]:
        return store.set_budget(user_id, update.limit_usd)

    @api.post("/api/users/{user_id}/reset")
    def reset_budget(user_id: str) -> dict[str, str]:
        try:
            return store.reset_budget(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.put("/api/prices/{deployment}")
    def upsert_price(deployment: str, price: PriceDocument) -> dict:
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
    def delete_price(deployment: str) -> Response:
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
    def simulate(request: SimulationRequest) -> SimulationResponse:
        try:
            return store.simulate(request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return api


app = create_simulator_app()


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


if __name__ == "__main__":
    uvicorn.run(
        "local_simulator:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8010")),
        reload=False,
    )