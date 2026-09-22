from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=lambda value: _to_camel(value), populate_by_name=True)


class ReservationRequest(ApiModel):
    operation_id: str = Field(min_length=1, max_length=128)
    caller_key: str = Field(min_length=1, max_length=512)
    app_id: str = Field(min_length=1, max_length=128)
    deployment: str = Field(min_length=1, max_length=128)
    period: Literal["Monthly"] = "Monthly"
    currency: Literal["USD"] = "USD"
    request: dict[str, Any]


class ReservationResponse(ApiModel):
    allowed: bool
    reservation_id: str | None = None
    reserved_usd: Decimal = Decimal('0')
    remaining_usd: Decimal
    reason: str | None = None


class SettlementRequest(ApiModel):
    operation_id: str = Field(min_length=1, max_length=128)
    deployment: str = Field(min_length=1, max_length=128)
    model: str = Field(default="", max_length=128)
    input_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(ge=0)
    status: int = Field(ge=100, le=599)


class SettlementResponse(ApiModel):
    reservation_id: str
    status: Literal["settled", "released"]
    actual_usd: Decimal
    released_usd: Decimal
    remaining_usd: Decimal


class BudgetUpdate(ApiModel):
    limit_usd: Decimal = Field(gt=0)


class UserBudgetUpdate(BudgetUpdate):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class BudgetResponse(ApiModel):
    caller_key: str
    period: str
    limit_usd: Decimal
    spent_usd: Decimal
    reserved_usd: Decimal
    remaining_usd: Decimal


class DefaultBudgetResponse(ApiModel):
    limit_usd: Decimal


class BudgetProbeRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    amount_usd: Decimal = Field(gt=0)


class BudgetProbeResponse(ApiModel):
    allowed: bool
    amount_usd: Decimal
    remaining_usd: Decimal
    reason: str | None = None


class PriceDocument(ApiModel):
    id: str
    deployment: str
    model: str = "*"
    model_version: str = "*"
    region: str = "global"
    deployment_type: str = "GlobalStandard"
    currency: Literal["USD"] = "USD"
    unit: Literal["usdPerMillionTokens"] = "usdPerMillionTokens"
    input_usd_per_million: Decimal = Field(ge=0)
    cache_write_usd_per_million: Decimal = Field(ge=0)
    cache_read_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    max_output_tokens: int = Field(gt=0)
    effective_from: datetime
    effective_to: datetime | None = None
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_window(self) -> "PriceDocument":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effectiveTo must be later than effectiveFrom")
        return self