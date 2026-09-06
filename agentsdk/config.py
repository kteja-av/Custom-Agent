"""Environment-sourced settings (LLD 5).

Credentials are read here and handed straight to the transport. Nothing in this
module is persisted, logged, or put in a RunEvent payload (NFR-4).

Redaction is a property of the *value*, not of one method on the container. An
overridden `__repr__` protects `repr`, `str` and f-strings, but leaves
`dataclasses.asdict`, `astuple` and `vars` leaking the raw string -- and
`asdict` is exactly the idiom that will write FR-11's ExecutionManifest row.
`Secret` therefore carries its own redaction, so every one of those paths yields
a redacted object and the raw value only ever appears via an explicit
`.reveal()` call that is greppable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

# SQLAlchemy-style URLs ("postgresql+psycopg://") are common in .env files, but
# psycopg's own parser rejects the driver suffix. Normalise instead of making
# the operator maintain two spellings of the same URL.
_DRIVER_SUFFIX = re.compile(r"^postgresql\+\w+://")

DEFAULT_MODEL = "openai.gpt-4o-mini"

REDACTED = "<redacted>"


class Secret:
    """A string that refuses to render itself.

    Survives repr, str, f-strings, dataclasses.asdict/astuple, vars, and JSON
    serialisation attempts. The raw value comes out only through `.reveal()`.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return REDACTED

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, _spec: str) -> str:
        return REDACTED

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    # dataclasses.asdict deep-copies values; without this a Secret would be
    # reconstructed correctly anyway, but being explicit keeps copy semantics
    # obvious to anyone reading.
    def __deepcopy__(self, _memo: dict) -> Secret:
        return self

    def __copy__(self) -> Secret:
        return self

    def __reduce__(self) -> tuple:
        """Refuse to pickle rather than round-trip the raw value.

        Serialising a credential to a byte stream is never what the caller
        wanted; if it genuinely is, `.reveal()` says so out loud.
        """
        raise TypeError("Secret refuses to be pickled; call .reveal() deliberately instead")

    def __getstate__(self) -> None:
        raise TypeError("Secret refuses to be serialised; call .reveal() deliberately instead")


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: Secret
    database_url: Secret | None = None
    default_model: str = DEFAULT_MODEL

    def __post_init__(self) -> None:
        # Accept plain strings at the boundary; store secrets wrapped.
        if not isinstance(self.api_key, Secret):
            object.__setattr__(self, "api_key", Secret(str(self.api_key)))
        if self.database_url is not None and not isinstance(self.database_url, Secret):
            object.__setattr__(self, "database_url", Secret(str(self.database_url)))

    @property
    def dsn(self) -> str | None:
        """The database URL as psycopg wants it. Explicit, so the reveal is greppable."""
        return self.database_url.reveal() if self.database_url else None

    @classmethod
    def from_env(cls, *, load_dotfile: bool = True) -> Settings:
        if load_dotfile:
            load_dotenv()
        base_url = os.environ.get("BASE_URL", "").strip()
        api_key = os.environ.get("MODEL_API_KEY", "").strip()
        if not base_url or not api_key:
            missing = [
                name
                for name, value in (("BASE_URL", base_url), ("MODEL_API_KEY", api_key))
                if not value
            ]
            raise RuntimeError(f"missing required environment variable(s): {', '.join(missing)}")
        database_url = normalise_database_url(os.environ.get("DATABASE_URL"))
        return cls(
            base_url=base_url,
            api_key=Secret(api_key),
            database_url=Secret(database_url) if database_url else None,
            default_model=os.environ.get("DEFAULT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        )


def normalise_database_url(url: str | None) -> str | None:
    if not url:
        return None
    return _DRIVER_SUFFIX.sub("postgresql://", url.strip())
