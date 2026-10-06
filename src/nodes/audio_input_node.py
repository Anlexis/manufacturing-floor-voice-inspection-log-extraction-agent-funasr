"""AgentCore Platform v1.0"""

# AudioInputNode — first inner domain node: materialise the validated audio
# request into the inner state.
#
# In the full nested graph the request arrives already validated in
# caller_context["audio"] (PreProcessNode + the context bridge). When the node
# runs standalone (unit tests, a direct inner-graph invoke) it applies the SAME
# contract to validated_input, so both entry paths get one fail-closed rule.
# The audio payload itself is never present: only its digest and length.

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.caller_contract import find_credential, parse_audio_request


def _security_gate_output(result: dict[str, Any]) -> dict[str, Any]:
    """S-3 output scan (recursive): withhold the delta if any value is credential-shaped.

    The only caller-derived value here is a sha256 hex digest, which cannot
    carry a credential shape; the scan is defence in depth over every nested
    value so a future field cannot slip past it.
    """
    hit = find_credential({k: v for k, v in result.items() if k != "status"})
    if hit is None:
        return result
    label, path = hit
    return {
        "status": AgentStatus.ERROR,
        "error_log": [f"AudioInputNode: output withheld ({label} in {path or 'result'})"],
    }


class AudioInputNode(FunctionNode):
    """Materialise the validated audio request for downstream transcription.

    Reads caller_context["audio"] (validated upstream) or, absent that, applies
    the audio contract to validated_input / user_input. Writes audio_ref,
    audio_length, audio_format, audio_duration_s — never the payload.

    Inner domain node — required_trust_level = ANONYMOUS (outer pre_process gates S-1).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        caller_context = state.get("caller_context") or {}
        audio_request = caller_context.get("audio") if isinstance(caller_context, dict) else None

        emit_trace_event(
            "audio_input_node.start",
            {"validated_upstream": isinstance(audio_request, dict)},
            state,
        )

        if not isinstance(audio_request, dict):
            raw = state.get("validated_input") or state.get("user_input", "") or ""
            if not isinstance(raw, str) or not raw.strip():
                emit_trace_event("audio_input_node.error", {"reason": "empty_input"}, state)
                return {
                    "status": AgentStatus.ERROR,
                    "error_log": ["AudioInputNode: no audio payload provided"],
                }
            audio_request, reason, field = parse_audio_request(raw)
            if reason is not None:
                emit_trace_event("audio_input_node.error", {"reason": reason, "field": field}, state)
                return {
                    "status": AgentStatus.ERROR,
                    "error_log": [f"AudioInputNode: {reason} (field: {field})"],
                }

        raw_result: dict[str, Any] = {
            "audio_ref": audio_request["audio_ref"],
            "audio_length": int(audio_request["audio_length"]),
            "audio_format": audio_request["format"],
            "audio_duration_s": audio_request.get("duration_s"),
            "status": AgentStatus.SUCCESS,
        }
        result = _security_gate_output(raw_result)

        emit_trace_event(
            "audio_input_node.complete",
            {
                "audio_format": result.get("audio_format"),
                "audio_length": result.get("audio_length"),
                "duration_s": result.get("audio_duration_s"),
            },
            state,
        )
        return result
