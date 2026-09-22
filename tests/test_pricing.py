from decimal import Decimal

import pytest

from app.models import ReservationResponse
from app.pricing import (
    InvalidUsageError,
    PriceRates,
    calculate_cost_usd,
    calculate_reservation_usd,
    normalize_usage,
)


RATES = PriceRates(
    input_usd_per_million=Decimal('2'),
    cache_write_usd_per_million=Decimal('2.5'),
    cache_read_usd_per_million=Decimal('0.5'),
    output_usd_per_million=Decimal('8'),
)


def test_openai_cached_input_is_subtracted_from_prompt_tokens() -> None:
    usage = normalize_usage(
        {
            "prompt_tokens": 800,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 200},
        }
    )

    assert usage.input_tokens == 600
    assert usage.cache_write_tokens == 0
    assert usage.cache_read_tokens == 200
    assert calculate_cost_usd(usage, RATES) == Decimal('0.003700')


def test_anthropic_cache_creation_and_read_are_separate_buckets() -> None:
    usage = normalize_usage(
        {
            "input_tokens": 600,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 200,
            "output_tokens": 300,
        }
    )

    assert usage.input_tokens == 600
    assert usage.cache_write_tokens == 100
    assert usage.cache_read_tokens == 200
    assert calculate_cost_usd(usage, RATES) == Decimal('0.003950')


def test_openai_rejects_overlapping_cache_buckets() -> None:
    with pytest.raises(InvalidUsageError):
        normalize_usage(
            {
                "prompt_tokens": 100,
                "prompt_tokens_details": {
                    "cached_tokens": 80,
                    "cache_write_tokens": 30,
                },
            }
        )


def test_reservation_uses_highest_possible_input_rate() -> None:
    assert calculate_reservation_usd(800, 300, RATES) == Decimal('0.004400')


def test_usd_response_serializes_decimal_as_string() -> None:
    response = ReservationResponse(allowed=True, reserved_usd=Decimal('0.004400'), remaining_usd=Decimal('99.995600'))

    assert response.model_dump(mode='json', by_alias=True) == {
        'allowed': True,
        'reservationId': None,
        'reservedUsd': '0.004400',
        'remainingUsd': '99.995600',
        'reason': None,
    }