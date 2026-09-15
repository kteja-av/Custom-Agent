"""Artifacts: content a run or a caller stores, bound to one tenant and project
(FR-53, FR-54, FR-55, ADR-11).

An artifact is bytes with a checked description: its MIME type, the SHA-256 of its
content, who created it, the run it came from, its provenance, and when it
expires. A store is bound to one tenant and project, and everything outside that
scope -- another tenant's artifact, a deleted or expired one, an id that never
existed -- is not found, indistinguishably.

Two stores implement the protocol: the in-memory one here, and the Postgres one in
`agentsdk.postgres`, reached through `Persistence.artifact_store`. Both validate a
`put` through the one function below, so they refuse exactly the same things.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .errors import ArtifactIntegrityError, ArtifactNotFound
from .primitives import ContentProvenance, checked_provenance, unstorable_reason

# P2-D8: the most content one artifact may hold by default, in bytes (10 MiB).
DEFAULT_MAX_CONTENT_BYTES = 10_485_760

# The longest mime_type, created_by_agent or classification (FR-54).
_MAX_TEXT = 255
_MIME_TYPE = re.compile(r"[^/\s]+/[^/\s]+")

RunAdmission = Callable[[str, str, str], bool]


@dataclass(frozen=True)
class ArtifactRef:
    """What is known about an artifact, without its content (FR-53)."""

    artifact_id: str
    tenant_id: str
    project_id: str
    uri: str
    mime_type: str
    content_hash: str
    size: int
    created_by_agent: str
    source_run: str | None
    source_task: str | None
    provenance: ContentProvenance
    classification: str | None
    created_at: datetime
    expires_at: datetime | None


@runtime_checkable
class ArtifactStore(Protocol):
    """A store bound to one tenant and project (FR-54). Every method is a coroutine."""

    async def put(
        self,
        content: bytes,
        *,
        mime_type: str,
        provenance: ContentProvenance,
        created_by_agent: str,
        source_run: str | None = None,
        classification: str | None = None,
        expires_at: datetime | None = None,
    ) -> ArtifactRef: ...

    async def get(self, artifact_id: str) -> bytes: ...

    async def metadata(self, artifact_id: str) -> ArtifactRef: ...

    async def delete(self, artifact_id: str) -> None: ...

    async def expire(self) -> int: ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def refuse_bad_scope(tenant_id: Any, project_id: Any) -> None:
    """A store's tenant and project: non-empty text a store can hold (ADR-11)."""
    for name, value in (("tenant_id", tenant_id), ("project_id", project_id)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"an artifact store's {name} must be non-empty text")
        reason = unstorable_reason(value)
        if reason is not None:
            raise ValueError(f"an artifact store's {name} cannot be stored: {reason}")


def checked_cap(value: Any) -> int:
    """max_content_bytes: a positive int (P2-D8). A bool passes every range check."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"max_content_bytes must be a positive int, got {value!r}")
    return value


def canonical_id(value: Any) -> str | None:
    """`value` as its canonical UUID text, or None if it has none.

    A uuid.UUID is accepted, because get_run and PostgresTrace hand run ids back in
    that type and column_rejection_reason accepts one for a UUID column (M7 round 2);
    exactly uuid.UUID, since a subclass can render itself as any text. Of strings,
    only the canonical form: uuid.UUID() and a Postgres uuid column both parse
    uppercase, braced, urn:uuid: and unhyphenated forms, and refusing them all keeps
    either store from finding an artifact the other would not. The text returned is
    an exact str, compared on the characters a str subclass holds.
    """
    if type(value) is uuid.UUID:
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None
    return canonical if str.__eq__(value, canonical) is True else None


def not_found(artifact_id: Any) -> ArtifactNotFound:
    """The one message, naming only the id asked for (FR-54)."""
    return ArtifactNotFound(f"no artifact {artifact_id!r}")


def integrity_error(artifact_id: Any) -> ArtifactIntegrityError:
    return ArtifactIntegrityError(f"artifact {artifact_id!r} does not match its content hash")


def is_expired(ref: ArtifactRef, now: datetime) -> bool:
    """Expired at or before the clock: not found by any method, even before expire() runs."""
    return ref.expires_at is not None and ref.expires_at <= now


def _text_field(name: str, value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text, got {type(value).__name__}")
    text = value if type(value) is str else str.__getitem__(value, slice(None))
    if len(text) > _MAX_TEXT:
        raise ValueError(f"{name} is {len(text)} characters; the limit is {_MAX_TEXT}")
    reason = unstorable_reason(text)
    if reason is not None:
        raise ValueError(f"{name} cannot be stored: {reason}")
    return text


def prepare_put(
    content: Any,
    *,
    tenant_id: str,
    project_id: str,
    mime_type: Any,
    provenance: Any,
    created_by_agent: Any,
    source_run: Any,
    classification: Any,
    expires_at: Any,
    now: datetime,
    max_content_bytes: int,
) -> tuple[ArtifactRef, bytes]:
    """Validate a put and build what it stores, before anything is written (FR-54).

    Each refusal is a ValueError naming its field. Content is copied into exact
    bytes, so a caller changing its buffer afterwards changes nothing stored.
    """
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValueError(f"content must be bytes, a bytearray or a memoryview, got {type(content).__name__}")
    data = memoryview(content).tobytes()
    if len(data) > max_content_bytes:
        raise ValueError(f"content is {len(data)} bytes, over this store's cap of {max_content_bytes}")

    mime = _text_field("mime_type", mime_type)
    if not _MIME_TYPE.fullmatch(mime):
        raise ValueError(f"mime_type must have the form type/subtype, got {mime!r}")
    agent = _text_field("created_by_agent", created_by_agent)
    classification_text = _text_field("classification", classification, optional=True)

    if expires_at is not None:
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("expires_at must be a timezone-aware datetime or None")
        if expires_at <= now:
            raise ValueError("expires_at must be later than the store's clock")

    checked = checked_provenance(provenance)
    if isinstance(checked, str):
        raise ValueError(f"provenance cannot be stored: {checked}")

    run_id = None
    if source_run is not None:
        run_id = canonical_id(source_run)
        if run_id is None:
            raise ValueError("source_run must be a canonical UUID, as text or a uuid.UUID, or None")

    artifact_id = str(uuid.uuid4())
    ref = ArtifactRef(
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        project_id=project_id,
        uri=f"urn:agentsdk:artifact:{artifact_id}",
        mime_type=mime,
        content_hash=hashlib.sha256(data).hexdigest(),
        size=len(data),
        created_by_agent=agent,
        source_run=run_id,
        source_task=None,
        provenance=checked,
        classification=classification_text,
        created_at=now,
        expires_at=expires_at,
    )
    return ref, data


_SOURCE_RUN_REFUSED = "source_run must be a run of this store's tenant and project"


class InMemoryArtifactStore:
    """FR-55's in-memory store: the same behaviour as Postgres, held in a dict.

    A `source_run` is accepted only when `runs(tenant_id, project_id, run_id)`
    returns True; built without that callable, the store refuses every source run,
    because it has no other way to know a run belongs to this scope.
    """

    def __init__(
        self,
        tenant_id: str,
        project_id: str,
        *,
        runs: RunAdmission | None = None,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        refuse_bad_scope(tenant_id, project_id)
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._runs = runs
        self._cap = checked_cap(max_content_bytes)
        self._clock = clock if clock is not None else utc_now
        # artifact_id -> (ArtifactRef, content). Shared by stores from for_scope(),
        # as one database is shared by every scope.
        self._rows: dict[str, tuple[ArtifactRef, bytes]] = {}

    def for_scope(self, tenant_id: str, project_id: str) -> InMemoryArtifactStore:
        """A store over the same artifacts, bound to another tenant and project."""
        other = InMemoryArtifactStore(
            tenant_id, project_id, runs=self._runs, max_content_bytes=self._cap, clock=self._clock
        )
        other._rows = self._rows
        return other

    async def put(
        self,
        content: bytes,
        *,
        mime_type: str,
        provenance: ContentProvenance,
        created_by_agent: str,
        source_run: str | None = None,
        classification: str | None = None,
        expires_at: datetime | None = None,
    ) -> ArtifactRef:
        ref, data = prepare_put(
            content,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
            mime_type=mime_type,
            provenance=provenance,
            created_by_agent=created_by_agent,
            source_run=source_run,
            classification=classification,
            expires_at=expires_at,
            now=self._clock(),
            max_content_bytes=self._cap,
        )
        if ref.source_run is not None and not (
            self._runs is not None and self._runs(self._tenant_id, self._project_id, ref.source_run) is True
        ):
            raise ValueError(_SOURCE_RUN_REFUSED)
        self._rows[ref.artifact_id] = (ref, data)
        return ref

    def _visible(self, artifact_id: Any) -> tuple[ArtifactRef, bytes]:
        key = canonical_id(artifact_id)
        row = self._rows.get(key) if key is not None else None
        if row is None:
            raise not_found(artifact_id)
        ref = row[0]
        if (ref.tenant_id, ref.project_id) != (self._tenant_id, self._project_id) or is_expired(ref, self._clock()):
            raise not_found(artifact_id)
        return row

    async def get(self, artifact_id: str) -> bytes:
        ref, data = self._visible(artifact_id)
        content = bytes(data)
        if hashlib.sha256(content).hexdigest() != ref.content_hash:
            raise integrity_error(artifact_id)
        return content

    async def metadata(self, artifact_id: str) -> ArtifactRef:
        return self._visible(artifact_id)[0]

    async def delete(self, artifact_id: str) -> None:
        ref, _ = self._visible(artifact_id)
        del self._rows[ref.artifact_id]

    async def expire(self) -> int:
        now = self._clock()
        expired = [
            artifact_id
            for artifact_id, (ref, _) in self._rows.items()
            if (ref.tenant_id, ref.project_id) == (self._tenant_id, self._project_id) and is_expired(ref, now)
        ]
        for artifact_id in expired:
            del self._rows[artifact_id]
        return len(expired)
