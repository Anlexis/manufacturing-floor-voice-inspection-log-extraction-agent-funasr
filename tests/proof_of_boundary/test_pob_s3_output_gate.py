# PoB-S3: Output Gate (S-3) Boundary Tests — MFG-C2-061
#
# Verifies that no credential-shaped token can leave the agent — at the node
# level (module-level _security_gate_output helpers) and end to end through a
# full agent.invoke(), with a credential INJECTED after the inner gates so the
# outer boundary is the only thing standing between it and the caller.
#
# The containment property under test: AgentBaseGraph.get_output() returns
# `formatted_output or result`, on error status too. A gate that only raised,
# or redacted formatted_output while leaving state["result"] untouched, would
# still ship the un-gated inner report. The boundary must WITHHOLD: ERROR,
# every output-bearing field cleared, a truthy notice, and the secret nowhere
# in the envelope.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.domain_workflow_graph import VoiceInspectionDomainWorkflow
from src.graph.graph import ManufacturingFloorVoiceInspectionLogExtractionAgent
from src.nodes.post_process_node import MISMATCH_NOTICE, WITHHELD_NOTICE

_PAYLOAD = json.dumps({"audio": "dGVzdF9hdWRpb19kYXRhX2NsZWFu", "format": "wav", "duration_s": 8.0})
_CTX = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def _invoke(agent=None, **kwargs):
    agent = agent or ManufacturingFloorVoiceInspectionLogExtractionAgent()
    agent.compile()
    return agent.invoke(_PAYLOAD, ctx=_CTX, **kwargs)


def _inject_into_inner_output(monkeypatch, mutate):
    """Alter the inner graph's sub_result AFTER every inner node gate has run."""
    original = VoiceInspectionDomainWorkflow.get_output

    def patched(self, state):
        sub_result = original(self, state)
        mutate(sub_result)
        return sub_result

    monkeypatch.setattr(VoiceInspectionDomainWorkflow, "get_output", patched)


class TestS3OutputGatePostProcessNode:
    """S-3 output gate at node level: credential-shaped tokens withhold formatted_output."""

    @pytest.mark.parametrize(
        "report",
        [
            "担当者 password=SECRETPASS12345 総合判定: 適合",
            "token=eyJhbGciOiJSUzI1NiJ9.ABCDEF123.xyz 総合判定: 適合",
            "AKIAIOSFODNN7EXAMPLE 総合判定: 適合",
        ],
    )
    def test_credential_withholds_formatted_output(self, report):
        from src.nodes.post_process_node import PostProcessNode

        result = PostProcessNode().execute({"inspection_report": report, "result": report, "spec_violations": []})
        assert result["status"] == AgentStatus.ERROR
        assert result["formatted_output"] == WITHHELD_NOTICE
        assert result["result"] is None
        for fragment in ("SECRETPASS12345", "eyJhbGciOiJSUzI1NiJ9", "AKIAIOSFODNN7EXAMPLE"):
            assert fragment not in json.dumps(result, ensure_ascii=False)

    def test_clean_output_not_modified(self):
        from src.nodes.post_process_node import PostProcessNode

        clean_report = "部品番号P-2061 外径寸法10.01mm 総合判定: 適合"
        result = PostProcessNode().execute({"inspection_report": clean_report, "spec_violations": []})
        assert result["status"] == AgentStatus.SUCCESS
        assert result["formatted_output"] == clean_report


class TestS3OutputGateAudioInputNode:
    """S-3 at the audio ingress: the payload is digested, so nothing credential-shaped enters state."""

    def test_credential_in_audio_payload_never_enters_state(self):
        from src.nodes.audio_input_node import AudioInputNode

        bad_audio = "api_key=MYSECRETAPIKEY1234567890ABCD"
        result = AudioInputNode().execute({"validated_input": json.dumps({"audio": bad_audio, "format": "wav"})})
        assert result["status"] == AgentStatus.SUCCESS
        assert "MYSECRETAPIKEY1234567890ABCD" not in json.dumps(result)

    def test_clean_audio_yields_a_digest(self):
        from src.nodes.audio_input_node import AudioInputNode

        clean_audio = "dGVzdF9hdWRpb19kYXRhX2NsZWFu"
        result = AudioInputNode().execute({"validated_input": json.dumps({"audio": clean_audio, "format": "wav"})})
        assert result["status"] == AgentStatus.SUCCESS
        assert len(result["audio_ref"]) == 64 and clean_audio not in result["audio_ref"]


class TestS3GateEndToEnd:
    """Containment through the full nested invoke — the outer boundary is the last line."""

    def test_clean_invoke_carries_no_credential_shape(self):
        result = _invoke()
        assert result["status"] == AgentStatus.SUCCESS
        assert "【品質検査記録】" in result["output"]
        assert not _find_secret(result)

    @pytest.mark.parametrize(
        "secret",
        [
            "password=hunter2secret",  # local pattern only — the framework gate would NOT raise on this
            "AKIAIOSFODNN7EXAMPLE",  # framework pattern — the framework gate WOULD raise on this
        ],
        ids=["local-only-shape", "framework-shape"],
    )
    def test_injected_credential_is_contained_in_the_error_envelope(self, monkeypatch, secret):
        """Both halves of the union are load-bearing at the boundary:
        - a local-only shape passes the framework's scan, so only the local
          patterns keep it out of the report;
        - a framework shape left in the node's delta would make the framework
          raise INSIDE post_process, the wrapper would return a bare error
          partial without formatted_output, and get_output would fall back to
          the un-gated state["result"] — the exact leak the union closes."""

        def leak(sub_result):
            sub_result["inspection_report"] = (sub_result["inspection_report"] or "") + f"\n備考 {secret}"

        _inject_into_inner_output(monkeypatch, leak)
        result = _invoke()

        assert result["status"] == AgentStatus.ERROR
        assert result["output"] == WITHHELD_NOTICE
        assert result["qms_record"] is None and result["spec_violations"] == []
        assert result["refusal_reason"] == "output_withheld"
        envelope = json.dumps(result, ensure_ascii=False)
        assert secret not in envelope and "hunter2secret" not in envelope
        assert "Traceback" not in envelope and "/src/" not in envelope

    def test_record_report_disagreement_is_withheld(self, monkeypatch):
        def tamper(sub_result):
            measurements = sub_result["qms_record"]["measurements"]
            measurements[next(iter(measurements))]["value"] = 99.0

        _inject_into_inner_output(monkeypatch, tamper)
        result = _invoke()
        assert result["status"] == AgentStatus.ERROR
        assert result["output"] == MISMATCH_NOTICE
        assert result["qms_record"] is None


def _find_secret(result: dict) -> bool:
    from src.services.caller_contract import find_credential

    return find_credential(result) is not None
