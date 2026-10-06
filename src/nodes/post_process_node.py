"""AgentCore Platform v1.0"""

# PostProcessNode — outer backbone post_process slot: the external output boundary.
#
# Layers, in this order:
#   (1) credential scan — the UNION of the framework's credential detector and
#       the local assignment/vendor-prefix patterns, walked recursively over the
#       report text, the QMS record and the violation list. Any hit WITHHOLDS the
#       output: status=ERROR, every output-bearing state field cleared, and a
#       truthy notice in formatted_output. Clearing is what contains the leak —
#       AgentBaseGraph.get_output() falls back to state["result"] whenever
#       formatted_output is falsy, on error status too, so a gate that merely
#       raised or redacted would still ship the un-gated inner report.
#       The union matters in the other direction as well: the framework's own
#       @final gate raises on anything ITS detector finds, and the node wrapper
#       then returns a bare error partial that discards this node's clearing —
#       so this scan must catch at least everything the framework catches.
#   (2) render-fidelity invariant — the human-readable report must carry exactly
#       the values in the structured record (every measurement, every violation,
#       both identifiers, the verdict). This template renders dimensional
#       measurements, not monetary aggregates, so there is no rounding grid; the
#       invariant enforced instead is that no representation can disagree with
#       another. A mismatch withholds the output (fail closed).
#   (3) violation summary prefix + publish: formatted_output AND result are set
#       to the SAME gated text, so no un-gated representation remains in state.
#
# The gate is the module-level function `_security_gate_output`, called from
# inside execute() (the agent class exposes the same function).

from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.spec_range_validate_and_qms_record_format_node import format_measurement
from src.services.caller_contract import find_credential

WITHHELD_NOTICE = "[品質検査記録は出力されませんでした: 出力に認証情報の形をした文字列が含まれていたため保留しました]"
EMPTY_NOTICE = "[品質検査記録なし: 検査結果が生成されませんでした]"
MISMATCH_NOTICE = "[品質検査記録は出力されませんでした: 記録と報告書の内容が一致しないため保留しました]"

# Every output-bearing state field, cleared on a withheld output.
_CLEARED: dict[str, Any] = {"result": None, "inspection_report": None, "qms_record": None, "spec_violations": []}
_NOT_SCANNED = frozenset({"status", "refusal_reason", "error_log"})


def _withhold(notice: str, reason: str, detail: str) -> dict[str, Any]:
    """The fail-closed delta: ERROR, cleared fields, truthy notice, closed-set reason."""
    return {
        **_CLEARED,
        "formatted_output": notice,
        "refusal_reason": reason,
        "status": AgentStatus.ERROR,
        "error_log": [f"PostProcessNode: output withheld ({detail})"],
    }


def _security_gate_output(result: dict[str, Any]) -> dict[str, Any]:
    """S-3 output gate (recursive): withhold the whole output on any credential shape.

    Walks every value of the outgoing delta — the report text, the QMS record
    (nested), the violation list — with the union detector. Returns the delta
    unchanged when clean, otherwise the cleared/withheld delta. The returned
    delta itself never carries the matched text.
    """
    hit = find_credential({k: v for k, v in result.items() if k not in _NOT_SCANNED})
    if hit is None:
        return result
    label, path = hit
    return _withhold(WITHHELD_NOTICE, "output_withheld", f"{label} in {path or 'output'}")


def render_fidelity_violation(report: str, qms_record: dict[str, Any]) -> Optional[str]:
    """Return the first field on which the report disagrees with the record, or None.

    Values are compared in their ONE rendering (format_measurement), so a
    measurement, a violation, an identifier or the verdict that is missing or
    altered in the human-readable text is detected.
    """
    for key in ("part_id", "inspector_id"):
        value = qms_record.get(key)
        if isinstance(value, str) and value not in report:
            return key
    verdict = qms_record.get("overall_verdict")
    if isinstance(verdict, str) and f"総合判定: {verdict}" not in report:
        return "overall_verdict"
    for metric, data in (qms_record.get("measurements") or {}).items():
        if isinstance(data, dict) and f"  {metric}: {format_measurement(data.get('value'))} " not in report:
            return f"measurements.{metric}"
    for violation in qms_record.get("violations") or []:
        if isinstance(violation, dict):
            expected = f"{violation.get('field')}: 実測 {format_measurement(violation.get('measured'))} "
            if expected not in report:
                return f"violations.{violation.get('field')}"
    return None


class PostProcessNode(FunctionNode):
    """Apply the output boundary and publish the inspection report.

    Outer backbone post_process slot. Declared ANONYMOUS — trust was already
    enforced at PreProcessNode (VERIFIED_EXTERNAL).

    Input state keys:  inspection_report, result, qms_record, spec_violations
    Output state keys: formatted_output, result (both the same gated text),
                       status; on a withheld output every output-bearing field
                       is cleared and refusal_reason names why.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        inspection_report = state.get("inspection_report") or ""
        result_text = state.get("result") or ""
        qms_record = state.get("qms_record") or {}
        spec_violations = list(state.get("spec_violations") or [])

        emit_trace_event(
            "post_process_node.start",
            {"has_report": bool(inspection_report), "violation_count": len(spec_violations)},
            state,
        )

        output_text = str(inspection_report or result_text)
        if not output_text.strip():
            emit_trace_event("post_process_node.withheld", {"reason": "empty_output"}, state)
            return _withhold(EMPTY_NOTICE, "empty_output", "no inspection report was produced")

        # Layer 1 — credential scan over everything that could be published,
        # BEFORE any other judgement, so a leak is always labelled as a leak.
        incoming = _security_gate_output(
            {"inspection_report": output_text, "qms_record": qms_record, "spec_violations": spec_violations}
        )
        if incoming.get("status") == AgentStatus.ERROR:
            emit_trace_event("post_process_node.withheld", {"reason": incoming.get("refusal_reason")}, state)
            return incoming

        # Layer 2 — render fidelity.
        if isinstance(qms_record, dict) and qms_record:
            mismatch = render_fidelity_violation(output_text, qms_record)
            if mismatch is not None:
                emit_trace_event("post_process_node.withheld", {"reason": "render_mismatch", "field": mismatch}, state)
                return _withhold(MISMATCH_NOTICE, "render_mismatch", f"report disagrees with the record on {mismatch}")

        if spec_violations:
            violation_summary = "; ".join(
                f"{v.get('field', '?')}={format_measurement(v.get('measured'))} "
                f"(range {format_measurement(v.get('spec_min'))}–{format_measurement(v.get('spec_max'))})"
                for v in spec_violations[:5]
                if isinstance(v, dict)
            )
            output_text = f"[VIOLATIONS: {violation_summary}]\n{output_text}"

        # Layer 3 — publish ONE gated representation, re-scanned as it ships.
        raw_result: dict[str, Any] = {
            "formatted_output": output_text,
            "result": output_text,
            "inspection_report": inspection_report or output_text,
            "qms_record": qms_record,
            "spec_violations": spec_violations,
            "status": AgentStatus.SUCCESS,
        }
        final_result = _security_gate_output(raw_result)

        if final_result.get("status") == AgentStatus.ERROR:
            emit_trace_event("post_process_node.withheld", {"reason": final_result.get("refusal_reason")}, state)
        else:
            emit_trace_event(
                "post_process_node.complete",
                {"output_length": len(final_result.get("formatted_output") or "")},
                state,
            )
        return final_result
