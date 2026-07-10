"""Base config model and secret references (PLAN.md §6.1, LF-002).

Every persisted schema uses ``extra="forbid"`` so unknown keys fail closed.
Secrets are referenced by environment-variable name only; the value is read
at resolution time and never stored on a model, in a dump, or in a hash.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class MissingSecretError(RuntimeError):
    """Raised when a referenced secret is unset or empty in the environment."""

    def __init__(self, env_name: str) -> None:
        super().__init__(
            f"secret environment variable {env_name!r} is unset or empty; "
            "export it or configure the secret manager (see .env.example)"
        )
        self.env_name = env_name


class StrictModel(BaseModel):
    """Immutable base model: unknown keys are rejected, defaults are validated."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SecretRef(StrictModel):
    """Reference to a secret by environment-variable name (value never stored)."""

    env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")

    def resolve(self) -> str:
        value = os.environ.get(self.env, "")
        if not value:
            raise MissingSecretError(self.env)
        return value
