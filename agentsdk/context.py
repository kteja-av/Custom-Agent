"""Context assembly (FR-7, ADR-26, LLD 3.11).

Sits between canonical internal messages and what a specific provider receives.
Distinct from ContextPolicy (Phase 2 -- what a subagent may see) and
ContextCompactor (Phase 7 -- token-budget summarisation); collapsing those three
into one component is exactly what this separation prevents.

Phase 0 behaviour: pass the whole history through -- one agent, no curated
briefing yet -- while carrying each tool result's ContentProvenance as request
METADATA rather than as text the model reads.

That distinction is the point. Provenance injected into the prompt would be
content the model can be talked out of; as metadata it is a policy input that
travels with the request and can never be argued with. Provenance informs
policy; it does not enforce it, and it is never an instruction to the model.
"""

from __future__ import annotations

from typing import Any

from .model import ModelRequest
from .primitives import Message, Role


class ContextAssembler:
    def build(
        self,
        history: list[Message],
        tool_schemas: list[dict[str, Any]] | None = None,
        *,
        instructions: str | None = None,
        model_settings: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ModelRequest:
        request_metadata = dict(metadata or {})
        provenance = self._provenance_manifest(history)
        if provenance:
            request_metadata["provenance"] = provenance
        return ModelRequest(
            messages=tuple(history),
            tools=tuple(tool_schemas or ()),
            instructions=instructions,
            # Always None in Phase 0; the slot exists so this call site does not
            # change when Phase 2 starts requesting structured output.
            output_schema=None,
            model_settings=dict(model_settings or {}),
            metadata=request_metadata,
        )

    def _provenance_manifest(self, history: list[Message]) -> list[dict[str, Any]]:
        """One entry per tool result carried in the history.

        Phase 2's ContextPolicy reads this to decide what a subagent may see;
        Phase 4's policy engine reads it to decide what a tainted result may
        trigger. Nothing reads it in Phase 0 -- but it is assembled correctly
        now so neither has to retrofit it.
        """
        manifest: list[dict[str, Any]] = []
        for message in history:
            if message.role is not Role.TOOL:
                continue
            for result in message.tool_results:
                p = result.provenance
                manifest.append(
                    {
                        "tool_call_id": result.tool_call_id,
                        "origin": p.origin.value,
                        "instruction_authority": p.instruction_authority.value,
                        "trust_zone": p.trust_zone.value,
                        "taint_flags": sorted(flag.value for flag in p.taint_flags),
                        "is_error": result.is_error,
                    }
                )
        return manifest
