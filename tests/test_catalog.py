from datetime import datetime, timezone

from app.catalog import PriceCatalog
from app.models import PriceDocument


class FakePriceContainer:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def query_items(self, query: str, parameters=None, partition_key=None, **kwargs):
        if partition_key is not None:
            return [item for (deployment, _), item in self.items.items() if deployment == partition_key]
        return list(self.items.values())

    def upsert_item(self, item: dict) -> None:
        self.items[(item["deployment"], item["id"])] = item

    def delete_item(self, item: str, partition_key: str) -> None:
        del self.items[(partition_key, item)]


def price(input_rate: str = "5") -> PriceDocument:
    return PriceDocument.model_validate(
        {
            "id": "model-a",
            "deployment": "model-a",
            "inputUsdPerMillion": input_rate,
            "cacheWriteUsdPerMillion": "6.25",
            "cacheReadUsdPerMillion": "0.5",
            "outputUsdPerMillion": "25",
            "maxOutputTokens": 32768,
            "effectiveFrom": "2026-09-01T00:00:00Z",
            "source": "test",
        }
    )


def test_admin_price_crud_invalidates_runtime_cache() -> None:
    container = FakePriceContainer()
    catalog = PriceCatalog(container)
    catalog.upsert(price("5"))
    assert catalog.get("model-a", now=datetime(2026, 9, 22, tzinfo=timezone.utc)).input_usd_per_million == 5

    catalog.upsert(price("7"))

    assert catalog.get("model-a", now=datetime(2026, 9, 22, tzinfo=timezone.utc)).input_usd_per_million == 7
    assert len(catalog.list()) == 1
    catalog.delete("model-a", "model-a")
    assert catalog.list() == []