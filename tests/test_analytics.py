from datetime import UTC, datetime

from analytics import AzureMonitorAnalytics, _aggregate


def test_aggregate_builds_finops_dimensions_and_latency_distribution() -> None:
    records = [
        {
            "timestamp": datetime(2026, 9, 10, 12, tzinfo=UTC),
            "teamRole": "Team.Engineering",
            "appId": "app-a",
            "deployment": "model-a-deployment",
            "model": "model-a",
            "userEmail": "one@example.com",
            "userName": "One",
            "status": 200,
            "inputTokens": 100,
            "outputTokens": 20,
            "cacheWriteTokens": 10,
            "cacheReadTokens": 40,
            "totalTokens": 170,
            "actualUsd": 0.02,
            "latencyMs": 800,
        },
        {
            "timestamp": datetime(2026, 9, 10, 13, tzinfo=UTC),
            "teamRole": "Team.Engineering",
            "appId": "app-a",
            "deployment": "model-a-deployment",
            "model": "model-a",
            "userEmail": "one@example.com",
            "userName": "One",
            "status": 500,
            "inputTokens": 60,
            "outputTokens": 10,
            "cacheWriteTokens": 0,
            "cacheReadTokens": 20,
            "totalTokens": 90,
            "actualUsd": 0.01,
            "latencyMs": 5500,
        },
    ]

    report = _aggregate(records, 30, "Team.Engineering", "", "")

    assert report["summary"] == {
        "requests": 2,
        "tokens": 260,
        "spendUsd": 0.03,
        "averageLatencyMs": 3150.0,
        "p95LatencyMs": 5500.0,
        "errorRate": 50.0,
        "cacheReadRate": 26.1,
    }
    assert report["teams"][0]["name"] == "Team.Engineering"
    assert report["models"][0]["tokenMix"] == {
        "input": 160,
        "output": 30,
        "cacheWrite": 10,
        "cacheRead": 60,
    }
    assert report["latencyDistribution"][0]["requests"] == 1
    assert report["latencyDistribution"][3]["requests"] == 1
    assert report["users"][0]["identity"] == "one@example.com"


def test_application_insights_resource_query_is_preferred() -> None:
    class Column:
        def __init__(self, name: str) -> None:
            self.name = name

    class Table:
        columns = [Column("model"), Column("totalTokens"), Column("actualUsd")]
        rows = [["model-a", 10, 0.01]]

    class Response:
        tables = [Table()]

    class Client:
        resource_call = None

        def query_resource(self, resource_id, query, *, timespan):
            self.resource_call = (resource_id, query, timespan)
            return Response()

        def query_workspace(self, *args, **kwargs):
            raise AssertionError("workspace fallback must not be used")

    client = Client()
    analytics = AzureMonitorAnalytics(
        workspace_id="workspace-id",
        resource_id="/subscriptions/test/components/dev-multiagent-ai",
        client=client,
    )

    report = analytics.dashboard(days=7)

    assert report["summary"]["tokens"] == 10
    assert client.resource_call[0].endswith("/components/dev-multiagent-ai")
    assert 'where message == "llm-request"' in client.resource_call[1]
    assert "inputTokens + outputTokens + cacheWriteTokens + cacheReadTokens" in client.resource_call[1]
    assert client.resource_call[2].days == 7