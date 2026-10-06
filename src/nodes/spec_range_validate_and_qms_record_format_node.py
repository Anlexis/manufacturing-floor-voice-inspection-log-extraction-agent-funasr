"""AgentCore Platform v1.0"""

# SpecRangeValidateAndQMSRecordFormatNode — compare every extracted measurement
# against its specification range and format the ISO 9001 QMS record.
#
# The conformity verdict is the decision this agent exists to make, so it is
# computed fail-CLOSED: a measurement that is not a finite number within the
# admissible magnitude is a NON-conformance ("測定値不正"), never a silent pass.
# NaN compares False against every bound; the naive form
# `measured < min or measured > max` would report a NaN reading as conforming.
#
# Specification ranges come from, in override order: the caller
# (input_context.spec_ranges, validated upstream), the runtime configuration
# (config/config.yaml voice_inspection.spec_ranges, validated at compile), the
# built-in defaults. product_line / plant_id resolve the same way.

import re
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.caller_contract import (
    MEASUREMENT_MAX,
    MEASUREMENT_MIN,
    find_credential,
    finite_in_range,
    is_inert_identifier,
)

# Default spec ranges for common manufacturing measurements.
# Format: {jis_metric_name: {"min": float, "max": float, "unit": str}}
DEFAULT_SPEC_RANGES: dict[str, dict[str, Any]] = {
    "外径寸法": {"min": 9.95, "max": 10.05, "unit": "mm"},
    "内径寸法": {"min": 9.90, "max": 10.00, "unit": "mm"},
    "算術平均粗さRa": {"min": 0.0, "max": 1.6, "unit": "μm"},
    "最大高さRz": {"min": 0.0, "max": 6.3, "unit": "μm"},
    "厚さ": {"min": 1.90, "max": 2.10, "unit": "mm"},
}
DEFAULT_PRODUCT_LINE = "UNKNOWN"
DEFAULT_PLANT_ID = "MFG-PLANT-01"

# Identifiers extracted from the transcript render only over this inert alphabet.
_EXTRACTED_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

VERDICT_PASS = "適合"
VERDICT_FAIL = "不適合"
STATUS_BELOW = "下限外"
STATUS_ABOVE = "上限外"
STATUS_INVALID = "測定値不正"


def _security_gate_output(result: dict[str, Any]) -> dict[str, Any]:
    """S-3 output scan (recursive over the record, the report and the violations)."""
    hit = find_credential({k: v for k, v in result.items() if k != "status"})
    if hit is None:
        return result
    label, path = hit
    return {
        "status": AgentStatus.ERROR,
        "error_log": [f"SpecRangeValidateAndQMSRecordFormatNode: output withheld ({label} in {path or 'result'})"],
    }


def _identifier_or(value: object, default: str) -> str:
    """Accept a value only over the inert identifier alphabet; otherwise the default."""
    return value if isinstance(value, str) and is_inert_identifier(value) else default


def format_measurement(value: Optional[float]) -> str:
    """The ONE rendering of a measurement value, shared by the record and the report."""
    return "?" if value is None else repr(float(value))


def render_measurement_line(metric: str, value: Optional[float], unit: str, spec: dict[str, Any], verdict: str) -> str:
    spec_str = (
        f"規格 [{format_measurement(spec['min'])}–{format_measurement(spec['max'])} {spec.get('unit', '')}]"
        if spec
        else ""
    )
    return f"  {metric}: {format_measurement(value)} {unit} {spec_str} → {verdict}"


class SpecRangeValidateAndQMSRecordFormatNode(FunctionNode):
    """Validate extracted measurements against product-line specification ranges
    and format an ISO 9001-compliant QMS inspection record.

    Settings (constructor-injected from config/config.yaml `voice_inspection`,
    validated at compile time):
      spec_ranges:  dict — overrides default spec ranges per JIS metric.
      product_line: str  — product line identifier for the QMS record.
      plant_id:     str  — plant / site identifier for the QMS record.
    The caller may override the same three through input_context (validated upstream).

    Outputs:
      spec_violations: list of {field, measured, spec_min, spec_max, status}
      qms_record:      ISO 9001 QMS dict record
      inspection_report: human-readable inspection log entry

    Inner domain node — required_trust_level = ANONYMOUS.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, settings: Optional[dict[str, Any]] = None) -> None:
        super().__init__()
        cfg = settings or {}
        self._spec_ranges: dict[str, dict[str, Any]] = {
            **DEFAULT_SPEC_RANGES,
            **{k: dict(v) for k, v in (cfg.get("spec_ranges") or {}).items()},
        }
        self._product_line: str = _identifier_or(cfg.get("product_line"), DEFAULT_PRODUCT_LINE)
        self._plant_id: str = _identifier_or(cfg.get("plant_id"), DEFAULT_PLANT_ID)

    def execute(self, state: AgentState) -> dict[str, Any]:
        normalized_fields = state.get("normalized_fields") or {}
        extracted_fields = state.get("extracted_fields") or {}
        caller_context = state.get("caller_context") or {}
        if not isinstance(caller_context, dict):
            caller_context = {}

        emit_trace_event(
            "spec_range_validate_and_qms_record_format_node.start",
            {"normalized_field_count": len(normalized_fields)},
            state,
        )

        if not normalized_fields and not extracted_fields:
            emit_trace_event(
                "spec_range_validate_and_qms_record_format_node.error",
                {"reason": "no_fields_to_validate"},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "error_log": [
                    "SpecRangeValidateAndQMSRecordFormatNode: neither normalized_fields nor extracted_fields are populated"
                ],
            }

        caller_ranges = caller_context.get("spec_ranges") or {}
        spec_ranges: dict[str, dict[str, Any]] = {**self._spec_ranges, **caller_ranges}
        product_line = _identifier_or(caller_context.get("product_line"), self._product_line)
        plant_id = _identifier_or(caller_context.get("plant_id"), self._plant_id)

        measurements: dict[str, dict[str, Any]] = {
            k: {
                "value": finite_in_range(v.get("value"), MEASUREMENT_MIN, MEASUREMENT_MAX),
                "unit": str(v.get("unit", "") or ""),
            }
            for k, v in normalized_fields.items()
            if isinstance(v, dict) and "value" in v
        }
        violations = self._validate_spec_ranges(measurements, spec_ranges)
        overall_verdict = VERDICT_FAIL if violations else VERDICT_PASS

        part_id = self._identifier(normalized_fields.get("part_id") or extracted_fields.get("part_id"))
        inspector_id = self._identifier(normalized_fields.get("inspector_id") or extracted_fields.get("inspector_id"))
        timestamp = str(
            normalized_fields.get("timestamp")
            or extracted_fields.get("timestamp")
            or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        )
        remarks = [str(r) for r in (normalized_fields.get("remarks") or []) if isinstance(r, str)]

        qms_record: dict[str, Any] = {
            "record_type": "QMS-INSPECTION-LOG",
            "standard": "ISO 9001:2015",
            "part_id": part_id,
            "product_line": product_line,
            "plant_id": plant_id,
            "inspector_id": inspector_id,
            "inspection_timestamp": timestamp,
            "overall_verdict": overall_verdict,
            "measurements": measurements,
            "violations": violations,
            "remarks": remarks,
        }

        lines = [
            f"【品質検査記録】{timestamp}",
            f"部品番号: {part_id} | 製品ライン: {product_line} | プラント: {plant_id}",
            f"担当者: {inspector_id}",
            "",
            "■ 測定結果:",
        ]
        violated = {v["field"] for v in violations}
        for metric, data in measurements.items():
            lines.append(
                render_measurement_line(
                    metric,
                    data["value"],
                    data["unit"],
                    spec_ranges.get(metric, {}),
                    "NG" if metric in violated else "OK",
                )
            )

        if violations:
            lines += ["", "■ 規格外:"]
            for v in violations:
                lines.append(
                    f"  {v['field']}: 実測 {format_measurement(v['measured'])} "
                    f"(規格 {format_measurement(v['spec_min'])}–{format_measurement(v['spec_max'])}) → {v['status']}"
                )

        if remarks:
            lines += ["", "■ 備考:"] + [f"  - {r}" for r in remarks]

        lines += ["", f"総合判定: {overall_verdict}"]
        inspection_report = "\n".join(lines)

        raw_result: dict[str, Any] = {
            "spec_violations": violations,
            "qms_record": qms_record,
            "inspection_report": inspection_report,
            "status": AgentStatus.SUCCESS,
        }
        result = _security_gate_output(raw_result)

        emit_trace_event(
            "spec_range_validate_and_qms_record_format_node.complete",
            {
                "violation_count": len(result.get("spec_violations") or []),
                "overall_verdict": overall_verdict,
                "report_length": len(result.get("inspection_report") or ""),
            },
            state,
        )
        return result

    @staticmethod
    def _identifier(value: object) -> str:
        """Render an extracted identifier only over the inert alphabet (1-64 chars)."""
        return value if isinstance(value, str) and _EXTRACTED_ID_RE.match(value) else "UNKNOWN"

    @staticmethod
    def _validate_spec_ranges(
        measurements: dict[str, dict[str, Any]], spec_ranges: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Compare measurement values against spec range bounds — fail CLOSED.

        A measurement with no finite value is reported as a violation with
        status 測定値不正; a measurement with no declared spec is not judged.
        """
        violations: list[dict[str, Any]] = []
        for metric, data in measurements.items():
            spec = spec_ranges.get(metric)
            if spec is None:
                continue
            measured = data.get("value")
            spec_min = finite_in_range(spec.get("min"), MEASUREMENT_MIN, MEASUREMENT_MAX)
            spec_max = finite_in_range(spec.get("max"), MEASUREMENT_MIN, MEASUREMENT_MAX)
            entry = {"field": metric, "measured": measured, "spec_min": spec_min, "spec_max": spec_max}
            if measured is None or spec_min is None or spec_max is None:
                violations.append({**entry, "status": STATUS_INVALID})
            elif measured < spec_min:
                violations.append({**entry, "status": STATUS_BELOW})
            elif measured > spec_max:
                violations.append({**entry, "status": STATUS_ABOVE})
        return violations
