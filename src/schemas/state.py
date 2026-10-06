"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.

from typing import Any, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """MFG-C2-061 state: audio voice inspection log extraction pipeline.

    Field write chain:
      PreProcessNode                         -> validated_input, enriched_context, caller_context,
                                                refusal_reason (on refusal)
      AudioInputNode                         -> audio_ref, audio_length, audio_format, audio_duration_s
      FunASRTranscribeNode                   -> transcript_raw, transcript_confidence, transcript_source
      SpeakerDiarizeNode                     -> diarized_segments, speaker_count
      FieldExtractAndJISTermNormalizeNode    -> extracted_fields, normalized_fields
      SpecRangeValidateAndQMSRecordFormatNode-> spec_violations, qms_record, inspection_report
      VoiceInspectionWorkflowGraphNode (merge_output) -> result (+ the inner fields above)
      PostProcessNode                        -> formatted_output, result (gated), refusal_reason
                                                (on a withheld output)

    The raw audio payload is never stored: only its digest (audio_ref) and
    length. All shared fields (user_input, input_context, status, session_id,
    node_history, error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # ── Backbone (PreProcessNode) ─────────────────────────────────────────────
    # Normalised audio request envelope (JSON string, payload replaced by its digest).
    validated_input: Optional[str]
    enriched_context: Optional[dict[str, Any]]
    # Validated caller data: audio request + declared input_context fields
    # (transcript, spec_ranges, product_line, plant_id, channel). Bridged into
    # the inner graph by src/graph/context_bridge.py.
    caller_context: Optional[dict[str, Any]]
    # Closed-set label naming why a request was refused or an output withheld.
    refusal_reason: Optional[str]

    # ── Audio input (AudioInputNode) ──────────────────────────────────────────
    # sha256 hex digest of the audio payload / file reference (the payload itself is never stored).
    audio_ref: Optional[str]
    # Length of the audio payload string in characters.
    audio_length: Optional[int]
    # Audio format: 'wav', 'mp3', 'pcm', 'flac', 'ogg'.
    audio_format: Optional[str]
    # Declared audio duration in seconds (finite, bounded).
    audio_duration_s: Optional[float]

    # ── Transcription (FunASRTranscribeNode) ──────────────────────────────────
    # Speech-to-text output: the caller-supplied transcript, or the offline baseline.
    transcript_raw: Optional[str]
    # Transcription confidence [0.0, 1.0]; None when the transcript was caller-supplied.
    transcript_confidence: Optional[float]
    # "caller" (input_context.transcript) or "baseline" (offline stub).
    transcript_source: Optional[str]

    # ── Speaker diarization (SpeakerDiarizeNode) ──────────────────────────────
    # List of dicts: {speaker_id: str, start_s: float, end_s: float, text: str}.
    diarized_segments: Optional[list[Any]]
    # Number of distinct speakers detected.
    speaker_count: Optional[int]

    # ── Field extraction + JIS normalization (FieldExtractAndJISTermNormalizeNode)
    # Structured inspection fields: part_id, measurements, inspector_id, timestamp, remarks.
    extracted_fields: Optional[dict[str, Any]]
    # JIS B 9700-normalised field names and unit representations.
    normalized_fields: Optional[dict[str, Any]]

    # ── Spec validation + QMS formatting (SpecRangeValidateAndQMSRecordFormatNode)
    # Spec range violations: [{field, measured, spec_min, spec_max, status}].
    spec_violations: Optional[list[Any]]
    # ISO 9001-compliant QMS record ready for system entry.
    qms_record: Optional[dict[str, Any]]
    # Human-readable inspection log entry (primary output of the pipeline).
    inspection_report: Optional[str]

    # ── Backbone output ───────────────────────────────────────────────────────
    # Written by merge_output() (raw inner report), then REPLACED by PostProcessNode
    # with the gated text — or cleared when the output is withheld.
    result: Optional[str]
    # Set by PostProcessNode after the output boundary; always truthy.
    formatted_output: Optional[str]
