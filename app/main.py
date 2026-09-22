from __future__ import annotations

import json
import os
from decimal import Decimal
from uuid import uuid4

from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

from app.auth import EntraAuthMiddleware
from app.catalog import AmbiguousPriceError, PriceCatalog, PriceNotFoundError, rates_from_price
from app.models import (
    BudgetResponse,
    BudgetProbeRequest,
    BudgetProbeResponse,
    BudgetUpdate,
    DefaultBudgetResponse,
    PriceDocument,
    ReservationRequest,
    ReservationResponse,
    SettlementRequest,
    SettlementResponse,
    UserBudgetUpdate,
)
from app.pricing import TokenUsage, calculate_cost_usd, calculate_reservation_usd
from app.repository import CosmosLedger, LedgerConflictError, ReservationNotFoundError


def create_app() -> FastAPI:
    endpoint = os.environ["COSMOS_ENDPOINT"]
    database_name = os.environ.get("COSMOS_DATABASE", "cost-enforcement")
    client = CosmosClient(endpoint, credential=DefaultAzureCredential())
    database = client.get_database_client(database_name)
    ledger = CosmosLedger(
        database.get_container_client(os.environ.get("COSMOS_LEDGER_CONTAINER", "ledger")),
        monthly_limit_usd=Decimal(os.environ.get("MONTHLY_LIMIT_USD", "100.00")),
    )
    catalog = PriceCatalog(database.get_container_client(os.environ.get("COSMOS_PRICING_CONTAINER", "pricing")))
    api = FastAPI(title="Cost Enforcement API", version="1.0.0")

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/v1/admin/budget/default", response_model=DefaultBudgetResponse, response_model_by_alias=True)
    def get_default_budget() -> DefaultBudgetResponse:
        return ledger.get_default_budget()

    @api.put("/v1/admin/budget/default", response_model=DefaultBudgetResponse, response_model_by_alias=True)
    def set_default_budget(update: BudgetUpdate) -> DefaultBudgetResponse:
        try:
            return ledger.set_default_budget(update.limit_usd)
        except LedgerConflictError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @api.get("/v1/admin/budget/users/{email}", response_model=BudgetResponse, response_model_by_alias=True)
    def get_user_budget(email: str) -> BudgetResponse:
        return ledger.get_budget(_email_caller_key(email))

    @api.put("/v1/admin/budget/users/{email}", response_model=BudgetResponse, response_model_by_alias=True)
    def set_user_budget(email: str, update: UserBudgetUpdate) -> BudgetResponse:
        if email.lower() != update.email.lower():
            raise HTTPException(status_code=400, detail="email path and body must match")
        try:
            return ledger.set_budget_limit(_email_caller_key(email), update.limit_usd)
        except LedgerConflictError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @api.post("/v1/admin/budget/probe", response_model=BudgetProbeResponse, response_model_by_alias=True)
    def probe_budget(request: BudgetProbeRequest) -> BudgetProbeResponse:
        try:
            reservation = ledger.reserve(
                f"admin-probe-{uuid4()}",
                _email_caller_key(request.email),
                request.amount_usd,
                {"deployment": "admin-probe", "appId": "admin-console"},
            )
            if not reservation.allowed:
                return BudgetProbeResponse(
                    allowed=False,
                    amount_usd=request.amount_usd,
                    remaining_usd=reservation.remaining_usd,
                    reason=reservation.reason,
                )
            released = ledger.release(str(reservation.reservation_id))
            return BudgetProbeResponse(
                allowed=True,
                amount_usd=request.amount_usd,
                remaining_usd=released.remaining_usd,
            )
        except LedgerConflictError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @api.get("/v1/admin/prices", response_model=list[PriceDocument], response_model_by_alias=True)
    def list_prices() -> list[PriceDocument]:
        return catalog.list()

    @api.put("/v1/admin/prices/{deployment}", response_model=PriceDocument, response_model_by_alias=True)
    def upsert_price(deployment: str, price: PriceDocument) -> PriceDocument:
        if deployment != price.deployment:
            raise HTTPException(status_code=400, detail="deployment path and body must match")
        return catalog.upsert(price)

    @api.delete("/v1/admin/prices/{deployment}/{price_id}", status_code=204, response_class=Response)
    def delete_price(deployment: str, price_id: str) -> Response:
        catalog.delete(deployment, price_id)
        return Response(status_code=204)

    @api.post("/v1/reservations", response_model=ReservationResponse, response_model_by_alias=True)
    def reserve(request: ReservationRequest) -> ReservationResponse:
        try:
            price = catalog.get(request.deployment)
            serialized = json.dumps(request.request, separators=(",", ":"), ensure_ascii=False).encode()
            requested_output = request.request.get("max_completion_tokens", request.request.get("max_tokens"))
            maximum_output = price.max_output_tokens if requested_output is None else int(requested_output)
            amount = calculate_reservation_usd(len(serialized), maximum_output, rates_from_price(price))
            return ledger.reserve(
                request.operation_id,
                request.caller_key,
                amount,
                {"appId": request.app_id, "deployment": request.deployment, "priceId": price.id},
            )
        except (PriceNotFoundError, AmbiguousPriceError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="invalid token limit") from exc
        except LedgerConflictError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @api.post("/v1/reservations/{reservation_id}/settle", response_model=SettlementResponse, response_model_by_alias=True)
    def settle(reservation_id: str, request: SettlementRequest) -> SettlementResponse:
        try:
            reservation = ledger.get_reservation(reservation_id)
            if reservation["operationId"] != request.operation_id or reservation["deployment"] != request.deployment:
                raise HTTPException(status_code=409, detail="reservation does not match settlement")
            price = catalog.get(request.deployment, request.model)
            usage = TokenUsage(
                input_tokens=request.input_tokens,
                cache_write_tokens=request.cache_write_tokens,
                cache_read_tokens=request.cache_read_tokens,
                output_tokens=request.output_tokens,
                schema="normalized",
            )
            actual = calculate_cost_usd(usage, rates_from_price(price))
            return ledger.settle(reservation_id, actual, request.model_dump(by_alias=True))
        except ReservationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="reservation not found") from exc
        except (PriceNotFoundError, AmbiguousPriceError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LedgerConflictError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @api.post("/v1/reservations/{reservation_id}/release", response_model=SettlementResponse, response_model_by_alias=True)
    def release(reservation_id: str) -> SettlementResponse:
        try:
            return ledger.release(reservation_id)
        except ReservationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="reservation not found") from exc
        except LedgerConflictError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return api


def _email_caller_key(email: str) -> str:
    return f"email:{email.strip().lower()}"


app = EntraAuthMiddleware(create_app())