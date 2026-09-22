from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import Field, model_validator

from app.models import ApiModel, PriceDocument
from app.pricing import PriceRates, TokenUsage, calculate_cost_usd, calculate_reservation_usd


ZERO = Decimal("0")


class BudgetUpdate(ApiModel):
    limit_usd: Decimal = Field(gt=0)


class SimulationRequest(ApiModel):
    user_id: str = Field(min_length=1, max_length=128)
    deployment: str = Field(min_length=1, max_length=128)
    input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    requested_max_output_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_actual_output(self) -> "SimulationRequest":
        if self.output_tokens > self.requested_max_output_tokens:
            raise ValueError("outputTokens cannot exceed requestedMaxOutputTokens")
        return self


class SimulationResponse(ApiModel):
    allowed: bool
    reason: str | None = None
    reserved_usd: Decimal
    actual_usd: Decimal
    released_usd: Decimal
    remaining_usd: Decimal


class SimulatorStore:
    def __init__(self, state_path: Path, seed_prices_path: Path | None = None) -> None:
        self._state_path = state_path
        self._lock = Lock()
        self._state = self._load(seed_prices_path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def set_budget(self, user_id: str, limit_usd: Decimal) -> dict[str, str]:
        with self._lock:
            existing = self._state["users"].get(user_id, {})
            user = {
                "userId": user_id,
                "limitUsd": _money(limit_usd),
                "spentUsd": existing.get("spentUsd", "0.000000"),
            }
            self._state["users"][user_id] = user
            self._save()
            return dict(user)

    def reset_budget(self, user_id: str) -> dict[str, str]:
        with self._lock:
            user = self._require_user(user_id)
            user["spentUsd"] = "0.000000"
            self._save()
            return dict(user)

    def upsert_price(self, price: PriceDocument) -> dict[str, Any]:
        with self._lock:
            document = price.model_dump(by_alias=True, mode="json", exclude_none=False)
            self._state["prices"][price.deployment] = document
            self._save()
            return dict(document)

    def delete_price(self, deployment: str) -> None:
        with self._lock:
            if deployment not in self._state["prices"]:
                raise KeyError(f"unknown deployment: {deployment}")
            del self._state["prices"][deployment]
            self._save()

    def simulate(self, request: SimulationRequest) -> SimulationResponse:
        with self._lock:
            user = self._require_user(request.user_id)
            price_data = self._state["prices"].get(request.deployment)
            if price_data is None:
                raise KeyError(f"unknown deployment: {request.deployment}")

            price = PriceDocument.model_validate(price_data)
            rates = _rates(price)
            estimated_input = request.input_tokens + request.cache_write_tokens + request.cache_read_tokens
            reserved = calculate_reservation_usd(
                estimated_input,
                request.requested_max_output_tokens,
                rates,
            )
            limit = Decimal(user["limitUsd"])
            spent = Decimal(user["spentUsd"])
            remaining = max(ZERO, limit - spent)

            if reserved > remaining:
                result = SimulationResponse(
                    allowed=False,
                    reason="budget_exceeded",
                    reserved_usd=reserved,
                    actual_usd=ZERO,
                    released_usd=ZERO,
                    remaining_usd=remaining,
                )
            else:
                usage = TokenUsage(
                    input_tokens=request.input_tokens,
                    cache_write_tokens=request.cache_write_tokens,
                    cache_read_tokens=request.cache_read_tokens,
                    output_tokens=request.output_tokens,
                    schema="normalized",
                )
                actual = calculate_cost_usd(usage, rates)
                user["spentUsd"] = _money(spent + actual)
                result = SimulationResponse(
                    allowed=True,
                    reserved_usd=reserved,
                    actual_usd=actual,
                    released_usd=max(ZERO, reserved - actual),
                    remaining_usd=max(ZERO, limit - spent - actual),
                )

            self._state["history"].insert(
                0,
                {
                    **request.model_dump(by_alias=True),
                    **result.model_dump(by_alias=True, mode="json"),
                },
            )
            self._state["history"] = self._state["history"][:100]
            self._save()
            return result

    def _load(self, seed_prices_path: Path | None) -> dict[str, Any]:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text(encoding="utf-8"))

        prices: dict[str, Any] = {}
        if seed_prices_path is not None and seed_prices_path.exists():
            for item in json.loads(seed_prices_path.read_text(encoding="utf-8")):
                price = PriceDocument.model_validate(item)
                prices[price.deployment] = price.model_dump(
                    by_alias=True,
                    mode="json",
                    exclude_none=False,
                )
        return {"users": {}, "prices": prices, "history": []}

    def _save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        temporary.replace(self._state_path)

    def _require_user(self, user_id: str) -> dict[str, str]:
        user = self._state["users"].get(user_id)
        if user is None:
            raise KeyError(f"unknown user: {user_id}")
        return user


def _rates(price: PriceDocument) -> PriceRates:
    return PriceRates(
        input_usd_per_million=price.input_usd_per_million,
        cache_write_usd_per_million=price.cache_write_usd_per_million,
        cache_read_usd_per_million=price.cache_read_usd_per_million,
        output_usd_per_million=price.output_usd_per_million,
    )


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")