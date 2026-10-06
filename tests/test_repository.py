from copy import deepcopy
from decimal import Decimal
from time import time

from azure.cosmos.exceptions import CosmosResourceNotFoundError

from app.models import ReservationRequest
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

    def query_items(self, query: str, partition_key: str | None = None, parameters=None, **kwargs) -> list[dict]:
        expected_team = next(
            (parameter["value"] for parameter in parameters or [] if parameter["name"] == "@teamRole"),
            None,
        )
        period_suffix = next(
            (parameter["value"] for parameter in parameters or [] if parameter["name"] == "@periodSuffix"),
            None,
        )
        expires_before = next(
            (parameter["value"] for parameter in parameters or [] if parameter["name"] == "@now"),
            None,
        )
        return [
            deepcopy(item)
            for (item_partition, _), item in self.items.items()
            if (partition_key is None or item_partition == partition_key)
            and (
                ("team-budget" in query and item.get("type") == "team-budget")
                or (
                    "c.type = 'budget'" in query
                    and item.get("type") == "budget"
                    and (period_suffix is None or item.get("partitionKey", "").endswith(period_suffix))
                )
                or (
                    "reservation" in query
                    and item.get("type") == "reservation"
                    and (expected_team is None or item.get("teamRole") == expected_team)
                    and (period_suffix is None or item.get("partitionKey", "").endswith(period_suffix))
                    and (
                        expires_before is None
                        or (
                            item.get("status") == "reserved"
                            and item.get("expiresAt", expires_before + 1) <= expires_before
                        )
                    )
                )
            )
        ]


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


def test_spend_breakdown_survives_idempotent_settlement() -> None:
    ledger = CosmosLedger(FakeContainer(), monthly_limit_usd=Decimal("1"))
    reservation = ledger.reserve("breakdown", "user", Decimal("0.1"), {"deployment": "model-a"})
    usage = {"spendBreakdown": {"input": "0.01", "output": "0.02", "cacheWrite": "0.005", "cacheRead": "0.001"}}
    first = ledger.settle(reservation.reservation_id, Decimal("0.036"), usage)
    duplicate = ledger.settle(reservation.reservation_id, Decimal("0.036"), {})
    assert duplicate == first
    assert first.model_dump(mode="json", by_alias=True)["spendBreakdown"] == usage["spendBreakdown"]


def test_expired_reservations_are_released_and_active_reservations_remain() -> None:
    container = FakeContainer()
    ledger = CosmosLedger(container, monthly_limit_usd=Decimal("1"))
    expired = ledger.reserve("expired", "user", Decimal("0.20"), {"deployment": "model-a"})
    active = ledger.reserve("active", "user", Decimal("0.30"), {"deployment": "model-a"})
    container.items[("user:2026-10", expired.reservation_id)]["expiresAt"] = int(time()) - 1
    container.items[("user:2026-10", active.reservation_id)]["expiresAt"] = int(time()) + 900

    released_count, released_usd = ledger.release_expired_reservations()

    assert released_count == 1
    assert released_usd == Decimal("0.20")
    assert ledger.get_reservation(expired.reservation_id)["status"] == "released"
    assert ledger.get_reservation(active.reservation_id)["status"] == "reserved"
    assert ledger.get_budget("user").reserved_usd == Decimal("0.30")


def test_settlement_api_emits_authoritative_cache_spend(monkeypatch) -> None:
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    container = FakeContainer()
    database = SimpleNamespace(get_container_client=lambda _name: container)
    monkeypatch.setenv("COSMOS_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: None)
    monkeypatch.setattr("azure.cosmos.CosmosClient", lambda *_args, **_kwargs: SimpleNamespace(
        get_database_client=lambda _name: database,
    ))
    from app import main

    monkeypatch.setattr(main, "DefaultAzureCredential", lambda: None)
    monkeypatch.setattr(main, "CosmosClient", lambda *_args, **_kwargs: SimpleNamespace(
        get_database_client=lambda _name: database,
    ))
    price = SimpleNamespace(
        id="claude-price",
        max_output_tokens=1000,
        input_usd_per_million=Decimal("2"),
        output_usd_per_million=Decimal("8"),
        cache_write_usd_per_million=Decimal("2.5"),
        cache_read_usd_per_million=Decimal("0.5"),
    )
    monkeypatch.setattr(main, "PriceCatalog", lambda _container: SimpleNamespace(get=lambda *_args: price))
    client = TestClient(main.create_app())
    reservation = client.post("/v1/reservations", json={
        "operationId": "claude-request",
        "callerKey": "user:tenant:oid",
        "appId": "cowork-client",
        "deployment": "claude",
        "request": {"model": "claude", "max_tokens": 1000},
    })
    assert reservation.status_code == 200
    reservation_id = reservation.json()["reservationId"]
    payload = {
        "operationId": "claude-request",
        "deployment": "claude",
        "model": "claude-version",
        "inputTokens": 600,
        "outputTokens": 300,
        "cacheWriteTokens": 100,
        "cacheReadTokens": 200,
        "status": 200,
    }
    first = client.post(f"/v1/reservations/{reservation_id}/settle", json=payload)
    assert first.status_code == 200
    assert first.json()["actualUsd"] == "0.003950"
    assert first.json()["spendBreakdown"] == {
        "input": "0.0012", "output": "0.0024", "cacheWrite": "0.00025", "cacheRead": "0.0001",
    }
    assert client.post(f"/v1/reservations/{reservation_id}/settle", json=payload).json() == first.json()


def test_reserve_denies_amount_above_remaining_budget() -> None:
    ledger = CosmosLedger(FakeContainer(), monthly_limit_usd=Decimal('0.001000'))

    response = ledger.reserve("op-2", "caller-1", Decimal('0.001001'), {"deployment": "model-a"})

    assert response.allowed is False
    assert response.reason == "budget_exceeded"


def test_legacy_apim_reservation_payload_remains_valid_during_rollout() -> None:
    request = ReservationRequest.model_validate({
        "operationId": "legacy-op",
        "callerKey": "email:user@example.com",
        "appId": "legacy-client",
        "deployment": "model-a",
        "request": {"max_tokens": 10},
    })

    assert request.team_role is None
    assert request.user_key is None


def test_team_policy_gives_each_user_an_independent_budget() -> None:
    container = FakeContainer()
    ledger = CosmosLedger(container, monthly_limit_usd=Decimal("1.00"))
    policy = ledger.set_team_budget("Team.Engineering", Decimal("0.60"))

    first = ledger.reserve_for_team(
        "op-team-1",
        "Team.Engineering",
        "user:tenant:111",
        Decimal("0.60"),
        {"userEmail": "one@example.com"},
    )
    first_user_again = ledger.reserve_for_team(
        "op-team-2",
        "Team.Engineering",
        "user:tenant:111",
        Decimal("0.01"),
        {"userEmail": "one@example.com"},
    )
    second_user = ledger.reserve_for_team(
        "op-team-3",
        "Team.Engineering",
        "user:tenant:222",
        Decimal("0.60"),
        {"userEmail": "two@example.com"},
    )

    assert first.allowed is True
    assert first_user_again.allowed is False
    assert second_user.allowed is True
    assert policy.per_user_limit_usd == Decimal("0.60")
    policy_document = container.items[("settings", "team-budget:team.engineering")]
    assert "memberCount" not in policy_document
    reservation = ledger.get_reservation(str(first.reservation_id))
    assert reservation["callerKey"] == "user:tenant:111"
    assert reservation["teamRole"] == "Team.Engineering"
    assert reservation["userEmail"] == "one@example.com"


def test_budget_metrics_aggregate_current_team_users() -> None:
    container = FakeContainer()
    ledger = CosmosLedger(container, monthly_limit_usd=Decimal("1.00"))
    ledger.set_team_budget("Team.Engineering", Decimal("0.60"))
    ledger.set_team_budget("Team.Marketing", Decimal("1.00"))

    first = ledger.reserve_for_team(
        "metrics-1", "Team.Engineering", "user:tenant:111", Decimal("0.20"),
        {"userEmail": "one@example.com", "userName": "One", "deployment": "model-a"},
    )
    ledger.settle(first.reservation_id, Decimal("0.15"), {"inputTokens": 100})
    ledger.reserve_for_team(
        "metrics-2", "Team.Engineering", "user:tenant:222", Decimal("0.10"),
        {"userEmail": "two@example.com", "userName": "Two", "deployment": "model-a"},
    )
    first_budget = container.items[(ledger._partition_key("user:tenant:111"), "budget")]
    first_budget.pop("userEmail")
    first_budget.pop("userName")

    report = ledger.get_budget_metrics()
    engineering = next(team for team in report.teams if team.team_role == "Team.Engineering")
    marketing = next(team for team in report.teams if team.team_role == "Team.Marketing")

    assert engineering.active_users == 2
    assert engineering.allocated_usd == Decimal("1.20")
    assert engineering.spent_usd == Decimal("0.15")
    assert engineering.reserved_usd == Decimal("0.10")
    assert engineering.remaining_usd == Decimal("0.95")
    assert marketing.active_users == 0
    assert marketing.allocated_usd == Decimal("0")
    assert len(report.users) == 2
    assert report.users[0].user_email == "one@example.com"


def test_budget_metrics_include_legacy_consumption_as_unassigned() -> None:
    container = FakeContainer()
    ledger = CosmosLedger(container, monthly_limit_usd=Decimal("1.00"))
    reservation = ledger.reserve(
        "legacy-metrics",
        "legacy-caller",
        Decimal("0.25"),
        {"userEmail": "legacy@example.com", "userName": "Legacy User"},
    )
    ledger.settle(reservation.reservation_id, Decimal("0.20"), {"inputTokens": 100})

    report = ledger.get_budget_metrics()

    assert len(report.users) == 1
    assert report.users[0].team_role == "Unassigned"
    assert report.users[0].spent_usd == Decimal("0.20")
    assert report.teams[0].team_role == "Unassigned"
    assert report.teams[0].spent_usd == Decimal("0.20")


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