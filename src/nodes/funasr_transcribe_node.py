"""AgentCore Platform v1.0"""

# FunASRTranscribeNode — produce the transcript the extraction pipeline works on.
#
# Three sources, in priority order:
#   1. caller transcript — input_context.transcript, already screened and
#      bounded by PreProcessNode and bridged into caller_context. This is the
#      real-work path: everything downstream is computed from it.
#   2. configured FunASR endpoint — the published template carries no FunASR
#      client, so a configured endpoint FAILS CLOSED with a clear error rather
#      than fabricating a transcript that looks like service output.
#   3. offline baseline — a fixed, deterministic Japanese inspection transcript
#      so the pipeline can be exercised end-to-end with no transcript supplied.
#      It is labelled transcript_source="baseline" so no consumer can mistake
#      it for a recognition result.

from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.caller_contract import find_credential

# Offline baseline transcript (used only when the caller supplies none).
_BASELINE_TRANSCRIPT = (
    "作業者A: 部品番号P-2061、外径測定値10.02ミリメートル。"
    " 作業者B: 内径9.98ミリメートル、表面粗さRa0.8。"
    " 作業者A: 異常なし、合格判定。担当者ID INS-042。"
)
_BASELINE_EMPTY = "音声データなし"
# Below this payload length the baseline reports "no audio content".
_MIN_AUDIO_LENGTH = 10


def _security_gate_output(result: dict[str, Any]) -> dict[str, Any]:
    """S-3 output scan (recursive): withhold the transcript if it is credential-shaped."""
    hit = find_credential({k: v for k, v in result.items() if k != "status"})
    if hit is None:
        return result
    label, path = hit
    return {
        "status": AgentStatus.ERROR,
        "error_log": [f"FunASRTranscribeNode: output withheld ({label} in {path or 'result'})"],
    }


class FunASRTranscribeNode(FunctionNode):
    """Select the transcript for the inspection pipeline.

    Settings (constructor-injected from config/config.yaml `voice_inspection`):
      funasr_endpoint: str — when set, transcription is refused (no client shipped).

    Inner domain node — required_trust_level = ANONYMOUS.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, settings: Optional[dict[str, Any]] = None) -> None:
        super().__init__()
        self._funasr_endpoint: str = str((settings or {}).get("funasr_endpoint") or "")

    def execute(self, state: AgentState) -> dict[str, Any]:
        audio_ref = state.get("audio_ref") or ""
        audio_length = state.get("audio_length") or 0
        audio_format = state.get("audio_format", "wav") or "wav"
        caller_context = state.get("caller_context") or {}
        caller_transcript = caller_context.get("transcript") if isinstance(caller_context, dict) else None

        emit_trace_event(
            "funasr_transcribe_node.start",
            {"audio_format": audio_format, "audio_length": audio_length, "caller_transcript": bool(caller_transcript)},
            state,
        )

        if not audio_ref:
            emit_trace_event("funasr_transcribe_node.error", {"reason": "no_audio_reference"}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": ["FunASRTranscribeNode: audio_ref is missing from state"],
            }

        raw_result: dict[str, Any]
        if isinstance(caller_transcript, str) and caller_transcript.strip():
            raw_result = {
                "transcript_raw": caller_transcript,
                "transcript_confidence": None,
                "transcript_source": "caller",
                "status": AgentStatus.SUCCESS,
            }
        elif self._funasr_endpoint:
            emit_trace_event("funasr_transcribe_node.error", {"reason": "endpoint_not_supported"}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": [
                    "FunASRTranscribeNode: a FunASR endpoint is configured but this template ships no "
                    "FunASR client; supply the recognised text as input_context.transcript"
                ],
            }
        else:
            has_content = int(audio_length) > _MIN_AUDIO_LENGTH
            raw_result = {
                "transcript_raw": _BASELINE_TRANSCRIPT if has_content else _BASELINE_EMPTY,
                "transcript_confidence": 0.92 if has_content else 0.0,
                "transcript_source": "baseline",
                "status": AgentStatus.SUCCESS,
            }

        result = _security_gate_output(raw_result)

        emit_trace_event(
            "funasr_transcribe_node.complete",
            {
                "source": result.get("transcript_source"),
                "confidence": result.get("transcript_confidence"),
                "transcript_length": len(result.get("transcript_raw", "") or ""),
            },
            state,
        )
        return result
