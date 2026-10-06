"""AgentCore Platform v1.0"""

# SpeakerDiarizeNode — split the transcript into per-speaker segments.
#
# Heuristic diarization on speaker labels embedded in the transcript
# ("作業者A:", "Inspector:", "Worker:", "検査員B:"). The transcript is bounded
# upstream (MAX_TRANSCRIPT_CHARS), so the segment list is bounded too; an
# explicit cap keeps the structure small even for a pathological label density.

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.caller_contract import find_credential

# Pattern to detect speaker-tagged lines: "作業者A:", "作業者B:", "Inspector:", etc.
_SPEAKER_PATTERN = re.compile(r"((?:作業者|Inspector|Worker|Speaker|検査員)[A-Za-z0-9_\-]{1,16})\s*[:：]")

# Structural cap on the number of speaker turns carried forward.
_MAX_SEGMENTS = 500
_WORDS_PER_SECOND = 3.0  # approximate spoken Japanese pace


def _security_gate_output(result: dict[str, Any]) -> dict[str, Any]:
    """S-3 output scan (recursive): withhold the segments if any text is credential-shaped."""
    hit = find_credential({k: v for k, v in result.items() if k != "status"})
    if hit is None:
        return result
    label, path = hit
    return {
        "status": AgentStatus.ERROR,
        "error_log": [f"SpeakerDiarizeNode: output withheld ({label} in {path or 'result'})"],
    }


class SpeakerDiarizeNode(FunctionNode):
    """Split a transcript into per-speaker segments for structured field extraction.

    Heuristic split on "作業者X:" / "Inspector:" / "Worker:" speaker labels
    embedded in the transcript. A transcript without labels is one segment
    attributed to SPEAKER_0.

    Inner domain node — required_trust_level = ANONYMOUS.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        transcript_raw = state.get("transcript_raw") or ""

        emit_trace_event("speaker_diarize_node.start", {"transcript_length": len(transcript_raw)}, state)

        if not isinstance(transcript_raw, str) or not transcript_raw.strip():
            emit_trace_event("speaker_diarize_node.error", {"reason": "no_transcript"}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": ["SpeakerDiarizeNode: transcript_raw is empty"],
            }

        segments = self._heuristic_diarize(transcript_raw)
        speaker_ids = sorted({str(s["speaker_id"]) for s in segments})

        raw_result: dict[str, Any] = {
            "diarized_segments": segments,
            "speaker_count": len(speaker_ids),
            "status": AgentStatus.SUCCESS,
        }
        result = _security_gate_output(raw_result)

        emit_trace_event(
            "speaker_diarize_node.complete",
            {
                "segment_count": len(result.get("diarized_segments") or []),
                "speaker_count": result.get("speaker_count"),
            },
            state,
        )
        return result

    @staticmethod
    def _segment(speaker_id: str, text: str, start: float, min_duration: float) -> tuple[dict[str, Any], float]:
        word_count = max(1, len(text.split()))
        duration = max(min_duration, word_count / _WORDS_PER_SECOND)
        return (
            {
                "speaker_id": speaker_id,
                "start_s": round(start, 2),
                "end_s": round(start + duration, 2),
                "text": text.strip(),
            },
            duration,
        )

    @classmethod
    def _heuristic_diarize(cls, transcript: str) -> list[dict[str, Any]]:
        """Split transcript on speaker labels and assign synthetic timestamps."""
        parts = _SPEAKER_PATTERN.split(transcript)
        # parts alternates: [pre, label1, text1, label2, text2, ...]
        # len(parts) == 1 means no speaker labels were found.
        if len(parts) == 1:
            segment, _ = cls._segment("SPEAKER_0", transcript, 0.0, 1.0)
            return [segment]

        segments: list[dict[str, Any]] = []
        time_cursor = 0.0

        # Text before the first speaker label is attributed to an UNKNOWN narrator.
        if parts[0].strip():
            segment, duration = cls._segment("UNKNOWN", parts[0], time_cursor, 1.0)
            segments.append(segment)
            time_cursor += duration

        idx = 1
        while idx + 1 < len(parts) and len(segments) < _MAX_SEGMENTS:
            segment, duration = cls._segment(parts[idx].strip(), parts[idx + 1], time_cursor, 0.5)
            segments.append(segment)
            time_cursor += duration
            idx += 2

        if not segments:
            segment, _ = cls._segment("SPEAKER_0", transcript, 0.0, 1.0)
            segments = [segment]
        return segments
