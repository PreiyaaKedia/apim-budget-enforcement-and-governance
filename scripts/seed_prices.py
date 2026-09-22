import json
import os
import sys
from pathlib import Path

from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

from app.models import PriceDocument


def main() -> None:
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "prices.example.json")
    prices = [PriceDocument.model_validate(item) for item in json.loads(source.read_text())]
    client = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=DefaultAzureCredential())
    container = client.get_database_client(
        os.environ.get("COSMOS_DATABASE", "cost-enforcement")
    ).get_container_client(os.environ.get("COSMOS_PRICING_CONTAINER", "pricing"))
    for price in prices:
        container.upsert_item(price.model_dump(by_alias=True, mode="json", exclude_none=True))
        print(f"Upserted {price.id} for {price.deployment}")


if __name__ == "__main__":
    main()