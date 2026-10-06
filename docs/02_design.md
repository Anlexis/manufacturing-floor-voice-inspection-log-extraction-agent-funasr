# docs/02_design.md — MFG-C2-061 Design

## 1. Template Overview

| Field | Value |
|---|---|
| Template ID | MFG-C2-061 |
| Template Name | Manufacturing Floor Voice Inspection Log Extraction Agent (FunASR) |
| Category | Cat 2 — multi-step domain pipeline |
| Industry | MFG |
| Inheritance | **L1 Base** (`AgentBaseGraph`) — direct framework inheritance, no intermediate base class |
| Sprint | S2 |

## 2. Use Case

Manufacturing floor inspectors dictate quality-check results verbally during production line
inspections. This agent takes the recognised transcript of such a recording, diarizes speakers,
extracts structured measurement fields, normalises terminology to JIS B 9700, validates
measurements against product-line specification ranges, and produces an ISO 9001-style QMS record
plus a human-readable inspection log entry suitable for direct entry into a quality management
system.

## 3. Architecture — Cat 2 Nested Pattern

```
Outer backbone (AgentBaseGraph):
  START → InitializeNode → PreProcessNode → VoiceInspectionWorkflowGraphNode
        → PostProcessNode → FinalizeNode → END

Inner domain workflow (VoiceInspectionDomainWorkflow, BaseGraph):
  START → AudioInputNode → FunASRTranscribeNode → SpeakerDiarizeNode
        → FieldExtractAndJISTermNormalizeNode
        → SpecRangeValidateAndQMSRecordFormatNode → END
```

**Outer graph** (`src/graph/graph.py`): `ManufacturingFloorVoiceInspectionLogExtractionAgent(AgentBaseGraph)`
— the fixed 5-node backbone. Domain complexity is encapsulated inside
`VoiceInspectionWorkflowGraphNode` (the `main` slot), which builds the inner graph with the
validated runtime settings. `add_edges()` is not overridden.

**Inner graph** (`src/graph/domain_workflow_graph.py`): `VoiceInspectionDomainWorkflow(BaseGraph)`
— the 5-step transcript-to-QMS pipeline with a linear custom topology.

**Context bridge** (`src/graph/context_bridge.py`): the framework's `GraphNode.execute()` forwards
only `user_input` into the inner graph. The validated caller context therefore crosses the boundary
through a `ContextVar`: `extract_input()` stashes `state["caller_context"]` immediately before the
inner invoke and the inner graph's `_extra_initial_state()` seeds it into the inner state. Only the
validated form crosses — never the raw `input_context`.

### 3.1 Node Composition

| Slot | Class | Responsibility |
|---|---|---|
| `initialize` | `InitializeNode` (framework default) | Set schema_version, session_id, trust_level |
| `pre_process` | `PreProcessNode` | Trust gate (VERIFIED_EXTERNAL); owner of the caller-data contract (§5); writes `validated_input` + `caller_context` |
| `main` | `VoiceInspectionWorkflowGraphNode` | Bridge the caller context; delegate to the inner domain workflow; merge its result |
| `post_process` | `PostProcessNode` | Output boundary (§6): credential containment, render fidelity, one gated representation |
| `finalize` | `FinalizeNode` (framework default) | Assemble response_metadata, total_time_ms |

#### Inner domain nodes

| Key | Class | Input | Output |
|---|---|---|---|
| `audio_input` | `AudioInputNode` | `caller_context.audio` (validated) or `validated_input` | `audio_ref`, `audio_length`, `audio_format`, `audio_duration_s` |
| `funasr_transcribe` | `FunASRTranscribeNode` | `caller_context.transcript` / `audio_ref` | `transcript_raw`, `transcript_confidence`, `transcript_source` |
| `speaker_diarize` | `SpeakerDiarizeNode` | `transcript_raw` | `diarized_segments`, `speaker_count` |
| `field_extract_jis_norm` | `FieldExtractAndJISTermNormalizeNode` | `diarized_segments` (fallback `transcript_raw`) | `extracted_fields`, `normalized_fields` |
| `spec_validate_qms_format` | `SpecRangeValidateAndQMSRecordFormatNode` | `normalized_fields`, `caller_context` overrides, injected settings | `spec_violations`, `qms_record`, `inspection_report` |

Transcript source, in priority order: the caller's `input_context.transcript` (the real-work path —
everything downstream is computed from it); a configured FunASR endpoint, which **fails closed**
because this template ships no FunASR client; the offline baseline transcript, labelled
`transcript_source: "baseline"` so it can never be mistaken for a recognition result.

### 3.2 Runtime configuration

`config/agent.yaml` is the flat registration manifest (identity, `generation_mode`,
`required_trust_level`, `requires`). `config/config.yaml` carries the runtime parameters and is
passed as `Graph(config=...)` by the registry and by the standalone server alike:

| Key | Consumer | Effect |
|---|---|---|
| `max_retry` | framework backbone | retry routing budget |
| `timeout_s` | declared; no reader in this template | — |
| `voice_inspection.funasr_endpoint` | `FunASRTranscribeNode` | non-empty → transcription refused (no client shipped) |
| `voice_inspection.product_line`, `plant_id` | `SpecRangeValidateAndQMSRecordFormatNode` | rendered into the QMS record |
| `voice_inspection.spec_ranges` | `SpecRangeValidateAndQMSRecordFormatNode` | tolerance table merged over the built-in defaults |

The block is validated in `_validate_config()` (`domain_settings()` in `src/graph/graph.py`) and an
invalid value raises `ConfigError` at compile time — a silently dropped tolerance override would
judge parts against the wrong specification. The validated settings reach the inner nodes through
their constructors (`register_nodes()`); the node contract stays `execute(self, state) -> dict`.

## 4. State Schema (`src/schemas/state.py`)

`State` extends `AgentState` as a flat `TypedDict`. All fields are `Optional`. No Pydantic models;
no credentials in state. **The raw audio payload never enters state** — only its digest and length.

| Field | Type | Description |
|---|---|---|
| `validated_input` | `Optional[str]` | Normalised audio request envelope (digest, length, format, duration) |
| `caller_context` | `Optional[dict]` | Validated caller data: audio request + declared `input_context` fields |
| `refusal_reason` | `Optional[str]` | Closed-set label naming a refusal or a withheld output |
| `audio_ref` / `audio_length` | `Optional[str]` / `Optional[int]` | sha256 hex digest of the payload; its length in characters |
| `audio_format` | `Optional[str]` | `wav`, `mp3`, `pcm`, `flac`, `ogg` |
| `audio_duration_s` | `Optional[float]` | Declared duration (finite, 0–86400) |
| `transcript_raw` | `Optional[str]` | Transcript the pipeline works on |
| `transcript_confidence` | `Optional[float]` | [0.0, 1.0] for the baseline; `None` for a caller transcript |
| `transcript_source` | `Optional[str]` | `"caller"` or `"baseline"` |
| `diarized_segments` | `Optional[list]` | `{speaker_id, start_s, end_s, text}` dicts (≤ 500) |
| `speaker_count` | `Optional[int]` | Distinct speakers |
| `extracted_fields` | `Optional[dict]` | part_id, measurements, inspector_id, timestamp, remarks |
| `normalized_fields` | `Optional[dict]` | JIS B 9700-normalised names and units |
| `spec_violations` | `Optional[list]` | `{field, measured, spec_min, spec_max, status}` |
| `qms_record` | `Optional[dict]` | ISO 9001-style QMS record |
| `inspection_report` | `Optional[str]` | Human-readable inspection log entry |
| `result` / `formatted_output` | `Optional[str]` | Both hold the SAME gated text after the output boundary; cleared / replaced by a truthy notice when the output is withheld |

## 5. Caller-Data Contract (`src/services/caller_contract.py`)

Everything a caller can influence is validated in `PreProcessNode` before any domain node runs, and
only the validated form is carried forward. A refusal returns `status: error`, a closed-set
`refusal_reason`, and an `error_log` line naming the **field** — never the rejected value.

**`user_input` — the audio request envelope** (JSON object string; a plain non-JSON string is a
file reference in `wav` format):

| Field | Rule |
|---|---|
| `audio` | non-empty, no whitespace, ≤ 262,144 characters; replaced by its sha256 digest |
| `format` | closed set `wav` / `mp3` / `pcm` / `flac` / `ogg` (default `wav`) |
| `duration_s` | optional; finite number in [0, 86400] — `NaN`, `Infinity`, booleans, words and out-of-range values are refused |

The raw envelope is additionally screened for injection markers, chat-template control tokens
(raw and markup-stripped) and personal-data shapes.

**`input_context` — structured inspection data** (all optional):

| Field | Rule |
|---|---|
| `transcript` | free text, 1–20,000 characters; screened raw AND after markup stripping for chat-template control tokens (`<\|…\|>`, `[INST]`, `<<SYS>>`, `<system>`), directive phrases (English and Japanese), credential shapes (framework detector ∪ local patterns) and personal data (e-mail, phones, card / national-ID digit runs including the unspaced Japanese form, labelled Japanese names). A hit **refuses** the request; nothing is masked into a quality record |
| `product_line`, `plant_id`, `channel` | inert identifier `[A-Za-z0-9][A-Za-z0-9_-]{0,31}` (full-match anchored — a trailing newline is not an identifier) |
| `spec_ranges` | ≤ 32 entries keyed by a JIS metric from the closed set; `min`/`max` finite in ±1,000,000 with `min ≤ max`; `unit` from the closed unit set |

Undeclared keys are ignored by the graph and never echoed; the standalone adapter refuses them
outright (see §7).

**Rendered caller strings**: `part_id` and `inspector_id` are extracted over an inert alphabet
(≤ 64 chars) or rendered as `UNKNOWN`; remarks are single-line, ≤ 200 characters, capped at 50,
with the report's section markers (`■`, `【】`) and the verdict-line shape `総合判定:` neutralised so a
remark cannot forge a heading, a violation line or the verdict.

**Conformity verdict — fail closed**: a measurement that is not a finite number within the
admissible magnitude is a non-conformance (`測定値不正`), never a silent pass. `NaN` compares
`False` against every bound, so the naive `measured < min or measured > max` would report a `NaN`
reading as conforming.

## 6. Output Boundary (`src/nodes/post_process_node.py`)

Layers, in order:

1. **Credential containment** — the union of the framework's credential detector (the floor:
   everything the framework's own output gate would raise on) and the local assignment/vendor-prefix
   patterns (`password=…`, `api_key: …`, GitLab/GitHub/Slack tokens, PEM blocks, three-part JWTs),
   walked recursively over the report text, the QMS record and the violation list. Any hit
   **withholds** the output: `status: error`, every output-bearing field cleared (`result`,
   `inspection_report`, `qms_record`, `spec_violations`), and a truthy notice in
   `formatted_output`. Clearing is what contains the leak — `AgentBaseGraph.get_output()` falls
   back to `state["result"]` whenever `formatted_output` is falsy, on error status too. The union
   matters in the other direction as well: the framework's own gate raises on anything **its**
   detector finds, and the node wrapper then discards this node's clearing, so the local scan must
   catch at least everything the framework catches.
2. **Render fidelity** — the human-readable report must carry exactly the values in the structured
   record: every measurement and violation (in the one shared rendering, `format_measurement`), both
   identifiers, and the verdict. A disagreement withholds the output.
3. **Publish one representation** — `formatted_output` and `result` are set to the same gated text
   (with a `[VIOLATIONS: …]` prefix when there are violations), so no un-gated representation
   remains in state. The outer `get_output()` surfaces `qms_record`, `spec_violations` and
   `transcript_source` only on success, plus `refusal_reason` (a closed-set label, never caller data).

**Precision grid: not applicable.** This template renders dimensional measurements (mm, μm)
against tolerances; it renders no monetary aggregates, so there is no rounding grid to enforce.
The invariant enforced instead is render fidelity (layer 2): no representation the caller receives
can disagree with another, and domain identifiers (`P-2061`, `INS-042`, `Ra0.8`) render unchanged.

## 7. Standalone Entry Point (`src/api/server.py`)

- `INVOKE_AUTH_TOKEN`: when set, an otherwise-anonymous caller must present it as
  `Authorization: Bearer <token>` and is then run at `VERIFIED_EXTERNAL` — the level
  `PreProcessNode` requires. Without it every standalone invoke is refused by the trust gate.
- `input_context` is accepted (≤ 256 KiB serialised, else 413). Before the graph runs, the adapter
  refuses with **400** naming the field: an undeclared key (the framework's first node returns the
  whole `input_context` into its result and its output gate scans every value, so an undeclared
  field is an unscreened channel straight into that gate) and a credential-shaped value (that same
  gate would fail the request at the first node with an opaque traceback). The value screen uses the
  framework's own detector, so the refusal set equals the gate's block set exactly. Hostile field
  names are referred to by position.
- Secrets are provisioned under the manifest namespace (`mfg`); a test reads both files and fails
  on drift.

## 8. Security Gates (S-1 through S-5)

| Gate | Node | Implementation |
|---|---|---|
| S-1 Trust | `PreProcessNode` | `required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; the adapter establishes it from the bearer token |
| S-2 Input validation | `PreProcessNode` | The caller-data contract (§5): type guard, size cap, injection + control-token screen, personal-data screen, audio contract, `input_context` contract |
| S-3 Output scan | `PostProcessNode` + module-level `_security_gate_output()` in each node | Recursive union credential scan; the boundary withholds (§6), inner nodes withhold their delta |
| S-4 Audit logging | Every node's `execute()` | `emit_trace_event(event, payload, state)` at start, completion and every refusal — reasons are closed-set labels, never values |
| S-5 Credential handling | All nodes | No hardcoded credentials; the runtime settings are injected from `config/config.yaml` through node constructors; `requires.secrets: []`; no secrets in `State` |

All inner domain nodes: `required_trust_level = TrustLevel.ANONYMOUS` (the inner graph runs with
the outer caller's `InvocationContext`; the trust gate is enforced at `pre_process` on the outer
backbone).

## 9. Data Flow

```
POST /invoke {input: <audio envelope>, input_context: {transcript, spec_ranges, ...}}
  ↓ adapter: bearer → VERIFIED_EXTERNAL; size cap; undeclared/credential-shaped fields → 400
PreProcessNode: contract validation → validated_input (digest envelope), caller_context
  ↓
VoiceInspectionWorkflowGraphNode.extract_input(): stash caller_context (context bridge)
  inner graph.invoke(validated_input, ctx=ctx) — _extra_initial_state() seeds caller_context
    AudioInputNode: audio_ref, audio_length, audio_format, audio_duration_s
    FunASRTranscribeNode: caller transcript | endpoint → refused | baseline
    SpeakerDiarizeNode: diarized_segments, speaker_count
    FieldExtractAndJISTermNormalizeNode: extracted_fields, normalized_fields
    SpecRangeValidateAndQMSRecordFormatNode: spec_violations, qms_record, inspection_report
  merge_output() → result, inspection_report, qms_record, spec_violations, transcript_source
  ↓
PostProcessNode: containment → render fidelity → one gated representation
  ↓
FinalizeNode → get_output(): output, status, qms_record, spec_violations, transcript_source, refusal_reason
```

## 10. Dependencies

`pyproject.toml`: `dependencies = []` — the framework wheel is provided by the deployment.
Dev extras pin pytest, pytest-cov, pytest-asyncio, ruff, mypy and `types-PyYAML` exactly.

## 11. Test Strategy

- **Unit tests** (`tests/unit/`): every node's success and refusal paths (`test_agent.py`); the
  caller contract with a non-finite matrix per numeric field, identifier alphabet, free-text screens
  both ways (`test_caller_contract.py`); the output boundary — containment for framework-only,
  local-only and nested shapes, render fidelity (`test_output_boundary.py`); manifest ↔ entry-point
  namespace parity read from both files (`test_manifest_entrypoint_parity.py`); framework gate
  non-bypassability (`test_framework_compliance_tc06_tc07.py`).
- **Integration** (`tests/integration/`): a declared `config/config.yaml` value changes behaviour
  through the full nested invoke; invalid domain config stops compilation.
- **Proof-of-boundary** (`tests/proof_of_boundary/`): PB-6 backbone order and the anonymous-caller
  denial; import isolation; state safety; PB-7 (skip stub — no HITL propagation); the output gate
  end to end with a credential injected after the inner gates; the real ASGI `/invoke` — auth,
  adapter refusals, real extraction from a caller transcript, output that depends on input, every
  refusal reason, containment.

## 12. File Layout

```
src/
  schemas/state.py                        ← flat TypedDict State(AgentState)
  services/caller_contract.py             ← the caller-data contract and its validators / screens
  nodes/
    pre_process_node.py                   ← outer backbone, VERIFIED_EXTERNAL, contract owner
    post_process_node.py                  ← outer backbone, ANONYMOUS, output boundary
    audio_input_node.py                   ← inner domain, ANONYMOUS
    funasr_transcribe_node.py             ← inner domain, ANONYMOUS
    speaker_diarize_node.py               ← inner domain, ANONYMOUS
    field_extract_and_jis_term_normalize_node.py  ← inner domain, ANONYMOUS
    spec_range_validate_and_qms_record_format_node.py  ← inner domain, ANONYMOUS
  graph/
    graph.py                              ← outer AgentBaseGraph + main-slot GraphNode
    domain_workflow_graph.py              ← inner BaseGraph domain workflow
    context_bridge.py                     ← validated caller context hand-off (outer → inner)
  api/server.py                           ← FastAPI entry point (bearer auth, input_context screen)
config/agent.yaml                         ← flat registration manifest
config/config.yaml                        ← runtime parameters (max_retry, voice_inspection.*)
```
