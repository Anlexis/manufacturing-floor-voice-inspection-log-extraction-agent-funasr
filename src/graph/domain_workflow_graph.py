"""AgentCore Platform v1.0"""

# Inner domain workflow graph for MFG-C2-061.
# Inherits BaseGraph (fully custom topology — no backbone slots).
# Called by VoiceInspectionWorkflowGraphNode.get_subgraph() in graph.py.
#
# Pipeline:
#   START → audio_input → funasr_transcribe → speaker_diarize
#         → field_extract_jis_norm → spec_validate_qms_format → END

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import get_caller_context
from src.nodes.audio_input_node import AudioInputNode
from src.nodes.field_extract_and_jis_term_normalize_node import FieldExtractAndJISTermNormalizeNode
from src.nodes.funasr_transcribe_node import FunASRTranscribeNode
from src.nodes.speaker_diarize_node import SpeakerDiarizeNode
from src.nodes.spec_range_validate_and_qms_record_format_node import SpecRangeValidateAndQMSRecordFormatNode
from src.schemas.state import State


class VoiceInspectionDomainWorkflow(BaseGraph):
    """Inner domain workflow: transcript → field extraction → QMS record.

    Pipeline (linear — no conditional branches in the happy path):
      START → audio_input → funasr_transcribe → speaker_diarize
            → field_extract_jis_norm → spec_validate_qms_format → END

    ERROR short-circuit: the linear layout stops naturally on error because
    every downstream node checks its required state fields, and the outer
    GraphNode re-raises an ERROR sub_result (error_strategy = "propagate").
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "voice_inspection_domain_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No mandatory keys: the outer graph validates the domain block before forwarding it."""

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner state with the VALIDATED caller context.

        GraphNode.execute() (framework) does not forward the outer state into
        subgraph.invoke(), so the outer graph stashes the validated context in
        a ContextVar (VoiceInspectionWorkflowGraphNode.extract_input) and this
        hook reads it back — see src/graph/context_bridge.py. Without this,
        inner-node reads of caller_context (the transcript, spec overrides,
        product line, plant) would always see nothing.
        """
        return {"caller_context": get_caller_context()}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all inner domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Config injection: the node contract is execute(self, state) -> dict, so
        the declared settings (validated by the outer graph and forwarded as
        this graph's `config`) reach the two config-driven nodes through their
        constructors. Built standalone (VoiceInspectionDomainWorkflow()), the
        settings are empty and every node runs on its built-in defaults.
        """
        settings = dict(self.config or {})
        self._nodes["audio_input"] = AudioInputNode()
        self._nodes["funasr_transcribe"] = FunASRTranscribeNode(settings=settings)
        self._nodes["speaker_diarize"] = SpeakerDiarizeNode()
        self._nodes["field_extract_jis_norm"] = FieldExtractAndJISTermNormalizeNode()
        self._nodes["spec_validate_qms_format"] = SpecRangeValidateAndQMSRecordFormatNode(settings=settings)

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear domain pipeline."""
        self._sg.add_edge(START, "audio_input")
        self._sg.add_edge("audio_input", "funasr_transcribe")
        self._sg.add_edge("funasr_transcribe", "speaker_diarize")
        self._sg.add_edge("speaker_diarize", "field_extract_jis_norm")
        self._sg.add_edge("field_extract_jis_norm", "spec_validate_qms_format")
        self._sg.add_edge("spec_validate_qms_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Required by BaseGraph ABC; not wired in the linear topology."""
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "spec_validate_qms_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Return the fields the outer VoiceInspectionWorkflowGraphNode.merge_output() needs.

        Designed together with merge_output() in graph.py.
        """
        return {
            "output": state.get("inspection_report") or state.get("formatted_output"),
            "inspection_report": state.get("inspection_report"),
            "qms_record": state.get("qms_record"),
            "spec_violations": state.get("spec_violations") or [],
            "transcript_source": state.get("transcript_source"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
