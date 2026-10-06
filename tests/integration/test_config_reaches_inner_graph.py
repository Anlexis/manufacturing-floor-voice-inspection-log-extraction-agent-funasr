"""A declared runtime value must change behaviour END TO END.

config/config.yaml is passed as Graph(config=...) by the registry and by the
standalone server. The `voice_inspection` block is validated at compile time
and injected into the inner domain nodes; these tests prove a declared value
reaches the inner graph through the full nested invoke — not at node level.
"""

import json

import pytest

from framework.errors import ConfigError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import ManufacturingFloorVoiceInspectionLogExtractionAgent, _runtime_config, domain_settings

_PAYLOAD = json.dumps(
    {"audio": "dGVzdF9hdWRpb19kYXRh_bWZnX2MyXzA2MV9pbnNwZWN0aW9u", "format": "wav", "duration_s": 10.0}
)
_CTX = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def _invoke(config: dict, input_context: dict | None = None) -> dict:
    agent = ManufacturingFloorVoiceInspectionLogExtractionAgent(config=config)
    agent.compile()
    return agent.invoke(_PAYLOAD, ctx=_CTX, input_context=input_context or {})


def test_the_shipped_config_file_is_read_and_valid():
    config = _runtime_config()
    assert config["max_retry"] == 3
    settings = domain_settings(config)
    assert settings["plant_id"] == "MFG-PLANT-01"
    assert "外径寸法" in settings["spec_ranges"]


def test_declared_plant_and_product_line_render_in_the_report():
    result = _invoke({"voice_inspection": {"plant_id": "PLANT-TEST-7", "product_line": "LINE-Z"}})
    assert result["status"] == AgentStatus.SUCCESS
    assert "PLANT-TEST-7" in result["output"] and "LINE-Z" in result["output"]
    assert result["qms_record"]["plant_id"] == "PLANT-TEST-7"


def test_declared_spec_ranges_change_the_verdict():
    # Baseline transcript reads 外径 10.02 mm — conforming under the defaults …
    assert _invoke({})["qms_record"]["overall_verdict"] == "適合"
    # … and non-conforming once the declared tolerance excludes it.
    tight = {"voice_inspection": {"spec_ranges": {"外径寸法": {"min": 9.95, "max": 10.00, "unit": "mm"}}}}
    result = _invoke(tight)
    assert result["qms_record"]["overall_verdict"] == "不適合"
    assert result["spec_violations"][0]["spec_max"] == 10.0


def test_configured_funasr_endpoint_fails_closed_without_a_transcript():
    result = _invoke({"voice_inspection": {"funasr_endpoint": "https://funasr.example.internal/asr"}})
    assert result["status"] == AgentStatus.ERROR
    assert not (result.get("output") or "")


def test_configured_funasr_endpoint_still_serves_a_caller_transcript():
    result = _invoke(
        {"voice_inspection": {"funasr_endpoint": "https://funasr.example.internal/asr"}},
        {"transcript": "部品番号P-1、外径10.01ミリメートル。担当者ID INS-1。合格。"},
    )
    assert result["status"] == AgentStatus.SUCCESS
    assert result["transcript_source"] == "caller"


@pytest.mark.parametrize(
    "block",
    [
        {"spec_ranges": {"外径寸法": {"min": "NaN", "max": 1}}},
        {"spec_ranges": {"外径寸法": {"min": 2, "max": 1}}},
        {"spec_ranges": {"not_a_metric": {"min": 0, "max": 1}}},
        {"plant_id": "plant 1"},
        {"funasr_endpoint": "ftp://x"},
        "not-a-mapping",
    ],
)
def test_invalid_domain_config_stops_graph_construction(block):
    agent = ManufacturingFloorVoiceInspectionLogExtractionAgent(config={"voice_inspection": block})
    with pytest.raises(ConfigError):
        agent.compile()


def test_max_retry_is_validated_by_the_framework():
    agent = ManufacturingFloorVoiceInspectionLogExtractionAgent(config={"max_retry": -1})
    with pytest.raises(ConfigError):
        agent.compile()
