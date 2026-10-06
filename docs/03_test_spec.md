# docs/03_test_spec.md — MFG-C2-061 Test Specification

## 1. Overview

**Template**: MFG-C2-061 Manufacturing Floor Voice Inspection Log Extraction Agent (FunASR)
**Test scope**: unit tests per node and per contract validator; integration via the full nested
`agent.invoke()`; proof-of-boundary tests including the real ASGI `/invoke` entry point
**Test framework**: pytest
**Suite size**: 372 passing tests + 2 deliberate skips (PB-7 stub; absent-duration case)

## 2. Test Environment

| Item | Value |
|---|---|
| Python | 3.11+ |
| Test runner | pytest |
| SDK | `agenticstar-agentcore` wheel (the version the deployment installs) |
| Stub strategy | `tests/conftest.py` silences `emit_trace_event` per node module via `monkeypatch.setattr`; NO `sys.modules` stubbing of `shared.*` |
| Assertion rule | Behaviour only — status, nothing carried forward, the field named — never a gate's wording |

## 3. Unit Test Cases

### TC-001: Empty / malformed request refused (PreProcessNode)

| Field | Value |
|---|---|
| File | `tests/unit/test_agent.py::TestPreProcessNode` (`test_error_empty_input`, `test_error_non_string_input_type_guard`, `test_error_over_long_input`) |
| Expected | `status == AgentStatus.ERROR`, `refusal_reason` set, no `validated_input` / `caller_context` |
| Gate | S-2 input validation |

### TC-002: Injection markers and chat-template control tokens refused

| Field | Value |
|---|---|
| File | `tests/unit/test_agent.py::TestPreProcessNode::test_error_injection_patterns_and_control_tokens` |
| Input | SQL / prompt-injection markers; `<\|im_start\|>`, `[INST]`, `<<SYS>>`, a markup-spliced `<system>` |
| Expected | `status == AgentStatus.ERROR`, `refusal_reason == "injection_pattern"` |
| Gate | S-2 injection screen (template-owned; raw and markup-stripped) |

### TC-003: Audio request contract — format closed set, non-finite duration matrix

| Field | Value |
|---|---|
| File | `tests/unit/test_agent.py::TestPreProcessNode::test_non_finite_duration_fails_closed`, `test_unsupported_format_is_refused_naming_the_field`; `tests/unit/test_caller_contract.py::TestAudioRequestContract` |
| Input | `duration_s` ∈ {"NaN", "Infinity", "-Infinity", nan, inf, True, -1, 1e9, "ten"}; `format` ∉ closed set |
| Expected | `refusal_reason == "invalid_audio_request"`, error names `user_input.duration_s` / `user_input.format`, value never echoed |
| Gate | Finite-number rule, fail closed |

### TC-004: The audio payload never enters state

| Field | Value |
|---|---|
| File | `tests/unit/test_agent.py::TestPreProcessNode::test_success_valid_input_normalises_envelope_without_the_payload`, `TestAudioInputNode::test_credential_shaped_audio_never_reaches_state` |
| Expected | `validated_input` carries `audio_ref` (sha256) + `audio_length`; the payload string appears nowhere in the delta |
| Gate | S-3 (payload is opaque and never stored) |

### TC-005: input_context contract — identifiers, transcript bounds, spec-range matrix

| Field | Value |
|---|---|
| File | `tests/unit/test_caller_contract.py::TestInputContextContract`; `tests/unit/test_agent.py::TestPreProcessNode::test_invalid_context_field_is_refused_naming_field_only` |
| Input | non-inert `channel`/`product_line`/`plant_id` (incl. a trailing newline); transcript over 20,000 chars; spec bounds ∈ non-finite matrix, `min > max`, unknown metric, unknown unit, > 32 entries |
| Expected | `refusal_reason == "invalid_input_context"`, the field named, value never echoed; unknown keys ignored |
| Gate | Caller-data contract |

### TC-006: Transcript screens — control tokens, directives, credential union, personal data

| Field | Value |
|---|---|
| File | `tests/unit/test_caller_contract.py::TestFreeTextScreen` |
| Input | control tokens raw / zero-width-spliced / markup-spliced; English and Japanese directive phrases; framework-only, local-only and nested credential shapes; e-mail, phones, card, national ID, unspaced Japanese `個人番号1234-5678-9012`, labelled Japanese name |
| Expected | the closed-set reason per class; ordinary inspection prose (incl. "ignore the previous reading", "Bearing Housing") passes |
| Gate | S-2 on the context channel (the framework never scans `input_context`) |

### TC-007: JIS B 9700 normalisation is context-guarded

| Field | Value |
|---|---|
| File | `tests/unit/test_agent.py::TestFieldExtractAndJISTermNormalizeNode::test_jis_text_normalization_is_context_guarded`, `test_spoken_connectors_between_metric_and_value_are_extracted` |
| Expected | `温度25度 → 温度25°`, `精度` unchanged, `粗さRa0.8 → 算術平均粗さRa0.8`, `不合格 → 不適合`, `外径寸法` not re-expanded; `外径測定値10.02` extracted |
| Gate | Extraction correctness |

### TC-008: Non-finite measurement is a non-conformance

| Field | Value |
|---|---|
| File | `tests/unit/test_agent.py::TestSpecRangeValidateAndQMSRecordFormatNode::test_non_finite_measurement_is_a_non_conformance` |
| Input | measurement value ∈ {nan, inf, -inf, "NaN", "Infinity", None, "ten", True, 1e12} |
| Expected | `overall_verdict == "不適合"`, violation status `測定値不正`, `measured is None` |
| Gate | Conformity verdict fails closed |

### TC-009: Credential in the output is withheld, not redacted

| Field | Value |
|---|---|
| File | `tests/unit/test_agent.py::TestPostProcessNode::test_credential_in_output_is_withheld_not_redacted`; `tests/unit/test_output_boundary.py::TestCredentialContainment` |
| Input | `password=…` (local-only), `AKIA…` (framework-only), JWT, Bearer — in the report, in a record remark, in a violation field |
| Expected | `status == AgentStatus.ERROR`, `formatted_output` is the truthy withheld notice, `result` / `inspection_report` / `qms_record` cleared, the secret nowhere in the delta |
| Gate | S-3 output containment |

### TC-010: Render fidelity between record and report

| Field | Value |
|---|---|
| File | `tests/unit/test_output_boundary.py::TestRenderFidelity` |
| Input | a measurement, an identifier, the verdict or a violation altered in the record; the report altered |
| Expected | the disagreeing field is detected; the output is withheld with the mismatch notice |
| Gate | Output invariant (precision grid not applicable — dimensional measurements) |

### TC-011: Manifest ↔ entry-point namespace parity

| Field | Value |
|---|---|
| File | `tests/unit/test_manifest_entrypoint_parity.py` |
| Expected | `secrets_factory(namespace=…)` in `src/api/server.py` (AST) equals `namespace` in `config/agent.yaml` (YAML), which equals the lower-cased industry; `agent_name` equals the manifest class |
| Gate | Deployment contract |

### TC-012: Declared configuration reaches the inner graph

| Field | Value |
|---|---|
| File | `tests/integration/test_config_reaches_inner_graph.py` |
| Expected | `voice_inspection.plant_id` renders in the report; `spec_ranges` change the verdict; a configured `funasr_endpoint` fails closed without a transcript and still serves a caller transcript; invalid domain config raises `ConfigError` at compile |
| Gate | Live runtime configuration |

### TC-013: Framework gate non-bypassability

| Field | Value |
|---|---|
| File | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| Expected | overriding `_security_gate_input` / `_security_gate_output` raises `TypeError` at class definition |
| Gate | S-2 / S-3 framework contract |

## 4. Proof-of-Boundary Tests

### PB-6: Backbone invoke order

| Field | Value |
|---|---|
| File | `tests/proof_of_boundary/test_pb_invoke_order.py::TestBackboneInvokeOrder::test_backbone_order_verified_external` |
| Caller | `InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)` |
| Payload | `{"audio": "<base64>", "format": "wav", "duration_s": 10.0}` — byte-identical to `deploy/invoke_payload.json` (asserted in `test_invoke_e2e.py::test_committed_stg_payload_is_the_pb6_payload`) |
| Expected | `status == AgentStatus.SUCCESS`, `node_history == [InitializeNode, PreProcessNode, VoiceInspectionWorkflowGraphNode, PostProcessNode, FinalizeNode]` |

### PB-S1: Anonymous caller rejected

| Field | Value |
|---|---|
| File | `tests/proof_of_boundary/test_pb_invoke_order.py::TestBackboneInvokeOrder::test_anonymous_caller_is_rejected` |
| Expected | `status == AgentStatus.ERROR` — the trust gate denies ANONYMOUS at `PreProcessNode` |

### PB-S3: Output gate end to end

| Field | Value |
|---|---|
| File | `tests/proof_of_boundary/test_pob_s3_output_gate.py` |
| Method | a credential is injected into the inner graph's result AFTER every inner node gate has run (local-only shape and framework shape), and a record/report disagreement is injected the same way |
| Expected | `output` is the withheld / mismatch notice, `qms_record is None`, the secret, no traceback and no source path anywhere in the envelope |

### PB-E2E: The real ASGI `/invoke`

| Field | Value |
|---|---|
| File | `tests/proof_of_boundary/test_invoke_e2e.py` |
| Auth | missing / wrong / non-ASCII bearer → 401; no `INVOKE_AUTH_TOKEN` configured → the trust gate refuses; oversized context → 413 |
| Adapter screen | undeclared field → 400 naming `input_context.<field>`; hostile field name → referred to by position; credential-shaped value → 400 naming the field, value never echoed; refusal set ⇔ the framework detector (property test) |
| Real work | baseline record without a transcript; a caller transcript drives part / inspector / measurements / verdict / violations; two transcripts give two different records; caller `spec_ranges` change the verdict |
| Refusals | empty input; the non-finite matrix on `spec_ranges` bounds and `duration_s`; non-inert identifiers; control tokens, directive phrases (EN/JP), personal data (incl. the unspaced Japanese digit form) on the transcript — each with its `refusal_reason` and nothing published; ordinary prose with the same words is not refused |
| Structure | caller text cannot forge a section heading or a second verdict line |
| Containment | injected credential → withheld notice, nothing leaked, no traceback / path in the envelope |

### PB-7: HITL interrupt propagation

| Field | Value |
|---|---|
| File | `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` |
| Status | skip stub — `propagate_hitl = False`, no interrupt checkpoint; the real assertion is written once propagation is wired |

### PB-4 / PB-2 / PB-5

`test_import_isolation.py` (no platform-SDK imports under `src/`) and `test_state_safety.py`
(`State` is a flat `TypedDict` with no credential-like fields or prohibited types).

## 5. Exclusions

- Real FunASR endpoint calls — no client ships; a configured endpoint fails closed (tested).
- Real audio decoding — nothing in the pipeline decodes audio; the payload is digested at ingest.
- Speaker diarization is a heuristic label split, not an acoustic model.

## 6. Assertion Conventions

| Do | Do Not |
|---|---|
| `result["status"] == AgentStatus.SUCCESS` | `result["status"] == "SUCCESS"` |
| `result["output"]` (outer invoke result) | `result["formatted_output"]` |
| `agent.invoke(payload, ctx=ctx, input_context=…)` | `agent.invoke(text=payload)` |
| `InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)` | `InvocationContext.for_internal()` |
| assert the refusal by status + `refusal_reason` + field named | assert a gate's message wording |
