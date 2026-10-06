"""AgentCore Platform v1.0"""

# PreProcessNode — outer backbone pre_process slot: the trust gate and the
# single owner of the caller-data contract.
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings
#  - Read input_context via state.get("input_context", {}) — read-only
#  - Never import from mediator/, api/, or other agents
#
# Everything a caller can influence is validated HERE, before any domain node
# runs, and only the validated form is carried forward:
#   user_input      → the audio request envelope (see src/services/caller_contract.py)
#   input_context   → transcript / spec_ranges / product_line / plant_id / channel
# A refusal returns status=ERROR, a closed-set refusal_reason, and an error_log
# line naming the FIELD — never the rejected value.
#
# The S-2 screens are module-level compiled regexes called inline from execute()
# (not _extra_security_gate_* hook methods).

import json
import re
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.caller_contract import (
    MAX_AUDIO_CHARS,
    contains_control_token,
    find_personal_data,
    parse_audio_request,
    validate_input_context,
)

# Upper bound on the raw request envelope: the audio payload cap plus the
# envelope's own JSON framing.
_MAX_USER_INPUT_CHARS = MAX_AUDIO_CHARS + 4_096

# ── S-2 backbone gate patterns ────────────────────────────────────────────────
# S-2 injection scan on the request ENVELOPE: SQL / command / script /
# prompt-injection markers. Deliberately narrow so it does NOT flag the
# legitimate audio-inspection payloads this agent receives — JSON
# {"audio","format","duration_s"} envelopes, base64 audio blobs (incl. '='
# padding), plain file references. Anchored on unambiguous attack tokens only.
_INJECTION_PATTERN = re.compile(
    r"(?i)(--|;|/\*|\*/|xp_|UNION\s+SELECT|DROP\s+TABLE|\|\||&&|`|\$\(|<script|javascript:"
    r"|ignore\s+previous|disregard\s+(above|instructions)|system\s+prompt|reveal\s+(your\s+)?prompt)"
)

_REFUSAL_LOG = "PreProcessNode: request refused ({reason}; field: {field})"


def _refuse(state: AgentState, reason: str, field: str) -> dict[str, Any]:
    """Build the fail-closed refusal delta: ERROR, closed-set reason, field name only."""
    emit_trace_event("pre_process_node.error", {"reason": reason, "field": field}, state)
    return {
        "status": AgentStatus.ERROR,
        "refusal_reason": reason,
        "error_log": [_REFUSAL_LOG.format(reason=reason, field=field)],
    }


class PreProcessNode(FunctionNode):
    """S-1/S-2 gate: validate the audio inspection request and the caller context.

    required_trust_level = VERIFIED_EXTERNAL — this is the outer backbone
    security boundary (Cat 2); only verified external callers may invoke this
    agent. Rejects non-string, empty, over-long, injection-bearing or
    PII-bearing envelopes, malformed audio requests and out-of-contract
    input_context fields before any domain logic runs.

    Output state keys (partial dict, success):
        validated_input:  str   normalised envelope (digest + length + format + duration)
        caller_context:   dict  validated audio request + declared context fields
        enriched_context: dict  source + channel (audit metadata)
        status:           AgentStatus.SUCCESS
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        # S-2 (1/4) type guard — a non-str user_input (list/dict/int) must be
        # rejected cleanly, FIRST, before any len()/.strip()/regex call.
        if not isinstance(user_input, str):
            emit_trace_event(
                "pre_process_node.error",
                {"reason": "non_string_user_input", "received_type": type(user_input).__name__},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "refusal_reason": "non_string_input",
                "error_log": ["PreProcessNode: user_input must be a string"],
            }

        emit_trace_event("pre_process_node.start", {"user_input_length": len(user_input)}, state)

        if not user_input or not user_input.strip():
            return _refuse(state, "empty_input", "user_input")

        if len(user_input) > _MAX_USER_INPUT_CHARS:
            return _refuse(state, "input_too_large", "user_input")

        sanitized = user_input.strip()

        # S-2 (2/4) injection scan — SQL / command / script / prompt-injection
        # markers and chat-template control tokens (raw and markup-stripped).
        if _INJECTION_PATTERN.search(sanitized) or contains_control_token(sanitized):
            return _refuse(state, "injection_pattern", "user_input")

        # S-2 (3/4) the audio request contract: format closed set, duration
        # finite and bounded, payload bounded and whitespace-free. The payload
        # itself is replaced by its digest — it never enters graph state.
        audio_request, reason, field = parse_audio_request(sanitized)
        if reason is not None:
            return _refuse(state, reason, f"user_input.{field}")

        # S-2 (4/4) personal-data screen on the raw envelope. (No credential
        # screen here on purpose: the only free-form value in the envelope is
        # the opaque audio payload, which is digested — never stored, rendered
        # or forwarded — and a credential-FORMAT pattern fires on random base64
        # often enough to refuse legitimate audio. Credential screening applies
        # where text can flow onward: the transcript channel and the output gate.)
        if find_personal_data(sanitized) is not None:
            return _refuse(state, "personal_data", "user_input")

        # Caller-data contract (input_context): every declared field bounded;
        # the transcript screened for control tokens, directives, credential
        # shapes and personal data. Undeclared keys are ignored, never echoed.
        context, reason, field = validate_input_context(input_context)
        if reason is not None:
            return _refuse(state, reason, field or "input_context")

        caller_context: dict[str, Any] = {"audio": audio_request, **context}
        validated_input = json.dumps(audio_request, ensure_ascii=False)
        channel: Optional[str] = context.get("channel")

        emit_trace_event(
            "pre_process_node.complete",
            {
                "validated": True,
                "channel": channel or "unknown",
                "transcript_supplied": "transcript" in context,
                "spec_ranges_supplied": "spec_ranges" in context,
            },
            state,
        )
        return {
            "validated_input": validated_input,
            "caller_context": caller_context,
            "enriched_context": {
                "source": "ManufacturingFloorVoiceInspectionLogExtractionAgent",
                "channel": channel or "unknown",
            },
            "status": AgentStatus.SUCCESS,
        }
