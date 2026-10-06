"""Shared test fixtures for MFG-C2-061.

Every domain node module imports emit_trace_event at module level; the
autouse fixture silences it per module via monkeypatch (never sys.modules
stubbing of shared.*), so node tests exercise the domain logic only.
"""

import pytest

_NODE_MODULES = (
    "src.nodes.pre_process_node",
    "src.nodes.post_process_node",
    "src.nodes.audio_input_node",
    "src.nodes.funasr_transcribe_node",
    "src.nodes.speaker_diarize_node",
    "src.nodes.field_extract_and_jis_term_normalize_node",
    "src.nodes.spec_range_validate_and_qms_record_format_node",
)


def _noop(*_args, **_kwargs):
    return None


@pytest.fixture(autouse=True)
def _patch_emit(monkeypatch):
    """Silence emit_trace_event for all domain node modules without stubbing shared.*"""
    for module in _NODE_MODULES:
        monkeypatch.setattr(f"{module}.emit_trace_event", _noop)
