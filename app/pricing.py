from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any


TOKENS_PER_PRICE_UNIT = 1_000_000
USD_QUANTUM = Decimal('0.000001')


class InvalidUsageError(ValueError):
    pass


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    schema: str


@dataclass(frozen=True)
class PriceRates:
    input_usd_per_million: Decimal
    cache_write_usd_per_million: Decimal
    cache_read_usd_per_million: Decimal
    output_usd_per_million: Decimal


def normalize_usage(usage: dict[str, Any]) -> TokenUsage:
    cache_write = _nonnegative_int(
        usage.get("cache_creation_input_tokens", usage.get("cache_write_tokens", 0)),
        "cache write tokens",
    )
    cache_read = _nonnegative_int(
        usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0)),
        "cache read tokens",
    )

    if "cache_creation_input_tokens" in usage or "cache_read_input_tokens" in usage:
        return TokenUsage(
            input_tokens=_nonnegative_int(usage.get("input_tokens", 0), "input tokens"),
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
            output_tokens=_nonnegative_int(usage.get("output_tokens", 0), "output tokens"),
            schema="anthropic",
        )

    prompt_tokens = _nonnegative_int(
        usage.get("prompt_tokens", usage.get("input_tokens", 0)), "input tokens"
    )
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        raise InvalidUsageError("token details must be an object")
    cache_read = _nonnegative_int(
        details.get("cached_tokens", cache_read), "cache read tokens"
    )
    cache_write = _nonnegative_int(
        details.get("cache_write_tokens", cache_write), "cache write tokens"
    )
    if cache_read + cache_write > prompt_tokens:
        raise InvalidUsageError("cache read and write tokens exceed total input tokens")

    return TokenUsage(
        input_tokens=prompt_tokens - cache_read - cache_write,
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_read,
        output_tokens=_nonnegative_int(
            usage.get("completion_tokens", usage.get("output_tokens", 0)),
            "output tokens",
        ),
        schema="openai",
    )


def calculate_cost_usd(usage: TokenUsage, rates: PriceRates) -> Decimal:
    cost = (
        Decimal(usage.input_tokens) * rates.input_usd_per_million
        + Decimal(usage.cache_write_tokens) * rates.cache_write_usd_per_million
        + Decimal(usage.cache_read_tokens) * rates.cache_read_usd_per_million
        + Decimal(usage.output_tokens) * rates.output_usd_per_million
    ) / Decimal(TOKENS_PER_PRICE_UNIT)
    return cost.quantize(USD_QUANTUM, rounding=ROUND_CEILING)


def calculate_reservation_usd(
    estimated_input_tokens: int, maximum_output_tokens: int, rates: PriceRates
) -> Decimal:
    input_rate = max(
        rates.input_usd_per_million,
        rates.cache_write_usd_per_million,
        rates.cache_read_usd_per_million,
    )
    cost = (
        Decimal(_nonnegative_int(estimated_input_tokens, "estimated input tokens")) * input_rate
        + Decimal(_nonnegative_int(maximum_output_tokens, "maximum output tokens"))
        * rates.output_usd_per_million
    ) / Decimal(TOKENS_PER_PRICE_UNIT)
    return cost.quantize(USD_QUANTUM, rounding=ROUND_CEILING)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidUsageError(f"{field} must be a nonnegative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidUsageError(f"{field} must be a nonnegative integer") from exc
    if parsed < 0 or parsed != value:
        raise InvalidUsageError(f"{field} must be a nonnegative integer")
    return parsed
