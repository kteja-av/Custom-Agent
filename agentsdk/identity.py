"""Principal identity (ADR-27, FR-16).

Deliberately inert in Phase 0: `PrincipalContext` is carried on RunConfig,
persisted as JSONB, and passed to PermissionChecker.check() -- but no Phase 0
checker reads it. It exists now so Phase 4's principal-aware checkers are a new
implementation of an unchanged interface rather than a signature change.

The security rule it anchors: the model receives capabilities and tool
interfaces, never raw bearer tokens or long-lived credentials. Nothing in this
type holds a secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .primitives import refuse_unstorable_fields


@dataclass(frozen=True)
class PrincipalContext:
    agent_principal: str
    user_principal: str | None = None
    service_principal: str | None = None
    acting_on_behalf_of: str | None = None
    delegation_id: str | None = None
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", tuple(self.scopes))
        # Every field, including `scopes` -- a tuple[str, ...] whose CONTENTS
        # reach runs.principal_context as JSONB. The storability walk on the
        # model-output primitives only covers plain string fields, which is how
        # this got through six rounds.
        refuse_unstorable_fields(self)

    def to_json(self) -> dict[str, object]:
        """Shape written to `runs.principal_context`. No consumer reads it yet."""
        return {
            "agent_principal": self.agent_principal,
            "user_principal": self.user_principal,
            "service_principal": self.service_principal,
            "acting_on_behalf_of": self.acting_on_behalf_of,
            "delegation_id": self.delegation_id,
            "scopes": list(self.scopes),
        }
