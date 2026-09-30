"""
Observatory settings — from the environment only.

  OBSERVATORY_DATABASE_URL  required  DSN for the read-only `observatory` role
  OBSERVATORY_TOKEN         required  bearer token every API caller must present
  OBSERVATORY_BIND          optional  default 127.0.0.1
  OBSERVATORY_PORT          optional  default 8787

A missing database URL or token is fatal. An unauthenticated observatory is
not a degraded mode, it is no observatory. Neither secret ever appears in a
repr, a log line or an error message.
"""
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8787

# Long enough that guessing is not a strategy. `openssl rand -hex 32` gives 64.
MIN_TOKEN_LENGTH = 16


class SettingsError(RuntimeError):
    """Raised when the observatory must refuse to start."""


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    token: str = field(repr=False)
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Read and validate the environment. Raises SettingsError, never guesses."""
    env = os.environ if env is None else env

    database_url = (env.get("OBSERVATORY_DATABASE_URL") or "").strip()
    token = (env.get("OBSERVATORY_TOKEN") or "").strip()

    missing = [
        name
        for name, value in (
            ("OBSERVATORY_DATABASE_URL", database_url),
            ("OBSERVATORY_TOKEN", token),
        )
        if not value
    ]
    if missing:
        raise SettingsError(
            "observatory refusing to start: missing "
            + ", ".join(missing)
            + ". An unauthenticated observatory is not a degraded mode, it is no observatory."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise SettingsError(
            f"observatory refusing to start: OBSERVATORY_TOKEN is shorter than "
            f"{MIN_TOKEN_LENGTH} characters (generate one with `openssl rand -hex 32`)."
        )

    bind = (env.get("OBSERVATORY_BIND") or DEFAULT_BIND).strip()
    raw_port = (env.get("OBSERVATORY_PORT") or str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise SettingsError(f"observatory refusing to start: OBSERVATORY_PORT {raw_port!r} is not an integer")
    if not 1 <= port <= 65535:
        raise SettingsError(f"observatory refusing to start: OBSERVATORY_PORT {port} is out of range")

    return Settings(database_url=database_url, token=token, bind=bind, port=port)
