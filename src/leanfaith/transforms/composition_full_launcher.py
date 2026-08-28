"""Serial, restartable schema-3 full-scale composition orchestration."""

from __future__ import annotations

import datetime
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import collect_code_state
from leanfaith.transforms.composition_seed import (
    CompositionSeedManifest,
    CompositionSeedRecord,
    _parse_canonical_jsonl,
)
from leanfaith.transforms.composition_smoke_launcher import (
    FAMILY_DEFINITIONS,
    CompositionSmokeLaunchError,
    FamilyProcessExecutor,
    SubprocessFamilyExecutor,
    _canonical_model,
    _clean_git_identity,
    _load_canonical,
    _lock,
    _overlaps,
    _profile_hash,
    _utcnow,
    _without_id,
    _write_atomic,
    _write_immutable_or_verify,
)
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
_SOURCE_COUNT = 3941
_SOURCE_FILES = frozenset(
    {"manifest.json", "seeds.jsonl", "theorems.jsonl", "representations.jsonl"}
)


class CompositionFullLaunchError(RuntimeError):
    """The full composition launcher or an immutable input failed closed."""


class FullFamilyPlan(StrictModel):
    family: str = Field(pattern=_FAMILY)
    run_kind: Literal["e2", "d0"]
    profile_id: str
    profile_path: str
    profile_file_sha256: str = Field(pattern=_HEX64)
    output_root: str


class CompositionFullLaunchSpec(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_composition_full_launch_spec"] = (
        "deterministic_composition_full_launch_spec"
    )
    launch_id: str = Field(pattern=r"^detcomp_full_launch:[0-9a-f]{64}$")
    code_root: str
    expected_commit: str = Field(pattern=_HEX40)
    code_tree_hash: str = Field(pattern=_HEX64)
    project_dir: str
    project_revision: str = Field(pattern=_HEX40)
    project_tree: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    lean_toolchain_sha256: str = Field(pattern=_HEX64)
    seed_dir: str
    seed_manifest_sha256: str = Field(pattern=_HEX64)
    seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    seed_partition_sha256: str = Field(pattern=_HEX64)
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    representation_partition_sha256: str = Field(pattern=_HEX64)
    source_count: Literal[3941] = 3941
    output_root: str
    families: tuple[FullFamilyPlan, ...]
    batch_size: Literal[64] = 64
    workers: Literal[1] = 1
    memory_hard_limit_mb: None = None
    base_seed: Literal[0] = 0
    python_executable: str
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _identity_and_order(self) -> CompositionFullLaunchSpec:
        if tuple(item.family for item in self.families) != tuple(
            item.key for item in FAMILY_DEFINITIONS
        ):
            raise ValueError("full composition family order changed")
        expected = "detcomp_full_launch:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "launch_id")
        )
        if self.launch_id != expected:
            raise ValueError("launch_id does not match the immutable full launch spec")
        return self


class FullFamilyStatus(StrictModel):
    family: str = Field(pattern=_FAMILY)
    state: Literal["pending", "running", "succeeded", "reused", "failed"]
    root_path: str
    log_path: str
    command: tuple[str, ...] = ()
    started_at: datetime.datetime | None = None
    finished_at: datetime.datetime | None = None
    exit_code: int | None = None
    error: str | None = None


class CompositionFullStatus(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_composition_full_status"] = (
        "deterministic_composition_full_status"
    )
    launch_id: str
    family_statuses: tuple[FullFamilyStatus, ...]
    updated_at: datetime.datetime


class FullRootReceipt(StrictModel):
    family: str = Field(pattern=_FAMILY)
    run_kind: Literal["e2", "d0"]
    profile_id: str
    root_path: str
    reused: bool
    root_binding_id: str
    root_tree_hash: str = Field(pattern=_HEX64)
    run_spec_sha256: str = Field(pattern=_HEX64)
    manifest_sha256: str = Field(pattern=_HEX64)
    results_sha256: str = Field(pattern=_HEX64)
    terminal_status_counts: dict[str, int]
    provisional_count: int = Field(ge=0)
    log_sha256: str = Field(pattern=_HEX64)


class CompositionFullReceipt(StrictModel):
    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_composition_full_receipt"] = (
        "deterministic_composition_full_receipt"
    )
    receipt_id: str = Field(pattern=r"^detcomp_full_receipt:[0-9a-f]{64}$")
    launch_id: str
    launch_spec_sha256: str = Field(pattern=_HEX64)
    final_status_sha256: str = Field(pattern=_HEX64)
    roots: tuple[FullRootReceipt, ...]
    completed_at: datetime.datetime
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _identity(self) -> CompositionFullReceipt:
        expected = "detcomp_full_receipt:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "receipt_id")
        )
        if self.receipt_id != expected:
            raise ValueError("receipt_id does not match its immutable payload")
        return self


def _load_seed_source(seed_dir: Path) -> CompositionSeedManifest:
    seed_dir = seed_dir.resolve(strict=True)
    if not seed_dir.is_dir() or seed_dir.is_symlink():
        raise CompositionFullLaunchError(f"seed input is not a real directory: {seed_dir}")
    actual = {item.name for item in seed_dir.iterdir()}
    if actual != _SOURCE_FILES:
        raise CompositionFullLaunchError(
            f"seed directory is not exact: expected {sorted(_SOURCE_FILES)}, found {sorted(actual)}"
        )
    try:
        manifest = _load_canonical(seed_dir / "manifest.json", CompositionSeedManifest)
    except (OSError, ValueError) as exc:
        raise CompositionFullLaunchError(f"invalid composition seed manifest: {exc}") from exc
    assert isinstance(manifest, CompositionSeedManifest)
    if (
        manifest.seed_count != _SOURCE_COUNT
        or manifest.theorem_count != _SOURCE_COUNT
        or manifest.representation_count != _SOURCE_COUNT
        or hash_file(seed_dir / "seeds.jsonl") != manifest.seed_output_sha256
        or hash_file(seed_dir / "theorems.jsonl") != manifest.theorem_output_sha256
        or hash_file(seed_dir / "representations.jsonl") != manifest.representation_output_sha256
    ):
        raise CompositionFullLaunchError("composition seed counts or hashes differ")
    try:
        seeds = _parse_canonical_jsonl(seed_dir / "seeds.jsonl", CompositionSeedRecord)
        from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord

        theorems = _parse_canonical_jsonl(seed_dir / "theorems.jsonl", TheoremRecord)
        representations = _parse_canonical_jsonl(
            seed_dir / "representations.jsonl", RepresentationRecord
        )
    except (OSError, ValueError) as exc:
        raise CompositionFullLaunchError(f"invalid composition seed partition: {exc}") from exc
    if not (len(seeds) == len(theorems) == len(representations) == _SOURCE_COUNT):
        raise CompositionFullLaunchError("composition seed partitions are not 3,941 rows")
    contexts: set[str] = set()
    for index, (seed, theorem, representation) in enumerate(
        zip(seeds, theorems, representations, strict=True)
    ):
        if (
            seed.intermediate_theorem_id != theorem.theorem_id
            or seed.intermediate_representation_id != representation.representation_id
            or theorem.theorem_id != representation.theorem_id
        ):
            raise CompositionFullLaunchError(
                f"composition seed partitions are misaligned at zero-based row {index}"
            )
        contexts.update((seed.context_id, theorem.context_id, representation.context_id))
    if len(contexts) != 1:
        raise CompositionFullLaunchError("composition full source must use exactly one context")
    return manifest


def _validate_full_root(
    *, plan: FullFamilyPlan, seed_dir: Path, project_dir: Path, log_path: Path, reused: bool
) -> FullRootReceipt:
    root = Path(plan.output_root)
    try:
        loaded = _load_root(root)
        run_kind, spec, manifest = _load_run_models(root)
    except (OSError, ValueError, ProvisionalPairCombineError) as exc:
        raise CompositionFullLaunchError(
            f"family {plan.family} root is partial, different, or invalid: {root}: {exc}"
        ) from exc
    if run_kind != plan.run_kind or spec.profile_id != plan.profile_id:
        raise CompositionFullLaunchError(f"family {plan.family} root has the wrong profile")
    if not isinstance(spec, V2E2ScaleRunSpec | V2D0ScaleRunSpec):
        raise CompositionFullLaunchError(f"family {plan.family} root is not schema 3")
    expected_profile_id, expected_profile_hash = _profile_hash(Path(plan.profile_path))
    if expected_profile_id != plan.profile_id or spec.profile_config_hash != expected_profile_hash:
        raise CompositionFullLaunchError(f"family {plan.family} profile hash differs")
    if (
        Path(spec.theorem_partition).resolve() != (seed_dir / "theorems.jsonl").resolve()
        or Path(spec.representation_partition).resolve()
        != (seed_dir / "representations.jsonl").resolve()
        or Path(spec.project_dir).resolve() != project_dir.resolve()
        or spec.batch_size != 64
        or spec.max_sources is not None
        or spec.workers != 1
        or spec.memory_hard_limit_mb is not None
        or spec.base_seed != 0
        or spec.import_header_sha256 != _EMPTY_SHA256
        or spec.source_count != _SOURCE_COUNT
        or spec.attempt_count != _SOURCE_COUNT
        or manifest.result_count != _SOURCE_COUNT
        or manifest.resolved_label_count != 0
        or manifest.promoted_item_count != 0
        or manifest.training_eligible is not False
    ):
        raise CompositionFullLaunchError(f"family {plan.family} root violates full settings")
    counts = manifest.terminal_status_counts
    if counts.get("candidate_infrastructure_error", 0) != 0:
        raise CompositionFullLaunchError(f"family {plan.family} root contains infra errors")
    if not log_path.is_file() or log_path.is_symlink():
        raise CompositionFullLaunchError(f"family {plan.family} launcher log is missing")
    return FullRootReceipt(
        family=plan.family,
        run_kind=run_kind,
        profile_id=plan.profile_id,
        root_path=str(root.resolve()),
        reused=reused,
        root_binding_id=loaded.binding.root_binding_id,
        root_tree_hash=loaded.binding.root_tree_hash,
        run_spec_sha256=hash_file(root / "run_spec.json"),
        manifest_sha256=hash_file(root / "manifest.json"),
        results_sha256=hash_file(root / "results.jsonl"),
        terminal_status_counts=dict(sorted(counts.items())),
        provisional_count=loaded.binding.provisional_count,
        log_sha256=hash_file(log_path),
    )


def _family_command(spec: CompositionFullLaunchSpec, plan: FullFamilyPlan) -> tuple[str, ...]:
    operation = (
        "materialize-deterministic-v2-e2-scale"
        if plan.run_kind == "e2"
        else "materialize-deterministic-v2-d0-scale"
    )
    return (
        spec.python_executable,
        "-m",
        "leanfaith.cli.app",
        operation,
        "--root",
        spec.code_root,
        "--theorems",
        str(Path(spec.seed_dir) / "theorems.jsonl"),
        "--representations",
        str(Path(spec.seed_dir) / "representations.jsonl"),
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
    spec: CompositionFullLaunchSpec, prior: Mapping[str, FullFamilyStatus]
) -> CompositionFullStatus:
    return CompositionFullStatus(
        launch_id=spec.launch_id,
        family_statuses=tuple(
            prior.get(
                plan.family,
                FullFamilyStatus(
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


def _build_spec(
    *,
    code_root: Path,
    expected_commit: str,
    seed_dir: Path,
    project_dir: Path,
    output_root: Path,
    python_executable: str,
) -> CompositionFullLaunchSpec:
    code_root = code_root.resolve(strict=True)
    seed_dir = seed_dir.resolve(strict=True)
    project_dir = project_dir.resolve(strict=True)
    output_root = output_root.resolve()
    code_revision, _ = _clean_git_identity(code_root, expected_commit=expected_commit)
    code = collect_code_state(code_root)
    if code.git_dirty or code.git_revision != code_revision or code.code_tree_hash is None:
        raise CompositionFullLaunchError("code-state collection did not preserve the clean pin")
    project_revision, project_tree = _clean_git_identity(project_dir)
    toolchain = project_dir / "lean-toolchain"
    if not toolchain.is_file() or toolchain.is_symlink():
        raise CompositionFullLaunchError("project lacks a regular lean-toolchain file")
    manifest = _load_seed_source(seed_dir)
    for protected in (code_root, seed_dir, project_dir):
        if _overlaps(output_root, protected):
            raise CompositionFullLaunchError(
                f"full output overlaps protected input: {output_root} versus {protected}"
            )
    plans: list[FullFamilyPlan] = []
    for definition in FAMILY_DEFINITIONS:
        profile = (code_root / "configs/transformations" / definition.profile_name).resolve(
            strict=True
        )
        observed_profile_id, _ = _profile_hash(profile)
        if observed_profile_id != definition.profile_id:
            raise CompositionFullLaunchError(f"profile identity changed for {definition.key}")
        plans.append(
            FullFamilyPlan(
                family=definition.key,
                run_kind=definition.run_kind,
                profile_id=definition.profile_id,
                profile_path=str(profile),
                profile_file_sha256=hash_file(profile),
                output_root=str(output_root / definition.key),
            )
        )
    payload: dict[str, object] = {
        "launch_id": f"detcomp_full_launch:{'0' * 64}",
        "code_root": str(code_root),
        "expected_commit": expected_commit,
        "code_tree_hash": code.code_tree_hash,
        "project_dir": str(project_dir),
        "project_revision": project_revision,
        "project_tree": project_tree,
        "lean_toolchain_sha256": hash_file(toolchain),
        "seed_dir": str(seed_dir),
        "seed_manifest_sha256": hash_file(seed_dir / "manifest.json"),
        "seed_set_id": manifest.seed_set_id,
        "seed_partition_sha256": manifest.seed_output_sha256,
        "theorem_partition_sha256": manifest.theorem_output_sha256,
        "representation_partition_sha256": manifest.representation_output_sha256,
        "output_root": str(output_root),
        "families": tuple(plans),
        "python_executable": str(Path(python_executable).absolute()),
    }
    placeholder = CompositionFullLaunchSpec.model_construct(_fields_set=None, **payload)
    launch_id = "detcomp_full_launch:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "launch_id")
    )
    return CompositionFullLaunchSpec.model_validate({**payload, "launch_id": launch_id})


def run_composition_full_scale(
    *,
    code_root: Path,
    expected_commit: str,
    seed_dir: Path,
    project_dir: Path,
    output_root: Path,
    python_executable: str = sys.executable,
    process_executor: FamilyProcessExecutor | None = None,
) -> CompositionFullReceipt:
    """Run or validate/skip all 13 full 3,941-row roots serially."""

    output_root = output_root.resolve()
    spec = _build_spec(
        code_root=code_root,
        expected_commit=expected_commit,
        seed_dir=seed_dir,
        project_dir=project_dir,
        output_root=output_root,
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
            receipt = _load_canonical(receipt_path, CompositionFullReceipt)
            assert isinstance(receipt, CompositionFullReceipt)
            if (
                receipt.launch_id != spec.launch_id
                or receipt.launch_spec_sha256 != hash_file(spec_path)
                or not status_path.is_file()
                or receipt.final_status_sha256 != hash_file(status_path)
            ):
                raise CompositionFullLaunchError("existing full receipt binding changed")
            for plan, expected in zip(spec.families, receipt.roots, strict=True):
                observed = _validate_full_root(
                    plan=plan,
                    seed_dir=seed_dir,
                    project_dir=project_dir,
                    log_path=orchestration / "logs" / f"{plan.family}.log",
                    reused=expected.reused,
                )
                if observed != expected:
                    raise CompositionFullLaunchError("existing full root binding changed")
            return receipt

        prior: dict[str, FullFamilyStatus] = {}
        if status_path.exists():
            old = _load_canonical(status_path, CompositionFullStatus)
            assert isinstance(old, CompositionFullStatus)
            if old.launch_id != spec.launch_id:
                raise CompositionFullLaunchError("existing full status belongs to another launch")
            prior = {item.family: item for item in old.family_statuses}
        _write_atomic(status_path, _canonical_model(_status(spec, prior)))
        roots: list[FullRootReceipt] = []
        for plan in spec.families:
            root = Path(plan.output_root)
            log_path = orchestration / "logs" / f"{plan.family}.log"
            command = _family_command(spec, plan)
            if root.exists():
                if root.is_symlink() or not root.is_dir():
                    raise CompositionFullLaunchError(
                        f"family {plan.family} root is not a real directory: {root}"
                    )
                log_path.parent.mkdir(parents=True, exist_ok=True)
                if not log_path.exists():
                    log_path.write_text(
                        f"validated existing root {root} for launch {spec.launch_id}\n",
                        encoding="utf-8",
                    )
                try:
                    validated = _validate_full_root(
                        plan=plan,
                        seed_dir=seed_dir,
                        project_dir=project_dir,
                        log_path=log_path,
                        reused=True,
                    )
                except CompositionFullLaunchError as exc:
                    # Schema-3 materializers journal immutable batches and can
                    # resume a compatible partial root.  Invoke the exact same
                    # child command; its immutable run-spec checks reject a
                    # conflicting or foreign root before any journal reuse.
                    with log_path.open("ab", buffering=0) as log:
                        log.write(
                            (
                                "existing root is not complete; attempting exact "
                                f"journal resume: {exc}\n"
                            ).encode()
                        )
                else:
                    prior[plan.family] = FullFamilyStatus(
                        family=plan.family,
                        state="reused",
                        root_path=str(root),
                        log_path=str(log_path),
                        command=command,
                        finished_at=_utcnow(),
                        exit_code=0,
                    )
                    roots.append(validated)
                    _write_atomic(status_path, _canonical_model(_status(spec, prior)))
                    continue
            running = FullFamilyStatus(
                family=plan.family,
                state="running",
                root_path=str(root),
                log_path=str(log_path),
                command=command,
                started_at=_utcnow(),
            )
            prior[plan.family] = running
            _write_atomic(status_path, _canonical_model(_status(spec, prior)))
            try:
                exit_code = executor.execute(
                    family=plan.family,
                    command=command,
                    cwd=Path(spec.code_root),
                    log_path=log_path,
                    lock_path=orchestration / "locks" / f"{plan.family}.lock",
                )
            except (KeyboardInterrupt, CompositionSmokeLaunchError) as exc:
                executor.terminate()
                prior[plan.family] = running.model_copy(
                    update={
                        "state": "failed",
                        "finished_at": _utcnow(),
                        "error": f"materializer interrupted: {exc}",
                    }
                )
                _write_atomic(status_path, _canonical_model(_status(spec, prior)))
                raise CompositionFullLaunchError(
                    f"full launch interrupted during {plan.family}; a compatible partial "
                    "root is retained for exact journal resume"
                ) from exc
            if exit_code != 0:
                prior[plan.family] = running.model_copy(
                    update={
                        "state": "failed",
                        "finished_at": _utcnow(),
                        "exit_code": exit_code,
                        "error": f"child exited with code {exit_code}",
                    }
                )
                _write_atomic(status_path, _canonical_model(_status(spec, prior)))
                raise CompositionFullLaunchError(
                    f"family {plan.family} exited {exit_code}; see {log_path}"
                )
            try:
                validated = _validate_full_root(
                    plan=plan,
                    seed_dir=seed_dir,
                    project_dir=project_dir,
                    log_path=log_path,
                    reused=False,
                )
            except CompositionFullLaunchError as exc:
                prior[plan.family] = running.model_copy(
                    update={
                        "state": "failed",
                        "finished_at": _utcnow(),
                        "exit_code": 0,
                        "error": f"post-run root validation failed: {exc}",
                    }
                )
                _write_atomic(status_path, _canonical_model(_status(spec, prior)))
                raise
            prior[plan.family] = running.model_copy(
                update={"state": "succeeded", "finished_at": _utcnow(), "exit_code": 0}
            )
            roots.append(validated)
            _write_atomic(status_path, _canonical_model(_status(spec, prior)))

        receipt_payload: dict[str, object] = {
            "receipt_id": f"detcomp_full_receipt:{'0' * 64}",
            "launch_id": spec.launch_id,
            "launch_spec_sha256": hash_file(spec_path),
            "final_status_sha256": hash_file(status_path),
            "roots": tuple(roots),
            "completed_at": _utcnow(),
        }
        placeholder = CompositionFullReceipt.model_construct(_fields_set=None, **receipt_payload)
        receipt_id = "detcomp_full_receipt:" + hash_canonical(
            _without_id(placeholder.model_dump(mode="json"), "receipt_id")
        )
        receipt = CompositionFullReceipt.model_validate(
            {**receipt_payload, "receipt_id": receipt_id}
        )
        _write_immutable_or_verify(receipt_path, _canonical_model(receipt))
        return receipt


__all__ = [
    "CompositionFullLaunchError",
    "CompositionFullLaunchSpec",
    "CompositionFullReceipt",
    "CompositionFullStatus",
    "FullFamilyPlan",
    "FullRootReceipt",
    "run_composition_full_scale",
]
