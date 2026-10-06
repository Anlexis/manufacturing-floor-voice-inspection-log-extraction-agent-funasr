# PB-6: Backbone Invoke-Order Verification — MFG-C2-061
#
# Verifies that a full Graph().invoke() with a VERIFIED_EXTERNAL caller
# executes the outer backbone nodes in the correct order:
#   InitializeNode → PreProcessNode → VoiceInspectionWorkflowGraphNode
#                  → PostProcessNode → FinalizeNode
#
# Uses InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL) —
# NEVER InvocationContext.for_internal(): an INTERNAL caller masks the inner-node
# trust-trap (INTERNAL >= ANONYMOUS always passes), making the test a false green.
#
# The SUCCESS-yielding payload must pass through AudioInputNode (valid JSON),
# FunASRTranscribeNode, SpeakerDiarizeNode, FieldExtractAndJISTermNormalizeNode,
# and SpecRangeValidateAndQMSRecordFormatNode without error.

import json

import pytest

from framework.nodes.defaults.finalize_node import FinalizeNode
from framework.nodes.defaults.initialize_node import InitializeNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import ManufacturingFloorVoiceInspectionLogExtractionAgent
from src.graph.graph import VoiceInspectionWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode

# Main-slot class for backbone-order assertion.
_MAIN_SLOT_NODE = VoiceInspectionWorkflowGraphNode

# Valid payload: passes through the full inner pipeline and produces SUCCESS.
# AudioInputNode parses JSON → FunASRTranscribeNode stub works on any non-empty audio_data
# → SpeakerDiarize works on any non-empty transcript → FieldExtract works → SpecValidate works.
_VALID_PAYLOAD = json.dumps(
    {
        "audio": "dGVzdF9hdWRpb19kYXRh_bWZnX2MyXzA2MV9pbnNwZWN0aW9u",
        "format": "wav",
        "duration_s": 10.0,
    }
)

_EXPECTED_BACKBONE = [
    InitializeNode.__name__,
    PreProcessNode.__name__,
    _MAIN_SLOT_NODE.__name__,
    PostProcessNode.__name__,
    FinalizeNode.__name__,
]


def _resolve_name(entry) -> str:
    """Normalise a node_history entry to a plain class-name string."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("node") or entry.get("name") or entry.get("class_name") or entry.get("node_name") or ""
    # type object
    if hasattr(entry, "__name__"):
        return entry.__name__
    return str(entry)


class TestBackboneInvokeOrder:
    """PB-6: full Graph().invoke() must run the backbone in the correct order."""

    @pytest.fixture(autouse=True)
    def _patch_emit(self, monkeypatch):
        def _noop(*_args, **_kwargs):
            return None

        monkeypatch.setattr("src.nodes.pre_process_node.emit_trace_event", _noop)
        monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", _noop)
        monkeypatch.setattr("src.nodes.audio_input_node.emit_trace_event", _noop)
        monkeypatch.setattr("src.nodes.funasr_transcribe_node.emit_trace_event", _noop)
        monkeypatch.setattr("src.nodes.speaker_diarize_node.emit_trace_event", _noop)
        monkeypatch.setattr("src.nodes.field_extract_and_jis_term_normalize_node.emit_trace_event", _noop)
        monkeypatch.setattr("src.nodes.spec_range_validate_and_qms_record_format_node.emit_trace_event", _noop)

    def test_backbone_order_verified_external(self):
        """Full invoke with VERIFIED_EXTERNAL asserts backbone node-history order."""
        agent = ManufacturingFloorVoiceInspectionLogExtractionAgent()
        agent.compile()

        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        assert (
            result["status"] == AgentStatus.SUCCESS
        ), f"invoke returned {result['status']}; error_log={result.get('error_log')}"
        assert result["output"], "output must be non-empty on SUCCESS"

        history = result.get("node_history", [])
        history_names = [_resolve_name(e) for e in history]

        assert history_names == _EXPECTED_BACKBONE, (
            f"Backbone order mismatch.\n" f"  expected: {_EXPECTED_BACKBONE}\n" f"  actual  : {history_names}"
        )

    def test_anonymous_caller_is_rejected(self):
        """S-1 gate: an ANONYMOUS caller must not pass PreProcessNode (VERIFIED_EXTERNAL gate)."""
        agent = ManufacturingFloorVoiceInspectionLogExtractionAgent()
        agent.compile()

        ctx = InvocationContext(caller_trust_level=TrustLevel.ANONYMOUS)
        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        # The S-1 gate on PreProcessNode (VERIFIED_EXTERNAL) must deny the ANONYMOUS caller.
        assert result["status"] == AgentStatus.ERROR, f"Expected ERROR for ANONYMOUS caller but got {result['status']}"
