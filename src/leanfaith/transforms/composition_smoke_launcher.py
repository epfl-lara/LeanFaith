"""Serial, restartable schema-3 smoke orchestration for composition families.

The launcher is intentionally conservative.  It runs exactly one materializer
process at a time, and every materializer uses one LeanInteract worker without
an RLIMIT_AS memory limit.  Completed roots are re-audited with the normal
provisional-pair loader; partial or differently configured roots fail closed.
"""

from __future__ import annotations

import datetime
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, Protocol

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import collect_code_state
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.provisional_pair_combine import (
    ProvisionalPairCombineError,
    _load_root,
    _load_run_models,
)
from leanfaith.transforms.v2_d0_scale_run import V2D0ScaleRunSpec
from leanfaith.transforms.v2_e2_scale_run import V2E2ScaleRunSpec

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_FAMILY = r"^(?:p1[4-8]|n1[1-8])$"
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class CompositionSmokeLaunchError(RuntimeError):
    """The smoke launcher or one of its immutable inputs failed closed."""


RunKind = Literal["e2", "d0"]
FamilyState = Literal["pending", "running", "succeeded", "reused", "failed"]


@dataclass(frozen=True, slots=True)
class FamilyDefinition:
    key: str
    run_kind: RunKind
    profile_name: str
    profile_id: str


FAMILY_DEFINITIONS: tuple[FamilyDefinition, ...] = (
    FamilyDefinition(
        "p14", "e2", "v2_e2_p14_experimental.yaml", "deterministic_v2_e2_p14_experimental"
    ),
    FamilyDefinition(
        "p18", "e2", "v2_e2_p18_experimental.yaml", "deterministic_v2_e2_p18_experimental"
    ),
    FamilyDefinition(
        "n18", "d0", "v2_d0_n18_experimental.yaml", "deterministic_v2_d0_n18_experimental"
    ),
    FamilyDefinition(
        "n11", "d0", "v2_d0_n11_experimental.yaml", "deterministic_v2_d0_n11_experimental"
    ),
    FamilyDefinition(
        "n12", "d0", "v2_d0_n12_experimental.yaml", "deterministic_v2_d0_n12_experimental"
    ),
    FamilyDefinition(
        "p15", "e2", "v2_e2_p15_experimental.yaml", "deterministic_v2_e2_p15_experimental"
    ),
    FamilyDefinition(
        "p16", "e2", "v2_e2_p16_experimental.yaml", "deterministic_v2_e2_p16_experimental"
    ),
    FamilyDefinition(
        "p17", "e2", "v2_e2_p17_experimental.yaml", "deterministic_v2_e2_p17_experimental"
    ),
    FamilyDefinition(
        "n13", "d0", "v2_d0_n13_experimental.yaml", "deterministic_v2_d0_n13_experimental"
    ),
    FamilyDefinition(
        "n14", "d0", "v2_d0_n14_experimental.yaml", "deterministic_v2_d0_n14_experimental"
    ),
    FamilyDefinition(
        "n15", "d0", "v2_d0_n15_experimental.yaml", "deterministic_v2_d0_n15_experimental"
    ),
    FamilyDefinition(
        "n16", "d0", "v2_d0_n16_experimental.yaml", "deterministic_v2_d0_n16_experimental"
    ),
    FamilyDefinition(
        "n17", "d0", "v2_d0_n17_experimental.yaml", "deterministic_v2_d0_n17_experimental"
    ),
)


class CompositionSmokeSourceManifest(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_composition_smoke_source"]
    seed_manifest_sha256: str = Field(pattern=_HEX64)
    seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    row_count: Literal[64]
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    representation_partition_sha256: str = Field(pattern=_HEX64)


class SmokeFamilyPlan(StrictModel):
    family: str = Field(pattern=_FAMILY)
    run_kind: RunKind
    profile_id: str
    profile_path: str
    profile_file_sha256: str = Field(pattern=_HEX64)
    output_root: str
    reuse_root: bool
    producer_commit_attestation: str | None = Field(default=None, pattern=_HEX40)


def _without_id(payload: Mapping[str, object], field: str) -> dict[str, object]:
    result = dict(payload)
    result.pop(field, None)
    return result


class CompositionSmokeLaunchSpec(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_composition_smoke_launch_spec"] = (
        "deterministic_composition_smoke_launch_spec"
    )
    launch_id: str = Field(pattern=r"^detcomp_smoke_launch:[0-9a-f]{64}$")
    code_root: str
    expected_commit: str = Field(pattern=_HEX40)
    code_tree_hash: str = Field(pattern=_HEX64)
    project_dir: str
    project_revision: str = Field(pattern=_HEX40)
    project_tree: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    lean_toolchain_sha256: str = Field(pattern=_HEX64)
    source_dir: str
    source_manifest_sha256: str = Field(pattern=_HEX64)
    source_seed_set_id: str
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    representation_partition_sha256: str = Field(pattern=_HEX64)
    source_count: Literal[64] = 64
    output_root: str
    families: tuple[SmokeFamilyPlan, ...]
    batch_size: Literal[64] = 64
    workers: Literal[1] = 1
    memory_hard_limit_mb: None = None
    base_seed: Literal[0] = 0
    python_executable: str
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _identity_and_order(self) -> CompositionSmokeLaunchSpec:
        expected_order = tuple(item.key for item in FAMILY_DEFINITIONS)
        if tuple(item.family for item in self.families) != expected_order:
            raise ValueError("composition smoke family order changed")
        expected = "detcomp_smoke_launch:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "launch_id")
        )
        if self.launch_id != expected:
            raise ValueError("launch_id does not match the immutable launch spec")
        return self


class SmokeFamilyStatus(StrictModel):
    family: str = Field(pattern=_FAMILY)
    state: FamilyState
    root_path: str
    log_path: str
    command: tuple[str, ...] = ()
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None
    exit_code: int | None = None
    error: str | None = None


class CompositionSmokeStatus(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_composition_smoke_status"] = (
        "deterministic_composition_smoke_status"
    )
    launch_id: str
    family_statuses: tuple[SmokeFamilyStatus, ...]
    updated_at: datetime.datetime


class SmokeRootReceipt(StrictModel):
    family: str = Field(pattern=_FAMILY)
    run_kind: RunKind
    profile_id: str
    root_path: str
    reused: bool
    producer_commit_attestation: str | None = Field(default=None, pattern=_HEX40)
    root_binding_id: str
    root_tree_hash: str = Field(pattern=_HEX64)
    run_spec_sha256: str = Field(pattern=_HEX64)
    manifest_sha256: str = Field(pattern=_HEX64)
    results_sha256: str = Field(pattern=_HEX64)
    terminal_status_counts: dict[str, int]
    provisional_count: int = Field(ge=0)
    log_sha256: str = Field(pattern=_HEX64)


class CompositionSmokeReceipt(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_composition_smoke_receipt"] = (
        "deterministic_composition_smoke_receipt"
    )
    receipt_id: str = Field(pattern=r"^detcomp_smoke_receipt:[0-9a-f]{64}$")
    launch_id: str
    launch_spec_sha256: str = Field(pattern=_HEX64)
    final_status_sha256: str = Field(pattern=_HEX64)
    roots: tuple[SmokeRootReceipt, ...]
    completed_at: datetime.datetime
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _identity(self) -> CompositionSmokeReceipt:
        expected = "detcomp_smoke_receipt:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "receipt_id")
        )
        if self.receipt_id != expected:
            raise ValueError("receipt_id does not match its immutable payload")
        return self


class FamilyProcessExecutor(Protocol):
    def execute(
        self,
        *,
        family: str,
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
        lock_path: Path,
    ) -> int: ...

    def terminate(self) -> None: ...


class SubprocessFamilyExecutor:
    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None

    def execute(
        self,
        *,
        family: str,
        command: Sequence[str],
        cwd: Path,
        log_path: Path,
        lock_path: Path,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as family_lock, log_path.open("ab", buffering=0) as log:
            try:
                fcntl.flock(family_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CompositionSmokeLaunchError(
                    f"family {family} still has a live materializer lock: {lock_path}"
                ) from exc
            log.write(f"\n=== {family} started {_utcnow().isoformat()} ===\n".encode())
            environment = dict(os.environ)
            source = str((cwd / "src").resolve())
            environment["PYTHONPATH"] = source + (
                os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
            )
            project_index = tuple(command).index("--project-dir") + 1
            toolchain_path = Path(command[project_index]) / "lean-toolchain"
            environment["ELAN_TOOLCHAIN"] = toolchain_path.read_text(encoding="utf-8").strip()
            self._process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(family_lock.fileno(),),
            )
            try:
                exit_code = self._process.wait()
            finally:
                self._process = None
            log.write(f"=== {family} finished exit_code={exit_code} ===\n".encode())
            fcntl.flock(family_lock.fileno(), fcntl.LOCK_UN)
        return exit_code

    def terminate(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            process.wait()


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _canonical_model(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise CompositionSmokeLaunchError(f"output is not a regular file: {path}")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_immutable_or_verify(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise CompositionSmokeLaunchError(f"immutable launcher artifact differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _load_canonical(path: Path, model: type[StrictModel]) -> StrictModel:
    if not path.is_file() or path.is_symlink():
        raise CompositionSmokeLaunchError(f"launcher artifact is not a regular file: {path}")
    try:
        value = model.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise CompositionSmokeLaunchError(f"invalid launcher artifact {path}: {exc}") from exc
    if path.read_bytes() != _canonical_model(value):
        raise CompositionSmokeLaunchError(f"launcher artifact is not canonical: {path}")
    return value


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ("git", *args), cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CompositionSmokeLaunchError(f"cannot inspect git tree {root}: {exc}") from exc


def _clean_git_identity(root: Path, *, expected_commit: str | None = None) -> tuple[str, str]:
    root = root.resolve(strict=True)
    revision = _git(root, "rev-parse", "HEAD")
    if expected_commit is not None and revision != expected_commit:
        raise CompositionSmokeLaunchError(
            f"code commit differs from EXPECTED_COMMIT: {revision} != {expected_commit}"
        )
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise CompositionSmokeLaunchError(f"git tree must be clean: {root}")
    return revision, _git(root, "rev-parse", "HEAD^{tree}")


def _overlaps(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def _load_smoke_source(source_dir: Path) -> CompositionSmokeSourceManifest:
    source_dir = source_dir.resolve(strict=True)
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise CompositionSmokeLaunchError(f"smoke source is not a real directory: {source_dir}")
    manifest_path = source_dir / "manifest.json"
    manifest = _load_canonical(manifest_path, CompositionSmokeSourceManifest)
    assert isinstance(manifest, CompositionSmokeSourceManifest)
    theorem_path = source_dir / "theorems.jsonl"
    representation_path = source_dir / "representations.jsonl"
    if hash_file(theorem_path) != manifest.theorem_partition_sha256:
        raise CompositionSmokeLaunchError("smoke theorem partition hash differs from manifest")
    if hash_file(representation_path) != manifest.representation_partition_sha256:
        raise CompositionSmokeLaunchError("smoke representation hash differs from manifest")
    theorem_ids: list[str] = []
    representation_ids: list[str] = []
    contexts: set[str] = set()
    for path, model, ids in (
        (theorem_path, TheoremRecord, theorem_ids),
        (representation_path, RepresentationRecord, representation_ids),
    ):
        with path.open("rb") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.endswith(b"\n") or not line.strip():
                    raise CompositionSmokeLaunchError(
                        f"invalid JSONL framing at {path}:{line_number}"
                    )
                try:
                    record = model.model_validate_json(line)
                except ValueError as exc:
                    raise CompositionSmokeLaunchError(
                        f"invalid smoke record at {path}:{line_number}: {exc}"
                    ) from exc
                ids.append(record.theorem_id)
                contexts.add(record.context_id)
    if len(theorem_ids) != 64 or len(representation_ids) != 64:
        raise CompositionSmokeLaunchError("smoke source must contain exactly 64 aligned records")
    if theorem_ids != representation_ids:
        raise CompositionSmokeLaunchError("smoke theorem and representation IDs are not aligned")
    if len(contexts) != 1:
        raise CompositionSmokeLaunchError("smoke source must contain exactly one Lean context")
    return manifest


def _profile_hash(path: Path) -> tuple[str, str]:
    raw = load_yaml_mapping(path)
    profile_id = raw.get("profile_id")
    if not isinstance(profile_id, str):
        raise CompositionSmokeLaunchError(f"profile lacks profile_id: {path}")
    return profile_id, hash_canonical(raw)


def _validate_root(
    *, plan: SmokeFamilyPlan, source_dir: Path, project_dir: Path, log_path: Path
) -> SmokeRootReceipt:
    root = Path(plan.output_root)
    try:
        loaded = _load_root(root)
        run_kind, spec, manifest = _load_run_models(root)
    except (OSError, ValueError, ProvisionalPairCombineError) as exc:
        raise CompositionSmokeLaunchError(
            f"family {plan.family} root is partial, different, or invalid: {root}: {exc}"
        ) from exc
    if run_kind != plan.run_kind or spec.profile_id != plan.profile_id:
        raise CompositionSmokeLaunchError(f"family {plan.family} root has the wrong profile")
    if not isinstance(spec, V2E2ScaleRunSpec | V2D0ScaleRunSpec):
        raise CompositionSmokeLaunchError(f"family {plan.family} root is not schema 3")
    expected_profile_id, expected_profile_hash = _profile_hash(Path(plan.profile_path))
    if expected_profile_id != plan.profile_id or spec.profile_config_hash != expected_profile_hash:
        raise CompositionSmokeLaunchError(f"family {plan.family} profile hash differs")
    expected_theorems = (source_dir / "theorems.jsonl").resolve()
    expected_representations = (source_dir / "representations.jsonl").resolve()
    if (
        Path(spec.theorem_partition).resolve() != expected_theorems
        or Path(spec.representation_partition).resolve() != expected_representations
        or Path(spec.project_dir).resolve() != project_dir.resolve()
        or spec.batch_size != 64
        or spec.max_sources is not None
        or spec.workers != 1
        or spec.memory_hard_limit_mb is not None
        or spec.base_seed != 0
        or spec.import_header_sha256 != _EMPTY_SHA256
        or spec.source_count != 64
        or spec.attempt_count != 64
        or manifest.result_count != 64
        or manifest.resolved_label_count != 0
        or manifest.promoted_item_count != 0
        or manifest.training_eligible is not False
    ):
        raise CompositionSmokeLaunchError(f"family {plan.family} root violates smoke settings")
    counts = manifest.terminal_status_counts
    if counts.get("candidate_infrastructure_error", 0) != 0:
        raise CompositionSmokeLaunchError(
            f"family {plan.family} root contains infrastructure errors"
        )
    if not log_path.is_file() or log_path.is_symlink():
        raise CompositionSmokeLaunchError(f"family {plan.family} launcher log is missing")
    return SmokeRootReceipt(
        family=plan.family,
        run_kind=run_kind,
        profile_id=plan.profile_id,
        root_path=str(root.resolve()),
        reused=plan.reuse_root,
        producer_commit_attestation=plan.producer_commit_attestation,
        root_binding_id=loaded.binding.root_binding_id,
        root_tree_hash=loaded.binding.root_tree_hash,
        run_spec_sha256=hash_file(root / "run_spec.json"),
        manifest_sha256=hash_file(root / "manifest.json"),
        results_sha256=hash_file(root / "results.jsonl"),
        terminal_status_counts=dict(sorted(counts.items())),
        provisional_count=loaded.binding.provisional_count,
        log_sha256=hash_file(log_path),
    )


def _family_command(*, spec: CompositionSmokeLaunchSpec, plan: SmokeFamilyPlan) -> tuple[str, ...]:
    command = (
        "materialize-deterministic-v2-e2-scale"
        if plan.run_kind == "e2"
        else "materialize-deterministic-v2-d0-scale"
    )
    return (
        spec.python_executable,
        "-m",
        "leanfaith.cli.app",
        command,
        "--root",
        spec.code_root,
        "--theorems",
        str(Path(spec.source_dir) / "theorems.jsonl"),
        "--representations",
        str(Path(spec.source_dir) / "representations.jsonl"),
        "--project-dir",
        spec.project_dir,
        "--output-dir",
        plan.output_root,
        "--profile",
        plan.profile_path,
        "--batch-size",
        "64",
        "--workers",
        "1",
        "--base-seed",
        "0",
    )


def _status(
    *, spec: CompositionSmokeLaunchSpec, prior: Mapping[str, SmokeFamilyStatus] | None = None
) -> CompositionSmokeStatus:
    prior = {} if prior is None else prior
    return CompositionSmokeStatus(
        launch_id=spec.launch_id,
        family_statuses=tuple(
            prior.get(
                plan.family,
                SmokeFamilyStatus(
                    family=plan.family,
                    state="pending",
                    root_path=plan.output_root,
                    log_path=str(
                        Path(spec.output_root) / "orchestration/logs" / f"{plan.family}.log"
                    ),
                ),
            )
            for plan in spec.families
        ),
        updated_at=_utcnow(),
    )


@contextmanager
def _lock(output_root: Path) -> Iterator[IO[bytes]]:
    directory = output_root / "orchestration"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "run.lock"
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CompositionSmokeLaunchError(f"another smoke launcher owns {path}") from exc
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _build_spec(
    *,
    code_root: Path,
    expected_commit: str,
    source_dir: Path,
    project_dir: Path,
    output_root: Path,
    reused_roots: Mapping[str, Path],
    reused_root_commits: Mapping[str, str],
    python_executable: str,
) -> CompositionSmokeLaunchSpec:
    code_root = code_root.resolve(strict=True)
    source_dir = source_dir.resolve(strict=True)
    project_dir = project_dir.resolve(strict=True)
    output_root = output_root.resolve()
    code_revision, _ = _clean_git_identity(code_root, expected_commit=expected_commit)
    code = collect_code_state(code_root)
    if code.git_dirty or code.git_revision != code_revision or code.code_tree_hash is None:
        raise CompositionSmokeLaunchError("code-state collection did not preserve the clean pin")
    project_revision, project_tree = _clean_git_identity(project_dir)
    toolchain = project_dir / "lean-toolchain"
    if not toolchain.is_file() or toolchain.is_symlink():
        raise CompositionSmokeLaunchError("project lacks a regular lean-toolchain file")
    source = _load_smoke_source(source_dir)
    for protected in (code_root, source_dir, project_dir):
        if _overlaps(output_root, protected):
            raise CompositionSmokeLaunchError(
                f"smoke output root overlaps protected input: {output_root} versus {protected}"
            )
    valid_families = {item.key for item in FAMILY_DEFINITIONS}
    if set(reused_roots) - valid_families or set(reused_root_commits) != set(reused_roots):
        raise CompositionSmokeLaunchError(
            "reuse roots and producer-commit attestations must name the same valid families"
        )
    for family, producer_commit in reused_root_commits.items():
        if len(producer_commit) != 40 or any(
            character not in "0123456789abcdef" for character in producer_commit
        ):
            raise CompositionSmokeLaunchError(
                f"reuse producer commit is not 40 lowercase hex for {family}"
            )
        _git(code_root, "cat-file", "-e", f"{producer_commit}^{{commit}}")
    plans: list[SmokeFamilyPlan] = []
    for definition in FAMILY_DEFINITIONS:
        profile = (code_root / "configs/transformations" / definition.profile_name).resolve(
            strict=True
        )
        observed_profile_id, _ = _profile_hash(profile)
        if observed_profile_id != definition.profile_id:
            raise CompositionSmokeLaunchError(f"profile identity changed for {definition.key}")
        reused = definition.key in reused_roots
        root = (
            reused_roots[definition.key].resolve(strict=True)
            if reused
            else output_root / definition.key
        )
        plans.append(
            SmokeFamilyPlan(
                family=definition.key,
                run_kind=definition.run_kind,
                profile_id=definition.profile_id,
                profile_path=str(profile),
                profile_file_sha256=hash_file(profile),
                output_root=str(root),
                reuse_root=reused,
                producer_commit_attestation=(
                    reused_root_commits[definition.key] if reused else None
                ),
            )
        )
    payload: dict[str, object] = {
        "launch_id": f"detcomp_smoke_launch:{'0' * 64}",
        "code_root": str(code_root),
        "expected_commit": expected_commit,
        "code_tree_hash": code.code_tree_hash,
        "project_dir": str(project_dir),
        "project_revision": project_revision,
        "project_tree": project_tree,
        "lean_toolchain_sha256": hash_file(toolchain),
        "source_dir": str(source_dir),
        "source_manifest_sha256": hash_file(source_dir / "manifest.json"),
        "source_seed_set_id": source.seed_set_id,
        "theorem_partition_sha256": source.theorem_partition_sha256,
        "representation_partition_sha256": source.representation_partition_sha256,
        "output_root": str(output_root),
        "families": tuple(plans),
        # Preserve the virtual-environment path rather than resolving its
        # interpreter symlink to the system Python (which would lose the venv).
        "python_executable": str(Path(python_executable).absolute()),
    }
    placeholder = CompositionSmokeLaunchSpec.model_construct(_fields_set=None, **payload)
    launch_id = "detcomp_smoke_launch:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "launch_id")
    )
    return CompositionSmokeLaunchSpec.model_validate({**payload, "launch_id": launch_id})


def run_composition_smokes(
    *,
    code_root: Path,
    expected_commit: str,
    source_dir: Path,
    project_dir: Path,
    output_root: Path,
    reused_roots: Mapping[str, Path] | None = None,
    reused_root_commits: Mapping[str, str] | None = None,
    python_executable: str = sys.executable,
    process_executor: FamilyProcessExecutor | None = None,
) -> CompositionSmokeReceipt:
    """Run or exactly reuse all 13 schema-3 composition smokes serially."""

    reused_roots = {} if reused_roots is None else dict(reused_roots)
    reused_root_commits = {} if reused_root_commits is None else dict(reused_root_commits)
    output_root = output_root.resolve()
    spec = _build_spec(
        code_root=code_root,
        expected_commit=expected_commit,
        source_dir=source_dir,
        project_dir=project_dir,
        output_root=output_root,
        reused_roots=reused_roots,
        reused_root_commits=reused_root_commits,
        python_executable=python_executable,
    )
    executor = process_executor or SubprocessFamilyExecutor()
    orchestration = output_root / "orchestration"
    spec_path = orchestration / "launch_spec.json"
    status_path = orchestration / "status.json"
    receipt_path = orchestration / "receipt.json"
    with _lock(output_root):
        _write_immutable_or_verify(spec_path, _canonical_model(spec))
        if receipt_path.exists():
            receipt = _load_canonical(receipt_path, CompositionSmokeReceipt)
            assert isinstance(receipt, CompositionSmokeReceipt)
            if receipt.launch_id != spec.launch_id or receipt.launch_spec_sha256 != hash_file(
                spec_path
            ):
                raise CompositionSmokeLaunchError("existing receipt belongs to another launch")
            if not status_path.is_file() or hash_file(status_path) != receipt.final_status_sha256:
                raise CompositionSmokeLaunchError("existing receipt final status changed")
            for plan, expected in zip(spec.families, receipt.roots, strict=True):
                log_path = orchestration / "logs" / f"{plan.family}.log"
                if not log_path.exists():
                    raise CompositionSmokeLaunchError("existing receipt lost a launcher log")
                observed = _validate_root(
                    plan=plan,
                    source_dir=source_dir,
                    project_dir=project_dir,
                    log_path=log_path,
                )
                if observed != expected:
                    raise CompositionSmokeLaunchError("existing receipt root binding changed")
            return receipt

        prior_map: dict[str, SmokeFamilyStatus] = {}
        if status_path.exists():
            prior = _load_canonical(status_path, CompositionSmokeStatus)
            assert isinstance(prior, CompositionSmokeStatus)
            if prior.launch_id != spec.launch_id:
                raise CompositionSmokeLaunchError("existing status belongs to another launch")
            prior_map = {item.family: item for item in prior.family_statuses}
        _write_atomic(status_path, _canonical_model(_status(spec=spec, prior=prior_map)))
        roots: list[SmokeRootReceipt] = []
        for plan in spec.families:
            root = Path(plan.output_root)
            log_path = orchestration / "logs" / f"{plan.family}.log"
            command = _family_command(spec=spec, plan=plan)
            if root.exists():
                log_path.parent.mkdir(parents=True, exist_ok=True)
                if not log_path.exists():
                    log_path.write_text(
                        f"validated existing root {root} for launch {spec.launch_id}\n",
                        encoding="utf-8",
                    )
                validated = _validate_root(
                    plan=plan,
                    source_dir=source_dir,
                    project_dir=project_dir,
                    log_path=log_path,
                )
                state: Literal["reused", "succeeded"] = "reused" if plan.reuse_root else "succeeded"
                prior_map[plan.family] = SmokeFamilyStatus(
                    family=plan.family,
                    state=state,
                    root_path=str(root),
                    log_path=str(log_path),
                    command=command,
                    finished_at=_utcnow(),
                    exit_code=0,
                )
                roots.append(validated)
                _write_atomic(status_path, _canonical_model(_status(spec=spec, prior=prior_map)))
                continue
            if plan.reuse_root:
                raise CompositionSmokeLaunchError(f"registered reuse root disappeared: {root}")
            running = SmokeFamilyStatus(
                family=plan.family,
                state="running",
                root_path=str(root),
                log_path=str(log_path),
                command=command,
                started_at=_utcnow(),
            )
            prior_map[plan.family] = running
            _write_atomic(status_path, _canonical_model(_status(spec=spec, prior=prior_map)))
            try:
                exit_code = executor.execute(
                    family=plan.family,
                    command=command,
                    cwd=Path(spec.code_root),
                    log_path=log_path,
                    lock_path=orchestration / "locks" / f"{plan.family}.lock",
                )
            except KeyboardInterrupt as exc:
                executor.terminate()
                raise CompositionSmokeLaunchError(
                    f"smoke launch interrupted during {plan.family}; partial roots are rejected"
                ) from exc
            if exit_code != 0:
                prior_map[plan.family] = running.model_copy(
                    update={
                        "state": "failed",
                        "finished_at": _utcnow(),
                        "exit_code": exit_code,
                        "error": f"child exited with code {exit_code}",
                    }
                )
                _write_atomic(status_path, _canonical_model(_status(spec=spec, prior=prior_map)))
                raise CompositionSmokeLaunchError(
                    f"family {plan.family} child exited with code {exit_code}; see {log_path}"
                )
            try:
                validated = _validate_root(
                    plan=plan,
                    source_dir=source_dir,
                    project_dir=project_dir,
                    log_path=log_path,
                )
            except CompositionSmokeLaunchError as exc:
                prior_map[plan.family] = running.model_copy(
                    update={
                        "state": "failed",
                        "finished_at": _utcnow(),
                        "exit_code": 0,
                        "error": f"post-run root validation failed: {exc}",
                    }
                )
                _write_atomic(status_path, _canonical_model(_status(spec=spec, prior=prior_map)))
                raise
            prior_map[plan.family] = running.model_copy(
                update={"state": "succeeded", "finished_at": _utcnow(), "exit_code": 0}
            )
            roots.append(validated)
            _write_atomic(status_path, _canonical_model(_status(spec=spec, prior=prior_map)))

        final_status_sha = hash_file(status_path)
        receipt_payload: dict[str, object] = {
            "receipt_id": f"detcomp_smoke_receipt:{'0' * 64}",
            "launch_id": spec.launch_id,
            "launch_spec_sha256": hash_file(spec_path),
            "final_status_sha256": final_status_sha,
            "roots": tuple(roots),
            "completed_at": _utcnow(),
        }
        placeholder_receipt = CompositionSmokeReceipt.model_construct(
            _fields_set=None, **receipt_payload
        )
        receipt_id = "detcomp_smoke_receipt:" + hash_canonical(
            _without_id(placeholder_receipt.model_dump(mode="json"), "receipt_id")
        )
        receipt = CompositionSmokeReceipt.model_validate(
            {**receipt_payload, "receipt_id": receipt_id}
        )
        _write_immutable_or_verify(receipt_path, _canonical_model(receipt))
        return receipt


__all__ = [
    "FAMILY_DEFINITIONS",
    "CompositionSmokeLaunchError",
    "CompositionSmokeLaunchSpec",
    "CompositionSmokeReceipt",
    "CompositionSmokeStatus",
    "FamilyProcessExecutor",
    "run_composition_smokes",
]
