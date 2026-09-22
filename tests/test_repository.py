from copy import deepcopy
from decimal import Decimal

from azure.cosmos.exceptions import CosmosResourceNotFoundError

from app.repository import CosmosLedger


class FakeContainer:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.batch_count = 0
        self.etag = 0

    def read_item(self, item: str, partition_key: str) -> dict:
        try:
            return deepcopy(self.items[(partition_key, item)])
        except KeyError as exc:
            raise CosmosResourceNotFoundError(status_code=404, message="not found") from exc

    def execute_item_batch(self, operations: list[tuple], partition_key: str) -> None:
        self.batch_count += 1
        for operation, arguments, options in (
            (entry[0], entry[1], entry[2] if len(entry) == 3 else {}) for entry in operations
        ):
            if operation == "create":
                body = deepcopy(arguments[0])
                item_id = body["id"]
            else:
                item_id, body = arguments
                body = deepcopy(body)
                assert options["if_match_etag"] == self.items[(partition_key, item_id)]["_etag"]
            self.etag += 1
            body["_etag"] = f'"{self.etag}"'
            self.items[(partition_key, item_id)] = body


def test_reserve_and_settle_are_idempotent() -> None:
    container = FakeContainer()
    ledger = CosmosLedger(container, monthly_limit_usd=Decimal('0.010000'))

    first = ledger.reserve("op-1", "caller-1", Decimal('0.004000'), {"deployment": "model-a"})
    duplicate = ledger.reserve("op-1", "caller-1", Decimal('0.004000'), {"deployment": "model-a"})

    assert first == duplicate
    assert first.remaining_usd == Decimal('0.006000')
    assert container.batch_count == 1

    settled = ledger.settle(first.reservation_id, Decimal('0.002500'), {"inputTokens": 100})
    duplicate_settlement = ledger.settle(first.reservation_id, Decimal('0.002500'), {"inputTokens": 100})

    assert settled == duplicate_settlement
    assert settled.released_usd == Decimal('0.001500')
    assert settled.remaining_usd == Decimal('0.007500')
    assert container.batch_count == 2


def test_reserve_denies_amount_above_remaining_budget() -> None:
    ledger = CosmosLedger(FakeContainer(), monthly_limit_usd=Decimal('0.001000'))

    response = ledger.reserve("op-2", "caller-1", Decimal('0.001001'), {"deployment": "model-a"})

    assert response.allowed is False
    assert response.reason == "budget_exceeded"


def test_set_budget_limit_preserves_usage_and_applies_immediately() -> None:
    container = FakeContainer()
    ledger = CosmosLedger(container, monthly_limit_usd=Decimal("1.00"))
    reservation = ledger.reserve("op-3", "caller-1", Decimal("0.400000"), {"deployment": "model-a"})
    ledger.settle(reservation.reservation_id, Decimal("0.250000"), {"inputTokens": 100})

    budget = ledger.set_budget_limit("caller-1", Decimal("0.200000"))

    assert budget.limit_usd == Decimal("0.200000")
    assert budget.spent_usd == Decimal("0.250000")
    assert budget.reserved_usd == Decimal("0.000000")
    assert budget.remaining_usd == Decimal("0")
    assert ledger.reserve("op-4", "caller-1", Decimal("0.000001"), {"deployment": "model-a"}).allowed is False


def test_default_budget_updates_existing_non_overridden_users() -> None:
    container = FakeContainer()
    ledger = CosmosLedger(container, monthly_limit_usd=Decimal("1.00"))
    reservation = ledger.reserve("op-default", "email:user@example.com", Decimal("0.25"), {"deployment": "model-a"})
    ledger.settle(reservation.reservation_id, Decimal("0.10"), {"inputTokens": 100})

    ledger.set_default_budget(Decimal("0.20"))
    budget = ledger.get_budget("email:user@example.com")

    assert budget.limit_usd == Decimal("0.20")
    assert budget.spent_usd == Decimal("0.10")
    assert budget.remaining_usd == Decimal("0.10")


def test_email_override_is_not_replaced_by_new_default() -> None:
    ledger = CosmosLedger(FakeContainer(), monthly_limit_usd=Decimal("1.00"))
    ledger.set_budget_limit("email:user@example.com", Decimal("0.50"))

    ledger.set_default_budget(Decimal("0.20"))

    assert ledger.get_budget("email:user@example.com").limit_usd == Decimal("0.50")