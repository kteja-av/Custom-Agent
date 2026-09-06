"""Execution manifest (FR-11, ADR-25, LLD 2.4).

A per-run snapshot of exactly what configuration produced it: SDK version, spec
and instruction hashes, model and adapter versions, tool schema hashes, policy
version.

Nothing reads it in Phase 0. It is written from the start so that "what
produced this run" is answerable during debugging today, and so Phase 8's
compatibility gating has a history to gate against rather than starting from an
empty table.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(
    *,
    sdk_version: str,
    agent_spec_id: str,
    instructions: str,
    tool_profile: tuple[str, ...],
    tool_spec_hashes: list[str],
    model_id: str | None,
    model_version: str | None = None,
    model_adapter_version: str | None = None,
    policy_version: str | None = None,
) -> dict[str, Any]:
    # The spec hash covers what actually changes behaviour: identity,
    # instructions and the tool profile. A spec whose display name changed is
    # not a different run configuration.
    spec_material = "|".join((agent_spec_id, instructions, ",".join(sorted(tool_profile))))
    return {
        "sdk_version": sdk_version,
        "agent_spec_hash": _sha256(spec_material),
        "instructions_hash": _sha256(instructions),
        "model_id": model_id,
        "model_version": model_version,
        "model_adapter_version": model_adapter_version,
        "tool_spec_hashes": sorted(tool_spec_hashes),
        "policy_version": policy_version,
    }
