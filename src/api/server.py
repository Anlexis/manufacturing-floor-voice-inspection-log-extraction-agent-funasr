"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from shared.secrets import factory as secrets_factory

from src.graph.graph import ManufacturingFloorVoiceInspectionLogExtractionAgent, _runtime_config
from src.services.caller_contract import INPUT_CONTEXT_FIELDS, field_label

app = FastAPI(title="Agent")

# The platform registry loads config/config.yaml and passes it as
# Graph(config=...); the standalone server mirrors that exactly so declared
# runtime parameters (max_retry, voice_inspection.*) are live in both deployments.
agent = ManufacturingFloorVoiceInspectionLogExtractionAgent(config=_runtime_config())
agent.compile()
# Namespace and agent_name must match the agent manifest values in config/agent.yaml
# (namespace = lower-cased industry code; tests/unit/test_manifest_entrypoint_parity.py
# reads both files and fails on any drift).
agent.provision_secrets(
    secrets_factory(namespace="mfg", agent_name="ManufacturingFloorVoiceInspectionLogExtractionAgent")
)

# Upper bound on the serialized input_context (bytes). The graph enforces
# per-field bounds (identifier alphabet, transcript length, finite numeric
# ranges, table caps); this is the coarse adapter-level guard against
# oversized payloads reaching the graph at all.
_MAX_INPUT_CONTEXT_BYTES = 262_144


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured invocation parameters (SDK BaseGraph.invoke input_context):
    # transcript, spec_ranges, product_line, plant_id, channel — validated
    # field-by-field inside the graph (PreProcessNode).
    input_context: dict[str, Any] | None = None


def _screen_input_context(input_context: dict[str, Any]) -> None:
    """Refuse an input_context the graph could not process, naming the field only.

    Two refusals, both 400 (pydantic owns 422):
      * an undeclared key — the graph ignores unknown keys, but the framework's
        first node returns the whole input_context into its result and its
        output gate scans every value, so an undeclared field is an unscreened
        channel straight into that gate; refusing here is what makes the
        declared contract the whole attack surface;
      * a credential-shaped VALUE — that same gate would fail the request at
        the first node with an opaque traceback before any template code runs.
        The screen uses the framework's own detector so the refusal set equals
        the gate's block set exactly; per-field scanning is equivalent to
        scanning the whole mapping (the detector is the union over values).
    Field names are caller data too: an inert name is echoed, anything else is
    referred to by position.
    """
    for position, (name, value) in enumerate(input_context.items(), start=1):
        label = field_label(name, position)
        if name not in INPUT_CONTEXT_FIELDS:
            raise HTTPException(status_code=400, detail=f"{label} is not an accepted input_context field.")
        if detect_credentials_in_value(value):
            raise HTTPException(
                status_code=400,
                detail=f"{label} contains a credential-shaped value; remove it and resend.",
            )


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone-deployment caller auth: when INVOKE_AUTH_TOKEN is set on the
    # server environment, callers that no upstream middleware vouched for
    # (still ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (standalone equivalent of
    # the platform auth middleware) — a deployment-level caller credential,
    # not an agent secret, so ctx.secrets does not apply (no InvocationContext
    # exists before auth) — the documented entry-point exception.
    #
    # Required here specifically: PreProcessNode (src/nodes/pre_process_node.py)
    # occupies the pre_process backbone slot and declares required_trust_level =
    # TrustLevel.VERIFIED_EXTERNAL. Nothing else sets request.state.trust_level
    # in the standalone deployment, so without this boundary every deployed
    # invoke arrives ANONYMOUS, the trust gate denies it, and the agent returns
    # status="error".
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    input_context = req.input_context or {}
    if input_context and len(json.dumps(input_context, default=str)) > _MAX_INPUT_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="input_context exceeds the maximum allowed size.")
    if input_context:
        _screen_input_context(input_context)
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "ManufacturingFloorVoiceInspectionLogExtractionAgent"}
