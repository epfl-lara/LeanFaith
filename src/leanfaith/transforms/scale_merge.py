"""Fail-closed merge and audit for deterministic materialization shards."""

from __future__ import annotations

import datetime
import json
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, TypeAdapter, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.representations import NORMALIZATION_VERSION
from leanfaith.schemas.enums import QualityTier, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import REPRESENTATION_PREFIX, make_id
from leanfaith.schemas.manifest import collect_code_state
from leanfaith.schemas.pair import PairRecord, check_pair_groups
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import (
    TransformationAttempt,
    TransformationAudit,
    VariantDraft,
    VariantRecord,
    check_deterministic_variant_lineage,
)
from leanfaith.transforms.materialize import (
    build_derived_theorem_record,
    build_deterministic_pair_record,
)
from leanfaith.transforms.protocol import (
    build_deterministic_variant_record,
    verify_deterministic_variant_id,
    verify_transformation_attempt_id,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)
from leanfaith.transforms.scale_materializer import (
    _REQUIRED_CANDIDATE_VIEWS,
    _RULE_POLARITY,
    DeterministicScaleConfig,
    DeterministicScaleError,
    DeterministicScaleManifest,
    DeterministicScaleRunSpec,
    ScaleQuarantineRecord,
    ScaleSourceShard,
    _AdmissionState,
    _admit_source_shard,
    _build_lean_replay_audit,
    _canonical_model_bytes,
    _clean_project_tree_hash,
    _journal_receipt_path,
    _load_journal_receipt,
    _load_jsonl,
    _load_lean_replay_audit,
    _load_source_shard,
    _project_records,
    _representation_payload_hash,
    _root_component_shard_assignments,
    _run_lock,
    _run_spec_payload,
    _selection_key,
    _shard_set_spec_payload,
    _source_shard_path,
    _tree_hash,
    _write_new_atomic,
    _write_partitions,
    run_deterministic_scale_materialization,
)

_HEX64_PATTERN = r"^[0-9a-f]{64}$"


class DeterministicScaleMergedShardBinding(StrictModel):
    """Content binding for one audited producer shard."""

    shard_index: int = Field(ge=0)
    output_dir: str
    run_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    run_spec_sha256: str = Field(pattern=_HEX64_PATTERN)
    manifest_sha256: str = Field(pattern=_HEX64_PATTERN)
    selected_source_count: int = Field(ge=1)
    selected_source_ids_sha256: str = Field(pattern=_HEX64_PATTERN)
    journal_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    journal_receipt_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    journal_chain_tip: str = Field(pattern=_HEX64_PATTERN)
    lean_replay_audit_hash: str = Field(pattern=_HEX64_PATTERN)
    lean_replay_audit_sha256: str = Field(pattern=_HEX64_PATTERN)


class DeterministicScaleMergedManifest(StrictModel):
    """Content-addressed audit manifest for one complete shard set."""

    schema_version: Literal[3] = 3
    artifact_kind: Literal["deterministic_scale_merged_manifest"] = (
        "deterministic_scale_merged_manifest"
    )
    merged_manifest_hash: str = Field(pattern=_HEX64_PATTERN)
    shard_set_spec_hash: str = Field(pattern=_HEX64_PATTERN)
    shard_count: int = Field(ge=1)
    shard_bindings: tuple[DeterministicScaleMergedShardBinding, ...]
    source_universe_count: int = Field(ge=1)
    source_universe_sha256: str = Field(pattern=_HEX64_PATTERN)
    source_assignment_sha256: str = Field(pattern=_HEX64_PATTERN)
    eligible_source_count: int = Field(ge=0)
    ineligible_source_count: int = Field(ge=0)
    rule_status_counts: dict[str, int]
    family_accepted_counts: dict[str, int]
    record_counts: dict[str, int]
    partition_sha256: dict[str, str]
    aggregate_journal_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    aggregate_receipt_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    aggregate_raw_response_tree_hash: str = Field(pattern=_HEX64_PATTERN)
    resolved_semantic_labels: Literal[0] = 0
    promoted_items: Literal[0] = 0
    output_quality_tier: Literal["provisional"] = "provisional"
    merge_replayed_with_lean: Literal[True] = True
    training_eligible: Literal[False] = False
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _self_authenticating(self) -> DeterministicScaleMergedManifest:
        payload = self.model_dump(mode="json")
        payload.pop("merged_manifest_hash")
        if self.merged_manifest_hash != hash_canonical(payload):
            raise ValueError("merged manifest hash does not match canonical payload")
        if len(self.shard_bindings) != self.shard_count:
            raise ValueError("merged shard binding count differs from shard_count")
        if tuple(binding.shard_index for binding in self.shard_bindings) != tuple(
            range(self.shard_count)
        ):
            raise ValueError("merged shard bindings are not complete and ordered")
        return self


@dataclass(frozen=True, slots=True)
class DeterministicScaleMergeArtifacts:
    output_dir: Path
    manifest_path: Path
    manifest_sha256: str
    merged_manifest_hash: str
    partition_paths: Mapping[str, Path]


def _load_canonical_model[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
) -> ModelT:
    try:
        payload = path.read_bytes()
        raw = json.loads(payload)
        parsed = model.model_validate(raw)
    except Exception as exc:
        raise DeterministicScaleError(f"invalid {model.__name__} at {path}: {exc}") from exc
    if payload != _canonical_model_bytes(parsed):
        raise DeterministicScaleError(f"{model.__name__} is not canonical JSON: {path}")
    return parsed


def _validate_run_spec(spec: DeterministicScaleRunSpec) -> None:
    dumped = spec.model_dump(mode="json")
    if hash_canonical(_run_spec_payload(dumped)) != spec.run_spec_hash:
        raise DeterministicScaleError("shard run_spec_hash does not match its payload")
    if hash_canonical(_shard_set_spec_payload(dumped)) != spec.shard_set_spec_hash:
        raise DeterministicScaleError("shard_set_spec_hash does not match its common payload")


def _canonical_partition_payload(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_model_bytes(record) for record in records)


def _validate_current_input_bindings(spec: DeterministicScaleRunSpec) -> None:
    bindings = {
        "theorem input": (spec.theorem_input_path, spec.theorem_input_sha256),
        "representation input": (
            spec.representation_input_path,
            spec.representation_input_sha256,
        ),
        "source inventory": (
            spec.source_inventory_manifest_path,
            spec.source_inventory_manifest_sha256,
        ),
        "theorem upstream manifest": (
            spec.theorem_upstream_manifest_path,
            spec.theorem_upstream_manifest_sha256,
        ),
        "representation upstream manifest": (
            spec.representation_upstream_manifest_path,
            spec.representation_upstream_manifest_sha256,
        ),
        "benchmark manifest": (
            spec.benchmark_manifest_path,
            spec.benchmark_manifest_sha256,
        ),
    }
    for label, (raw_path, expected_hash) in bindings.items():
        path = Path(raw_path)
        if not path.is_file() or hash_file(path) != expected_hash:
            raise DeterministicScaleError(f"{label} changed or is unavailable: {path}")
    loaded_config = load_config(Path(spec.config_path), DeterministicScaleConfig)
    if loaded_config.config_hash != spec.config_hash:
        raise DeterministicScaleError("deterministic scale config changed after shard execution")


def _replay_shard_with_lean(
    *,
    paths: RepoPaths,
    output_dir: Path,
    spec: DeterministicScaleRunSpec,
    manifest: DeterministicScaleManifest,
) -> None:
    """Re-run one completed shard in scratch through the pinned materializer.

    The producer's self-hashed journal and replay audit are not trust anchors.
    Merge first verifies every producer-bound file, then rebuilds the entire
    shard in a separate scratch output. Only the replay audit is added to the
    producer after exact semantic equality succeeds; verification never heals
    or rewrites producer journals, receipts, partitions, manifests, or raw
    Lean responses.
    """

    _validate_producer_artifact_bindings(
        output_dir=output_dir,
        spec=spec,
        manifest=manifest,
    )
    project_dir = Path(spec.project_dir)
    _clean_project_tree_hash(
        project_dir,
        expected_revision=spec.project_revision,
        expected_tree_hash=spec.project_tree_hash,
    )
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.lean-replay-",
        dir=output_dir.parent,
    ) as scratch:
        replay_output = Path(scratch) / "shard"
        artifacts = run_deterministic_scale_materialization(
            paths=paths,
            theorem_jsonl=Path(spec.theorem_input_path),
            representation_jsonl=Path(spec.representation_input_path),
            source_inventory_manifest=Path(spec.source_inventory_manifest_path),
            project_dir=project_dir,
            output_dir=replay_output,
            config_path=Path(spec.config_path),
            max_sources=spec.max_sources,
            shard_count=spec.shard_count,
            shard_index=spec.shard_index,
            resume=False,
            fast_resume=False,
            memory_hard_limit_mb=spec.memory_hard_limit_mb,
        )
        if (
            artifacts.run_spec_path != replay_output / "run_spec.json"
            or artifacts.manifest_path != replay_output / "manifest.json"
        ):
            raise DeterministicScaleError(
                "exact Lean replay returned artifact paths outside its scratch shard"
            )
        replayed_spec = _load_canonical_model(
            artifacts.run_spec_path,
            DeterministicScaleRunSpec,
        )
        if replayed_spec != spec:
            raise DeterministicScaleError(
                "exact Lean replay changed the producer run specification"
            )
        replayed_manifest = _load_canonical_model(
            artifacts.manifest_path,
            DeterministicScaleManifest,
        )
        producer_semantic = manifest.model_dump(mode="json")
        replay_semantic = replayed_manifest.model_dump(mode="json")
        for operational_field in (
            "raw_response_file_count",
            "raw_response_tree_hash",
        ):
            producer_semantic.pop(operational_field)
            replay_semantic.pop(operational_field)
        if replay_semantic != producer_semantic:
            raise DeterministicScaleError(
                "scratch Lean replay differs from the producer scientific manifest"
            )

    _clean_project_tree_hash(
        project_dir,
        expected_revision=spec.project_revision,
        expected_tree_hash=spec.project_tree_hash,
    )
    replay_audit = _build_lean_replay_audit(
        run_spec=spec,
        run_spec_path=output_dir / "run_spec.json",
        replayed_source_ids=spec.selected_source_theorem_ids,
        journal_tree_hash=manifest.journal_tree_hash,
        partition_sha256=manifest.partition_sha256,
        created_at=manifest.created_at,
    )
    _write_new_atomic(
        output_dir / "full_lean_replay_audit.json",
        _canonical_model_bytes(replay_audit),
    )


def _validate_producer_artifact_bindings(
    *,
    output_dir: Path,
    spec: DeterministicScaleRunSpec,
    manifest: DeterministicScaleManifest,
) -> None:
    """Fail before replay if any producer-bound artifact is absent or changed."""

    run_spec_path = output_dir / "run_spec.json"
    if (
        manifest.run_spec_hash != spec.run_spec_hash
        or manifest.run_spec_sha256 != hash_file(run_spec_path)
        or manifest.shard_set_spec_hash != spec.shard_set_spec_hash
    ):
        raise DeterministicScaleError("producer manifest/run-spec binding changed before replay")

    entries = _expected_local_entries(spec)
    journal_dir = output_dir / "journal"
    receipt_dir = output_dir / "journal_receipts"
    expected_journals = tuple(
        _source_shard_path(journal_dir, global_index, theorem_id)
        for global_index, theorem_id in entries
    )
    expected_receipts = tuple(
        _journal_receipt_path(receipt_dir, journal) for journal in expected_journals
    )
    if set(journal_dir.glob("*.json")) != set(expected_journals):
        raise DeterministicScaleError(
            "producer journal is incomplete or contains foreign files before replay"
        )
    if set(receipt_dir.glob("*.json")) != set(expected_receipts):
        raise DeterministicScaleError(
            "producer receipt chain is incomplete or contains foreign files before replay"
        )
    journal_count, journal_tree_hash = _tree_hash(journal_dir, "*.json")
    receipt_count, receipt_tree_hash = _tree_hash(receipt_dir, "*.json")
    if (
        journal_count != manifest.journal_shard_count
        or journal_tree_hash != manifest.journal_tree_hash
        or receipt_count != manifest.journal_receipt_count
        or receipt_tree_hash != manifest.journal_receipt_tree_hash
    ):
        raise DeterministicScaleError("producer journal/receipt binding changed before replay")

    partition_dir = output_dir / "partitions"
    actual_partitions = {path.stem: path for path in partition_dir.glob("*.jsonl")}
    if set(actual_partitions) != set(manifest.partition_sha256):
        raise DeterministicScaleError(
            "producer partitions are incomplete or contain foreign files before replay"
        )
    for name, expected_hash in manifest.partition_sha256.items():
        if hash_file(actual_partitions[name]) != expected_hash:
            raise DeterministicScaleError(f"producer partition changed before replay: {name}")

    raw_count, raw_tree_hash = _tree_hash(output_dir / "raw_lean_responses", "*")
    if (
        raw_count != manifest.raw_response_file_count
        or raw_tree_hash != manifest.raw_response_tree_hash
    ):
        raise DeterministicScaleError("producer raw Lean-response binding changed before replay")


def _expected_local_entries(
    spec: DeterministicScaleRunSpec,
) -> tuple[tuple[int, str], ...]:
    return tuple(
        (global_index, theorem_id)
        for global_index, (theorem_id, assignment) in enumerate(
            zip(
                spec.source_universe_theorem_ids,
                spec.source_shard_assignments,
                strict=True,
            )
        )
        if assignment == spec.shard_index
    )


def _unique_by[RecordT: StrictModel](
    records: Sequence[RecordT],
    *,
    field: str,
    label: str,
) -> dict[str, RecordT]:
    result: dict[str, RecordT] = {}
    for record in records:
        value = getattr(record, field)
        if not isinstance(value, str) or not value:
            raise DeterministicScaleError(f"{label} has an invalid {field}")
        if value in result:
            raise DeterministicScaleError(f"duplicate {field} values detected: {value}")
        result[value] = record
    return result


def _validate_projected_semantic_lineage(
    *,
    projected: Mapping[str, Sequence[StrictModel]],
    source_shards: Sequence[ScaleSourceShard],
    source_theorems: Sequence[TheoremRecord],
    source_representations: Sequence[RepresentationRecord],
    spec: DeterministicScaleRunSpec,
    config: DeterministicScaleConfig,
    source_run_spec_hashes: Mapping[str, str] | None = None,
) -> None:
    """Rebuild every accepted cross-record lineage from immutable inventories.

    Partition equality with a producer journal is necessary but not
    sufficient: an internally rehashed journal could still carry a corrupted
    pair split group or candidate/source link.  This validator treats the
    theorem/repr_v3 inputs named by ``run_spec`` as the independent inventory
    and recomputes all deterministic identities and cross-record projections.
    """

    sources = _unique_by(source_theorems, field="theorem_id", label="source theorem")
    source_reprs = _unique_by(
        source_representations,
        field="representation_id",
        label="source representation",
    )
    source_repr_by_theorem: dict[str, RepresentationRecord] = {}
    for representation in source_representations:
        if representation.theorem_id not in sources:
            raise DeterministicScaleError(
                "source representation references a theorem outside the immutable inventory"
            )
        if representation.theorem_id in source_repr_by_theorem:
            raise DeterministicScaleError(
                f"duplicate source representation theorem link: {representation.theorem_id}"
            )
        if representation.normalization_version != NORMALIZATION_VERSION:
            raise DeterministicScaleError("source representation version is not repr_v3")
        expected_id = make_id(
            REPRESENTATION_PREFIX,
            {
                "theorem_id": representation.theorem_id,
                "normalization_version": NORMALIZATION_VERSION,
            },
        )
        if (
            representation.representation_id != expected_id
            or representation.content_hash != _representation_payload_hash(representation)
        ):
            raise DeterministicScaleError(
                f"source representation identity/content mismatch: {representation.theorem_id}"
            )
        source_repr_by_theorem[representation.theorem_id] = representation
    if len(source_reprs) != len(source_repr_by_theorem):
        raise DeterministicScaleError("source representation IDs/theorem links do not reconcile")
    if set(source_repr_by_theorem) != set(sources):
        raise DeterministicScaleError(
            "immutable source inventory does not contain exactly one repr_v3 record per theorem"
        )

    attempts = cast(Sequence[TransformationAttempt], projected["attempts"])
    drafts = cast(Sequence[VariantDraft], projected["drafts"])
    candidate_theorems = cast(
        Sequence[TheoremRecord],
        projected["candidate_theorems"],
    )
    candidate_representations = cast(
        Sequence[RepresentationRecord],
        projected["candidate_representations"],
    )
    audits = cast(Sequence[TransformationAudit], projected["audits"])
    variants = cast(Sequence[VariantRecord], projected["variants"])
    pairs = cast(Sequence[PairRecord], projected["pairs"])
    quarantine = cast(Sequence[ScaleQuarantineRecord], projected["quarantine"])

    attempt_by_id = _unique_by(attempts, field="attempt_id", label="attempt")
    draft_by_id = _unique_by(drafts, field="draft_id", label="draft")
    candidate_by_id = _unique_by(
        candidate_theorems,
        field="theorem_id",
        label="candidate theorem",
    )
    candidate_repr_by_id = _unique_by(
        candidate_representations,
        field="representation_id",
        label="candidate representation",
    )
    audit_by_id = _unique_by(audits, field="audit_id", label="audit")
    variant_by_id = _unique_by(variants, field="variant_id", label="variant")
    pair_by_id = _unique_by(pairs, field="pair_id", label="pair")
    quarantine_by_draft = _unique_by(
        quarantine,
        field="draft_id",
        label="quarantine record",
    )
    del variant_by_id, pair_by_id

    accepted_count = len(drafts)
    linked_counts = {
        "candidate_theorems": len(candidate_theorems),
        "candidate_representations": len(candidate_representations),
        "audits": len(audits),
        "variants": len(variants),
        "pairs": len(pairs),
    }
    if any(count != accepted_count for count in linked_counts.values()):
        raise DeterministicScaleError(
            "accepted cross-record partitions do not have one-to-one cardinality: "
            f"drafts={accepted_count}, linked={linked_counts}"
        )
    accepted_draft_ids = set(draft_by_id)
    quarantined_draft_ids = set(quarantine_by_draft)
    if accepted_draft_ids & quarantined_draft_ids:
        raise DeterministicScaleError("accepted and quarantined draft IDs overlap")

    nested_attempts: list[TransformationAttempt] = []
    expected_quarantine: dict[str, ScaleQuarantineRecord] = {}
    nested_draft_ids: set[str] = set()
    source_index_by_id = {
        theorem_id: index for index, theorem_id in enumerate(spec.source_universe_theorem_ids)
    }
    for source_shard in source_shards:
        expected_source_index = source_index_by_id.get(source_shard.source_theorem_id)
        expected_run_spec_hash = (
            source_run_spec_hashes.get(source_shard.source_theorem_id)
            if source_run_spec_hashes is not None
            else spec.run_spec_hash
        )
        if (
            source_shard.source_theorem_id not in sources
            or expected_run_spec_hash is None
            or source_shard.run_spec_hash != expected_run_spec_hash
            or source_shard.source_index != expected_source_index
        ):
            raise DeterministicScaleError(
                "source journal shard leaves its exact run/inventory assignment"
            )
        for rule in source_shard.rule_results:
            if (
                source_shard.source_theorem_id not in rule.source_theorem_ids
                or any(theorem_id not in sources for theorem_id in rule.source_theorem_ids)
                or rule.rule_id not in config.active_rule_ids
                or rule.family_id != rule.rule_id
                or rule.polarity != _RULE_POLARITY[rule.rule_id]
            ):
                raise DeterministicScaleError(
                    f"rule result leaves its owning source/family policy: {rule.rule_id}"
                )
            expected_rule_sources: tuple[str, ...]
            if rule.rule_id == "n10_nearby_theorem":
                if rule.status == "no_donor":
                    expected_rule_sources = (source_shard.source_theorem_id,)
                else:
                    if rule.donor_theorem_id is None:
                        raise DeterministicScaleError("N10 journal result lacks its donor role")
                    expected_rule_sources = tuple(
                        sorted(
                            (
                                source_shard.source_theorem_id,
                                rule.donor_theorem_id,
                            )
                        )
                    )
            else:
                if rule.donor_theorem_id is not None:
                    raise DeterministicScaleError(
                        f"unary journal result carries a donor: {rule.rule_id}"
                    )
                expected_rule_sources = (source_shard.source_theorem_id,)
            if rule.source_theorem_ids != expected_rule_sources:
                raise DeterministicScaleError(
                    f"rule result source/role lineage mismatch: {rule.rule_id}"
                )
            attempt = rule.attempt
            if attempt is not None:
                nested_attempts.append(attempt)
                if (
                    attempt.family_id != rule.family_id
                    or attempt.rule_id != rule.rule_id
                    or attempt.source_theorem_ids != rule.source_theorem_ids
                    or attempt.seed != rule.seed
                ):
                    raise DeterministicScaleError(
                        f"nested attempt/rule lineage mismatch: {attempt.attempt_id}"
                    )
            if rule.draft_results and attempt is None:
                raise DeterministicScaleError(
                    f"rule with draft outcomes lacks its owning attempt: {rule.rule_id}"
                )
            for result in rule.draft_results:
                draft_id = result.persistent_draft_id
                if draft_id in nested_draft_ids:
                    raise DeterministicScaleError(
                        f"nested draft ID occurs more than once: {draft_id}"
                    )
                nested_draft_ids.add(draft_id)
                if attempt is None or draft_id not in attempt.draft_ids:
                    raise DeterministicScaleError(
                        f"nested draft is not owned by its transformation attempt: {draft_id}"
                    )
                if result.failure is not None and (
                    result.failure.draft_id != draft_id
                    or result.failure.rule_id != rule.rule_id
                    or result.failure.source_theorem_ids != rule.source_theorem_ids
                ):
                    raise DeterministicScaleError(
                        f"nested draft failure lineage mismatch: {draft_id}"
                    )
                if result.draft is not None:
                    try:
                        verify_variant_draft_id(result.draft)
                    except ValueError as exc:
                        raise DeterministicScaleError(
                            f"nested draft identity mismatch: {draft_id}"
                        ) from exc
                    if (
                        result.draft.draft_id != draft_id
                        or result.draft.family_id != rule.family_id
                        or result.draft.rule_id != rule.rule_id
                        or result.draft.source_theorem_ids != rule.source_theorem_ids
                        or result.draft.seed != rule.seed
                        or result.draft.candidate_code_hash != result.persistent_candidate_code_hash
                    ):
                        raise DeterministicScaleError(
                            f"nested draft/rule payload lineage mismatch: {draft_id}"
                        )
                if result.status != "accepted":
                    assert result.failure is not None
                    expected_quarantine[draft_id] = ScaleQuarantineRecord(
                        status=result.status,
                        source_theorem_ids=rule.source_theorem_ids,
                        rule_id=rule.rule_id,
                        family_id=rule.family_id,
                        polarity=rule.polarity,
                        draft_id=draft_id,
                        candidate_code_hash=result.persistent_candidate_code_hash,
                        failure=result.failure,
                        candidate_content_redacted=(result.status == "protected_benchmark_overlap"),
                    )

    if tuple(attempts) != tuple(nested_attempts):
        raise DeterministicScaleError(
            "attempt partition differs from the exact nested journal projection"
        )
    if set(expected_quarantine) != set(quarantine_by_draft):
        raise DeterministicScaleError(
            "quarantine partition inventory differs from nested journal outcomes"
        )
    for draft_id, expected in expected_quarantine.items():
        if quarantine_by_draft[draft_id] != expected:
            raise DeterministicScaleError(
                f"quarantine record differs from its exact owning outcome: {draft_id}"
            )

    draft_owner: dict[str, str] = {}
    for attempt in attempts:
        try:
            verify_transformation_attempt_id(attempt)
        except ValueError as exc:
            raise DeterministicScaleError(
                f"transformation attempt identity mismatch: {attempt.attempt_id}"
            ) from exc
        if (
            attempt.registry_hash != spec.registry_hash
            or attempt.generation_config_hash != spec.registry_hash
        ):
            raise DeterministicScaleError(
                f"attempt is not bound to the run registry: {attempt.attempt_id}"
            )
        for theorem_id, representation_id in zip(
            attempt.source_theorem_ids,
            attempt.source_representation_ids,
            strict=True,
        ):
            source = sources.get(theorem_id)
            linked_source_representation = source_repr_by_theorem.get(theorem_id)
            if (
                source is None
                or linked_source_representation is None
                or linked_source_representation.representation_id != representation_id
                or source.context_id != attempt.context_id
            ):
                raise DeterministicScaleError(
                    f"attempt source inventory link mismatch: {attempt.attempt_id}"
                )
        for draft_id in attempt.draft_ids:
            previous = draft_owner.setdefault(draft_id, attempt.attempt_id)
            if previous != attempt.attempt_id:
                raise DeterministicScaleError(f"draft ID is owned by multiple attempts: {draft_id}")

    projected_draft_ids = accepted_draft_ids | quarantined_draft_ids
    if set(draft_owner) != projected_draft_ids:
        raise DeterministicScaleError(
            "attempt draft inventory differs from accepted plus quarantined projections"
        )
    for record in quarantine:
        if any(theorem_id not in sources for theorem_id in record.source_theorem_ids):
            raise DeterministicScaleError(
                f"quarantine source link leaves immutable inventory: {record.draft_id}"
            )
        if record.failure.draft_id != record.draft_id:
            raise DeterministicScaleError(
                f"quarantine failure/draft identity mismatch: {record.draft_id}"
            )
        if record.candidate_content_redacted != (record.status == "protected_benchmark_overlap"):
            raise DeterministicScaleError(
                f"quarantine redaction status mismatch: {record.draft_id}"
            )

    pairs_by_candidate: dict[str, PairRecord] = {}
    for pair in pairs:
        if pair.theorem_b_id in pairs_by_candidate:
            raise DeterministicScaleError(
                f"multiple pairs reference one candidate theorem: {pair.theorem_b_id}"
            )
        pairs_by_candidate[pair.theorem_b_id] = pair

    consumed_candidates: set[str] = set()
    consumed_representations: set[str] = set()
    consumed_audits: set[str] = set()
    consumed_pairs: set[str] = set()
    for variant in variants:
        try:
            verify_deterministic_variant_id(variant)
        except ValueError as exc:
            raise DeterministicScaleError(
                f"deterministic variant identity mismatch: {variant.variant_id}"
            ) from exc
        if (
            variant.draft_id is None
            or variant.audit_id is None
            or variant.transformation_attempt_id is None
            or variant.derived_theorem_id is None
            or variant.derived_representation_id is None
        ):
            raise DeterministicScaleError(
                f"deterministic variant lacks complete links: {variant.variant_id}"
            )
        draft = draft_by_id.get(variant.draft_id)
        audit = audit_by_id.get(variant.audit_id)
        linked_attempt = attempt_by_id.get(variant.transformation_attempt_id)
        candidate = candidate_by_id.get(variant.derived_theorem_id)
        candidate_representation = candidate_repr_by_id.get(variant.derived_representation_id)
        linked_pair = pairs_by_candidate.get(variant.derived_theorem_id)
        if any(
            item is None
            for item in (
                draft,
                audit,
                linked_attempt,
                candidate,
                candidate_representation,
                linked_pair,
            )
        ):
            raise DeterministicScaleError(
                f"variant cross-record link is missing: {variant.variant_id}"
            )
        assert draft is not None
        assert audit is not None
        assert linked_attempt is not None
        assert candidate is not None
        assert candidate_representation is not None
        assert linked_pair is not None
        attempt = linked_attempt
        pair = linked_pair
        source_records = tuple(
            sources[theorem_id] for theorem_id in draft.source_theorem_ids if theorem_id in sources
        )
        if len(source_records) != len(draft.source_theorem_ids):
            raise DeterministicScaleError(
                f"draft source theorem leaves immutable inventory: {draft.draft_id}"
            )
        source_representation_ids = tuple(
            source_repr_by_theorem[theorem_id].representation_id
            for theorem_id in draft.source_theorem_ids
            if theorem_id in source_repr_by_theorem
        )
        if (
            len(source_representation_ids) != len(draft.source_theorem_ids)
            or draft.source_representation_ids != source_representation_ids
        ):
            raise DeterministicScaleError(
                f"draft source representation link mismatch: {draft.draft_id}"
            )
        try:
            verify_variant_draft_id(draft)
            verify_transformation_audit_id(audit)
        except ValueError as exc:
            raise DeterministicScaleError(
                f"draft/audit identity mismatch for variant {variant.variant_id}"
            ) from exc
        lineage_violations = check_deterministic_variant_lineage(
            variant,
            draft,
            audit,
            attempt,
        )
        if lineage_violations:
            raise DeterministicScaleError(
                "deterministic variant cross-record lineage mismatch: "
                + ",".join(lineage_violations)
            )

        primary_source_id = candidate.metadata.get("primary_source_id")
        if not isinstance(primary_source_id, str) or primary_source_id not in sources:
            raise DeterministicScaleError(
                f"candidate primary source link is invalid: {candidate.theorem_id}"
            )
        if primary_source_id not in draft.source_theorem_ids:
            raise DeterministicScaleError(
                f"candidate primary source is absent from draft: {candidate.theorem_id}"
            )
        expected_source_index = source_index_by_id.get(primary_source_id)
        if expected_source_index is None:
            raise DeterministicScaleError(
                f"candidate primary source leaves run universe: {candidate.theorem_id}"
            )
        owner_run_spec_hash = (
            source_run_spec_hashes.get(primary_source_id)
            if source_run_spec_hashes is not None
            else spec.run_spec_hash
        )
        if owner_run_spec_hash is None:
            raise DeterministicScaleError(
                f"candidate primary source lacks an owning run spec: {candidate.theorem_id}"
            )
        validation_request_hash = candidate.metadata.get("validation_request_hash")
        inline_context_sha256 = candidate.metadata.get("inline_context_sha256")
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in (validation_request_hash, inline_context_sha256)
        ):
            raise DeterministicScaleError(
                f"candidate lacks bound Lean validation hashes: {candidate.theorem_id}"
            )
        expected_candidate = build_derived_theorem_record(
            draft=draft,
            sources=source_records,
            primary_source_id=primary_source_id,
            elaboration_status=candidate.elaboration_status,
            elaboration_diagnostics=candidate.elaboration_diagnostics,
            inline_elaboration_source=draft.candidate_code,
            metadata={
                "run_spec_hash": owner_run_spec_hash,
                "scale_profile_id": config.profile_id,
                "source_index": expected_source_index,
                "validation_request_hash": validation_request_hash,
                "inline_context_sha256": inline_context_sha256,
                "inline_context_persisted": False,
            },
        ).model_copy(update={"inline_elaboration_source": None})
        if candidate != expected_candidate:
            raise DeterministicScaleError(
                f"candidate theorem differs from deterministic lineage: {candidate.theorem_id}"
            )
        expected_representation_id = make_id(
            REPRESENTATION_PREFIX,
            {
                "theorem_id": candidate.theorem_id,
                "normalization_version": NORMALIZATION_VERSION,
            },
        )
        if (
            candidate_representation.representation_id != expected_representation_id
            or candidate_representation.theorem_id != candidate.theorem_id
            or candidate_representation.context_id != candidate.context_id
            or candidate_representation.normalization_version != NORMALIZATION_VERSION
            or candidate_representation.content_hash
            != _representation_payload_hash(candidate_representation)
            or candidate_representation.alpha_identity_fingerprint is None
            or any(
                candidate_representation.view_status[view] != ViewStatus.OK
                for view in _REQUIRED_CANDIDATE_VIEWS
            )
            or candidate.elaboration_status
            not in {
                ValidationStatus.ELABORATES,
                ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            }
        ):
            raise DeterministicScaleError(
                f"candidate representation/status mismatch: {candidate.theorem_id}"
            )
        expected_metadata: dict[str, str | int | float | bool | None] = {
            "run_spec_hash": owner_run_spec_hash,
            "scale_profile_id": config.profile_id,
            "source_index": expected_source_index,
        }
        expected_variant = build_deterministic_variant_record(
            attempt=attempt,
            draft=draft,
            audit=audit,
            candidate=candidate,
            candidate_representation=candidate_representation,
            polarity=variant.polarity_metadata,
            metadata=expected_metadata,
        )
        if variant != expected_variant:
            raise DeterministicScaleError(
                f"variant differs from deterministic projection: {variant.variant_id}"
            )
        primary = sources[primary_source_id]
        pair_candidate = candidate.model_copy(
            update={"inline_elaboration_source": draft.candidate_code}
        )
        expected_pair = build_deterministic_pair_record(
            source=primary,
            candidate=pair_candidate,
            draft=draft,
            audit=audit,
            all_sources=source_records,
            metadata=expected_metadata,
        )
        group_violations = check_pair_groups(pair, primary, candidate)
        if pair != expected_pair or group_violations:
            raise DeterministicScaleError(
                "pair identity/split lineage mismatch: "
                f"{pair.pair_id}; violations={group_violations}"
            )
        consumed_candidates.add(candidate.theorem_id)
        consumed_representations.add(candidate_representation.representation_id)
        consumed_audits.add(audit.audit_id)
        consumed_pairs.add(pair.pair_id)

    if (
        consumed_candidates != set(candidate_by_id)
        or consumed_representations != set(candidate_repr_by_id)
        or consumed_audits != set(audit_by_id)
        or consumed_pairs != {pair.pair_id for pair in pairs}
    ):
        raise DeterministicScaleError(
            "accepted candidate/audit/pair partitions contain unconsumed lineage records"
        )


def _validate_shard_output(
    *,
    output_dir: Path,
    spec: DeterministicScaleRunSpec,
    manifest: DeterministicScaleManifest,
    config: DeterministicScaleConfig,
) -> tuple[tuple[ScaleSourceShard, ...], DeterministicScaleMergedShardBinding]:
    run_spec_path = output_dir / "run_spec.json"
    manifest_path = output_dir / "manifest.json"
    if (
        manifest.run_spec_hash != spec.run_spec_hash
        or manifest.run_spec_sha256 != hash_file(run_spec_path)
        or manifest.shard_set_spec_hash != spec.shard_set_spec_hash
        or manifest.shard_count != spec.shard_count
        or manifest.shard_index != spec.shard_index
        or manifest.source_universe_count != len(spec.source_universe_theorem_ids)
    ):
        raise DeterministicScaleError("shard manifest/run-spec identity does not reconcile")
    assignment_hash = hash_canonical(
        {
            "source_universe_theorem_ids": spec.source_universe_theorem_ids,
            "source_shard_assignments": spec.source_shard_assignments,
        }
    )
    if manifest.source_assignment_sha256 != assignment_hash:
        raise DeterministicScaleError("shard manifest source assignment hash mismatch")

    entries = _expected_local_entries(spec)
    journal_dir = output_dir / "journal"
    receipt_dir = output_dir / "journal_receipts"
    expected_paths = tuple(
        _source_shard_path(journal_dir, global_index, theorem_id)
        for global_index, theorem_id in entries
    )
    expected_receipts = tuple(_journal_receipt_path(receipt_dir, path) for path in expected_paths)
    if set(journal_dir.glob("*.json")) != set(expected_paths):
        raise DeterministicScaleError("shard journal is incomplete or contains foreign files")
    if set(receipt_dir.glob("*.json")) != set(expected_receipts):
        raise DeterministicScaleError(
            "shard journal receipt chain is incomplete or contains foreign files"
        )

    shards: list[ScaleSourceShard] = []
    previous_receipt_hash = "0" * 64
    for (global_index, theorem_id), shard_path, receipt_path in zip(
        entries,
        expected_paths,
        expected_receipts,
        strict=True,
    ):
        shard = _load_source_shard(shard_path)
        if (
            shard.run_spec_hash != spec.run_spec_hash
            or shard.source_index != global_index
            or shard.source_theorem_id != theorem_id
        ):
            raise DeterministicScaleError("journal shard source/run assignment mismatch")
        receipt = _load_journal_receipt(
            path=receipt_path,
            shard=shard,
            shard_path=shard_path,
            previous_receipt_hash=previous_receipt_hash,
        )
        previous_receipt_hash = receipt.receipt_hash
        shards.append(shard)

    projected = _project_records(shards)
    expected_partition_names = set(projected)
    partition_dir = output_dir / "partitions"
    actual_partition_names = {path.stem for path in partition_dir.glob("*.jsonl")}
    if actual_partition_names != expected_partition_names:
        raise DeterministicScaleError(
            "shard partitions are incomplete or contain an unexpected label/artifact partition"
        )
    partition_hashes: dict[str, str] = {}
    for name, records in projected.items():
        path = partition_dir / f"{name}.jsonl"
        expected_payload = _canonical_partition_payload(records)
        if path.read_bytes() != expected_payload:
            raise DeterministicScaleError(f"shard partition differs from journal: {path}")
        partition_hashes[name] = hash_file(path)

    state = _AdmissionState(
        root_counts=Counter(),
        family_root_counts=Counter(),
        family_counts=Counter(),
        candidate_keys=set(),
        variant_ids=set(),
        pair_ids=set(),
    )
    for shard in shards:
        _admit_source_shard(state, shard)
    status_counts = Counter(result.status for shard in shards for result in shard.rule_results)
    journal_count, journal_tree_hash = _tree_hash(journal_dir, "*.json")
    receipt_count, receipt_tree_hash = _tree_hash(receipt_dir, "*.json")
    raw_count, raw_tree_hash = _tree_hash(output_dir / "raw_lean_responses", "*")
    replay_audit_path = output_dir / "full_lean_replay_audit.json"
    if not replay_audit_path.is_file():
        raise DeterministicScaleError(
            "producer shard lacks its replay accounting audit; scientific merge "
            "must invoke exact Lean replay before validating the shard"
        )
    replay_audit = _load_lean_replay_audit(
        path=replay_audit_path,
        run_spec=spec,
        run_spec_path=run_spec_path,
        replayed_source_ids=spec.selected_source_theorem_ids,
        journal_tree_hash=journal_tree_hash,
        partition_sha256=partition_hashes,
        created_at=config.record_timestamp_utc,
    )
    expected_manifest = DeterministicScaleManifest(
        run_spec_hash=spec.run_spec_hash,
        run_spec_sha256=hash_file(run_spec_path),
        shard_set_spec_hash=spec.shard_set_spec_hash,
        shard_count=spec.shard_count,
        shard_index=spec.shard_index,
        source_universe_count=len(spec.source_universe_theorem_ids),
        source_assignment_sha256=assignment_hash,
        source_count=len(shards),
        eligible_source_count=sum(shard.source_status == "eligible" for shard in shards),
        ineligible_source_count=sum(shard.source_status == "ineligible" for shard in shards),
        journal_shard_count=journal_count,
        rule_status_counts=dict(sorted(status_counts.items())),
        family_accepted_counts=dict(sorted(state.family_counts.items())),
        record_counts={name: len(records) for name, records in projected.items()},
        partition_sha256=dict(sorted(partition_hashes.items())),
        journal_tree_hash=journal_tree_hash,
        journal_receipt_count=receipt_count,
        journal_receipt_tree_hash=receipt_tree_hash,
        journal_chain_tip=previous_receipt_hash,
        raw_response_file_count=raw_count,
        raw_response_tree_hash=raw_tree_hash,
        created_at=config.record_timestamp_utc,
    )
    if manifest != expected_manifest:
        raise DeterministicScaleError("shard manifest does not reconcile from immutable outputs")

    return (
        tuple(shards),
        DeterministicScaleMergedShardBinding(
            shard_index=spec.shard_index,
            output_dir=str(output_dir),
            run_spec_hash=spec.run_spec_hash,
            run_spec_sha256=hash_file(run_spec_path),
            manifest_sha256=hash_file(manifest_path),
            selected_source_count=len(spec.selected_source_theorem_ids),
            selected_source_ids_sha256=hash_canonical(spec.selected_source_theorem_ids),
            journal_tree_hash=journal_tree_hash,
            journal_receipt_tree_hash=receipt_tree_hash,
            journal_chain_tip=previous_receipt_hash,
            lean_replay_audit_hash=replay_audit.audit_hash,
            lean_replay_audit_sha256=hash_file(replay_audit_path),
        ),
    )


def _reject_cross_shard_semantic_leakage(
    projected: Mapping[str, Sequence[StrictModel]],
) -> None:
    variants = cast(Sequence[VariantRecord], projected["variants"])
    pairs = cast(Sequence[PairRecord], projected["pairs"])
    if any(variant.quality_tier != QualityTier.PROVISIONAL for variant in variants):
        raise DeterministicScaleError("merged variants are not uniformly provisional")
    if any(pair.resolved_label_id is not None for pair in pairs):
        raise DeterministicScaleError("merged pairs contain resolved semantic labels")

    id_fields = {
        "attempts": "attempt_id",
        "drafts": "draft_id",
        "candidate_theorems": "theorem_id",
        "candidate_representations": "representation_id",
        "audits": "audit_id",
        "variants": "variant_id",
        "pairs": "pair_id",
        "quarantine": "draft_id",
    }
    for partition, field in id_fields.items():
        values = [getattr(record, field) for record in projected[partition]]
        if len(values) != len(set(values)):
            raise DeterministicScaleError(f"duplicate {field} values detected across merged shards")

    candidate_theorems = cast(Sequence[TheoremRecord], projected["candidate_theorems"])
    if len(candidate_theorems) != len(variants):
        raise DeterministicScaleError(
            "candidate theorem and variant partition counts do not reconcile"
        )
    candidate_keys = [
        (
            theorem.root_ancestry_ids,
            variant.candidate_code_hash,
        )
        for theorem, variant in zip(
            candidate_theorems,
            variants,
            strict=True,
        )
    ]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise DeterministicScaleError(
            "duplicate ancestry/candidate payload detected across merged shards"
        )


def merge_deterministic_scale_shards(
    *,
    paths: RepoPaths,
    shard_output_dirs: Sequence[Path],
    output_dir: Path,
) -> DeterministicScaleMergeArtifacts:
    """Audit a complete shard set and write deterministic merged projections."""

    resolved_dirs = tuple(path.resolve() for path in shard_output_dirs)
    if not resolved_dirs or len(set(resolved_dirs)) != len(resolved_dirs):
        raise DeterministicScaleError("shard output directories must be nonempty and unique")
    output = output_dir.resolve()
    if output in resolved_dirs:
        raise DeterministicScaleError("merged output directory cannot be a producer shard")

    loaded: list[tuple[Path, DeterministicScaleRunSpec, DeterministicScaleManifest]] = []
    for shard_dir in resolved_dirs:
        spec = _load_canonical_model(
            shard_dir / "run_spec.json",
            DeterministicScaleRunSpec,
        )
        _validate_run_spec(spec)
        manifest = _load_canonical_model(
            shard_dir / "manifest.json",
            DeterministicScaleManifest,
        )
        loaded.append((shard_dir, spec, manifest))
    loaded.sort(key=lambda item: item[1].shard_index)

    first_spec = loaded[0][1]
    common_payload = _shard_set_spec_payload(first_spec.model_dump(mode="json"))
    if len(loaded) != first_spec.shard_count:
        raise DeterministicScaleError("merge requires every shard in the bound shard set")
    for expected_index, (_, spec, _) in enumerate(loaded):
        if spec.shard_index != expected_index:
            raise DeterministicScaleError("shard indices contain a gap or overlap")
        if (
            spec.shard_set_spec_hash != first_spec.shard_set_spec_hash
            or _shard_set_spec_payload(spec.model_dump(mode="json")) != common_payload
        ):
            raise DeterministicScaleError(
                "shards do not share identical input/config/code provenance"
            )

    _validate_current_input_bindings(first_spec)
    loaded_config = load_config(
        Path(first_spec.config_path),
        DeterministicScaleConfig,
    )
    if first_spec.shard_count > 1 and "n10_nearby_theorem" in loaded_config.config.active_rule_ids:
        raise DeterministicScaleError(
            "sharded N10 output is scientifically invalid: run unary families in "
            "source shards and N10 in a dedicated shard_count=1 global pass"
        )
    if collect_code_state(paths.root) != first_spec.code:
        raise DeterministicScaleError(
            "merge implementation/code state differs from the producer run spec; "
            "archive this shard set and use an explicit migration/replay"
        )
    theorems = _load_jsonl(
        Path(first_spec.theorem_input_path),
        TheoremRecord,
        wrapper_key="theorem",
    )
    representations = _load_jsonl(
        Path(first_spec.representation_input_path),
        RepresentationRecord,
    )
    ordered = tuple(
        sorted(
            theorems,
            key=lambda theorem: _selection_key(
                loaded_config.config.base_seed,
                theorem.theorem_id,
            ),
        )
    )
    universe = ordered if first_spec.max_sources is None else ordered[: first_spec.max_sources]
    if tuple(theorem.theorem_id for theorem in universe) != (
        first_spec.source_universe_theorem_ids
    ):
        raise DeterministicScaleError("source universe no longer matches immutable inputs")
    recomputed_assignments = _root_component_shard_assignments(
        universe,
        shard_count=first_spec.shard_count,
    )
    if recomputed_assignments != first_spec.source_shard_assignments:
        raise DeterministicScaleError("source shard assignment does not recompute")

    all_shards: list[ScaleSourceShard] = []
    bindings: list[DeterministicScaleMergedShardBinding] = []
    observed_sources: set[str] = set()
    for shard_dir, spec, manifest in loaded:
        _replay_shard_with_lean(
            paths=paths,
            output_dir=shard_dir,
            spec=spec,
            manifest=manifest,
        )
        overlap = observed_sources & set(spec.selected_source_theorem_ids)
        if overlap:
            raise DeterministicScaleError(
                f"source assignment overlaps across shards: {sorted(overlap)[:3]}"
            )
        observed_sources.update(spec.selected_source_theorem_ids)
        shards, binding = _validate_shard_output(
            output_dir=shard_dir,
            spec=spec,
            manifest=manifest,
            config=loaded_config.config,
        )
        all_shards.extend(shards)
        bindings.append(binding)
    if observed_sources != set(first_spec.source_universe_theorem_ids):
        missing = set(first_spec.source_universe_theorem_ids) - observed_sources
        raise DeterministicScaleError(
            f"source assignment is incomplete: missing {sorted(missing)[:3]}"
        )

    all_shards.sort(key=lambda shard: shard.source_index)
    if tuple(shard.source_theorem_id for shard in all_shards) != (
        first_spec.source_universe_theorem_ids
    ):
        raise DeterministicScaleError("merged source journal order is not the source universe")
    projected = _project_records(all_shards)
    _reject_cross_shard_semantic_leakage(projected)
    universe_ids = set(first_spec.source_universe_theorem_ids)
    universe_representations = tuple(
        representation
        for representation in representations
        if representation.theorem_id in universe_ids
    )
    _validate_projected_semantic_lineage(
        projected=projected,
        source_shards=all_shards,
        source_theorems=universe,
        source_representations=universe_representations,
        spec=first_spec,
        config=loaded_config.config,
        source_run_spec_hashes={
            theorem_id: spec.run_spec_hash
            for _, spec, _ in loaded
            for theorem_id in spec.selected_source_theorem_ids
        },
    )

    global_state = _AdmissionState(
        root_counts=Counter(),
        family_root_counts=Counter(),
        family_counts=Counter(),
        candidate_keys=set(),
        variant_ids=set(),
        pair_ids=set(),
    )
    for shard in all_shards:
        _admit_source_shard(global_state, shard)
    config = loaded_config.config
    if any(
        count > config.max_accepted_variants_per_root_ancestry
        for count in global_state.root_counts.values()
    ):
        raise DeterministicScaleError("merged output violates the per-root admission cap")
    if any(
        count > config.max_accepted_variants_per_family_per_root_ancestry
        for count in global_state.family_root_counts.values()
    ):
        raise DeterministicScaleError("merged output violates the per-family/root admission cap")
    if config.max_accepted_variants_per_family is not None and any(
        count > config.max_accepted_variants_per_family
        for count in global_state.family_counts.values()
    ):
        raise DeterministicScaleError("merged output violates the global family cap")

    with _run_lock(output):
        unexpected = tuple(
            path
            for path in output.iterdir()
            if path.name != "run.lock"
            and not path.name.startswith("merged_manifest.")
            and path.name != "partitions"
        )
        if unexpected:
            raise DeterministicScaleError(
                f"merged output directory contains foreign files: {unexpected[:3]}"
            )
        existing_partition_names = {path.stem for path in (output / "partitions").glob("*.jsonl")}
        foreign_partitions = existing_partition_names - set(projected)
        if foreign_partitions:
            raise DeterministicScaleError(
                f"merged output contains foreign partitions: {sorted(foreign_partitions)}"
            )
        partition_paths, partition_hashes = _write_partitions(output, projected)
        status_counts = Counter(
            result.status for shard in all_shards for result in shard.rule_results
        )
        source_assignment_hash = hash_canonical(
            {
                "source_universe_theorem_ids": first_spec.source_universe_theorem_ids,
                "source_shard_assignments": first_spec.source_shard_assignments,
            }
        )
        data: dict[str, object] = {
            "schema_version": 3,
            "artifact_kind": "deterministic_scale_merged_manifest",
            "shard_set_spec_hash": first_spec.shard_set_spec_hash,
            "shard_count": first_spec.shard_count,
            "shard_bindings": tuple(bindings),
            "source_universe_count": len(first_spec.source_universe_theorem_ids),
            "source_universe_sha256": hash_canonical(first_spec.source_universe_theorem_ids),
            "source_assignment_sha256": source_assignment_hash,
            "eligible_source_count": sum(shard.source_status == "eligible" for shard in all_shards),
            "ineligible_source_count": sum(
                shard.source_status == "ineligible" for shard in all_shards
            ),
            "rule_status_counts": dict(sorted(status_counts.items())),
            "family_accepted_counts": dict(sorted(global_state.family_counts.items())),
            "record_counts": {name: len(records) for name, records in projected.items()},
            "partition_sha256": dict(sorted(partition_hashes.items())),
            "aggregate_journal_tree_hash": hash_canonical(
                tuple((binding.shard_index, binding.journal_tree_hash) for binding in bindings)
            ),
            "aggregate_receipt_tree_hash": hash_canonical(
                tuple(
                    (binding.shard_index, binding.journal_receipt_tree_hash) for binding in bindings
                )
            ),
            "aggregate_raw_response_tree_hash": hash_canonical(
                tuple(
                    (
                        spec.shard_index,
                        manifest.raw_response_tree_hash,
                        manifest.raw_response_file_count,
                    )
                    for _, spec, manifest in loaded
                )
            ),
            "resolved_semantic_labels": 0,
            "promoted_items": 0,
            "output_quality_tier": "provisional",
            "merge_replayed_with_lean": True,
            "training_eligible": False,
            "created_at": config.record_timestamp_utc,
        }
        hash_payload = {
            **data,
            "shard_bindings": tuple(binding.model_dump(mode="json") for binding in bindings),
            "created_at": TypeAdapter(datetime.datetime).dump_python(
                config.record_timestamp_utc,
                mode="json",
            ),
        }
        merged_manifest_hash = hash_canonical(hash_payload)
        merged_manifest = DeterministicScaleMergedManifest.model_validate(
            {"merged_manifest_hash": merged_manifest_hash, **data}
        )
        manifest_path = output / f"merged_manifest.{merged_manifest_hash}.json"
        manifest_sha256 = _write_new_atomic(
            manifest_path,
            _canonical_model_bytes(merged_manifest),
        )
    return DeterministicScaleMergeArtifacts(
        output_dir=output,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        merged_manifest_hash=merged_manifest_hash,
        partition_paths=partition_paths,
    )


__all__ = [
    "DeterministicScaleMergeArtifacts",
    "DeterministicScaleMergedManifest",
    "DeterministicScaleMergedShardBinding",
    "merge_deterministic_scale_shards",
]
