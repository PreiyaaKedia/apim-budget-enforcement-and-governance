from azure.cosmos import _cosmos_client_connection


def test_batch_replace_formats_etag_as_if_match() -> None:
    operations = [
        ("replace", ("budget", {"id": "budget"}), {"if_match_etag": '"etag-1"'}),
    ]

    formatted = _cosmos_client_connection.base._format_batch_operations(operations)

    assert formatted[0]["ifMatch"] == '"etag-1"'