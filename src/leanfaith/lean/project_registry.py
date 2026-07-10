"""Project registry, toolchain checks, and context identity (PLAN.md §8.3, §8.11, §12.5).

The registry loads pinned project specs from ``configs/projects/*.yaml`` and
the environment lock from ``configs/environment.lock.yaml``. Toolchain checks
enforce the binding LeanInteract range (§6.2) with the single ADR-0001
stable-exception escape hatch; there are no silent overrides.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.loading import load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.lean.versions import (
    LeanVersion,
    LeanVersionError,
    in_advertised_range,
    parse_lean_version,
)
from leanfaith.schemas.theorem import ContextRecord

STABLE_EXCEPTION_VERSION = parse_lean_version("v4.31.0")


class ToolchainMode(StrEnum):
    """§6.2 Phase 0 toolchain modes."""

    ADVERTISED_RANGE = "advertised_range"
    STABLE_V4_31_EXCEPTION = "stable_v4_31_exception"


class ProjectKind(StrEnum):
    LOCAL = "local"
    GIT = "git"


class ProjectRole(StrEnum):
    FIXTURE = "fixture"
    MVP_SOURCE = "mvp_source"
    PROBE_NOW_ADAPTER_AT_OOD = "probe_now_adapter_at_ood"


class ProjectSpec(StrictModel):
    """One pinned Lean project (configs/projects/<key>.yaml)."""

    registry_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: ProjectKind
    uri: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    expected_toolchain: str | None = None
    root_module: str | None = None
    globs: tuple[str, ...] = ()
    role: ProjectRole

    def local_directory(self, paths: RepoPaths) -> Path | None:
        """Absolute directory for local projects (repo-root relative URI)."""
        if self.kind != ProjectKind.LOCAL:
            return None
        return paths.root / self.uri


class ToolchainLock(StrictModel):
    """§6.2/B.1 toolchain decision."""

    mode: ToolchainMode
    accepted_lean: str
    mathlib_toolchain_must_match: bool = True
    stable_v4_31_exception_adr: str | None = None


class LeanInteractLock(StrictModel):
    package: str = "lean-interact"
    version: str
    advertised_lean_min: str
    advertised_lean_max: str
    repl_fork: str


class PythonLock(StrictModel):
    version: str


class LeanBackendSettings(StrictModel):
    """B.1 lean_backend block."""

    backend: str = "leaninteract"
    server_mode: str = "stable"
    workers: int | None = None
    timeout_seconds: float = 30.0
    memory_hard_limit_mb: int | None = None
    allow_sorry: bool = False
    infotree: str = "none"
    save_raw_responses: bool = True


class EnvironmentLock(StrictModel):
    """configs/environment.lock.yaml (B.1); doctor rejects unresolved values."""

    environment_schema_version: int
    python: PythonLock
    lean_interact: LeanInteractLock
    toolchain_lock: ToolchainLock
    lean_backend: LeanBackendSettings


class ToolchainViolation(RuntimeError):
    """Raised when a toolchain violates the §6.2 binding constraint."""


def read_project_toolchain(project_dir: Path) -> LeanVersion:
    """Read and parse a project's checked-in ``lean-toolchain`` file."""
    toolchain_file = project_dir / "lean-toolchain"
    try:
        text = toolchain_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ToolchainViolation(f"cannot read {toolchain_file}: {exc}") from exc
    try:
        return parse_lean_version(text)
    except LeanVersionError as exc:
        raise ToolchainViolation(f"{toolchain_file}: {exc}") from exc


def check_toolchain_allowed(version: LeanVersion, lock: ToolchainLock) -> None:
    """Enforce §6.2: in-range by default; stable v4.31.0 only with tested ADR."""
    if in_advertised_range(version):
        return
    if (
        lock.mode == ToolchainMode.STABLE_V4_31_EXCEPTION
        and version == STABLE_EXCEPTION_VERSION
        and lock.stable_v4_31_exception_adr
    ):
        return
    raise ToolchainViolation(
        f"Lean {version} is outside the advertised LeanInteract range and no tested "
        "ADR-0001 stable-v4.31.0 exception applies (§6.2); do not override the "
        "project toolchain silently"
    )


def check_project_toolchain(
    spec: ProjectSpec, project_dir: Path, lock: ToolchainLock
) -> LeanVersion:
    """Validate a checked-out project's toolchain against pin and lock.

    Fixture/MVP-source projects must match the accepted lock exactly when
    ``mathlib_toolchain_must_match`` is set (§6.2). OOD probe projects
    (CSLib/Physlib) only need an in-range or ADR-excepted toolchain; their
    extraction contexts are pinned separately at Phase 11 prep (§9.6).
    """
    version = read_project_toolchain(project_dir)
    if spec.expected_toolchain is not None:
        expected = parse_lean_version(spec.expected_toolchain)
        if version != expected:
            raise ToolchainViolation(
                f"project {spec.registry_key}: checked-in toolchain {version} does not "
                f"match pinned expected_toolchain {expected} (§6.2)"
            )
    check_toolchain_allowed(version, lock)
    accepted = parse_lean_version(lock.accepted_lean)
    requires_lock_match = spec.role in (ProjectRole.FIXTURE, ProjectRole.MVP_SOURCE)
    if lock.mathlib_toolchain_must_match and requires_lock_match and version != accepted:
        raise ToolchainViolation(
            f"project {spec.registry_key}: toolchain {version} does not match the "
            f"accepted lock {accepted} and mathlib_toolchain_must_match is set"
        )
    return version


def load_environment_lock(paths: RepoPaths) -> EnvironmentLock:
    loaded = load_config(paths.configs / "environment.lock.yaml", EnvironmentLock)
    return loaded.config


def load_project_registry(paths: RepoPaths) -> dict[str, ProjectSpec]:
    """Load every configs/projects/*.yaml; keys must match file stems."""
    registry: dict[str, ProjectSpec] = {}
    projects_dir = paths.configs / "projects"
    for config_path in sorted(projects_dir.glob("*.yaml")):
        spec = load_config(config_path, ProjectSpec).config
        if spec.registry_key != config_path.stem:
            raise ValueError(
                f"{config_path}: registry_key {spec.registry_key!r} must match file stem"
            )
        registry[spec.registry_key] = spec
    return registry


# --- Context identity (§8.11, §12.5) ---


class ContextPayload(StrictModel):
    """The canonical §12.5 context payload hashed into the fingerprint."""

    environment_schema_version: int
    lean_version: str
    lean_interact_version: str
    repl_revision: str
    project_uri: str
    project_revision: str
    imports: tuple[str, ...] = ()
    namespace_context: tuple[str, ...] = ()
    open_context: tuple[str, ...] = ()
    scoped_context: tuple[str, ...] = ()
    options: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    notation_context: tuple[str, ...] = ()
    header_text: str = ""


def context_fingerprint(payload: ContextPayload) -> str:
    """SHA256 of the canonical context payload (§8.11)."""
    return hash_canonical(payload.model_dump(mode="json"))


def build_context_record(
    payload: ContextPayload,
    *,
    project_kind: str,
    project_registry_key: str,
) -> ContextRecord:
    """Assemble the §11.2 ContextRecord with ``ctx:`` + fingerprint identity."""
    fingerprint = context_fingerprint(payload)
    return ContextRecord(
        environment_schema_version=payload.environment_schema_version,
        context_id=f"ctx:{fingerprint}",
        context_fingerprint=fingerprint,
        project_kind=project_kind,
        project_uri=payload.project_uri,
        project_revision=payload.project_revision,
        project_registry_key=project_registry_key,
        lean_version=payload.lean_version,
        lean_interact_version=payload.lean_interact_version,
        repl_revision=payload.repl_revision,
        imports=payload.imports,
        namespace_context=payload.namespace_context,
        open_context=payload.open_context,
        scoped_context=payload.scoped_context,
        options=dict(payload.options),
        notation_context=payload.notation_context,
        header_text=payload.header_text,
        header_hash=sha256_hex(payload.header_text.encode("utf-8")),
    )
