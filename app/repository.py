from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from time import time
from typing import Any

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError, CosmosResourceNotFoundError

from app.models import (
    BudgetMetricsResponse,
    BudgetResponse,
    DefaultBudgetResponse,
    ReservationResponse,
    SettlementResponse,
    TeamBudgetMetrics,
    TeamBudgetResponse,
    UserBudgetMetrics,
)


class LedgerConflictError(RuntimeError):
    pass


class ReservationNotFoundError(LookupError):
    pass


class TeamBudgetNotFoundError(LookupError):
    pass


class CosmosLedger:
    def __init__(self, container: ContainerProxy, monthly_limit_usd: Decimal, reservation_ttl_seconds: int = 900) -> None:
        self._container = container
        self._monthly_limit = monthly_limit_usd
        self._ttl = reservation_ttl_seconds

    def reserve(
        self,
        operation_id: str,
        caller_key: str,
        amount: Decimal,
        metadata: dict[str, Any],
        limit_usd: Decimal | None = None,
    ) -> ReservationResponse:
        partition_key = self._partition_key(caller_key)
        reservation_id = self._reservation_id(partition_key, operation_id)
        for _ in range(4):
            existing = self._read_optional(reservation_id, partition_key)
            if existing:
                return self._reservation_response(existing)

            budget = self._effective_budget(partition_key, caller_key)
            if limit_usd is not None:
                budget["limitUsd"] = _decimal_string(limit_usd)
                budget["limitSource"] = "team-policy"
                budget["teamRole"] = metadata["teamRole"]
                budget["userKey"] = metadata["userKey"]
                budget["userEmail"] = metadata.get("userEmail", "")
                budget["userName"] = metadata.get("userName", "")
            remaining = (
                _decimal(budget["limitUsd"])
                - _decimal(budget["spentUsd"])
                - _decimal(budget["reservedUsd"])
            )
            if amount > remaining:
                return ReservationResponse(allowed=False, remaining_usd=max(Decimal('0'), remaining), reason="budget_exceeded")

            now = datetime.now(timezone.utc).isoformat()
            reservation = {
                "id": reservation_id,
                "type": "reservation",
                "partitionKey": partition_key,
                "operationId": operation_id,
                "callerKey": caller_key,
                "status": "reserved",
                "reservedUsd": _decimal_string(amount),
                "remainingAfterReserveUsd": _decimal_string(remaining - amount),
                "createdAt": now,
                "expiresAt": int(time()) + self._ttl,
                **metadata,
            }
            budget["reservedUsd"] = _decimal_string(_decimal(budget["reservedUsd"]) + amount)
            budget["updatedAt"] = now
            operations: list[tuple] = []
            if "_etag" in budget:
                operations.append(("replace", ("budget", budget), {"if_match_etag": budget["_etag"]}))
            else:
                operations.append(("create", (budget,)))
            operations.append(("create", (reservation,)))
            try:
                self._container.execute_item_batch(operations, partition_key=partition_key)
                return ReservationResponse(
                    allowed=True,
                    reservation_id=reservation_id,
                    reserved_usd=amount,
                    remaining_usd=remaining - amount,
                )
            except (CosmosBatchOperationError, CosmosHttpResponseError) as exc:
                if getattr(exc, "status_code", None) not in (409, 412, 424):
                    raise
        raise LedgerConflictError("reservation conflicted repeatedly")

    def reserve_for_team(
        self,
        operation_id: str,
        team_role: str,
        user_key: str,
        amount: Decimal,
        metadata: dict[str, Any],
    ) -> ReservationResponse:
        policy = self.get_team_budget(team_role)
        return self.reserve(
            operation_id,
            user_key,
            amount,
            {**metadata, "userKey": user_key, "teamRole": policy.team_role},
            limit_usd=policy.per_user_limit_usd,
        )

    def get_budget(self, caller_key: str) -> BudgetResponse:
        partition_key = self._partition_key(caller_key)
        budget = self._effective_budget(partition_key, caller_key)
        return self._budget_response(budget)

    def get_default_budget(self) -> DefaultBudgetResponse:
        settings = self._read_optional("default-budget", "settings")
        limit = self._monthly_limit if settings is None else _decimal(settings["limitUsd"])
        return DefaultBudgetResponse(limit_usd=limit)

    def set_default_budget(self, limit_usd: Decimal) -> DefaultBudgetResponse:
        for _ in range(4):
            settings = self._read_optional("default-budget", "settings") or {
                "id": "default-budget",
                "type": "settings",
                "partitionKey": "settings",
            }
            settings["limitUsd"] = _decimal_string(limit_usd)
            settings["updatedAt"] = datetime.now(timezone.utc).isoformat()
            if "_etag" in settings:
                operations = [("replace", ("default-budget", settings), {"if_match_etag": settings["_etag"]})]
            else:
                operations = [("create", (settings,))]
            try:
                self._container.execute_item_batch(operations, partition_key="settings")
                return DefaultBudgetResponse(limit_usd=limit_usd)
            except (CosmosBatchOperationError, CosmosHttpResponseError) as exc:
                if getattr(exc, "status_code", None) not in (409, 412, 424):
                    raise
        raise LedgerConflictError("default budget update conflicted repeatedly")

    def set_budget_limit(
        self,
        caller_key: str,
        limit_usd: Decimal,
        metadata: dict[str, Any] | None = None,
    ) -> BudgetResponse:
        partition_key = self._partition_key(caller_key)
        for _ in range(4):
            budget = self._effective_budget(partition_key, caller_key)
            budget["limitUsd"] = _decimal_string(limit_usd)
            budget["limitSource"] = "override"
            if metadata:
                budget.update(metadata)
            budget["updatedAt"] = datetime.now(timezone.utc).isoformat()
            if "_etag" in budget:
                operations = [("replace", ("budget", budget), {"if_match_etag": budget["_etag"]})]
            else:
                operations = [("create", (budget,))]
            try:
                self._container.execute_item_batch(operations, partition_key=partition_key)
                return self._budget_response(budget)
            except (CosmosBatchOperationError, CosmosHttpResponseError) as exc:
                if getattr(exc, "status_code", None) not in (409, 412, 424):
                    raise
        raise LedgerConflictError("budget update conflicted repeatedly")

    def get_team_budget(self, team_role: str) -> TeamBudgetResponse:
        item = self._read_optional(self._team_budget_id(team_role), "settings")
        if item is None:
            raise TeamBudgetNotFoundError(team_role)
        return self._team_budget_response(item)

    def list_team_budgets(self) -> list[TeamBudgetResponse]:
        items = self._container.query_items(
            query="SELECT * FROM c WHERE c.type = 'team-budget'",
            partition_key="settings",
        )
        return sorted(
            (self._team_budget_response(item) for item in items),
            key=lambda item: item.team_role.lower(),
        )

    def set_team_budget(
        self,
        team_role: str,
        per_user_limit_usd: Decimal,
    ) -> TeamBudgetResponse:
        item_id = self._team_budget_id(team_role)
        for _ in range(4):
            item = self._read_optional(item_id, "settings") or {
                "id": item_id,
                "type": "team-budget",
                "partitionKey": "settings",
            }
            item.update(
                teamRole=team_role,
                perUserLimitUsd=_decimal_string(per_user_limit_usd),
                updatedAt=datetime.now(timezone.utc).isoformat(),
            )
            if "_etag" in item:
                operations = [("replace", (item_id, item), {"if_match_etag": item["_etag"]})]
            else:
                operations = [("create", (item,))]
            try:
                self._container.execute_item_batch(operations, partition_key="settings")
                return self._team_budget_response(item)
            except (CosmosBatchOperationError, CosmosHttpResponseError) as exc:
                if getattr(exc, "status_code", None) not in (409, 412, 424):
                    raise
        raise LedgerConflictError("team budget update conflicted repeatedly")

    def get_budget_metrics(self) -> BudgetMetricsResponse:
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        documents = self._container.query_items(
            query=(
                "SELECT * FROM c WHERE c.type = 'budget' "
                "AND ENDSWITH(c.partitionKey, @periodSuffix)"
            ),
            parameters=[{"name": "@periodSuffix", "value": f":{period}"}],
            enable_cross_partition_query=True,
        )
        reservations = self._container.query_items(
            query=(
                "SELECT c.callerKey, c.userKey, c.userEmail, c.userName, c.createdAt FROM c "
                "WHERE c.type = 'reservation' AND ENDSWITH(c.partitionKey, @periodSuffix)"
            ),
            parameters=[{"name": "@periodSuffix", "value": f":{period}"}],
            enable_cross_partition_query=True,
        )
        identities: dict[str, dict[str, Any]] = {}
        for reservation in reservations:
            caller_key = reservation.get("callerKey", "")
            if caller_key and reservation.get("createdAt", "") >= identities.get(caller_key, {}).get("createdAt", ""):
                identities[caller_key] = reservation
        users = []
        for document in documents:
            identity = identities.get(document.get("callerKey", ""), {})
            enriched = {
                **document,
                "userKey": document.get("userKey") or identity.get("userKey") or document["callerKey"],
                "userEmail": document.get("userEmail") or identity.get("userEmail", ""),
                "userName": document.get("userName") or identity.get("userName", ""),
            }
            users.append(self._user_budget_metrics(enriched))
        users.sort(key=lambda item: (item.team_role.lower(), item.user_email.lower(), item.user_key))

        team_roles = {policy.team_role for policy in self.list_team_budgets()}
        team_roles.update(user.team_role for user in users)
        teams = []
        for team_role in sorted(team_roles, key=str.lower):
            members = [user for user in users if user.team_role.lower() == team_role.lower()]
            teams.append(TeamBudgetMetrics(
                team_role=team_role,
                period=period,
                active_users=len(members),
                allocated_usd=sum((user.allocated_usd for user in members), Decimal("0")),
                spent_usd=sum((user.spent_usd for user in members), Decimal("0")),
                reserved_usd=sum((user.reserved_usd for user in members), Decimal("0")),
                remaining_usd=sum((user.remaining_usd for user in members), Decimal("0")),
            ))
        return BudgetMetricsResponse(period=period, teams=teams, users=users)

    def settle(
        self,
        reservation_id: str,
        actual_usd: Decimal,
        usage: dict[str, Any],
        final_status: str = "settled",
    ) -> SettlementResponse:
        partition_key = self._partition_from_reservation_id(reservation_id)
        for _ in range(4):
            reservation = self._read_optional(reservation_id, partition_key)
            if not reservation:
                raise ReservationNotFoundError(reservation_id)
            if reservation["status"] in ("settled", "released"):
                return self._settlement_response(reservation)

            budget = self._container.read_item(item="budget", partition_key=partition_key)
            reserved = _decimal(reservation["reservedUsd"])
            now = datetime.now(timezone.utc).isoformat()
            budget["reservedUsd"] = _decimal_string(_decimal(budget["reservedUsd"]) - reserved)
            budget["spentUsd"] = _decimal_string(_decimal(budget["spentUsd"]) + actual_usd)
            budget["updatedAt"] = now
            remaining_after_settlement = max(
                Decimal('0'),
                _decimal(budget["limitUsd"])
                - _decimal(budget["spentUsd"])
                - _decimal(budget["reservedUsd"]),
            )
            reservation.update(
                status=final_status,
                actualUsd=_decimal_string(actual_usd),
                releasedUsd=_decimal_string(max(Decimal('0'), reserved - actual_usd)),
                remainingAfterSettlementUsd=_decimal_string(remaining_after_settlement),
                settledAt=now,
                usage=usage,
            )
            operations = [
                ("replace", ("budget", budget), {"if_match_etag": budget["_etag"]}),
                ("replace", (reservation_id, reservation), {"if_match_etag": reservation["_etag"]}),
            ]
            try:
                self._container.execute_item_batch(operations, partition_key=partition_key)
                return self._settlement_response(reservation, budget)
            except (CosmosBatchOperationError, CosmosHttpResponseError) as exc:
                if getattr(exc, "status_code", None) not in (412, 424):
                    raise
        raise LedgerConflictError("settlement conflicted repeatedly")

    def release(self, reservation_id: str) -> SettlementResponse:
        return self.settle(reservation_id, Decimal('0'), {"released": True}, final_status="released")

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        partition_key = self._partition_from_reservation_id(reservation_id)
        reservation = self._read_optional(reservation_id, partition_key)
        if not reservation:
            raise ReservationNotFoundError(reservation_id)
        return reservation

    def _new_budget(self, partition_key: str, caller_key: str) -> dict[str, Any]:
        default_limit = self.get_default_budget().limit_usd
        return {
            "id": "budget", "type": "budget", "partitionKey": partition_key,
            "callerKey": caller_key, "currency": "USD", "limitUsd": _decimal_string(default_limit),
            "limitSource": "default", "spentUsd": "0", "reservedUsd": "0",
        }

    def _effective_budget(self, partition_key: str, caller_key: str) -> dict[str, Any]:
        budget = self._read_optional("budget", partition_key)
        if budget is None:
            return self._new_budget(partition_key, caller_key)
        if budget.get("limitSource", "default") == "default":
            budget["limitUsd"] = _decimal_string(self.get_default_budget().limit_usd)
            budget["limitSource"] = "default"
        return budget

    def _read_optional(self, item_id: str, partition_key: str) -> dict[str, Any] | None:
        try:
            return self._container.read_item(item=item_id, partition_key=partition_key)
        except CosmosResourceNotFoundError:
            return None

    @staticmethod
    def _partition_key(caller_key: str) -> str:
        return f"{caller_key}:{datetime.now(timezone.utc):%Y-%m}"

    @staticmethod
    def _reservation_id(partition_key: str, operation_id: str) -> str:
        encoded = base64.urlsafe_b64encode(partition_key.encode()).decode().rstrip("=")
        return f"{encoded}.{operation_id}"

    @staticmethod
    def _partition_from_reservation_id(reservation_id: str) -> str:
        try:
            encoded, _ = reservation_id.split(".", 1)
            return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReservationNotFoundError(reservation_id) from exc

    @staticmethod
    def _reservation_response(item: dict[str, Any]) -> ReservationResponse:
        return ReservationResponse(
            allowed=True,
            reservation_id=item["id"],
            reserved_usd=_decimal(item["reservedUsd"]),
            remaining_usd=_decimal(item["remainingAfterReserveUsd"]),
        )

    def _settlement_response(self, item: dict[str, Any], budget: dict[str, Any] | None = None) -> SettlementResponse:
        remaining = _decimal(item.get("remainingAfterSettlementUsd", "0"))
        if budget is not None:
            remaining = max(
                Decimal('0'),
                _decimal(budget["limitUsd"])
                - _decimal(budget["spentUsd"])
                - _decimal(budget["reservedUsd"]),
            )
        return SettlementResponse(
            reservation_id=item["id"], status=item["status"], actual_usd=_decimal(item["actualUsd"]),
            released_usd=_decimal(item["releasedUsd"]), remaining_usd=remaining,
            spend_breakdown=item.get("usage", {}).get("spendBreakdown"),
        )

    @staticmethod
    def _budget_response(item: dict[str, Any]) -> BudgetResponse:
        limit = _decimal(item["limitUsd"])
        spent = _decimal(item["spentUsd"])
        reserved = _decimal(item["reservedUsd"])
        return BudgetResponse(
            caller_key=item["callerKey"],
            period=item["partitionKey"].rsplit(":", 1)[-1],
            limit_usd=limit,
            spent_usd=spent,
            reserved_usd=reserved,
            remaining_usd=max(Decimal("0"), limit - spent - reserved),
        )

    @staticmethod
    def _team_budget_response(item: dict[str, Any]) -> TeamBudgetResponse:
        return TeamBudgetResponse(
            team_role=item["teamRole"],
            per_user_limit_usd=_decimal(item["perUserLimitUsd"]),
        )

    @staticmethod
    def _user_budget_metrics(item: dict[str, Any]) -> UserBudgetMetrics:
        limit = _decimal(item["limitUsd"])
        spent = _decimal(item["spentUsd"])
        reserved = _decimal(item["reservedUsd"])
        return UserBudgetMetrics(
            user_key=item.get("userKey", item["callerKey"]),
            user_email=item.get("userEmail", ""),
            user_name=item.get("userName", ""),
            team_role=item.get("teamRole") or "Unassigned",
            period=item["partitionKey"].rsplit(":", 1)[-1],
            allocated_usd=limit,
            spent_usd=spent,
            reserved_usd=reserved,
            remaining_usd=max(Decimal("0"), limit - spent - reserved),
        )

    @staticmethod
    def _team_budget_id(team_role: str) -> str:
        return f"team-budget:{team_role.lower()}"


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _decimal_string(value: Decimal) -> str:
    return format(value, 'f')