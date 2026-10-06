from pathlib import Path
import xml.etree.ElementTree as ET


POLICY = Path(__file__).resolve().parents[1] / "policy.xml"


def test_policy_is_valid_xml_and_does_not_retry_the_same_backend() -> None:
    policy = ET.parse(POLICY).getroot()
    assert policy.find("backend/retry") is None
    assert policy.find("backend/forward-request") is not None
    assert len(policy.findall(".//set-backend-service")) == 1


def test_count_tokens_bypasses_inference_reservations_and_telemetry() -> None:
    policy = ET.parse(POLICY).getroot()
    branch = policy.find("inbound/choose/when/send-request")
    assert branch is not None
    for section in ("inbound", "outbound"):
        guarded = policy.find(f"{section}/choose/when[@condition='@(!(bool)context.Variables[\"isTokenCount\"])']")
        assert guarded is not None
        assert guarded.find("send-request") is not None or guarded.find("choose") is not None


def test_usage_totals_include_cache_and_streaming_is_not_settled_as_zero() -> None:
    policy = ET.parse(POLICY).getroot()
    usage = policy.find(".//set-variable[@name='responseUsage']").attrib["value"]
    assert "text/event-stream" in usage
    assert "streaming_unsettled" in usage
    total = policy.find(".//set-variable[@name='totalTokens']").attrib["value"]
    assert all(key in total for key in ("inputTokens", "completionTokens", "cacheWriteTokens", "cacheReadTokens"))
    settlement = policy.find("outbound/choose/when/choose/when/send-request")
    assert settlement is not None
    assert "/settle" in settlement.find("set-url").text


def test_unsettled_trace_metadata_never_uses_an_empty_value() -> None:
    policy = ET.parse(POLICY).getroot()
    unresolved_trace = policy.find(
        "outbound/choose/when//trace[@source='cost-settlement']"
    )
    usage_trace = policy.find(
        "outbound/choose/when//trace[@source='llm-usage']"
    )
    assert unresolved_trace is not None
    assert usage_trace is not None
    unresolved_metadata = {
        node.attrib["name"]: node.attrib["value"]
        for node in unresolved_trace.findall("metadata")
    }
    usage_metadata = {
        node.attrib["name"]: node.attrib["value"]
        for node in usage_trace.findall("metadata")
    }
    assert '?? ""' not in unresolved_metadata["status"]
    assert "not_called" in unresolved_metadata["status"]
    for name in (
        "actualUsd",
        "inputUsd",
        "outputUsd",
        "cacheWriteUsd",
        "cacheReadUsd",
        "remainingBudgetUsd",
    ):
        assert '?? ""' not in usage_metadata[name]
    assert all(
        "unavailable" in usage_metadata[name]
        for name in (
            "inputUsd",
            "outputUsd",
            "cacheWriteUsd",
            "cacheReadUsd",
        )
    )


def test_thresholds_remain_high_and_quota_period_is_30_days() -> None:
    policy = ET.parse(POLICY).getroot()
    assert [node.attrib["tokens-per-minute"] for node in policy.findall(".//llm-token-limit")] == [
        "10000000", "50000000",
    ]
    assert all(node.attrib["renewal-period"] == "2592000" for node in policy.findall(".//quota-by-key"))
