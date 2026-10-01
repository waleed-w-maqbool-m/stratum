"""Runtime configuration.

Credentials are encrypted at rest with a Fernet key from the environment (PLAN.md 4.1).
If no key is configured one is generated and stored beside the app database, which keeps
a local install working without ceremony while still never writing a password in clear.
"""

from __future__ import annotations

import contextlib
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_prefix="STRATUM_", extra="ignore"
    )

    home: Path = Path(".stratum")
    secret_key: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Execution limits. Overridden per connection by its capability profile.
    statement_timeout_ms: int = 15_000
    row_cap: int = 5_000

    # Token ceiling per question (CLAUDE.md non-negotiable #4).
    question_token_limit: int = 6_000

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def db_path(self) -> Path:
        return self.home / "stratum.db"

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.home

    def fernet_key(self) -> bytes:
        """The key used to encrypt stored connection credentials."""
        from cryptography.fernet import Fernet

        if self.secret_key:
            return self.secret_key.encode()
        self.ensure_home()
        key_file = self.home / "secret.key"
        if key_file.exists():
            return key_file.read_bytes().strip()
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        # Windows ACLs make chmod a no-op; the file is still inside the app directory.
        with contextlib.suppress(OSError):
            key_file.chmod(0o600)
        return key


def load_env(start: Path | None = None) -> Path | None:
    """Read the nearest `.env` into the environment.

    `pydantic-settings` reads it for `Settings`, but the LLM client looks up provider
    keys in `os.environ` directly — it has to, since which variable to read is itself
    configuration. Without this the CLI and the eval harness run keyless and every
    question abstains, which looks like a bug in the pipeline rather than a missing file.

    Existing environment variables always win: an explicit export should override a file.

    Every `.env` up the tree is read, the nearest first, so a key set closer to the
    working directory wins and a key set only further up still arrives. Stopping at the
    nearest file meant `engine/.env` hid the Groq key in the root one, and running from
    the root read an empty `GEMINI_API_KEY=` and never saw the real one in `engine/`.
    Empty values are skipped, so a blank placeholder never shadows a real key.
    """
    here = (start or Path.cwd()).resolve()
    candidates = [here / "engine", here, *here.parents]
    nearest: Path | None = None
    for directory in candidates:
        candidate = directory / ".env"
        if not candidate.exists():
            continue
        nearest = nearest or candidate
        for line in candidate.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
    return nearest


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


def encrypt(value: str) -> str:
    from cryptography.fernet import Fernet

    if not value:
        return ""
    return Fernet(settings().fernet_key()).encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    from cryptography.fernet import Fernet

    if not value:
        return ""
    return Fernet(settings().fernet_key()).decrypt(value.encode()).decode()
