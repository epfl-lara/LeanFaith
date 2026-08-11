"""Narrow LF-024 batch label-resolution operation.

The operation consumes explicit target, linked-evidence, admission, and
candidate partitions and delegates every semantic decision to
:func:`leanfaith.labeling.resolution.resolve_target`.
It does not construct, infer, repair, or promote ``ResolutionCandidate``
records.  Missing, orphaned, mixed-kind, and cross-target inputs fail before
any output partition is written.  This foundation is diagnostic-only until
typed adapters independently verify authority and admission artifact content.

CLI registration intentionally lives elsewhere; this module exposes a tested
operation that a later Typer command can call without duplicating policy or
lineage behavior.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import shutil
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.labeling.aggregation import EvidenceAdmissionRecord
from leanfaith.labeling.quality import (
    ResolutionCandidate,
    load_active_label_resolution_policy,
)
from leanfaith.labeling.resolution import (
    ResolutionInputError,
    ResolutionTarget,
    resolve_target,
    verify_resolution_artifacts,
)
from leanfaith.schemas.enums import (
    ArtifactClass,
    EvidenceTargetKind,
    ResolutionOutcome,
    SemanticLabelTargetKind,
)
from leanfaith.schemas.evidence import EvidenceRecord
from leanfaith.schemas.ids import id_pattern, make_id
from leanfaith.schemas.label import ResolvedLabel
from leanfaith.schemas.manifest import (
    CodeState,
    RunManifest,
    collect_code_state,
    new_run_id,
    run_manifest_path,
    write_manifest,
)
from leanfaith.schemas.nl_lean import NLPLeanRecord
from leanfaith.schemas.pair import PairRecord


class LabelResolutionBatchInputError(ValueError):
    """The explicit batch graph is malformed, incomplete, or ambiguous."""


@dataclass(frozen=True, slots=True)
class LabelResolutionBatchArtifacts:
    """Paths and counts emitted by one externally committed batch."""

    run_id: str
    target_kind: SemanticLabelTargetKind
    output_dir: Path
    linked_targets_path: Path
    labels_path: Path
    audits_path: Path
    derivations_path: Path
    conflicts_path: Path
    overrides_path: Path
    run_manifest_path: Path
    run_manifest_sha256: str
    target_count: int
    resolved_count: int
    unresolved_count: int
    derivation_count: int
    conflict_count: int
    override_count: int


_COMMIT_CONTROL_FILE = ".leanfaith_lf024_commit_control.json"
_COMMIT_CONTROL_PREFIX = "lf024_commit_control"


class LabelResolutionCommitControl(StrictModel):
    """Resolver-owned marker for an output awaiting its external manifest.

    The descriptor deliberately lives inside the output directory and is not
    itself included in the run manifest's semantic output hashes: it contains
    the expected hash of that manifest.  The external run manifest is the sole
    commit marker.
    """

    schema_version: Literal[1] = 1
    control_id: str = Field(pattern=id_pattern(_COMMIT_CONTROL_PREFIX))
    kind: Literal["lf024_diagnostic_output_commit_control"] = (
        "lf024_diagnostic_output_commit_control"
    )
    run_id: str = Field(pattern=r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
    expected_manifest_relative_path: str
    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_path_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        expected_path = f"runs/{self.run_id}/manifest.json"
        if self.expected_manifest_relative_path != expected_path:
            raise ValueError(
                "expected_manifest_relative_path must name the run's external manifest"
            )
        expected_id = make_id(
            _COMMIT_CONTROL_PREFIX,
            self.model_dump(mode="json", exclude={"control_id"}),
        )
        if self.control_id != expected_id:
            raise ValueError("control_id does not match commit-control content")
        return self


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _load_jsonl[ModelT: StrictModel](
    path: Path,
    model_type: type[ModelT],
    *,
    record_kind: str,
) -> tuple[ModelT, ...]:
    records: list[ModelT] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise LabelResolutionBatchInputError(
            f"cannot read {record_kind} input {path}: {exc}"
        ) from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
                if not isinstance(value, dict):
                    raise ValueError("expected a JSON object")
                records.append(model_type.model_validate(value))
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise LabelResolutionBatchInputError(
                    f"{path}:{line_number}: invalid {record_kind}: {exc}"
                ) from exc
    return tuple(records)


def _load_targets(
    path: Path,
) -> tuple[SemanticLabelTargetKind, tuple[ResolutionTarget, ...]]:
    targets: list[ResolutionTarget] = []
    observed_kind: SemanticLabelTargetKind | None = None
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise LabelResolutionBatchInputError(f"cannot read target input {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
                if not isinstance(value, dict):
                    raise ValueError("expected a JSON object")
                has_pair_id = "pair_id" in value
                has_nl_lean_id = "nl_lean_id" in value
                if has_pair_id == has_nl_lean_id:
                    raise ValueError("target must contain exactly one of pair_id or nl_lean_id")
                if has_pair_id:
                    kind = SemanticLabelTargetKind.LEAN_PAIR
                    target: ResolutionTarget = PairRecord.model_validate(value)
                else:
                    kind = SemanticLabelTargetKind.NL_LEAN
                    target = NLPLeanRecord.model_validate(value)
                if observed_kind is not None and kind is not observed_kind:
                    raise ValueError("PairRecord and NLPLeanRecord inputs cannot be mixed")
                observed_kind = kind
                targets.append(target)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise LabelResolutionBatchInputError(
                    f"{path}:{line_number}: invalid resolution target: {exc}"
                ) from exc
    if observed_kind is None:
        raise LabelResolutionBatchInputError("target input must contain at least one record")
    return observed_kind, tuple(targets)


def _index_unique[ModelT: StrictModel](
    records: Iterable[ModelT],
    *,
    identifier: Callable[[ModelT], str],
    record_kind: str,
) -> dict[str, ModelT]:
    indexed: dict[str, ModelT] = {}
    for record in records:
        record_id = identifier(record)
        if record_id in indexed:
            raise LabelResolutionBatchInputError(f"duplicate {record_kind} ID {record_id!r}")
        indexed[record_id] = record
    return indexed


def _target_id(target: ResolutionTarget) -> str:
    return target.pair_id if isinstance(target, PairRecord) else target.nl_lean_id


def _expected_evidence_kind(kind: SemanticLabelTargetKind) -> EvidenceTargetKind:
    if kind is SemanticLabelTargetKind.LEAN_PAIR:
        return EvidenceTargetKind.LEAN_PAIR
    return EvidenceTargetKind.NL_LEAN


def _group_closed_inputs(
    *,
    target_kind: SemanticLabelTargetKind,
    targets: Sequence[ResolutionTarget],
    evidence: Sequence[EvidenceRecord],
    admissions: Sequence[EvidenceAdmissionRecord],
    candidates: Sequence[ResolutionCandidate],
    prior_labels: Sequence[ResolvedLabel],
) -> tuple[
    dict[str, ResolutionTarget],
    dict[str, tuple[EvidenceRecord, ...]],
    dict[str, tuple[EvidenceAdmissionRecord, ...]],
    dict[str, tuple[ResolutionCandidate, ...]],
    dict[str, ResolvedLabel],
]:
    target_by_id = _index_unique(
        targets,
        identifier=_target_id,
        record_kind="target",
    )
    expected_evidence_kind = _expected_evidence_kind(target_kind)

    grouped_evidence: defaultdict[str, list[EvidenceRecord]] = defaultdict(list)
    evidence_by_id = _index_unique(
        evidence,
        identifier=lambda item: item.evidence_id,
        record_kind="evidence",
    )
    for record in evidence_by_id.values():
        if record.target_kind is not expected_evidence_kind:
            raise LabelResolutionBatchInputError(
                f"evidence {record.evidence_id} has kind {record.target_kind.value}, "
                f"expected {expected_evidence_kind.value}"
            )
        if record.target_id not in target_by_id:
            raise LabelResolutionBatchInputError(
                f"evidence {record.evidence_id} targets absent item {record.target_id}"
            )
        grouped_evidence[record.target_id].append(record)

    grouped_admissions: defaultdict[str, list[EvidenceAdmissionRecord]] = defaultdict(list)
    admission_by_id = _index_unique(
        admissions,
        identifier=lambda item: item.admission_id,
        record_kind="evidence admission",
    )
    for admission in admission_by_id.values():
        if admission.target_kind is not expected_evidence_kind:
            raise LabelResolutionBatchInputError(
                f"admission {admission.admission_id} has kind {admission.target_kind.value}, "
                f"expected {expected_evidence_kind.value}"
            )
        if admission.target_id not in target_by_id:
            raise LabelResolutionBatchInputError(
                f"admission {admission.admission_id} targets absent item {admission.target_id}"
            )
        grouped_admissions[admission.target_id].append(admission)

    grouped_candidates: defaultdict[str, list[ResolutionCandidate]] = defaultdict(list)
    candidate_by_id = _index_unique(
        candidates,
        identifier=lambda item: item.candidate_id,
        record_kind="resolution candidate",
    )
    for candidate in candidate_by_id.values():
        if candidate.target_kind is not target_kind:
            raise LabelResolutionBatchInputError(
                f"candidate {candidate.candidate_id} has kind {candidate.target_kind.value}, "
                f"expected {target_kind.value}"
            )
        if candidate.target_id not in target_by_id:
            raise LabelResolutionBatchInputError(
                f"candidate {candidate.candidate_id} targets absent item {candidate.target_id}"
            )
        grouped_candidates[candidate.target_id].append(candidate)

    prior_by_id = _index_unique(
        prior_labels,
        identifier=lambda item: item.label_id,
        record_kind="prior resolved label",
    )
    prior_by_target: dict[str, ResolvedLabel] = {}
    for label in prior_by_id.values():
        if label.target_kind is not target_kind:
            raise LabelResolutionBatchInputError(
                f"prior label {label.label_id} has kind {label.target_kind.value}, "
                f"expected {target_kind.value}"
            )
        if label.target_id not in target_by_id:
            raise LabelResolutionBatchInputError(
                f"prior label {label.label_id} targets absent item {label.target_id}"
            )
        if label.target_id in prior_by_target:
            raise LabelResolutionBatchInputError(
                f"multiple prior labels supplied for target {label.target_id}"
            )
        prior_by_target[label.target_id] = label

    # Require an exact closed evidence set for each target before invoking the
    # single-target resolver.  This gives batch callers target-local diagnostics
    # and rejects omitted linked evidence even when every supplied record is valid.
    for target_id, target in target_by_id.items():
        supplied = {item.evidence_id for item in grouped_evidence[target_id]}
        linked = set(target.evidence_ids)
        if supplied != linked:
            missing = sorted(linked - supplied)
            extra = sorted(supplied - linked)
            raise LabelResolutionBatchInputError(
                f"target {target_id} evidence set is not closed; missing={missing}, extra={extra}"
            )

    return (
        target_by_id,
        {
            key: tuple(sorted(value, key=lambda item: item.evidence_id))
            for key, value in grouped_evidence.items()
        },
        {
            key: tuple(sorted(value, key=lambda item: item.admission_id))
            for key, value in grouped_admissions.items()
        },
        {
            key: tuple(sorted(value, key=lambda item: item.candidate_id))
            for key, value in grouped_candidates.items()
        },
        prior_by_target,
    )


def _write_jsonl(records: Sequence[StrictModel], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_bytes(
        b"".join(canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records)
    )
    os.replace(partial, path)
    return hash_file(path)


def _remove_transaction_path(path: Path) -> None:
    """Remove one resolver-owned staging/publication path if it exists."""

    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _output_path_sha256(path: Path) -> str:
    return sha256_hex(str(path.resolve()).encode("utf-8"))


def _build_commit_control(
    *,
    run_id: str,
    manifest_sha256: str,
    output_dir: Path,
) -> LabelResolutionCommitControl:
    payload = {
        "schema_version": 1,
        "kind": "lf024_diagnostic_output_commit_control",
        "run_id": run_id,
        "expected_manifest_relative_path": f"runs/{run_id}/manifest.json",
        "expected_manifest_sha256": manifest_sha256,
        "output_path_sha256": _output_path_sha256(output_dir),
    }
    return LabelResolutionCommitControl.model_validate(
        {
            "control_id": make_id(_COMMIT_CONTROL_PREFIX, payload),
            **payload,
        }
    )


def _load_commit_control(output_dir: Path) -> LabelResolutionCommitControl:
    path = output_dir / _COMMIT_CONTROL_FILE
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        return LabelResolutionCommitControl.model_validate(value)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise LabelResolutionBatchInputError(
            f"nonempty output has no valid resolver commit control: {output_dir}: {exc}"
        ) from exc


def _prepare_output_destination(*, paths: RepoPaths, output_dir: Path) -> tuple[bool, Path | None]:
    """Validate output, or atomically quarantine a recognized orphan."""

    if not output_dir.exists():
        return False, None
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise LabelResolutionBatchInputError(f"output path is not a plain directory: {output_dir}")
    if not any(output_dir.iterdir()):
        return True, None

    control = _load_commit_control(output_dir)
    if control.output_path_sha256 != _output_path_sha256(output_dir):
        raise LabelResolutionBatchInputError(
            f"resolver commit control does not bind output path: {output_dir}"
        )
    expected_manifest = paths.root / control.expected_manifest_relative_path
    if (
        expected_manifest.is_file()
        and not expected_manifest.is_symlink()
        and hash_file(expected_manifest) == control.expected_manifest_sha256
    ):
        raise LabelResolutionBatchInputError(
            f"output directory is already externally committed: {output_dir}"
        )

    quarantine = output_dir.parent / (
        f"{output_dir.name}.orphan-{control.run_id}-{control.control_id.split(':', 1)[1][:12]}"
    )
    if quarantine.exists():
        raise LabelResolutionBatchInputError(f"orphan quarantine already exists: {quarantine}")
    os.replace(output_dir, quarantine)
    if expected_manifest.exists() or expected_manifest.is_symlink():
        os.replace(expected_manifest, quarantine / "mismatched_external_manifest.json")
    stale_staging_manifest = expected_manifest.parent / (
        f".{expected_manifest.name}.{control.run_id}.partial"
    )
    if stale_staging_manifest.exists() or stale_staging_manifest.is_symlink():
        os.replace(stale_staging_manifest, quarantine / "stale_staging_manifest.json")
    return False, quarantine


def _display_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _hash_inputs(
    inputs: Sequence[tuple[str, Path]],
    *,
    root: Path,
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for role, path in inputs:
        key = f"{role}::{_display_path(path, root)}"
        try:
            hashes[key] = hash_file(path)
        except OSError as exc:
            raise LabelResolutionBatchInputError(f"cannot hash {role} input {path}: {exc}") from exc
    return hashes


def _resolve_label_batch_locked(
    *,
    paths: RepoPaths,
    target_path: Path,
    evidence_path: Path,
    admission_path: Path,
    candidate_path: Path,
    prior_label_path: Path | None = None,
    output_dir: Path | None = None,
    artifact_class: ArtifactClass = ArtifactClass.DIAGNOSTIC,
    resolved_at: datetime.datetime | None = None,
    run_nonce: str | None = None,
    code_state: CodeState | None = None,
) -> LabelResolutionBatchArtifacts:
    """Resolve an explicit PairRecord or NLPLeanRecord JSONL batch.

    All inputs are explicit.  In particular, an empty candidate partition
    produces unresolved REVIEW labels; this operation never turns evidence or
    generation intent into a semantic candidate.
    """

    now = resolved_at or datetime.datetime.now(tz=datetime.UTC)

    # Record and execute against canonical paths so persisted invocation
    # provenance never depends on the caller's working directory. ``resolve``
    # is intentionally non-strict here; the explicit hash/read steps below
    # provide the role-specific missing-input diagnostics.
    paths = RepoPaths(root=paths.root.resolve())
    target_path = target_path.resolve()
    evidence_path = evidence_path.resolve()
    admission_path = admission_path.resolve()
    candidate_path = candidate_path.resolve()
    if prior_label_path is not None:
        prior_label_path = prior_label_path.resolve()
    if output_dir is not None:
        output_dir = output_dir.resolve()

    input_path_items: list[tuple[str, Path]] = [
        ("targets", target_path),
        ("evidence", evidence_path),
        ("admissions", admission_path),
        ("candidates", candidate_path),
    ]
    if prior_label_path is not None:
        input_path_items.append(("prior_labels", prior_label_path))
    input_paths = tuple(input_path_items)
    input_hashes = _hash_inputs(input_paths, root=paths.root)

    target_kind, targets = _load_targets(target_path)
    evidence = _load_jsonl(evidence_path, EvidenceRecord, record_kind="EvidenceRecord")
    admissions = _load_jsonl(
        admission_path,
        EvidenceAdmissionRecord,
        record_kind="EvidenceAdmissionRecord",
    )
    candidates = _load_jsonl(
        candidate_path,
        ResolutionCandidate,
        record_kind="ResolutionCandidate",
    )
    prior_labels = (
        ()
        if prior_label_path is None
        else _load_jsonl(prior_label_path, ResolvedLabel, record_kind="prior ResolvedLabel")
    )
    (
        target_by_id,
        evidence_by_target,
        admissions_by_target,
        candidates_by_target,
        prior_by_target,
    ) = _group_closed_inputs(
        target_kind=target_kind,
        targets=targets,
        evidence=evidence,
        admissions=admissions,
        candidates=candidates,
        prior_labels=prior_labels,
    )

    policy = load_active_label_resolution_policy(paths.root)
    artifacts = []
    for target_id in sorted(target_by_id):
        target = target_by_id[target_id]
        try:
            target_artifacts = resolve_target(
                target=target,
                evidence_records=evidence_by_target.get(target_id, ()),
                admissions=admissions_by_target.get(target_id, ()),
                candidates=candidates_by_target.get(target_id, ()),
                policy=policy,
                resolved_at=now,
                prior_label=prior_by_target.get(target_id),
            )
            verify_resolution_artifacts(
                artifacts=target_artifacts,
                original_target=target,
                evidence_records=evidence_by_target.get(target_id, ()),
                admissions=admissions_by_target.get(target_id, ()),
                candidates=candidates_by_target.get(target_id, ()),
                policy=policy,
                prior_label=prior_by_target.get(target_id),
            )
        except ResolutionInputError as exc:
            raise LabelResolutionBatchInputError(
                f"target {target_id} resolution graph is invalid: {exc}"
            ) from exc
        artifacts.append(target_artifacts)

    replayed_input_hashes = _hash_inputs(input_paths, root=paths.root)
    if replayed_input_hashes != input_hashes:
        raise LabelResolutionBatchInputError(
            "one or more explicit input partitions changed during resolution"
        )
    if (
        hash_file(paths.root / policy.policy_relative_path) != policy.policy_file_sha256
        or hash_file(paths.root / policy.gate_relative_path) != policy.gate_file_sha256
    ):
        raise LabelResolutionBatchInputError(
            "active label-resolution policy or Gate-0 binding changed during resolution"
        )

    run_id = new_run_id(now, nonce=run_nonce)
    effective_code_state = code_state or collect_code_state(paths.root)
    manifest_path = run_manifest_path(paths, run_id)
    resolved_output = (
        output_dir or paths.data / "labeled" / "lf024_resolution_diagnostic_v1" / run_id
    ).resolve()
    output_preexisted, _quarantined_output = _prepare_output_destination(
        paths=paths,
        output_dir=resolved_output,
    )
    if manifest_path.exists():
        raise LabelResolutionBatchInputError(
            f"run manifest already exists for generated run ID {run_id}: {manifest_path}"
        )

    staging_output = resolved_output.parent / f".{resolved_output.name}.{run_id}.partial"
    staging_manifest = manifest_path.parent / f".{manifest_path.name}.{run_id}.partial"
    if staging_output.exists() or staging_manifest.exists():
        raise LabelResolutionBatchInputError(
            "resolver transaction staging path already exists; refusing to overwrite "
            f"{staging_output if staging_output.exists() else staging_manifest}"
        )

    linked_targets = tuple(item.target for item in artifacts)
    labels = tuple(item.label for item in artifacts)
    audits = tuple(item.audit for item in artifacts)
    derivations = tuple(
        sorted(
            (item.derivation for item in artifacts if item.derivation is not None),
            key=lambda item: item.derivation_id,
        )
    )
    conflicts = tuple(
        sorted(
            (record for item in artifacts for record in item.conflicts),
            key=lambda item: item.conflict_id,
        )
    )
    overrides = tuple(
        sorted(
            (record for item in artifacts for record in item.overrides),
            key=lambda item: item.override_id,
        )
    )

    linked_targets_path = resolved_output / (
        "pairs.jsonl" if target_kind is SemanticLabelTargetKind.LEAN_PAIR else "nl_lean.jsonl"
    )
    labels_path = resolved_output / "labels.jsonl"
    audits_path = resolved_output / "resolution_audits.jsonl"
    derivations_path = resolved_output / "evidence_derivations.jsonl"
    conflicts_path = resolved_output / "conflicts.jsonl"
    overrides_path = resolved_output / "overrides.jsonl"
    output_paths: tuple[tuple[str, Path, Sequence[StrictModel]], ...] = (
        ("linked_targets", linked_targets_path, linked_targets),
        ("labels", labels_path, labels),
        ("resolution_audits", audits_path, audits),
        ("evidence_derivations", derivations_path, derivations),
        ("conflicts", conflicts_path, conflicts),
        ("overrides", overrides_path, overrides),
    )

    resolved_count = sum(
        label.resolution_outcome is not ResolutionOutcome.UNRESOLVED for label in labels
    )
    unresolved_count = len(labels) - resolved_count
    argv = [
        "leanfaith",
        "resolve-labels",
        "--targets",
        str(target_path),
        "--evidence",
        str(evidence_path),
        "--admissions",
        str(admission_path),
        "--candidates",
        str(candidate_path),
        "--output-dir",
        str(resolved_output),
    ]
    if prior_label_path is not None:
        argv.extend(("--prior-labels", str(prior_label_path)))
    argv.extend(("--root", str(paths.root)))

    published_output = False
    published_manifest = False
    removed_preexisting_output = False
    try:
        staging_output.mkdir(parents=True)
        output_hashes: dict[str, str] = {}
        for role, final_path, records in output_paths:
            staged_path = staging_output / final_path.name
            output_hashes[f"{role}::{_display_path(final_path, paths.root)}"] = _write_jsonl(
                records,
                staged_path,
            )

        # Recheck every mutable authority immediately before publication. Any
        # drift removes staging and leaves no visible output partition.
        if _hash_inputs(input_paths, root=paths.root) != input_hashes:
            raise LabelResolutionBatchInputError(
                "one or more explicit input partitions changed during output staging"
            )
        if (
            hash_file(paths.root / policy.policy_relative_path) != policy.policy_file_sha256
            or hash_file(paths.root / policy.gate_relative_path) != policy.gate_file_sha256
        ):
            raise LabelResolutionBatchInputError(
                "active label-resolution policy or Gate-0 binding changed during output staging"
            )

        manifest = RunManifest(
            run_id=run_id,
            artifact_class=artifact_class,
            command="leanfaith resolve-labels",
            argv=tuple(argv),
            code=effective_code_state,
            config_hashes={
                policy.policy_relative_path: policy.policy_file_sha256,
                policy.gate_relative_path: policy.gate_file_sha256,
            },
            input_hashes=input_hashes,
            output_hashes=output_hashes,
            execution={
                "target_kind": target_kind.value,
                "linked_evidence_graph_closed": True,
                "candidate_partition_explicit": True,
                "candidate_set_closed": False,
                "candidate_inference": False,
                "candidate_promotion": False,
                "production_admission": False,
                "prior_label_partition_supplied": prior_label_path is not None,
                "external_manifest_is_commit_marker": True,
                "output_control_descriptor": _COMMIT_CONTROL_FILE,
            },
            status_counts={
                "input_targets": len(targets),
                "input_evidence_records": len(evidence),
                "input_admission_records": len(admissions),
                "input_resolution_candidates": len(candidates),
                "input_prior_labels": len(prior_labels),
                "resolved_labels": resolved_count,
                "unresolved_labels": unresolved_count,
                "evidence_derivations": len(derivations),
                "resolution_conflicts": len(conflicts),
                "resolution_overrides": len(overrides),
                "candidates_invented": 0,
                "candidates_promoted": 0,
            },
            created_at=now,
            notes=(
                "Diagnostic-only LF-024 explicit-partition resolution foundation. The linked "
                "evidence graph was closed; the candidate partition was explicit but not "
                "claimed exhaustive. The operation never inferred or promoted a candidate, "
                "made every label train/evaluation-ineligible, and performed no production "
                "admission. Typed authority/admission adapters remain required before "
                "production resolution."
            ),
        )
        manifest_sha256 = write_manifest(manifest, staging_manifest)
        control = _build_commit_control(
            run_id=run_id,
            manifest_sha256=manifest_sha256,
            output_dir=resolved_output,
        )
        (staging_output / _COMMIT_CONTROL_FILE).write_bytes(
            canonical_json_bytes(control.model_dump(mode="json")) + b"\n"
        )

        # Recheck both destinations after staging to avoid overwriting a
        # concurrently published run or newly populated output directory.
        if manifest_path.exists():
            raise LabelResolutionBatchInputError(
                f"run manifest appeared during resolution: {manifest_path}"
            )
        if output_preexisted:
            if (
                not resolved_output.exists()
                or resolved_output.is_symlink()
                or not resolved_output.is_dir()
                or any(resolved_output.iterdir())
            ):
                raise LabelResolutionBatchInputError(
                    f"preexisting empty output directory changed during resolution: "
                    f"{resolved_output}"
                )
            resolved_output.rmdir()
            removed_preexisting_output = True
        elif resolved_output.exists():
            raise LabelResolutionBatchInputError(
                f"output path appeared during resolution: {resolved_output}"
            )

        os.replace(staging_output, resolved_output)
        published_output = True
        os.replace(staging_manifest, manifest_path)
        published_manifest = True
        if hash_file(manifest_path) != manifest_sha256:
            raise LabelResolutionBatchInputError(
                "published run manifest hash differs from staged manifest hash"
            )
    except BaseException:
        # The two publication renames cannot be one filesystem transaction
        # because the artifacts live under data/ and runs/. Use best-effort
        # rollback for Python exceptions. A hard process/power loss between the
        # renames is recovered later through the descriptor/commit-marker check.
        _remove_transaction_path(staging_output)
        _remove_transaction_path(staging_manifest)
        if published_manifest:
            _remove_transaction_path(manifest_path)
        if published_output:
            _remove_transaction_path(resolved_output)
        if output_preexisted and (removed_preexisting_output or published_output):
            resolved_output.mkdir(parents=True, exist_ok=True)
        raise

    return LabelResolutionBatchArtifacts(
        run_id=run_id,
        target_kind=target_kind,
        output_dir=resolved_output,
        linked_targets_path=linked_targets_path,
        labels_path=labels_path,
        audits_path=audits_path,
        derivations_path=derivations_path,
        conflicts_path=conflicts_path,
        overrides_path=overrides_path,
        run_manifest_path=manifest_path,
        run_manifest_sha256=manifest_sha256,
        target_count=len(targets),
        resolved_count=resolved_count,
        unresolved_count=unresolved_count,
        derivation_count=len(derivations),
        conflict_count=len(conflicts),
        override_count=len(overrides),
    )


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Hold one persistent advisory lock without waiting for another publisher."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LabelResolutionBatchInputError(
                f"resolver publication/recovery lock is already held: {path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def resolve_label_batch(
    *,
    paths: RepoPaths,
    target_path: Path,
    evidence_path: Path,
    admission_path: Path,
    candidate_path: Path,
    prior_label_path: Path | None = None,
    output_dir: Path | None = None,
    artifact_class: ArtifactClass = ArtifactClass.DIAGNOSTIC,
    resolved_at: datetime.datetime | None = None,
    run_nonce: str | None = None,
    code_state: CodeState | None = None,
) -> LabelResolutionBatchArtifacts:
    """Resolve and publish one batch while excluding concurrent recovery."""

    now = resolved_at or datetime.datetime.now(tz=datetime.UTC)
    if now.tzinfo is None or now.utcoffset() != datetime.timedelta(0):
        raise LabelResolutionBatchInputError("resolved_at must be timezone-aware UTC")
    if artifact_class is not ArtifactClass.DIAGNOSTIC:
        raise LabelResolutionBatchInputError(
            "only diagnostic label resolution is enabled until typed authority and "
            "evidence-admission adapters independently verify bound artifact content"
        )
    root = paths.root.resolve()
    run_id = new_run_id(now, nonce=run_nonce)
    resolved_output = (
        output_dir or root / "data" / "labeled" / "lf024_resolution_diagnostic_v1" / run_id
    ).resolve()
    output_lock = resolved_output.parent / f".{resolved_output.name}.lf024.lock"
    run_lock = root / "runs" / run_id / ".lf024.lock"
    with _exclusive_lock(output_lock), _exclusive_lock(run_lock):
        return _resolve_label_batch_locked(
            paths=RepoPaths(root=root),
            target_path=target_path,
            evidence_path=evidence_path,
            admission_path=admission_path,
            candidate_path=candidate_path,
            prior_label_path=prior_label_path,
            output_dir=resolved_output,
            artifact_class=artifact_class,
            resolved_at=now,
            run_nonce=run_nonce,
            code_state=code_state,
        )


__all__ = [
    "LabelResolutionBatchArtifacts",
    "LabelResolutionBatchInputError",
    "LabelResolutionCommitControl",
    "resolve_label_batch",
]
