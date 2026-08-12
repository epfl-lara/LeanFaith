"""Artifact-to-corpus orchestration for experimental mixed supervision.

The lower-level :mod:`experimental_mixed_supervision` module deliberately
accepts typed, already-verified objects.  This module is the reproducible I/O
boundary that constructs those objects from frozen artifacts:

* a complete first-hop projection;
* one or more complete, clean LF-022 Codex audits;
* canonical theorem and representation partitions for the LF-022 sources;
* the current representation-aware benchmark denylist; and
* the exact mixed-corpus policy file.

No semantic labels are created here.  The resulting corpus remains
experimental, provisional proxy supervision.  Composition is deliberately
recorded as ``omitted_pending_receipt`` until a receipt-bound export exists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from leanfaith.config import hash_file, load_config
from leanfaith.datasets.denylist import (
    LF016_AUTHORIZATION_PATH,
    REPRESENTATION_SIGNATURE_MANIFEST_PATH,
    ActiveBenchmarkRegistry,
    load_active_benchmark_registry,
)
from leanfaith.datasets.experimental_first_hop_projection import (
    ExperimentalFirstHopProjectionManifest,
    load_selectable_experimental_first_hop_projection,
    verify_experimental_first_hop_projection,
)
from leanfaith.datasets.experimental_mixed_supervision import (
    ExperimentalMixedCandidate,
    ExperimentalMixedExclusion,
    ExperimentalMixedInputBinding,
    ExperimentalMixedSupervisionArtifacts,
    ExperimentalMixedSupervisionConfig,
    ExperimentalMixedSupervisionError,
    ExperimentalMixedSupervisionManifest,
    adapt_selectable_first_hop_projection,
    adapt_verified_lf022_codex_audit,
    bind_experimental_mixed_input,
    freeze_experimental_mixed_supervision,
    verify_experimental_mixed_supervision,
)
from leanfaith.generation.lf022_codex_audit import (
    LF022VerifiedCodexAudit,
    verify_completed_lf022_codex_audit,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord

_ARTIFACT_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_THEOREM_ID_IN_JSON = re.compile(rb'"theorem_id"\s*:\s*"(thm:[0-9a-f]{64})"')


@dataclass(frozen=True, slots=True)
class ExperimentalLF022AuditSource:
    """One named audit and the repository root used by its relative artifacts."""

    name: str
    repo_root: Path
    checks_path: Path
    audit_root: Path
    parent_audit_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if _ARTIFACT_NAME.fullmatch(self.name) is None:
            raise ExperimentalMixedSupervisionError(
                "LF-022 audit name must match [a-z0-9][a-z0-9_.-]*"
            )
        resolved = tuple(path.resolve() for path in self.parent_audit_roots)
        if len(resolved) != len(set(resolved)):
            raise ExperimentalMixedSupervisionError("LF-022 parent audit roots must be unique")


@dataclass(frozen=True, slots=True)
class ExperimentalMixedOrchestrationResult:
    """A frozen corpus plus transparent source-level construction counts."""

    artifacts: ExperimentalMixedSupervisionArtifacts
    first_hop_input_count: int
    first_hop_candidate_count: int
    lf022_audit_count: int
    lf022_judgment_count: int
    lf022_candidate_count: int
    adapter_exclusion_count: int
    input_binding_count: int


class _InputBindings:
    """Build deterministic, path-distinct freezer bindings."""

    def __init__(self) -> None:
        self._values: dict[str, ExperimentalMixedInputBinding] = {}

    def add(
        self,
        name: str,
        path: Path,
        *,
        partition: Literal["first_hop", "lf022_codex", "composition", "policy"],
    ) -> None:
        if not name or name in self._values:
            raise ExperimentalMixedSupervisionError(
                f"duplicate or empty mixed input binding name: {name!r}"
            )
        self._values[name] = bind_experimental_mixed_input(path, partition=partition)

    def finish(self) -> dict[str, ExperimentalMixedInputBinding]:
        return dict(sorted(self._values.items()))


def _resolve_from(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _regular_files_below(root: Path) -> tuple[Path, ...]:
    """Return every regular file and reject symlinks anywhere in the tree."""

    if root.is_symlink() or not root.is_dir():
        raise ExperimentalMixedSupervisionError(f"artifact root is not a real directory: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ExperimentalMixedSupervisionError(f"artifact tree contains a symlink: {path}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ExperimentalMixedSupervisionError(
                f"artifact tree contains a non-file entry: {path}"
            )
    return tuple(files)


def _add_tree_bindings(
    bindings: _InputBindings,
    *,
    prefix: str,
    root: Path,
    partition: Literal["first_hop", "lf022_codex"],
) -> None:
    for path in _regular_files_below(root):
        bindings.add(
            f"{prefix}/{path.relative_to(root).as_posix()}",
            path,
            partition=partition,
        )


def _add_benchmark_bindings(
    bindings: _InputBindings,
    *,
    registry: ActiveBenchmarkRegistry,
    authorization_path: Path | None,
) -> None:
    paths = {
        "benchmark/representation_manifest": registry.manifest_path,
        "benchmark/base_registry": registry.base_registry_path,
        "benchmark/active_registry": registry.active_registry_path,
        "benchmark/detailed_index": registry.detailed_index_path,
        "benchmark/input_manifest": registry.input_manifest_path,
        "benchmark/code_bundle": registry.code_bundle_path,
    }
    if authorization_path is not None:
        paths["benchmark/authorization"] = authorization_path
    for name, path in sorted(paths.items()):
        bindings.add(name, path, partition="policy")


def _load_target_records[ModelT: TheoremRecord | RepresentationRecord](
    paths: Sequence[Path],
    *,
    target_theorem_ids: frozenset[str],
    model: type[ModelT],
    wrapper_key: Literal["theorem", "representation"],
) -> dict[str, ModelT]:
    """Stream huge JSONL partitions and parse only requested theorem rows.

    Representation partitions can contain exceptionally large operator trees.
    A byte-level theorem-ID prefilter prevents unrelated records from ever
    becoming Python/Pydantic objects.
    """

    found: dict[str, ModelT] = {}
    if not target_theorem_ids:
        return found
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ExperimentalMixedSupervisionError(f"source partition is absent: {path}")
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.endswith(b"\n") or not raw.strip():
                    raise ExperimentalMixedSupervisionError(
                        f"invalid source JSONL framing at {path}:{line_number}"
                    )
                matches = {item.decode("ascii") for item in _THEOREM_ID_IN_JSON.findall(raw)}
                selected_ids = matches & target_theorem_ids
                if not selected_ids:
                    continue
                if len(selected_ids) != 1:
                    raise ExperimentalMixedSupervisionError(
                        f"source row contains multiple requested theorem IDs at "
                        f"{path}:{line_number}"
                    )
                selected_id = next(iter(selected_ids))
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise TypeError("row is not an object")
                    selected = payload.get(wrapper_key, payload)
                    if not isinstance(selected, dict):
                        raise TypeError(f"{wrapper_key} wrapper is not an object")
                    record = cast(ModelT, model.model_validate(selected))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ExperimentalMixedSupervisionError(
                        f"invalid {model.__name__} at {path}:{line_number}: {exc}"
                    ) from exc
                theorem_id = record.theorem_id
                if theorem_id != selected_id:
                    raise ExperimentalMixedSupervisionError(
                        f"prefilter/model theorem ID mismatch at {path}:{line_number}"
                    )
                if theorem_id in found:
                    raise ExperimentalMixedSupervisionError(
                        f"canonical source theorem ID appears more than once: {theorem_id}"
                    )
                found[theorem_id] = record
    missing = sorted(target_theorem_ids - set(found))
    if missing:
        preview = ", ".join(missing[:5])
        raise ExperimentalMixedSupervisionError(
            f"canonical source partition lacks {len(missing)} requested theorem IDs: {preview}"
        )
    return found


def _verify_audits(
    audit_sources: Sequence[ExperimentalLF022AuditSource],
) -> tuple[LF022VerifiedCodexAudit, ...]:
    names = [source.name for source in audit_sources]
    if len(names) != len(set(names)):
        raise ExperimentalMixedSupervisionError("LF-022 audit names must be unique")
    verified: list[LF022VerifiedCodexAudit] = []
    for source in sorted(audit_sources, key=lambda item: item.name):
        verified.append(
            verify_completed_lf022_codex_audit(
                repo_root=source.repo_root,
                checks_path=source.checks_path,
                audit_root=source.audit_root,
                require_complete_clean=True,
                parent_audit_roots=source.parent_audit_roots,
            )
        )
    return tuple(verified)


def _source_theorem_ids(
    audits: Sequence[LF022VerifiedCodexAudit],
) -> frozenset[str]:
    theorem_ids: set[str] = set()
    for audit in audits:
        for judgment in audit.judgments:
            selected = tuple(
                value for value in judgment.source_record_ids if value.startswith("thm:")
            )
            if len(selected) != 1:
                raise ExperimentalMixedSupervisionError(
                    "verified LF-022 judgment lacks exactly one canonical source theorem ID"
                )
            theorem_ids.add(selected[0])
    return frozenset(theorem_ids)


def _bind_lf022_source_artifacts(
    bindings: _InputBindings,
    *,
    audit_source: ExperimentalLF022AuditSource,
    verified: LF022VerifiedCodexAudit,
) -> None:
    """Bind audit bytes plus variant/task/raw-Lean artifacts used by verification."""

    _add_tree_bindings(
        bindings,
        prefix=f"lf022/{audit_source.name}/audit",
        root=audit_source.audit_root,
        partition="lf022_codex",
    )
    verified_parents = {
        Path(binding.audit_root).resolve(): binding for binding in verified.parent_audit_bindings
    }
    declared_parents = tuple(path.resolve() for path in audit_source.parent_audit_roots)
    if set(verified_parents) != set(declared_parents):
        raise ExperimentalMixedSupervisionError(
            "verified LF-022 parent bindings differ from declared parent audit roots"
        )
    for index, parent_root in enumerate(sorted(declared_parents)):
        parent_binding = verified_parents[parent_root]
        parent_manifest = parent_root / "manifest.json"
        if hash_file(parent_manifest) != parent_binding.manifest_sha256:
            raise ExperimentalMixedSupervisionError(
                f"LF-022 parent manifest hash differs: {parent_manifest}"
            )
        _add_tree_bindings(
            bindings,
            prefix=f"lf022/{audit_source.name}/parent{index:02d}",
            root=parent_root,
            partition="lf022_codex",
        )
    bindings.add(
        f"lf022/{audit_source.name}/checks",
        audit_source.checks_path,
        partition="lf022_codex",
    )
    extra_paths: dict[str, Path] = {}
    for check in verified.checks:
        variant_path = _resolve_from(audit_source.repo_root, Path(check.source_variant_artifact))
        extra_paths[f"variant/{check.source_variant_artifact_sha256}"] = variant_path
        task_path = variant_path.with_name("task.json")
        extra_paths[f"task/{hash_file(task_path)}"] = task_path
        for attempt in check.attempts:
            if attempt.raw_response_path is None:
                continue
            raw_path = _resolve_from(audit_source.repo_root, Path(attempt.raw_response_path))
            if hash_file(raw_path) != attempt.raw_response_sha256:
                raise ExperimentalMixedSupervisionError(
                    f"LF-022 Lean raw response hash differs: {raw_path}"
                )
            extra_paths[f"lean_raw/{attempt.raw_response_sha256}"] = raw_path
    for suffix, path in sorted(extra_paths.items()):
        bindings.add(
            f"lf022/{audit_source.name}/{suffix}",
            path,
            partition="lf022_codex",
        )


def _load_registry(
    *,
    repo_root: Path,
    manifest_path: Path | None,
    expected_manifest_sha256: str | None,
    authorization_path: Path | None,
) -> tuple[ActiveBenchmarkRegistry, Path | None]:
    effective_manifest = _resolve_from(
        repo_root,
        manifest_path or REPRESENTATION_SIGNATURE_MANIFEST_PATH,
    )
    effective_authorization: Path | None = None
    if expected_manifest_sha256 is None:
        effective_authorization = _resolve_from(
            repo_root,
            authorization_path or LF016_AUTHORIZATION_PATH,
        )
    registry = load_active_benchmark_registry(
        effective_manifest,
        repo_root=repo_root,
        expected_manifest_sha256=expected_manifest_sha256,
        authorization_path=effective_authorization,
    )
    return registry, effective_authorization


def _assemble_and_freeze(
    *,
    repo_root: Path,
    output_dir: Path,
    config_path: Path,
    first_hop_projection_dir: Path,
    lf022_audits: Sequence[ExperimentalLF022AuditSource],
    source_theorem_paths: Sequence[Path],
    source_representation_paths: Sequence[Path],
    benchmark_manifest_path: Path | None,
    benchmark_expected_manifest_sha256: str | None,
    benchmark_authorization_path: Path | None,
) -> ExperimentalMixedOrchestrationResult:
    repo_root = repo_root.resolve()
    config_path = _resolve_from(repo_root, config_path)
    first_hop_projection_dir = first_hop_projection_dir.resolve()
    theorem_paths = tuple(path.resolve() for path in source_theorem_paths)
    representation_paths = tuple(path.resolve() for path in source_representation_paths)
    if not theorem_paths or not representation_paths:
        raise ExperimentalMixedSupervisionError(
            "LF-022 orchestration requires theorem and representation partitions"
        )

    loaded = load_config(config_path, ExperimentalMixedSupervisionConfig)
    config = loaded.config
    if (
        config.first_hop_partition != "included"
        or config.lf022_codex_partition != "included"
        or config.composition_partition != "omitted_pending_receipt"
    ):
        raise ExperimentalMixedSupervisionError(
            "this orchestration requires included first-hop/LF-022 partitions and "
            "composition=omitted_pending_receipt"
        )
    if not lf022_audits:
        raise ExperimentalMixedSupervisionError("at least one complete LF-022 audit is required")

    registry, effective_authorization = _load_registry(
        repo_root=repo_root,
        manifest_path=benchmark_manifest_path,
        expected_manifest_sha256=benchmark_expected_manifest_sha256,
        authorization_path=benchmark_authorization_path,
    )
    first_hop_manifest: ExperimentalFirstHopProjectionManifest = (
        verify_experimental_first_hop_projection(
            first_hop_projection_dir,
            verify_external_inputs=True,
        )
    )
    current_registry_sha256 = hash_file(registry.active_registry_path)
    if first_hop_manifest.config.benchmark_active_registry_sha256 != current_registry_sha256:
        raise ExperimentalMixedSupervisionError(
            "first-hop projection was screened against a different active benchmark registry"
        )
    first_hop_records = load_selectable_experimental_first_hop_projection(
        first_hop_projection_dir,
        allow_experimental_first_hop_projection=True,
        purpose="mixed_proxy_construction",
    )
    if len(first_hop_records) != first_hop_manifest.selectable_count:
        raise ExperimentalMixedSupervisionError(
            "first-hop selectable partition differs from its verified manifest"
        )

    verified_audits = _verify_audits(lf022_audits)
    target_ids = _source_theorem_ids(verified_audits)
    theorem_models = _load_target_records(
        theorem_paths,
        target_theorem_ids=target_ids,
        model=TheoremRecord,
        wrapper_key="theorem",
    )
    representation_models = _load_target_records(
        representation_paths,
        target_theorem_ids=target_ids,
        model=RepresentationRecord,
        wrapper_key="representation",
    )
    source_theorems = dict(theorem_models)
    source_representations = dict(representation_models)

    candidates: list[ExperimentalMixedCandidate] = []
    exclusions: list[ExperimentalMixedExclusion] = []
    first_hop_candidate_count = 0
    for record in first_hop_records:
        adapted = adapt_selectable_first_hop_projection(
            record,
            benchmark_registry=registry,
        )
        candidates.extend(adapted.candidates)
        exclusions.extend(adapted.exclusions)
        first_hop_candidate_count += len(adapted.candidates)

    lf022_candidate_count = 0
    for verified in verified_audits:
        adapted = adapt_verified_lf022_codex_audit(
            verified,
            source_theorems=source_theorems,
            source_representations=source_representations,
            benchmark_registry=registry,
        )
        candidates.extend(adapted.candidates)
        exclusions.extend(adapted.exclusions)
        lf022_candidate_count += len(adapted.candidates)

    bindings = _InputBindings()
    bindings.add("policy/mixed_config", config_path, partition="policy")
    _add_benchmark_bindings(
        bindings,
        registry=registry,
        authorization_path=effective_authorization,
    )
    _add_tree_bindings(
        bindings,
        prefix="first_hop/projection",
        root=first_hop_projection_dir,
        partition="first_hop",
    )
    for name, binding in sorted(first_hop_manifest.inputs.items()):
        bindings.add(
            f"first_hop/upstream/{name}",
            Path(binding.path),
            partition="first_hop",
        )
    for audit_source, verified in zip(
        sorted(lf022_audits, key=lambda item: item.name),
        verified_audits,
        strict=True,
    ):
        _bind_lf022_source_artifacts(
            bindings,
            audit_source=audit_source,
            verified=verified,
        )
    for index, path in enumerate(theorem_paths):
        bindings.add(
            f"lf022/source_theorems/{index:04d}",
            path,
            partition="lf022_codex",
        )
    for index, path in enumerate(representation_paths):
        bindings.add(
            f"lf022/source_representations/{index:04d}",
            path,
            partition="lf022_codex",
        )
    frozen_bindings = bindings.finish()
    artifacts = freeze_experimental_mixed_supervision(
        repo_root=repo_root,
        output_dir=output_dir,
        config=config,
        candidates=tuple(candidates),
        adapter_exclusions=tuple(exclusions),
        inputs=frozen_bindings,
    )
    return ExperimentalMixedOrchestrationResult(
        artifacts=artifacts,
        first_hop_input_count=len(first_hop_records),
        first_hop_candidate_count=first_hop_candidate_count,
        lf022_audit_count=len(verified_audits),
        lf022_judgment_count=sum(len(audit.judgments) for audit in verified_audits),
        lf022_candidate_count=lf022_candidate_count,
        adapter_exclusion_count=len(exclusions),
        input_binding_count=len(frozen_bindings),
    )


def freeze_experimental_mixed_supervision_from_artifacts(
    *,
    repo_root: Path,
    output_dir: Path,
    config_path: Path,
    first_hop_projection_dir: Path,
    lf022_audits: Sequence[ExperimentalLF022AuditSource],
    source_theorem_paths: Sequence[Path],
    source_representation_paths: Sequence[Path],
    benchmark_manifest_path: Path | None = None,
    benchmark_expected_manifest_sha256: str | None = None,
    benchmark_authorization_path: Path | None = None,
) -> ExperimentalMixedOrchestrationResult:
    """Verify every source and freeze (or exactly replay) the mixed corpus."""

    return _assemble_and_freeze(
        repo_root=repo_root,
        output_dir=output_dir,
        config_path=config_path,
        first_hop_projection_dir=first_hop_projection_dir,
        lf022_audits=lf022_audits,
        source_theorem_paths=source_theorem_paths,
        source_representation_paths=source_representation_paths,
        benchmark_manifest_path=benchmark_manifest_path,
        benchmark_expected_manifest_sha256=benchmark_expected_manifest_sha256,
        benchmark_authorization_path=benchmark_authorization_path,
    )


def replay_verify_experimental_mixed_supervision_from_artifacts(
    *,
    repo_root: Path,
    output_dir: Path,
    config_path: Path,
    first_hop_projection_dir: Path,
    lf022_audits: Sequence[ExperimentalLF022AuditSource],
    source_theorem_paths: Sequence[Path],
    source_representation_paths: Sequence[Path],
    benchmark_manifest_path: Path | None = None,
    benchmark_expected_manifest_sha256: str | None = None,
    benchmark_authorization_path: Path | None = None,
) -> ExperimentalMixedOrchestrationResult:
    """Reassemble all sources and require byte-identical existing output."""

    result = _assemble_and_freeze(
        repo_root=repo_root,
        output_dir=output_dir,
        config_path=config_path,
        first_hop_projection_dir=first_hop_projection_dir,
        lf022_audits=lf022_audits,
        source_theorem_paths=source_theorem_paths,
        source_representation_paths=source_representation_paths,
        benchmark_manifest_path=benchmark_manifest_path,
        benchmark_expected_manifest_sha256=benchmark_expected_manifest_sha256,
        benchmark_authorization_path=benchmark_authorization_path,
    )
    if not result.artifacts.replayed:
        raise ExperimentalMixedSupervisionError(
            "replay verification unexpectedly created a new corpus"
        )
    verify_experimental_mixed_supervision(output_dir, verify_external_inputs=True)
    return result


def verify_frozen_experimental_mixed_supervision(
    output_dir: Path,
) -> ExperimentalMixedSupervisionManifest:
    """Verify frozen bytes, external bindings, split unions, and policy invariants."""

    return verify_experimental_mixed_supervision(
        output_dir,
        verify_external_inputs=True,
    )


__all__ = [
    "ExperimentalLF022AuditSource",
    "ExperimentalMixedOrchestrationResult",
    "freeze_experimental_mixed_supervision_from_artifacts",
    "replay_verify_experimental_mixed_supervision_from_artifacts",
    "verify_frozen_experimental_mixed_supervision",
]
