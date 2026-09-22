from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from azure.cosmos import ContainerProxy

from app.models import PriceDocument
from app.pricing import PriceRates


class PriceNotFoundError(LookupError):
    pass


class AmbiguousPriceError(LookupError):
    pass


class PriceCatalog:
    def __init__(self, container: ContainerProxy, cache_seconds: int = 60) -> None:
        self._container = container
        self._cache_seconds = cache_seconds
        self._cache: dict[tuple[str, str], tuple[float, PriceDocument]] = {}
        self._lock = Lock()

    def get(self, deployment: str, model: str = "", now: datetime | None = None) -> PriceDocument:
        instant = now or datetime.now(timezone.utc)
        key = (deployment, model)
        cached = self._cache.get(key)
        if cached and cached[0] > monotonic():
            return cached[1]

        query = """
            SELECT * FROM c
            WHERE c.deployment = @deployment
              AND c.currency = 'USD'
              AND c.effectiveFrom <= @now
              AND (NOT IS_DEFINED(c.effectiveTo) OR IS_NULL(c.effectiveTo) OR c.effectiveTo > @now)
        """
        records = [
            PriceDocument.model_validate(item)
            for item in self._container.query_items(
                query=query,
                parameters=[
                    {"name": "@deployment", "value": deployment},
                    {"name": "@now", "value": instant.isoformat()},
                ],
                partition_key=deployment,
            )
        ]
        exact = [record for record in records if model and record.model == model]
        matches = exact or [record for record in records if record.model in ("*", model)]
        if not matches:
            raise PriceNotFoundError(f"no active USD price for deployment {deployment}")
        if len(matches) != 1:
            raise AmbiguousPriceError(f"multiple active USD prices for deployment {deployment}")

        with self._lock:
            self._cache[key] = (monotonic() + self._cache_seconds, matches[0])
        return matches[0]

    def list(self) -> list[PriceDocument]:
        records = [
            PriceDocument.model_validate(item)
            for item in self._container.query_items(
                query="SELECT * FROM c",
                enable_cross_partition_query=True,
            )
        ]
        return sorted(records, key=lambda price: (price.deployment, -price.effective_from.timestamp()))

    def upsert(self, price: PriceDocument) -> PriceDocument:
        self._container.upsert_item(price.model_dump(by_alias=True, mode="json", exclude_none=True))
        self._invalidate(price.deployment)
        return price

    def delete(self, deployment: str, price_id: str) -> None:
        self._container.delete_item(item=price_id, partition_key=deployment)
        self._invalidate(deployment)

    def _invalidate(self, deployment: str) -> None:
        with self._lock:
            self._cache = {key: value for key, value in self._cache.items() if key[0] != deployment}


def rates_from_price(price: PriceDocument) -> PriceRates:
    return PriceRates(
        input_usd_per_million=price.input_usd_per_million,
        cache_write_usd_per_million=price.cache_write_usd_per_million,
        cache_read_usd_per_million=price.cache_read_usd_per_million,
        output_usd_per_million=price.output_usd_per_million,
    )