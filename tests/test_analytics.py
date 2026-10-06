from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from analytics import AzureMonitorAnalytics, _aggregate, _application_insights_usage_query, _gateway_usage_query


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
        "unsettledRequests": 0,
        "spendBreakdown": {"input": 0, "output": 0, "cacheWrite": 0, "cacheRead": 0, "unallocated": 0.03},
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
    assert 'where message in ("llm-request", "llm-usage: llm-request")' in client.resource_call[1]
    assert 'Metadata["prop__inputTokens"]' in client.resource_call[1]
    assert client.resource_call[2].days == 7


def test_duplicate_gateway_traces_count_once_and_recompute_claude_total() -> None:
    record = {
        "correlationId": "request-1",
        "userEmail": "user@example.com",
        "teamRole": "Team.Engineering",
        "appId": "cowork",
        "inputTokens": 100,
        "outputTokens": 20,
        "cacheWriteTokens": 10,
        "cacheReadTokens": 1000,
        "totalTokens": 0,
        "actualUsd": 0.02,
    }
    original = dict(record)
    summary = _aggregate([record, dict(record)], 30, "", "", "")["summary"]
    assert summary["requests"] == 1
    assert summary["tokens"] == 1130
    assert summary["spendUsd"] == 0.02
    assert record == original


def test_distinct_cowork_calls_and_anonymous_records_are_not_collapsed() -> None:
    rows = [{"correlationId": f"request-{i}", "actualUsd": 0} for i in range(20)]
    rows.extend([{"actualUsd": 0}, {"actualUsd": 0}])
    assert _aggregate(rows, 30, "", "", "")["summary"]["requests"] == 22


def test_duplicate_prefers_settled_usage_and_keeps_users_split_by_app() -> None:
    rows = [
        {"correlationId": "one", "userEmail": "u@example.com", "appId": "a", "actualUsd": None},
        {"correlationId": "one", "userEmail": "u@example.com", "appId": "a", "actualUsd": 0.01},
        {"correlationId": "two", "userEmail": "u@example.com", "appId": "b", "actualUsd": 0.02},
    ]
    report = _aggregate(rows, 30, "", "", "")
    assert report["summary"]["requests"] == 2
    assert {(user["appId"], user["requests"], user["spendUsd"]) for user in report["users"]} == {
        ("a", 1, 0.01), ("b", 1, 0.02),
    }
    assert len(report["apps"]) == 2


def test_cache_spend_and_unknown_settlement_are_explicit() -> None:
    rows = [
        {
            "correlationId": "settled",
            "actualUsd": 0.004,
            "inputUsd": 0.001,
            "outputUsd": 0.002,
            "cacheWriteUsd": 0.00075,
            "cacheReadUsd": 0.00025,
        },
        {"correlationId": "stream", "actualUsd": None, "settlementState": "streaming_unsettled"},
    ]
    report = _aggregate(rows, 30, "", "", "")
    assert report["summary"]["unsettledRequests"] == 1
    assert report["summary"]["spendBreakdown"] == {
        "input": 0.001, "output": 0.002, "cacheWrite": 0.00075, "cacheRead": 0.00025, "unallocated": 0,
    }
    assert "no confirmed actual cost" in report["reason"]


def test_queries_read_both_apim_metadata_shapes_without_duplicate_projections() -> None:
    for query in (_application_insights_usage_query(7), _gateway_usage_query(7)):
        for key in ("teamRole", "appId", "cacheReadTokens", "cacheWriteUsd", "settlementState"):
            assert f'Metadata["prop__{key}"]' in query
        assert query.count("actualUsd = ") == 1
        assert query.count("servedBy = ") == 1


def test_partial_monitor_query_is_not_reported_as_an_empty_success() -> None:
    client = SimpleNamespace(query_workspace=lambda *_args, **_kwargs: SimpleNamespace(
        partial_error="query limit exceeded", tables=[],
    ))
    with pytest.raises(RuntimeError, match="incomplete usage data"):
        AzureMonitorAnalytics(workspace_id="workspace", client=client).dashboard()