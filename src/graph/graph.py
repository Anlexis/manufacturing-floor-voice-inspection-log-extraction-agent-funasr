"""AgentCore Platform v1.0"""

# Cat 2 — AgentBaseGraph outer graph + GraphNode in the `main` slot.
# Domain workflow is encapsulated inside VoiceInspectionWorkflowGraphNode.
#
# Backbone (fixed — do NOT override add_edges()):
#   START → initialize → pre_process → main → post_process → finalize → END
#
# Inner domain pipeline (VoiceInspectionDomainWorkflow in domain_workflow_graph.py):
#   audio_input → funasr_transcribe → speaker_diarize
#               → field_extract_jis_norm → spec_validate_qms_format → END
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph
#   src/graph/context_bridge.py        ← validated caller context hand-off (outer → inner)
#
# Runtime configuration: the platform registry loads config/config.yaml and
# passes it as Graph(config=...); the standalone server does the same via
# _runtime_config(). AgentBaseGraph consumes max_retry (retry routing); the
# `voice_inspection` block is validated at compile time and injected into the
# inner domain nodes through their constructors.
#
# Class name here MUST match:
#   config/agent.yaml  →  class: "src.graph.graph.ManufacturingFloorVoiceInspectionLogExtractionAgent"
#   src/api/server.py  →  from src.graph.graph import ManufacturingFloorVoiceInspectionLogExtractionAgent

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from framework.errors import ConfigError
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.context_bridge import set_caller_context
from src.nodes.post_process_node import PostProcessNode, _security_gate_output
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.caller_contract import is_inert_identifier, validate_spec_ranges

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import VoiceInspectionDomainWorkflow

# Runtime-parameter file: src/graph/graph.py -> parents[2] is the repo root.
# config/agent.yaml (the static manifest) holds only registration identity;
# every runtime parameter lives in config/config.yaml.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_ENDPOINT_RE = re.compile(r"\Ahttps?://\S{1,250}\Z")
_DOMAIN_BLOCK = "voice_inspection"


def _runtime_config() -> dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the same file the platform registry loads and passes as
    Graph(config=...); the standalone server (src/api/server.py) reads it here
    so the deployed agent and a registry-loaded agent see identical
    configuration. Returns an empty dict — never raises — when the file is
    absent, unreadable, not valid YAML, or not a mapping (the graph then runs
    on its built-in defaults).
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def domain_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the `voice_inspection` config block; raise ConfigError on an invalid value.

    Returns only the keys that were declared. Validation is fail-closed: an
    invalid spec-range table would silently change which tolerances a part is
    judged against, so a bad value stops graph construction rather than being
    dropped.
    """
    block = config.get(_DOMAIN_BLOCK)
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError(f"'{_DOMAIN_BLOCK}' must be a mapping")
    settings: dict[str, Any] = {}

    endpoint = block.get("funasr_endpoint")
    if endpoint not in (None, ""):
        if not isinstance(endpoint, str) or not _ENDPOINT_RE.match(endpoint):
            raise ConfigError(f"'{_DOMAIN_BLOCK}.funasr_endpoint' must be an http(s) URL without whitespace")
        settings["funasr_endpoint"] = endpoint

    for key in ("product_line", "plant_id"):
        value = block.get(key)
        if value is None:
            continue
        if not is_inert_identifier(value):
            raise ConfigError(f"'{_DOMAIN_BLOCK}.{key}' must be an identifier (letters, digits, _ -; 1-32 chars)")
        settings[key] = value

    ranges, error_field = validate_spec_ranges(block.get("spec_ranges"))
    if error_field is not None or ranges is None:
        raise ConfigError(f"'{_DOMAIN_BLOCK}.{error_field or 'spec_ranges'}' is invalid")
    if ranges:
        settings["spec_ranges"] = ranges
    return settings


class VoiceInspectionWorkflowGraphNode(GraphNode):
    """Wraps VoiceInspectionDomainWorkflow; assigned to the `main` slot.

    GraphNode contract:
      get_subgraph()   — instantiate and return VoiceInspectionDomainWorkflow (with the validated settings)
      extract_input()  — bridge the validated caller context, pass validated_input to inner graph.invoke()
      merge_output()   — map sub_result fields into the outer state delta

    Inner nodes use required_trust_level = ANONYMOUS; the outer PreProcessNode
    enforces VERIFIED_EXTERNAL before this node is reached.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, settings: Optional[dict[str, Any]] = None) -> None:
        super().__init__()
        self._settings: dict[str, Any] = dict(settings or {})

    def get_subgraph(self) -> "VoiceInspectionDomainWorkflow":
        """Instantiate the inner domain workflow graph with the validated settings."""
        from src.graph.domain_workflow_graph import VoiceInspectionDomainWorkflow

        return VoiceInspectionDomainWorkflow(config=dict(self._settings))

    def extract_input(self, state: AgentState) -> str:
        """Pass the validated input to the inner graph and bridge the caller context.

        GraphNode.execute() does not forward anything but user_input on
        subgraph.invoke(); this is the last hook that sees the outer state
        before the inner invoke — see src/graph/context_bridge.py.
        """
        set_caller_context(cast("dict[str, Any] | None", state.get("caller_context")))
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map inner graph outputs back into the outer state.

        Returns ONLY changed keys — never the full state.
        Designed together with VoiceInspectionDomainWorkflow.get_output().
        The values written here are PRE-boundary; PostProcessNode replaces
        result with the gated text (or clears every one of them).
        """
        return {
            "result": sub_result.get("inspection_report"),
            "inspection_report": sub_result.get("inspection_report"),
            "qms_record": sub_result.get("qms_record"),
            "spec_violations": sub_result.get("spec_violations") or [],
            "transcript_source": sub_result.get("transcript_source"),
            "status": sub_result.get("status"),
        }


class ManufacturingFloorVoiceInspectionLogExtractionAgent(AgentBaseGraph):
    """MFG-C2-061 outer graph.

    Backbone: initialize → pre_process → main → post_process → finalize.
    Domain logic is in VoiceInspectionWorkflowGraphNode (main slot) which
    delegates to VoiceInspectionDomainWorkflow (inner BaseGraph).

    Class name matches config/agent.yaml class: and src/api/server.py import.
    """

    @property
    def name(self) -> str:
        return "ManufacturingFloorVoiceInspectionLogExtractionAgent"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        """Framework checks (max_retry, hitl, memory) plus the domain block — fail closed."""
        super()._validate_config()
        domain_settings(self.config)

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = VoiceInspectionWorkflowGraphNode(settings=domain_settings(self.config))
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def _security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        """Canonical output-gate entry point on the agent class.

        Delegates to the module-level gate in post_process_node so the node
        and the agent can never diverge on what a credential looks like.
        """
        return _security_gate_output(result)

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Surface the gated inspection result on the outer invoke() return.

        `output` is the base envelope's caller-facing text (formatted_output,
        which PostProcessNode always sets to a truthy value). The structured
        fields are surfaced ONLY on a successful, gated run; on any non-success
        outcome they are withheld. refusal_reason is a closed-set label — never
        caller data — so a caller can act on a refusal.
        """
        output = cast(dict[str, Any], super().get_output(state))
        succeeded = state.get("status") == AgentStatus.SUCCESS.value
        output["qms_record"] = state.get("qms_record") if succeeded else None
        output["spec_violations"] = list(state.get("spec_violations") or []) if succeeded else []
        output["transcript_source"] = state.get("transcript_source") if succeeded else None
        output["refusal_reason"] = state.get("refusal_reason")
        return output
