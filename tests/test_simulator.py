from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import PriceDocument
from app.simulator import SimulationRequest, SimulatorStore


def price() -> PriceDocument:
    return PriceDocument.model_validate(
        {
            "id": "model-a",
            "deployment": "model-a",
            "inputUsdPerMillion": "5",
            "cacheWriteUsdPerMillion": "6.25",
            "cacheReadUsdPerMillion": "0.5",
            "outputUsdPerMillion": "25",
            "maxOutputTokens": 32768,
            "effectiveFrom": "2026-09-01T00:00:00Z",
            "source": "test",
        }
    )


def simulator(tmp_path: Path) -> SimulatorStore:
    store = SimulatorStore(tmp_path / "state.json")
    store.upsert_price(price())
    return store


def test_allowed_request_settles_actual_cost_and_releases_reserve(tmp_path: Path) -> None:
    store = simulator(tmp_path)
    store.set_budget("alice", Decimal("1.00"))

    result = store.simulate(
        SimulationRequest(
            user_id="alice",
            deployment="model-a",
            input_tokens=1000,
            output_tokens=100,
            requested_max_output_tokens=1000,
        )
    )

    assert result.allowed is True
    assert result.reserved_usd == Decimal("0.031250")
    assert result.actual_usd == Decimal("0.007500")
    assert result.released_usd == Decimal("0.023750")
    assert result.remaining_usd == Decimal("0.992500")


def test_request_over_budget_is_denied_without_spend(tmp_path: Path) -> None:
    store = simulator(tmp_path)
    store.set_budget("alice", Decimal("0.01"))

    result = store.simulate(
        SimulationRequest(
            user_id="alice",
            deployment="model-a",
            requested_max_output_tokens=1000,
        )
    )

    assert result.allowed is False
    assert result.reason == "budget_exceeded"
    assert result.actual_usd == Decimal("0")
    assert store.snapshot()["users"]["alice"]["spentUsd"] == "0.000000"


def test_budgets_are_isolated_per_user(tmp_path: Path) -> None:
    store = simulator(tmp_path)
    store.set_budget("alice", Decimal("0.01"))
    store.set_budget("bob", Decimal("1.00"))
    request = {
        "deployment": "model-a",
        "requested_max_output_tokens": 1000,
    }

    alice = store.simulate(SimulationRequest(user_id="alice", **request))
    bob = store.simulate(SimulationRequest(user_id="bob", **request))

    assert alice.allowed is False
    assert bob.allowed is True


def test_state_is_persisted_and_reloaded(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = SimulatorStore(state_path)
    store.upsert_price(price())
    store.set_budget("alice", Decimal("2.50"))

    reloaded = SimulatorStore(state_path)

    assert reloaded.snapshot()["users"]["alice"]["limitUsd"] == "2.500000"
    assert reloaded.snapshot()["prices"]["model-a"]["outputUsdPerMillion"] == "25"


def test_actual_output_cannot_exceed_reserved_maximum() -> None:
    with pytest.raises(ValidationError, match="outputTokens cannot exceed"):
        SimulationRequest(
            user_id="alice",
            deployment="model-a",
            output_tokens=1001,
            requested_max_output_tokens=1000,
        )