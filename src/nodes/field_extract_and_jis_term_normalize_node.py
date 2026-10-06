"""AgentCore Platform v1.0"""

# FieldExtractAndJISTermNormalizeNode — extract structured inspection fields
# from the diarized transcript and normalise terminology to JIS B 9700.
#
# Extraction is regex-based over a closed set of metric names, so the
# structure it produces is bounded by construction: at most one measurement
# per metric name, identifiers over an inert alphabet, remarks capped in
# number and length and stripped of report-structure markers.
#
# Terminology normalisation is applied longest-term-first with context guards,
# so a term that is already in its standard form is never re-expanded
# ("外径寸法" does not become "外径寸法寸法") and a single-character unit
# ("度" → "°") only fires after a digit, leaving 温度/角度/精度 untouched.

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.caller_contract import (
    MAX_REMARKS,
    MEASUREMENT_MAX,
    MEASUREMENT_MIN,
    find_credential,
    finite_in_range,
    sanitize_remark,
)

# JIS B 9700-based terminology normalization map (raw term -> standard term).
_JIS_B9700_TERM_MAP: dict[str, str] = {
    # Measurement terms
    "外径": "外径寸法",
    "内径": "内径寸法",
    "板厚": "厚さ",
    "表面粗さRa": "算術平均粗さRa",
    "表面粗さRz": "最大高さRz",
    "粗さRa": "算術平均粗さRa",
    "粗さRz": "最大高さRz",
    "粗さ": "表面粗さ",
    "Ra": "算術平均粗さRa",
    "Rz": "最大高さRz",
    "硬度": "硬さ",
    # Status terms
    "不合格": "不適合",
    "合格": "適合",
    "OK": "適合",
    "NG": "不適合",
    "異常なし": "適合",
    # Units (normalize to SI)
    "ミリメートル": "mm",
    "ミリ": "mm",
    "マイクロメートル": "μm",
    "マイクロ": "μm",
    "度": "°",
    "キロ": "kN",
}

# Terms that are units: they normalise only when they follow a digit.
_UNIT_TERMS = frozenset({"ミリメートル", "ミリ", "マイクロメートル", "マイクロ", "度", "キロ"})
# Latin terms must be standalone words (Ra inside "Radius" is not a metric).
_LATIN_TERMS = frozenset({"Ra", "Rz", "OK", "NG"})


def _term_pattern(raw: str, std: str) -> re.Pattern[str]:
    """Compile the guarded replacement pattern for one raw term.

    Guards: the raw term is not replaced when the surrounding text already
    spells its standard form (lookbehind on the standard prefix, lookahead on
    the standard suffix); units require a preceding digit; Latin terms must be
    standalone. "粗さ" additionally skips the "粗さRa"/"粗さRz" compounds, which
    are mapped by their own longer keys.
    """
    prefix = suffix = ""
    idx = std.find(raw)
    if idx >= 0:
        prefix, suffix = std[:idx], std[idx + len(raw) :]
    pattern = ""
    if prefix:
        pattern += f"(?<!{re.escape(prefix)})"
    if raw in _UNIT_TERMS:
        pattern += r"(?<=\d)"
    if raw in _LATIN_TERMS:
        pattern += r"(?<![A-Za-z])"
    pattern += re.escape(raw)
    if suffix:
        pattern += f"(?!{re.escape(suffix)})"
    if raw in _LATIN_TERMS:
        pattern += r"(?![A-Za-z])"
    if raw == "粗さ":
        pattern += r"(?!R[az])"
    return re.compile(pattern)


# Longest raw term first, so compounds ("粗さRa", "不合格", "ミリメートル") win
# over their sub-terms.
_TERM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_term_pattern(raw, std), std)
    for raw, std in sorted(_JIS_B9700_TERM_MAP.items(), key=lambda item: len(item[0]), reverse=True)
]

# Regex patterns to extract common inspection fields from transcript text.
_PART_ID_PATTERN = re.compile(r"部品番号\s*([A-Za-z0-9\-_]{1,64})")
_INSPECTOR_PATTERN = re.compile(r"担当者(?:ID)?\s*([A-Za-z0-9\-_]{1,64})")
_TIMESTAMP_PATTERN = re.compile(r"(\d{4}[-/]\d{2}[-/]\d{2}(?:\s+\d{2}:\d{2})?)")
# Measurement pattern: "外径10.02ミリメートル", "外径測定値10.02ミリ", "内径は9.98mm" or "Ra0.8".
# Spoken inspections put a short connector between the metric and the value
# (測定値 / 値 / は / が / ：), so one optional connector is allowed.
_MEASUREMENT_PATTERN = re.compile(
    r"(外径寸法|内径寸法|外径|内径|板厚|粗さ|Ra|Rz|硬度)\s*(?:測定値|値|は|が|[:：])?\s*"
    r"(\d{1,7}(?:\.\d{1,6})?)\s*(mm|μm|ミリメートル|マイクロメートル|ミリ|°)?",
    re.IGNORECASE,
)
_VERDICT_TOKENS = ("合格", "不合格", "適合", "不適合", "異常なし", "OK", "NG")
_SENTENCE_PATTERNS = [re.compile(r"[^。．\n]*" + re.escape(token) + r"[^。．\n]*") for token in _VERDICT_TOKENS]


def _security_gate_output(result: dict[str, Any]) -> dict[str, Any]:
    """S-3 output scan (recursive over both field dicts): withhold on any credential shape."""
    hit = find_credential({k: v for k, v in result.items() if k != "status"})
    if hit is None:
        return result
    label, path = hit
    return {
        "status": AgentStatus.ERROR,
        "error_log": [f"FieldExtractAndJISTermNormalizeNode: output withheld ({label} in {path or 'result'})"],
    }


def normalize_jis_text(text: str) -> str:
    """Apply the guarded JIS B 9700 terminology map to free text."""
    for pattern, std in _TERM_PATTERNS:
        text = pattern.sub(std, text)
    return text


class FieldExtractAndJISTermNormalizeNode(FunctionNode):
    """Extract structured inspection fields from speaker-diarized transcript segments
    and normalize measurement terminology to JIS B 9700 standard.

    Outputs:
      extracted_fields: {part_id, measurements, inspector_id, timestamp, remarks}
      normalized_fields: JIS B 9700-normalized keys and unit strings

    Inner domain node — required_trust_level = ANONYMOUS.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> dict[str, Any]:
        diarized_segments = state.get("diarized_segments") or []
        transcript_raw = state.get("transcript_raw") or ""

        emit_trace_event(
            "field_extract_and_jis_term_normalize_node.start",
            {"segment_count": len(diarized_segments)},
            state,
        )

        # Combine all segment text for regex extraction.
        full_text = " ".join(str(seg.get("text", "")) for seg in diarized_segments if isinstance(seg, dict))
        if not full_text.strip():
            full_text = str(transcript_raw)

        if not full_text.strip():
            emit_trace_event(
                "field_extract_and_jis_term_normalize_node.error",
                {"reason": "no_text_to_extract"},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "error_log": ["FieldExtractAndJISTermNormalizeNode: no text available for field extraction"],
            }

        extracted = self._extract_fields(full_text)
        normalized = self._normalize_jis(extracted)

        raw_result: dict[str, Any] = {
            "extracted_fields": extracted,
            "normalized_fields": normalized,
            "status": AgentStatus.SUCCESS,
        }
        result = _security_gate_output(raw_result)

        emit_trace_event(
            "field_extract_and_jis_term_normalize_node.complete",
            {
                "extracted_field_count": len(result.get("extracted_fields") or {}),
                "measurement_count": len((result.get("extracted_fields") or {}).get("measurements") or {}),
            },
            state,
        )
        return result

    @staticmethod
    def _extract_fields(text: str) -> dict[str, Any]:
        """Extract structured inspection fields from combined transcript text."""
        fields: dict[str, Any] = {
            "part_id": None,
            "measurements": {},
            "inspector_id": None,
            "timestamp": None,
            "remarks": [],
        }

        m = _PART_ID_PATTERN.search(text)
        if m:
            fields["part_id"] = m.group(1)

        m = _INSPECTOR_PATTERN.search(text)
        if m:
            fields["inspector_id"] = m.group(1)

        m = _TIMESTAMP_PATTERN.search(text)
        if m:
            fields["timestamp"] = m.group(1)

        # Measurements — first occurrence per metric; values finite and bounded.
        for m in _MEASUREMENT_PATTERN.finditer(text):
            metric = m.group(1)
            if metric in fields["measurements"]:
                continue
            value = finite_in_range(m.group(2), MEASUREMENT_MIN, MEASUREMENT_MAX)
            if value is None:
                continue
            fields["measurements"][metric] = {"value": value, "unit": m.group(3) or ""}

        # Remarks: sentences containing a conformance verdict (capped, one line each).
        remarks: list[str] = []
        for pattern in _SENTENCE_PATTERNS:
            for sentence in pattern.findall(text):
                remark = sanitize_remark(sentence)
                if remark and remark not in remarks:
                    remarks.append(remark)
                if len(remarks) >= MAX_REMARKS:
                    break
            if len(remarks) >= MAX_REMARKS:
                break
        fields["remarks"] = remarks

        return fields

    @staticmethod
    def _normalize_jis(extracted: dict[str, Any]) -> dict[str, Any]:
        """Apply JIS B 9700 terminology normalization to extracted field names and values."""
        normalized: dict[str, Any] = {}

        measurements = extracted.get("measurements") or {}
        for raw_metric, data in measurements.items():
            std_metric = _JIS_B9700_TERM_MAP.get(raw_metric, raw_metric)
            raw_unit = data.get("unit", "") or ""
            std_unit = _JIS_B9700_TERM_MAP.get(raw_unit, raw_unit)
            if std_metric not in normalized:
                normalized[std_metric] = {"value": data.get("value"), "unit": std_unit}

        normalized["remarks"] = [normalize_jis_text(remark) for remark in (extracted.get("remarks") or [])]

        for key in ("part_id", "inspector_id", "timestamp"):
            if extracted.get(key):
                normalized[key] = extracted[key]

        return normalized
