"""AgentCore Platform v1.0"""

# src/graph/context_bridge.py — carries the VALIDATED caller context across the
# outer→inner graph boundary.
#
# Why this exists: GraphNode.execute() (framework) invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` WITHOUT forwarding any
# other outer-state field, so inner-node reads of the caller's inspection data
# (the transcript, spec-range overrides, product line, plant) would always see
# nothing through the full nested graph. The sanctioned subclass hooks bridge it:
#
#   VoiceInspectionWorkflowGraphNode.extract_input(state)  [runs BEFORE subgraph.invoke]
#       → set_caller_context(state["caller_context"])
#   VoiceInspectionDomainWorkflow._extra_initial_state()  [runs INSIDE subgraph.invoke]
#       → returns {"caller_context": get_caller_context()}
#
# What crosses is the context PreProcessNode already validated — never the raw
# input_context. A ContextVar keeps the hand-off correct per thread/task, so
# concurrent invocations in one process cannot see each other's context.

from contextvars import ContextVar
from typing import Any

_CALLER_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("mfg_c2_061_caller_context", default=None)


def set_caller_context(caller_context: dict[str, Any] | None) -> None:
    """Stash the outer graph's validated caller context for the imminent inner-graph invoke."""
    _CALLER_CONTEXT.set(dict(caller_context) if caller_context else {})


def get_caller_context() -> dict[str, Any]:
    """Read (without consuming) the stashed caller context; {} when none was set."""
    return _CALLER_CONTEXT.get() or {}
