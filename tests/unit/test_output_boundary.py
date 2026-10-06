"""Unit tests for the output boundary (src/nodes/post_process_node.py).

The boundary's stated invariants, each asserted for EVERY representation:
  * a credential shape anywhere in the outgoing structure WITHHOLDS the whole
    output — union of the framework detector and the local patterns, walked
    recursively — and clears every output-bearing field (containment, not
    redaction; the notice left in formatted_output is truthy so the base
    envelope cannot fall back to state["result"]);
  * the human-readable report carries exactly the values in the structured
    record (render fidelity) — a disagreement withholds the output;
  * on success formatted_output and result are the SAME gated text.
"""

import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import (
    EMPTY_NOTICE,
    MISMATCH_NOTICE,
    WITHHELD_NOTICE,
    PostProcessNode,
    _security_gate_output,
    render_fidelity_violation,
)
from src.nodes.spec_range_validate_and_qms_record_format_node import SpecRangeValidateAndQMSRecordFormatNode

_FIELDS = {
    "外径寸法": {"value": 10.10, "unit": "mm"},
    "内径寸法": {"value": 9.97, "unit": "mm"},
    "part_id": "P-2061",
    "inspector_id": "INS-042",
    "remarks": ["適合判定"],
}


def _inner_result() -> dict:
    return SpecRangeValidateAndQMSRecordFormatNode().execute({"normalized_fields": _FIELDS})


def _state_from(inner: dict) -> dict:
    return {
        "inspection_report": inner["inspection_report"],
        "result": inner["inspection_report"],
        "qms_record": inner["qms_record"],
        "spec_violations": inner["spec_violations"],
    }


class TestSuccessPath:
    def test_publishes_one_gated_representation(self):
        result = PostProcessNode().execute(_state_from(_inner_result()))
        assert result["status"] == AgentStatus.SUCCESS
        assert result["formatted_output"] == result["result"]
        assert result["formatted_output"].startswith("[VIOLATIONS: 外径寸法=10.1 (range 9.95–10.05)]")
        assert "総合判定: 不適合" in result["formatted_output"]
        assert result["qms_record"]["part_id"] == "P-2061"

    def test_empty_report_is_withheld_with_a_truthy_notice(self):
        result = PostProcessNode().execute({"inspection_report": "", "result": "", "spec_violations": []})
        assert result["status"] == AgentStatus.ERROR
        assert result["formatted_output"] == EMPTY_NOTICE and result["formatted_output"]
        assert result["result"] is None


class TestCredentialContainment:
    @pytest.mark.parametrize(
        "secret",
        [
            "password=hunter2secret",  # local pattern only
            "AKIAIOSFODNN7EXAMPLE",  # framework pattern only
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk",  # both
            "Bearer abcdefghijklmnopqrstuvwxyz",
        ],
    )
    @pytest.mark.parametrize("where", ["report", "record_remark", "violation_field"])
    def test_any_credential_anywhere_withholds_everything(self, secret, where):
        inner = _inner_result()
        state = _state_from(inner)
        if where == "report":
            state["inspection_report"] = state["result"] = inner["inspection_report"] + f"\n備考 {secret}"
        elif where == "record_remark":
            state["qms_record"]["remarks"].append(f"note {secret}")
        else:
            state["spec_violations"][0]["status"] = f"上限外 {secret}"

        result = PostProcessNode().execute(state)

        assert result["status"] == AgentStatus.ERROR
        assert result["formatted_output"] == WITHHELD_NOTICE
        assert result["result"] is None
        assert result["inspection_report"] is None
        assert result["qms_record"] is None
        assert result["spec_violations"] == []
        assert result["refusal_reason"] == "output_withheld"
        assert secret not in json.dumps(result, ensure_ascii=False)
        assert secret.split("=")[-1] not in json.dumps(result, ensure_ascii=False)

    def test_gate_is_recursive_over_nested_structures(self):
        delta = {"qms_record": {"measurements": {"x": {"note": ["password=hunter2secret"]}}}, "status": "success"}
        gated = _security_gate_output(delta)
        assert gated["status"] == AgentStatus.ERROR
        assert gated["qms_record"] is None
        assert "hunter2secret" not in json.dumps(gated)

    def test_clean_delta_passes_through_unchanged(self):
        delta = {"formatted_output": "総合判定: 適合", "qms_record": {"part_id": "P-1"}, "status": "success"}
        assert _security_gate_output(delta) is delta


class TestRenderFidelity:
    def test_a_report_that_agrees_with_the_record_passes(self):
        inner = _inner_result()
        assert render_fidelity_violation(inner["inspection_report"], inner["qms_record"]) is None

    @pytest.mark.parametrize(
        "mutate,expected",
        [
            (lambda r: r["measurements"]["外径寸法"].__setitem__("value", 10.0), "measurements.外径寸法"),
            (lambda r: r.__setitem__("part_id", "P-9999"), "part_id"),
            (lambda r: r.__setitem__("overall_verdict", "適合"), "overall_verdict"),
            (lambda r: r["violations"][0].__setitem__("measured", 10.11), "violations.外径寸法"),
        ],
        ids=["measurement", "part_id", "verdict", "violation"],
    )
    def test_any_disagreement_is_detected_and_withheld(self, mutate, expected):
        inner = _inner_result()
        mutate(inner["qms_record"])
        assert render_fidelity_violation(inner["inspection_report"], inner["qms_record"]) == expected

        result = PostProcessNode().execute(_state_from(inner))
        assert result["status"] == AgentStatus.ERROR
        assert result["formatted_output"] == MISMATCH_NOTICE
        assert result["result"] is None and result["qms_record"] is None
        assert result["refusal_reason"] == "render_mismatch"

    def test_an_altered_report_is_detected(self):
        inner = _inner_result()
        inner["inspection_report"] = inner["inspection_report"].replace("10.1 mm", "10.0 mm")
        result = PostProcessNode().execute(_state_from(inner))
        assert result["status"] == AgentStatus.ERROR
        assert result["formatted_output"] == MISMATCH_NOTICE
