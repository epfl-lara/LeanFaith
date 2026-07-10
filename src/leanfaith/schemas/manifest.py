"""Run/output manifests and migration maps (PLAN.md §28.2, §10, §11.12).

Manifests are strict models serialized as canonical JSON files. Unlike
semantic IDs they may carry UTC timestamps; they never carry secret values.
Reading fails closed on unknown keys, malformed JSON, or schema mismatch.
"""

from __future__ import annotations

import datetime
import json
import secrets
import subprocess
from pathlib import Path

from pydantic import Field, ValidationError, field_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas.enums import ArtifactClass, DataStage

MANIFEST_SCHEMA_VERSION = 1

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_RUN_ID_PATTERN = r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$"


class ManifestError(ValueError):
    """Raised when a manifest cannot be read, parsed, or validated."""


def _require_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None or value.utcoffset() != datetime.timedelta(0):
        raise ValueError("timestamps must be timezone-aware UTC")
    return value


class CodeState(StrictModel):
    """Code identity for a run: exact revision plus dirty flag (§28.2)."""

    git_revision: str = Field(pattern=_HEX40)
    git_dirty: bool


class RunManifest(StrictModel):
    """Per-run manifest written to ``runs/<run_id>/manifest.json`` (§28.2)."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    artifact_class: ArtifactClass
    command: str
    argv: tuple[str, ...] = ()
    code: CodeState
    environment_schema_version: int | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    config_hashes: dict[str, str] = Field(default_factory=dict)
    input_hashes: dict[str, str] = Field(default_factory=dict)
    output_hashes: dict[str, str] = Field(default_factory=dict)
    seeds: dict[str, int] = Field(default_factory=dict)
    execution: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    revisions: dict[str, str] = Field(default_factory=dict)
    status_counts: dict[str, int] = Field(default_factory=dict)
    retry_count: int = 0
    measurements: dict[str, int | float] = Field(default_factory=dict)
    tracker_id: str | None = None
    tracker_offline_path: str | None = None
    parent_run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)
    resumed_from_run_id: str | None = Field(default=None, pattern=_RUN_ID_PATTERN)
    created_at: datetime.datetime
    notes: str = ""

    _utc = field_validator("created_at")(_require_utc)


class OutputManifest(StrictModel):
    """Immutable partition manifest (§10 rule 3)."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    stage: DataStage
    artifact_class: ArtifactClass
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    source: str
    source_revision: str
    config_hash: str = Field(pattern=_HEX64)
    record_schema_version: int
    row_count: int = Field(ge=0)
    shard_ids: tuple[str, ...] = ()
    file_checksums: dict[str, str] = Field(default_factory=dict)
    input_manifest_hashes: dict[str, str] = Field(default_factory=dict)
    code: CodeState
    created_at: datetime.datetime
    notes: str = ""

    _utc = field_validator("created_at")(_require_utc)

    @field_validator("file_checksums", "input_manifest_hashes")
    @classmethod
    def _hex_digests(cls, value: dict[str, str]) -> dict[str, str]:
        for name, digest in value.items():
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"checksum for {name!r} is not a sha256 hex digest")
        return value


class MigrationMap(StrictModel):
    """Explicit old→new ID mapping for schema migrations (§11.12)."""

    schema_version: int = MANIFEST_SCHEMA_VERSION
    migration_id: str
    record_kind: str
    from_schema_version: int
    to_schema_version: int
    id_mapping: dict[str, str]
    created_at: datetime.datetime
    notes: str = ""

    _utc = field_validator("created_at")(_require_utc)


def new_run_id(created_at: datetime.datetime, *, nonce: str | None = None) -> str:
    """Build ``run_<UTC stamp>_<8 hex>``; run IDs are operational, not semantic."""
    stamp = _require_utc(created_at).strftime("%Y%m%dT%H%M%SZ")
    suffix = nonce if nonce is not None else secrets.token_hex(4)
    if len(suffix) != 8 or any(c not in "0123456789abcdef" for c in suffix):
        raise ValueError(f"run-id nonce must be 8 hex chars, got {suffix!r}")
    return f"run_{stamp}_{suffix}"


def run_manifest_path(paths: RepoPaths, run_id: str) -> Path:
    return paths.runs / run_id / "manifest.json"


def write_manifest(manifest: StrictModel, path: Path) -> str:
    """Write a manifest as canonical JSON plus newline; return the file's sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    path.write_bytes(payload)
    return hash_file(path)


def read_manifest[ModelT: StrictModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Read and validate a manifest; fail closed on any mismatch."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in manifest {path}: {exc}") from exc
    try:
        return model_type.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(f"invalid manifest {path}:\n{exc}") from exc


def manifest_hash(path: Path) -> str:
    """Content hash of a manifest file's exact bytes."""
    return hash_file(path)


def collect_code_state(root: Path) -> CodeState:
    """Read the git revision and dirty state at ``root``.

    Outside a git checkout this fails: a run manifest without code identity
    would break provenance, so there is no permissive fallback.
    """
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ManifestError(f"cannot determine git code state at {root}: {exc}") from exc
    return CodeState(git_revision=revision, git_dirty=bool(status.strip()))
