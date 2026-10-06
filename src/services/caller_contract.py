"""AgentCore Platform v1.0"""

# src/services/caller_contract.py — the caller-data contract for MFG-C2-061.
#
# This module is the SINGLE definition of what the agent accepts from a caller
# and the fail-closed validators behind it. Two caller channels exist:
#
#   user_input      the audio request envelope, a JSON object string:
#                   {"audio": <str>, "format": <wav|mp3|pcm|flac|ogg>, "duration_s": <number>}
#                   (a plain non-JSON string is a file reference in wav format)
#
#   input_context   optional structured inspection data:
#                   transcript     free text — the speech-to-text output to extract from
#                   product_line   inert identifier rendered into the QMS record
#                   plant_id       inert identifier rendered into the QMS record
#                   spec_ranges    {jis_metric: {"min", "max", "unit"}} tolerance overrides
#                   channel        inert identifier, audit metadata only
#
# Rules applied uniformly, in code rather than by convention:
#   * every number is parsed finite AND bounded — NaN/Infinity parse fine but
#     compare False, which on the conformity verdict this agent exists to make
#     is a silent fail-OPEN (a NaN measurement would read as "within spec");
#   * every caller string that is rendered into the report is an inert
#     identifier (letters, digits, "_" and "-"; 1-32 chars);
#   * the transcript is free text and is screened RAW and AFTER markup
#     stripping for chat-template control tokens, directive phrases,
#     credential shapes and personal data — a hit refuses the request; nothing
#     is masked into a quality record, because a masked value is not an
#     extracted value;
#   * the raw audio payload never enters graph state: only its digest and
#     length are carried (nothing in the pipeline decodes audio);
#   * a refusal names the FIELD, never the value.

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Optional

from framework.security.credential_detector import detect_credentials
from framework.security.pii_detector import detect_pii

# ── Bounds ────────────────────────────────────────────────────────────────────

SUPPORTED_AUDIO_FORMATS: frozenset[str] = frozenset({"wav", "mp3", "pcm", "flac", "ogg"})
MAX_AUDIO_CHARS = 262_144
MAX_DURATION_S = 86_400.0
MAX_TRANSCRIPT_CHARS = 20_000
MAX_SPEC_RANGE_ENTRIES = 32
SPEC_BOUND_MIN = -1_000_000.0
SPEC_BOUND_MAX = 1_000_000.0
MEASUREMENT_MIN = -1_000_000.0
MEASUREMENT_MAX = 1_000_000.0
IDENTIFIER_MAX = 32

# JIS B 9700 metric names a specification range may be declared for.
SPEC_METRICS: frozenset[str] = frozenset(
    {"外径寸法", "内径寸法", "算術平均粗さRa", "最大高さRz", "厚さ", "硬さ", "表面粗さ"}
)
SPEC_UNITS: frozenset[str] = frozenset({"mm", "μm", "°", "kN", "HRC", "HV"})

# input_context keys this agent declares. Unknown keys are ignored by the
# graph (never echoed); the standalone /invoke adapter refuses them outright.
INPUT_CONTEXT_FIELDS: frozenset[str] = frozenset({"channel", "transcript", "product_line", "plant_id", "spec_ranges"})

# ── Alphabets ─────────────────────────────────────────────────────────────────

# Full-match anchors are \Z, never $: `$` also matches before a trailing
# newline, so "PLANT-7\n" would pass as an identifier and render a line break.
_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
_NO_WHITESPACE_RE = re.compile(r"\A\S+\Z")
_HEX_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_NUMERIC_STR_RE = re.compile(r"\A[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?\Z")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\ufeff\u00ad]")
_MARKUP_RE = re.compile(r"<[^<>]{0,200}>")

# ── Screens ───────────────────────────────────────────────────────────────────

# Chat-template control tokens — screened as a CLASS, not as phrases. The
# framework's own detector covers <|im_start|> and [INST] but not <<SYS>>, and
# it never scans input_context at all.
_CONTROL_TOKEN_RE = re.compile(
    r"<\|[^|<>]{0,32}\|>"  # <|im_start|>, <|system|>, <|endoftext|>
    r"|\[/?(?:INST|SYS)\]"  # [INST] [/INST] [SYS]
    r"|<<\s*/?\s*SYS\s*>>"  # <<SYS>> <</SYS>>
    r"|<\s*/?\s*(?:system|user|assistant)\s*>"  # <system> … </assistant>
    r"|###\s*(?:system|instruction)\b",
    re.IGNORECASE,
)

# Directive phrasing addressed to a model, anchored on whole phrase shapes so
# ordinary inspection prose ("ignore the previous reading") passes.
_DIRECTIVE_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+|my\s+)*"
    r"(?:previous|prior|above|earlier|preceding|original|system)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)\b"
    r"|\b(?:reveal|show|print|repeat|output|disclose|display)\s+(?:me\s+)?"
    r"(?:your\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|prompts|instructions)"
    r"|the\s+(?:system|initial|hidden)\s+(?:prompt|prompts|instructions|message))\b"
    r"|\byou\s+are\s+now\s+(?:a\s+|an\s+)?(?:different\s+|unrestricted\s+|new\s+)?"
    r"(?:assistant|ai|chatbot|language\s+model|llm|dan)\b"
    r"|\bact\s+as\s+(?:if\s+you\s+are\s+)?(?:a\s+|an\s+)?(?:developer|admin|root|jailbroken)\s+mode\b"
    r"|\boverride\s+(?:your|the)\s+(?:instruction|instructions|rule|rules|safety|guardrail|guardrails)\b"
    r"|\bunion\s+(?:all\s+)?select\b|\bdrop\s+table\b|<script\b|javascript:"
    r"|以前の指示(?:を|は)?(?:すべて)?無視|指示を(?:すべて)?無視|システムプロンプト|プロンプトを(?:表示|出力|開示)",
    re.IGNORECASE,
)

# Local credential shapes — the half of the UNION the framework detector does
# not carry (its patterns describe key FORMATS: sk_live_, sk-, eyJ, AKIA,
# Bearer, db URIs). These describe ASSIGNMENTS and vendor prefixes.
_LOCAL_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "credential_assignment",
        re.compile(
            r"(?:password|passwd|pwd|secret|api[-_]?key|token|access[-_]?key|private[-_]?key)\s*[:=]\s*\S{4,}",
            re.IGNORECASE,
        ),
    ),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_\-]{16,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("prefixed_api_key", re.compile(r"\b(?:sk|pk|ak|rk)-[A-Za-z0-9]{16,}")),
    ("jwt_triplet", re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

# Personal-data shapes refused on caller free text. The digit patterns use
# non-digit lookarounds rather than \b: Japanese is written without spaces,
# so "個人番号1234-5678-9012を確認" has no word boundary around the number and
# a \b-anchored pattern misses exactly the normal case.
_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")),
    ("card", re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)")),
    ("national_id", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    ("my_number", re.compile(r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}(?!\d)")),
)

# The framework's title-case "name" heuristic masks ordinary product phrases
# ("Bearing Housing", "Warfarin Sodium"); on a transcript screen that would
# refuse legitimate inspection text, so that one finding type is post-filtered.
# Every other framework PII type (labelled Japanese names, e-mail, phones,
# cards, national identifiers) still refuses.
_FRAMEWORK_PII_IGNORED: frozenset[str] = frozenset({"name"})

# Structural markers of the rendered report, neutralised inside caller remarks
# so a remark can never forge a section heading or a verdict line.
_REPORT_MARKERS = str.maketrans({"■": "□", "【": "「", "】": "」"})
_VERDICT_LINE_RE = re.compile(r"総合判定\s*[:：]")
MAX_REMARK_CHARS = 200
MAX_REMARKS = 50


# ── Primitive validators ──────────────────────────────────────────────────────


def finite_in_range(value: object, lo: float, hi: float) -> Optional[float]:
    """Parse a caller number: a real, finite value within [lo, hi] — or None.

    Bools, non-numeric strings, the words NaN/Infinity (which float() happily
    parses), raw non-finite floats and out-of-range magnitudes all return None.
    Every comparison the pipeline makes therefore runs on a finite number.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not _NUMERIC_STR_RE.match(text):
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(parsed) or not (lo <= parsed <= hi):
        return None
    return parsed


def is_inert_identifier(value: object) -> bool:
    """True for a string over the inert alphabet [A-Za-z0-9_-], 1-32 chars."""
    return isinstance(value, str) and _IDENTIFIER_RE.match(value) is not None


def field_label(name: object, position: int) -> str:
    """Name a caller field in a refusal without echoing an arbitrary key.

    An inert identifier is echoed as ``input_context.<name>``; anything else
    (free text, control characters, a credential-shaped key) is referred to
    by position only.
    """
    if is_inert_identifier(name):
        return f"input_context.{name}"
    return f"input_context field #{position}"


def strip_control_chars(text: str) -> str:
    """Remove control characters (tab/newline kept) and zero-width characters."""
    return _ZERO_WIDTH_RE.sub("", _CONTROL_CHARS_RE.sub("", text))


def markup_stripped(text: str) -> str:
    """Return the text with markup tags and zero-width characters removed.

    A sanitizer is not a refusal: stripping ``<b>`` from ``ig<b>nore`` REASSEMBLES
    a directive, so every screen runs on both the raw and the stripped form.
    """
    return re.sub(r"\s+", " ", _MARKUP_RE.sub("", _ZERO_WIDTH_RE.sub("", text)))


# ── Credential and PII screens (recursive) ────────────────────────────────────


def find_credential(value: object, path: str = "") -> Optional[tuple[str, str]]:
    """Find a credential-shaped string anywhere in a JSON-like structure.

    Returns ``(pattern_label, path)`` for the first hit, or None. The block
    set is the UNION of the framework's ``detect_credentials`` (the floor —
    everything its output gate would raise on) and the local assignment/
    vendor-prefix patterns above. Walks dict values and list items at any
    depth; never returns the matched text.
    """
    if isinstance(value, str):
        findings = detect_credentials(value)
        if findings:
            return str(findings[0]["type"]), path
        for label, pattern in _LOCAL_CREDENTIAL_PATTERNS:
            if pattern.search(value):
                return label, path
        return None
    if isinstance(value, dict):
        for key, nested in value.items():
            hit = find_credential(nested, f"{path}.{key}" if path else str(key))
            if hit is not None:
                return hit
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            hit = find_credential(item, f"{path}[{index}]")
            if hit is not None:
                return hit
    return None


def find_personal_data(text: str) -> Optional[str]:
    """Return the label of the first personal-data shape in ``text``, or None."""
    for label, pattern in _PII_PATTERNS:
        if pattern.search(text):
            return label
    for finding in detect_pii(text):
        if finding["type"] not in _FRAMEWORK_PII_IGNORED:
            return str(finding["type"])
    return None


def screen_free_text(text: str) -> Optional[str]:
    """Screen caller free text; return a closed-set refusal label or None.

    Order matters: control tokens are checked on the RAW text first (a markup
    strip would delete them silently), then every screen runs again on the
    markup-stripped form (which re-assembles spliced directives).
    """
    for candidate in (text, markup_stripped(text)):
        if _CONTROL_TOKEN_RE.search(candidate):
            return "control_token"
        if _DIRECTIVE_RE.search(candidate):
            return "directive_phrase"
        if find_credential(candidate) is not None:
            return "credential_shape"
        if find_personal_data(candidate) is not None:
            return "personal_data"
    return None


def contains_control_token(value: object) -> bool:
    """True when any string in a JSON-like structure — keys included — carries
    a chat-template control token, raw or after markup stripping."""
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _CONTROL_TOKEN_RE.search(item) or _CONTROL_TOKEN_RE.search(markup_stripped(item)):
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


# ── Structured validators ─────────────────────────────────────────────────────


def validate_spec_ranges(raw: object) -> tuple[Optional[dict[str, dict[str, Any]]], Optional[str]]:
    """Validate a spec-range table: {jis_metric: {"min", "max", "unit"?}}.

    Returns (normalised, error_field). Metric names come from the closed JIS
    set, bounds are finite within [SPEC_BOUND_MIN, SPEC_BOUND_MAX] with
    min <= max, units come from the closed unit set. The error names the
    offending entry by metric only when the metric is a known name; an
    unknown key is reported by position.
    """
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "spec_ranges"
    if len(raw) > MAX_SPEC_RANGE_ENTRIES:
        return None, "spec_ranges"
    normalised: dict[str, dict[str, Any]] = {}
    for position, (metric, spec) in enumerate(raw.items(), start=1):
        if not isinstance(metric, str) or metric not in SPEC_METRICS:
            return None, f"spec_ranges entry #{position}"
        if not isinstance(spec, dict):
            return None, f"spec_ranges.{metric}"
        lo = finite_in_range(spec.get("min"), SPEC_BOUND_MIN, SPEC_BOUND_MAX)
        hi = finite_in_range(spec.get("max"), SPEC_BOUND_MIN, SPEC_BOUND_MAX)
        if lo is None or hi is None or lo > hi:
            return None, f"spec_ranges.{metric}"
        unit = spec.get("unit", "")
        if unit is None:
            unit = ""
        if not isinstance(unit, str) or (unit and unit not in SPEC_UNITS):
            return None, f"spec_ranges.{metric}.unit"
        normalised[metric] = {"min": lo, "max": hi, "unit": unit}
    return normalised, None


def validate_input_context(input_context: object) -> tuple[dict[str, Any], Optional[str], Optional[str]]:
    """Validate the caller's input_context against the declared contract.

    Returns ``(normalised, refusal_reason, field)``. ``refusal_reason`` is a
    closed-set label; ``field`` names the offending field (never its value).
    Absent fields are simply absent from the result (``channel`` defaults to
    "unknown"). Undeclared keys are ignored here — the standalone adapter
    refuses them before the graph runs — and are never echoed.
    """
    if input_context is None:
        return {"channel": "unknown"}, None, None
    if not isinstance(input_context, dict):
        return {}, "invalid_input_context", "input_context"

    normalised: dict[str, Any] = {}

    channel = input_context.get("channel")
    if channel is None:
        normalised["channel"] = "unknown"
    elif is_inert_identifier(channel):
        normalised["channel"] = channel
    else:
        return {}, "invalid_input_context", "input_context.channel"

    for key in ("product_line", "plant_id"):
        value = input_context.get(key)
        if value is None:
            continue
        if not is_inert_identifier(value):
            return {}, "invalid_input_context", f"input_context.{key}"
        normalised[key] = value

    transcript = input_context.get("transcript")
    if transcript is not None:
        if not isinstance(transcript, str):
            return {}, "invalid_input_context", "input_context.transcript"
        cleaned = strip_control_chars(transcript).strip()
        if not cleaned or len(cleaned) > MAX_TRANSCRIPT_CHARS:
            return {}, "invalid_input_context", "input_context.transcript"
        reason = screen_free_text(cleaned)
        if reason is not None:
            return {}, reason, "input_context.transcript"
        normalised["transcript"] = cleaned

    ranges, error_field = validate_spec_ranges(input_context.get("spec_ranges"))
    if error_field is not None or ranges is None:
        return {}, "invalid_input_context", f"input_context.{error_field or 'spec_ranges'}"
    if ranges:
        normalised["spec_ranges"] = ranges

    return normalised, None, None


def parse_audio_request(raw: str) -> tuple[dict[str, Any], Optional[str], Optional[str]]:
    """Validate the audio request envelope carried in ``user_input``.

    Returns ``(audio_request, refusal_reason, field)``. The normalised request
    never carries the payload itself — only ``audio_ref`` (sha256 hex),
    ``audio_length``, ``format`` and ``duration_s``. A plain non-JSON string is
    a file reference in wav format. An already-normalised envelope (one that
    carries ``audio_ref``) is validated and passed through.
    """
    text = raw.strip()
    if not text:
        return {}, "empty_input", "audio"
    payload: Any
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = {"audio": text, "format": "wav"}

    audio_format = payload.get("format", "wav")
    if audio_format is None:
        audio_format = "wav"
    if not isinstance(audio_format, str) or audio_format.lower().strip() not in SUPPORTED_AUDIO_FORMATS:
        return {}, "invalid_audio_request", "format"
    audio_format = audio_format.lower().strip()

    duration_raw = payload.get("duration_s")
    duration: Optional[float] = None
    if duration_raw is not None:
        duration = finite_in_range(duration_raw, 0.0, MAX_DURATION_S)
        if duration is None:
            return {}, "invalid_audio_request", "duration_s"

    if "audio_ref" in payload:
        ref = payload.get("audio_ref")
        length = finite_in_range(payload.get("audio_length"), 0, MAX_AUDIO_CHARS)
        if not isinstance(ref, str) or not _HEX_RE.match(ref) or length is None or length != int(length):
            return {}, "invalid_audio_request", "audio_ref"
        return (
            {"audio_ref": ref, "audio_length": int(length), "format": audio_format, "duration_s": duration},
            None,
            None,
        )

    audio = payload.get("audio")
    if not isinstance(audio, str) or not audio:
        return {}, "invalid_audio_request", "audio"
    if len(audio) > MAX_AUDIO_CHARS or not _NO_WHITESPACE_RE.match(audio):
        return {}, "invalid_audio_request", "audio"

    digest = hashlib.sha256(audio.encode("utf-8")).hexdigest()
    return {"audio_ref": digest, "audio_length": len(audio), "format": audio_format, "duration_s": duration}, None, None


def sanitize_remark(text: str) -> str:
    """Make a transcript remark safe to render as ONE line of the report.

    Newlines are flattened, section markers are swapped for inert look-alikes,
    and the verdict-line shape ``総合判定:`` cannot appear inside a remark, so no
    remark can be read as a section heading, a violation line or the verdict.
    """
    flat = strip_control_chars(text).replace("\n", " ").replace("\r", " ").translate(_REPORT_MARKERS)
    flat = _VERDICT_LINE_RE.sub("総合判定=", flat)
    return re.sub(r"\s+", " ", flat).strip()[:MAX_REMARK_CHARS]
