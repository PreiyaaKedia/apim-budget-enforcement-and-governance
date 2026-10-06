from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import timedelta
from typing import Any


EMPTY_DASHBOARD = {
    "available": False,
    "reason": "LOG_ANALYTICS_WORKSPACE_ID is not configured.",
    "rangeDays": 30,
    "truncated": False,
    "summary": {},
    "trends": [],
    "teams": [],
    "apps": [],
    "models": [],
    "users": [],
    "filters": {"teams": [], "apps": [], "models": []},
}


class AzureMonitorAnalytics:
    def __init__(
        self,
        workspace_id: str = "",
        resource_id: str = "",
        client: Any | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.resource_id = resource_id
        if client is None:
            from azure.identity import DefaultAzureCredential
            from azure.monitor.query import LogsQueryClient

            client = LogsQueryClient(DefaultAzureCredential(process_timeout=60))
        self.client = client

    def dashboard(
        self,
        days: int = 30,
        team: str = "",
        app: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        safe_days = min(max(days, 1), 90)
        if self.resource_id:
            response = self.client.query_resource(
                self.resource_id,
                _application_insights_usage_query(safe_days),
                timespan=timedelta(days=safe_days),
            )
        else:
            response = self.client.query_workspace(
                self.workspace_id,
                _gateway_usage_query(safe_days),
                timespan=timedelta(days=safe_days),
            )
        partial_error = getattr(response, "partial_error", None)
        if partial_error is not None:
            raise RuntimeError(f"Azure Monitor returned incomplete usage data: {partial_error}")
        tables = getattr(response, "tables", [])
        if not tables:
            return _aggregate([], safe_days, team, app, model)
        table = tables[0]
        columns = [getattr(column, "name", str(column)) for column in table.columns]
        records = [dict(zip(columns, row, strict=True)) for row in table.rows]
        return _aggregate(records, safe_days, team, app, model)


def configured_analytics() -> AzureMonitorAnalytics | None:
    resource_id = os.environ.get("APPLICATION_INSIGHTS_RESOURCE_ID", "").strip()
    workspace_id = os.environ.get("LOG_ANALYTICS_WORKSPACE_ID", "").strip()
    if not resource_id and not workspace_id:
        return None
    return AzureMonitorAnalytics(workspace_id=workspace_id, resource_id=resource_id)


def _application_insights_usage_query(days: int) -> str:
    return f"""
let RequestLatency = requests
| where timestamp >= ago({days}d)
| summarize
    latencyMs = max(duration),
    requestResultCode = max(toint(resultCode))
    by operation_Id;
traces
| where timestamp >= ago({days}d)
| where message in ("llm-request", "llm-usage: llm-request")
| extend Metadata = todynamic(customDimensions)
| project
    timestamp,
    operation_Id,
    {_usage_projection()},
    totalTokens = tolong(coalesce(Metadata.totalTokens, Metadata["prop__totalTokens"]))
| join kind=leftouter RequestLatency on operation_Id
| extend
    latencyMs = coalesce(todouble(latencyMs), 0.0),
    status = coalesce(status, requestResultCode),
    correlationId = iff(isempty(correlationId), operation_Id, correlationId)
| project-away operation_Id1, requestResultCode
| summarize arg_max(timestamp, *) by correlationId
| order by timestamp desc
| take 20001
""".strip()


def _gateway_usage_query(days: int) -> str:
    return f"""
ApiManagementGatewayLogs
| where TimeGenerated >= ago({days}d)
| where isnotempty(TraceRecords)
| mv-expand TraceRecord = TraceRecords
| extend Source = tostring(coalesce(TraceRecord.source, TraceRecord.Source))
| where Source == "llm-usage"
| extend Metadata = coalesce(TraceRecord.metadata, TraceRecord.Metadata, TraceRecord.data, TraceRecord.Data)
| project
    timestamp = TimeGenerated,
    {_usage_projection(correlation_fallback="tostring(CorrelationId)")},
    totalTokens = tolong(coalesce(Metadata.totalTokens, Metadata["totalTokens"])),
    latencyMs = tolong(TotalTime),
    backendLatencyMs = tolong(BackendTime)
| summarize arg_max(timestamp, *) by correlationId
| order by timestamp desc
| take 20001
""".strip()


def _usage_projection(correlation_fallback: str = '""') -> str:
    fields = {
        "correlationId": ("correlationId", "tostring"),
        "teamRole": ("teamRole", "tostring"),
        "appId": ("appId", "tostring"),
        "deployment": ("deployment", "tostring"),
        "model": ("model", "tostring"),
        "userKey": ("userKey", "tostring"),
        "userEmail": ("userEmail", "tostring"),
        "userName": ("userName", "tostring"),
        "status": ("status", "toint"),
        "inputTokens": ("inputTokens", "tolong"),
        "outputTokens": ("completionTokens", "tolong"),
        "cacheWriteTokens": ("cacheWriteTokens", "tolong"),
        "cacheReadTokens": ("cacheReadTokens", "tolong"),
        "inputUsd": ("inputUsd", "todouble"),
        "outputUsd": ("outputUsd", "todouble"),
        "cacheWriteUsd": ("cacheWriteUsd", "todouble"),
        "cacheReadUsd": ("cacheReadUsd", "todouble"),
        "actualUsd": ("actualUsd", "todouble"),
        "servedBy": ("servedBy", "tostring"),
        "usageState": ("usageState", "tostring"),
        "settlementState": ("settlementState", "tostring"),
    }
    projections = []
    for target, (source, conversion) in fields.items():
        value = f'coalesce(Metadata.{source}, Metadata["prop__{source}"])'
        if target == "correlationId":
            value = f"coalesce({value}, {correlation_fallback})"
        projections.append(f"{target} = {conversion}({value})")
    return ",\n    ".join(projections)


def _aggregate(
    records: list[dict[str, Any]],
    days: int,
    team_filter: str,
    app_filter: str,
    model_filter: str,
) -> dict[str, Any]:
    truncated = len(records) > 20000
    records = records[:20000]
    # Duplicate ingestion is not an additional model request. Do not collapse
    # different gateway correlation IDs (Cowork can make many calls per turn).
    deduplicated: dict[str, dict[str, Any]] = {}
    anonymous = []
    for record in records:
        row = dict(record)
        buckets = ("inputTokens", "outputTokens", "cacheWriteTokens", "cacheReadTokens")
        if any(_number(row.get(key)) > 0 for key in buckets) or row.get("usageState") == "available":
            row["totalTokens"] = sum(_number(row.get(key)) for key in buckets)
        correlation = str(row.get("correlationId") or "")
        if not correlation:
            anonymous.append(row)
            continue
        previous = deduplicated.get(correlation)
        if previous is None or _record_quality(row) > _record_quality(previous):
            deduplicated[correlation] = row
    records = list(deduplicated.values()) + anonymous
    filter_values = {
        "teams": sorted({str(row.get("teamRole") or "Unassigned") for row in records}),
        "apps": sorted({str(row.get("appId") or "Unknown") for row in records}),
        "models": sorted({str(row.get("model") or row.get("deployment") or "Unknown") for row in records}),
    }
    filtered = [
        row for row in records
        if (not team_filter or row.get("teamRole") == team_filter)
        and (not app_filter or row.get("appId") == app_filter)
        and (not model_filter or (row.get("model") or row.get("deployment")) == model_filter)
    ]
    summary = _metrics(filtered)
    return {
        "available": True,
        "reason": (
            f"{summary['unsettledRequests']} request(s) have no confirmed actual cost; "
            "streaming requires a streaming-aware settlement path. Totals exclude unknown usage/cost."
            if summary["unsettledRequests"]
            else "" if records else "No APIM usage telemetry was found in the selected period."
        ),
        "rangeDays": days,
        "truncated": truncated,
        "summary": summary,
        "trends": _trend(filtered),
        "teams": _group(filtered, "teamRole", "Unassigned"),
        "apps": _group(filtered, "appId", "Unknown"),
        "models": _group(filtered, "model", "Unknown", include_token_mix=True),
        "latencyDistribution": _latency_distribution(filtered),
        "users": _users(filtered),
        "filters": filter_values,
    }


def _record_quality(row: dict[str, Any]) -> tuple[bool, bool, str]:
    return (
        row.get("actualUsd") not in (None, ""),
        row.get("usageState") == "available" or _number(row.get("totalTokens")) > 0,
        str(row.get("timestamp") or ""),
    )


def _metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    requests = len(records)
    latencies = sorted(_number(row.get("latencyMs")) for row in records)
    cache_reads = sum(_number(row.get("cacheReadTokens")) for row in records)
    input_tokens = sum(_number(row.get("inputTokens")) for row in records)
    cache_writes = sum(_number(row.get("cacheWriteTokens")) for row in records)
    input_side_tokens = input_tokens + cache_reads + cache_writes
    failures = sum(1 for row in records if int(_number(row.get("status"))) >= 400)
    return {
        "requests": requests,
        "tokens": int(sum(_number(row.get("totalTokens")) for row in records)),
        "spendUsd": round(sum(_number(row.get("actualUsd")) for row in records), 6),
        "averageLatencyMs": round(sum(latencies) / requests, 1) if requests else 0,
        "p95LatencyMs": round(_percentile(latencies, 0.95), 1),
        "errorRate": round(failures / requests * 100, 1) if requests else 0,
        "cacheReadRate": round(cache_reads / input_side_tokens * 100, 1) if input_side_tokens else 0,
        "unsettledRequests": sum(
            row.get("actualUsd") in (None, "")
            or row.get("settlementState") in (
                "settlement_failed", "streaming_unsettled", "missing_usage", "invalid_response",
            )
            for row in records
        ),
        "spendBreakdown": {
            "input": round(sum(_number(row.get("inputUsd")) for row in records), 9),
            "output": round(sum(_number(row.get("outputUsd")) for row in records), 9),
            "cacheWrite": round(sum(_number(row.get("cacheWriteUsd")) for row in records), 9),
            "cacheRead": round(sum(_number(row.get("cacheReadUsd")) for row in records), 9),
            "unallocated": round(sum(
                _number(row.get("actualUsd")) - sum(
                    _number(row.get(key)) for key in ("inputUsd", "outputUsd", "cacheWriteUsd", "cacheReadUsd")
                ) for row in records
            ), 9),
        },
    }


def _group(
    records: list[dict[str, Any]],
    key: str,
    fallback: str,
    include_token_mix: bool = False,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        groups[str(row.get(key) or (row.get("deployment") if key == "model" else "") or fallback)].append(row)
    result = []
    for name, rows in groups.items():
        item = {"name": name, **_metrics(rows)}
        if include_token_mix:
            item["tokenMix"] = {
                "input": int(sum(_number(row.get("inputTokens")) for row in rows)),
                "output": int(sum(_number(row.get("outputTokens")) for row in rows)),
                "cacheWrite": int(sum(_number(row.get("cacheWriteTokens")) for row in rows)),
                "cacheRead": int(sum(_number(row.get("cacheReadTokens")) for row in rows)),
            }
        result.append(item)
    return sorted(result, key=lambda item: (-item["spendUsd"], -item["tokens"], item["name"]))


def _trend(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        timestamp = row.get("timestamp")
        day = timestamp.date().isoformat() if hasattr(timestamp, "date") else str(timestamp)[:10]
        groups[day].append(row)
    return [{"date": day, **_metrics(groups[day])} for day in sorted(groups)]


def _users(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        identity = str(row.get("userEmail") or row.get("userKey") or "Unknown")
        groups[(identity, str(row.get("teamRole") or "Unassigned"), str(row.get("appId") or "Unknown"))].append(row)
    result = []
    for (identity, team_role, app_id), rows in groups.items():
        latest = rows[0]
        result.append({
            "identity": identity,
            "name": str(latest.get("userName") or ""),
            "teamRole": team_role,
            "appId": app_id,
            **_metrics(rows),
        })
    return sorted(result, key=lambda item: (-item["spendUsd"], -item["tokens"], item["identity"], item["appId"]))


def _latency_distribution(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = [
        ("< 1s", 0, 1000),
        ("1-2s", 1000, 2000),
        ("2-5s", 2000, 5000),
        ("5-10s", 5000, 10000),
        ("10-30s", 10000, 30000),
        ("30s+", 30000, math.inf),
    ]
    latencies = [_number(row.get("latencyMs")) for row in records]
    return [
        {"label": label, "requests": sum(1 for value in latencies if minimum <= value < maximum)}
        for label, minimum, maximum in buckets
    ]


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0
    return values[max(0, math.ceil(len(values) * percentile) - 1)]