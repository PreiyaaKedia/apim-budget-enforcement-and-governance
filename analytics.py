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
| where message == "llm-request"
| extend Metadata = todynamic(customDimensions)
| project
    timestamp,
    operation_Id,
    correlationId = tostring(Metadata.correlationId),
    teamRole = tostring(Metadata.teamRole),
    appId = tostring(Metadata.appId),
    deployment = tostring(Metadata.deployment),
    model = tostring(Metadata.model),
    userKey = tostring(Metadata.userKey),
    userEmail = tostring(Metadata.userEmail),
    userName = tostring(Metadata.userName),
    status = toint(Metadata.status),
    inputTokens = tolong(Metadata.inputTokens),
    outputTokens = tolong(Metadata.completionTokens),
    cacheWriteTokens = tolong(Metadata.cacheWriteTokens),
    cacheReadTokens = tolong(Metadata.cacheReadTokens),
    actualUsd = todouble(Metadata.actualUsd),
    servedBy = tostring(Metadata.servedBy)
| extend totalTokens = inputTokens + outputTokens + cacheWriteTokens + cacheReadTokens
| join kind=leftouter RequestLatency on operation_Id
| extend
    latencyMs = coalesce(todouble(latencyMs), 0.0),
    status = coalesce(status, requestResultCode)
| project-away operation_Id1, requestResultCode
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
    correlationId = CorrelationId,
    teamRole = tostring(coalesce(Metadata.teamRole, Metadata["teamRole"])),
    appId = tostring(coalesce(Metadata.appId, Metadata["appId"])),
    deployment = tostring(coalesce(Metadata.deployment, Metadata["deployment"])),
    model = tostring(coalesce(Metadata.model, Metadata["model"])),
    userKey = tostring(coalesce(Metadata.userKey, Metadata["userKey"])),
    userEmail = tostring(coalesce(Metadata.userEmail, Metadata["userEmail"])),
    userName = tostring(coalesce(Metadata.userName, Metadata["userName"])),
    status = toint(coalesce(Metadata.status, Metadata["status"])),
    inputTokens = tolong(coalesce(Metadata.inputTokens, Metadata["inputTokens"])),
    outputTokens = tolong(coalesce(Metadata.completionTokens, Metadata["completionTokens"])),
    cacheWriteTokens = tolong(coalesce(Metadata.cacheWriteTokens, Metadata["cacheWriteTokens"])),
    cacheReadTokens = tolong(coalesce(Metadata.cacheReadTokens, Metadata["cacheReadTokens"])),
    totalTokens = tolong(coalesce(Metadata.totalTokens, Metadata["totalTokens"])),
    actualUsd = todouble(coalesce(Metadata.actualUsd, Metadata["actualUsd"])),
    servedBy = tostring(coalesce(Metadata.servedBy, Metadata["servedBy"])),
    latencyMs = tolong(TotalTime),
    backendLatencyMs = tolong(BackendTime)
| order by timestamp desc
| take 20001
""".strip()


def _aggregate(
    records: list[dict[str, Any]],
    days: int,
    team_filter: str,
    app_filter: str,
    model_filter: str,
) -> dict[str, Any]:
    truncated = len(records) > 20000
    records = records[:20000]
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
        "reason": "" if records else "No APIM usage telemetry was found in the selected period.",
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
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        identity = str(row.get("userEmail") or row.get("userKey") or "Unknown")
        groups[identity].append(row)
    result = []
    for identity, rows in groups.items():
        latest = rows[0]
        result.append({
            "identity": identity,
            "name": str(latest.get("userName") or ""),
            "teamRole": str(latest.get("teamRole") or "Unassigned"),
            "appId": str(latest.get("appId") or "Unknown"),
            **_metrics(rows),
        })
    return sorted(result, key=lambda item: (-item["spendUsd"], -item["tokens"], item["identity"]))


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