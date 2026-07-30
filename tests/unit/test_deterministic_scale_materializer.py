"""Scientific-scale deterministic materializer policy and CLI tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets import ActiveBenchmarkRegistry
from leanfaith.lean.extraction import EXTRACTION_SCHEMA_VERSION
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.schemas import (
    Applicability,
    ArtifactClass,
    DataStage,
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
    ViewStatus,
    make_id,
    make_source_ancestry_id,
)
from leanfaith.schemas.manifest import CodeState, OutputManifest
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import VariantDraft
from leanfaith.transforms.materialize import (
    build_derived_theorem_record,
    build_deterministic_pair_record,
)
from leanfaith.transforms.protocol import (
    build_deterministic_variant_record,
    build_transformation_attempt,
    build_transformation_audit,
    build_variant_draft,
)
from leanfaith.transforms.registry import TransformationExecution
from leanfaith.transforms.scale_materializer import (
    DeterministicScaleArtifacts,
    DeterministicScaleConfig,
    DeterministicScaleError,
    ScaleDraftResult,
    ScaleFailure,
    ScaleQuarantineRecord,
    ScaleRuleResult,
    ScaleSourceShard,
    _AdmissionState,
    _candidate_inline_source,
    _candidate_raw_request_ids,
    _candidate_validation,
    _CandidateValidation,
    _CandidateValidationFailure,
    _canonical_model_bytes,
    _clean_project_tree_hash,
    _load_jsonl,
    _load_source_inventory_manifest,
    _materialize_draft,
    _path_checksum,
    _project_records,
    _purge_candidate_raw_artifacts,
    _representation_payload_hash,
    _require_exact_resume_replay,
    _seed,
    _selection_key,
    _validate_resume_shard,
    _validate_unique_inputs,
    _write_new_atomic,
)
from tests.unit.record_factories import UTC_NOW, representation_record, theorem_record

_ROOT = Path(__file__).resolve().parents[2]
_RULES = (
    "n01_operator",
    "n02_quantifier",
    "n03_drop_hypothesis",
    "n07_literal_bound",
    "n10_nearby_theorem",
    "p01_alpha",
    "p02_binders",
    "p04_notation_lite",
)


class _ScriptedBackend:
    def __init__(self, statuses: list[LeanStatus]) -> None:
        self._statuses = iter(statuses)
        self.seen: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.seen.append(request)
        status = next(self._statuses)
        return LeanResult(
            request_id=request.request_id,
            request_hash="f" * 64,
            context_id=request.context_id,
            context_fingerprint="0" * 64,
            status=status,
            infrastructure_error=(
                "scripted infrastructure failure" if status != LeanStatus.VALID else None
            ),
        )


class _StaticRuleRuntime:
    def __init__(self, execution: TransformationExecution) -> None:
        self.execution = execution

    def execute(
        self,
        rule_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> TransformationExecution:
        del rule_id, theorem, representation, seed
        return self.execution


def _validation_draft() -> VariantDraft:
    return build_variant_draft(
        source_theorem_ids=(make_id("thm", {"scale": "source"}),),
        source_representation_ids=(make_id("repr", {"scale": "source"}),),
        context_id="ctx:" + "0" * 64,
        rule_id="p01_alpha",
        rule_version="1.0.0",
        family_id="p01_alpha",
        seed=7,
        candidate_code="theorem scale_candidate : True := by sorry",
        intended_relation=IntendedRelation.EQUIVALENT,
        intended_error_types=(),
        candidate_pool="deterministic_positive_provisional",
        transformation_trace=({"operation": "rename"},),
        generation_config_hash="e" * 64,
    )


def _accepted_source_shard(
    *,
    seed: int = 7,
    registry_hash: str = "a" * 64,
    generation_config_hash: str | None = None,
    candidate_code: str = "theorem t : True := by sorry",
) -> tuple[
    TheoremRecord,
    RepresentationRecord,
    ScaleSourceShard,
]:
    source = theorem_record(
        declaration_full_name="t",
        statement_content_hash=hashlib.sha256(b"theorem t : True := sorry").hexdigest(),
    )
    source_representation = representation_record(
        representation_id=make_id(
            "repr",
            {"theorem_id": source.theorem_id, "normalization_version": "repr_v3"},
        ),
        normalization_version="repr_v3",
        signature_explicit="theorem t : True",
        semantic_atoms=("True",),
        operator_tree={"kind": "const", "name": "True"},
        alpha_identity_fingerprint="1" * 64,
        view_status={
            "raw_proof_stripped": ViewStatus.OK,
            "headless": ViewStatus.OK,
            "signature_pp": ViewStatus.OK,
            "signature_explicit": ViewStatus.OK,
            "alpha_structural": ViewStatus.NOT_ATTEMPTED,
            "notation_light": ViewStatus.NOT_ATTEMPTED,
            "semantic_atoms": ViewStatus.OK,
            "operator_tree": ViewStatus.OK,
        },
    )
    source_representation = source_representation.model_copy(
        update={"content_hash": _representation_payload_hash(source_representation)}
    )
    draft = build_variant_draft(
        source_theorem_ids=(source.theorem_id,),
        source_representation_ids=(source_representation.representation_id,),
        context_id=source.context_id,
        rule_id="p01_alpha",
        rule_version="1.0.0",
        family_id="p01_alpha",
        seed=seed,
        candidate_code=candidate_code,
        intended_relation=IntendedRelation.EQUIVALENT,
        intended_error_types=(),
        candidate_pool="deterministic_positive_provisional",
        transformation_trace=({"operation": "alpha_rename"},),
        generation_config_hash=generation_config_hash or registry_hash,
    )
    attempt = build_transformation_attempt(
        family_id=draft.family_id,
        rule_id=draft.rule_id,
        rule_version=draft.rule_version,
        source_theorem_ids=draft.source_theorem_ids,
        source_representation_ids=draft.source_representation_ids,
        context_id=draft.context_id,
        registry_hash=registry_hash,
        generation_config_hash=draft.generation_config_hash,
        seed=draft.seed,
        applicability=Applicability(applicable=True, reason_codes=()),
        terminal_outcome="generated",
        draft_ids=(draft.draft_id,),
    )
    full_candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source=(
            f"import Mathlib\ntheorem helper : True := by trivial\n{draft.candidate_code}\n"
        ),
        metadata={
            "run_spec_hash": "b" * 64,
            "scale_profile_id": "deterministic_scale_v1",
            "source_index": 0,
            "validation_request_hash": "c" * 64,
            "inline_context_sha256": "d" * 64,
            "inline_context_persisted": False,
        },
    )
    candidate_representation = RepresentationRecord(
        representation_id=make_id(
            "repr",
            {
                "theorem_id": full_candidate.theorem_id,
                "normalization_version": "repr_v3",
            },
        ),
        theorem_id=full_candidate.theorem_id,
        normalization_version="repr_v3",
        context_id=full_candidate.context_id,
        raw_proof_stripped=full_candidate.proof_stripped_declaration,
        headless=": True",
        signature_pp="True",
        signature_explicit="True",
        semantic_atoms=("True",),
        operator_tree={"kind": "const", "name": "True"},
        alpha_identity_fingerprint="2" * 64,
        view_status={
            "raw_proof_stripped": ViewStatus.OK,
            "headless": ViewStatus.OK,
            "signature_pp": ViewStatus.OK,
            "signature_explicit": ViewStatus.OK,
            "alpha_structural": ViewStatus.NOT_ATTEMPTED,
            "notation_light": ViewStatus.NOT_ATTEMPTED,
            "semantic_atoms": ViewStatus.OK,
            "operator_tree": ViewStatus.OK,
        },
        content_hash="0" * 64,
        created_at=UTC_NOW,
    )
    candidate_representation = candidate_representation.model_copy(
        update={"content_hash": _representation_payload_hash(candidate_representation)}
    )
    audit = build_transformation_audit(
        draft=draft,
        applicability=Applicability(applicable=True, reason_codes=()),
        audit_config_hash="f" * 64,
        recommended_validation_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        recommended_quality_tier=QualityTier.PROVISIONAL,
        candidate_theorem_id=full_candidate.theorem_id,
        candidate_representation_id=candidate_representation.representation_id,
    )
    variant = build_deterministic_variant_record(
        attempt=attempt,
        draft=draft,
        audit=audit,
        candidate=full_candidate,
        candidate_representation=candidate_representation,
        polarity=Polarity.POSITIVE,
        metadata={
            "run_spec_hash": "b" * 64,
            "scale_profile_id": "deterministic_scale_v1",
            "source_index": 0,
        },
    )
    pair = build_deterministic_pair_record(
        source=source,
        candidate=full_candidate,
        draft=draft,
        audit=audit,
        metadata={
            "run_spec_hash": "b" * 64,
            "scale_profile_id": "deterministic_scale_v1",
            "source_index": 0,
        },
    )
    candidate = full_candidate.model_copy(update={"inline_elaboration_source": None})
    accepted = ScaleDraftResult(
        status="accepted",
        draft=draft,
        candidate_theorem=candidate,
        candidate_representation=candidate_representation,
        audit=audit,
        variant=variant,
        pair=pair,
    )
    rule = ScaleRuleResult(
        status="accepted",
        rule_id="p01_alpha",
        family_id="p01_alpha",
        polarity=Polarity.POSITIVE,
        seed=seed,
        source_theorem_ids=(source.theorem_id,),
        attempt=attempt,
        draft_results=(accepted,),
    )
    return (
        source,
        source_representation,
        ScaleSourceShard(
            run_spec_hash="b" * 64,
            source_index=0,
            source_theorem_id=source.theorem_id,
            source_representation_id=source_representation.representation_id,
            source_status="eligible",
            rule_results=(rule,),
        ),
    )


def _runtime_for_shard(shard: ScaleSourceShard) -> _StaticRuleRuntime:
    result = shard.rule_results[0]
    assert result.attempt is not None
    drafts = tuple(
        draft for draft_result in result.draft_results if (draft := draft_result.draft) is not None
    )
    return _StaticRuleRuntime(TransformationExecution(attempt=result.attempt, drafts=drafts))


def _canonical_extracted_inventory() -> tuple[TheoremRecord, RepresentationRecord]:
    source = "mathlib"
    revision = "fixture-revision"
    source_record = "Mathlib/Fixture.lean"
    full_name = "Fixture.t"
    signature_hash = "9" * 64
    context_id = theorem_record().context_id
    theorem_id = make_id(
        "thm",
        {
            "source": source,
            "revision": revision,
            "context_id": context_id,
            "source_record_id": source_record,
            "declaration_ordinal": 0,
            "extracted_signature_hash": signature_hash,
            "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        },
    )
    ancestry_id = make_source_ancestry_id(
        source=source,
        revision=revision,
        source_locator=source_record,
        declaration_full_name=full_name,
    )
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source=source,
        source_revision=revision,
        source_record=source_record,
        declaration_full_name=full_name,
        declaration_ordinal=0,
        statement_content_hash=signature_hash,
    )
    representation = representation_record(
        theorem_id=theorem_id,
        representation_id=make_id(
            "repr",
            {"theorem_id": theorem_id, "normalization_version": "repr_v3"},
        ),
        normalization_version="repr_v3",
        context_id=context_id,
        raw_proof_stripped=theorem.proof_stripped_declaration,
    )
    return theorem, representation.model_copy(
        update={"content_hash": _representation_payload_hash(representation)}
    )


def _write_output_manifests(
    tmp_path: Path,
    *,
    theorem_path: Path,
    representation_path: Path,
    theorem: TheoremRecord,
) -> tuple[Path, Path]:
    code = CodeState(
        git_revision="a" * 40,
        git_dirty=False,
        base_git_commit="a" * 40,
        code_tree_hash="b" * 64,
        tracked_diff_hash="e" * 64,
    )
    theorem_sha = hash_file(theorem_path)
    representation_sha = hash_file(representation_path)
    extraction = OutputManifest(
        stage=DataStage.ELABORATED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id="run_20260730T000000Z_00000001",
        source=theorem.source,
        source_revision=theorem.source_revision,
        config_hash="c" * 64,
        record_schema_version=1,
        row_count=1,
        attempted_row_count=1,
        file_checksums={str(theorem_path): theorem_sha},
        output_partition_checksums={str(theorem_path): theorem_sha},
        code_tree_hash=code.code_tree_hash,
        code=code,
        created_at=UTC_NOW,
    )
    represented = OutputManifest(
        stage=DataStage.REPRESENTED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id="run_20260730T000000Z_00000002",
        source="gate3",
        source_revision="from_theorem_partition",
        config_hash="d" * 64,
        record_schema_version=1,
        row_count=1,
        attempted_row_count=1,
        file_checksums={str(representation_path): representation_sha},
        input_partition_checksums={str(theorem_path): theorem_sha},
        output_partition_checksums={str(representation_path): representation_sha},
        context_hash=hash_canonical({"context_id": theorem.context_id}),
        code_tree_hash=code.code_tree_hash,
        code=code,
        created_at=UTC_NOW,
    )
    extraction_path = tmp_path / "extraction_manifest.json"
    representation_manifest_path = tmp_path / "representation_manifest.json"
    extraction_path.write_bytes(_canonical_model_bytes(extraction))
    representation_manifest_path.write_bytes(_canonical_model_bytes(represented))
    return extraction_path, representation_manifest_path


def test_scale_config_enables_exact_v1_families_and_preserves_semantic_boundary() -> None:
    loaded = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    )

    assert loaded.config.active_rule_ids == _RULES
    assert loaded.config.normalization_version == "repr_v3"
    assert loaded.config.negatives_remain_provisional is True
    assert loaded.config.positives_require_clean_mechanical_audit is True
    assert loaded.config.failed_proof_search_is_negative_evidence is False
    assert loaded.config.max_accepted_variants_per_root_ancestry == len(_RULES)
    assert loaded.config.max_accepted_variants_per_family_per_root_ancestry == 1


def test_candidate_validation_retries_infrastructure_but_not_semantic_invalidity() -> None:
    recovering = _ScriptedBackend([LeanStatus.CRASH, LeanStatus.VALID])
    result = _candidate_validation(
        recovering,  # type: ignore[arg-type]
        draft=_validation_draft(),
        context_id="ctx:" + "0" * 64,
        inline_source="theorem scale_candidate : True := by sorry",
        timeout_seconds=30.0,
    )

    assert result.status == ValidationStatus.ELABORATES
    assert [request.metadata["attempt"] for request in recovering.seen] == ["0", "1"]

    invalid = _ScriptedBackend([LeanStatus.INVALID, LeanStatus.VALID])
    with pytest.raises(_CandidateValidationFailure) as caught:
        _candidate_validation(
            invalid,  # type: ignore[arg-type]
            draft=_validation_draft(),
            context_id="ctx:" + "0" * 64,
            inline_source="theorem scale_candidate : MissingType := by sorry",
            timeout_seconds=30.0,
        )
    assert caught.value.status == LeanStatus.INVALID
    assert len(invalid.seen) == 1


def test_seed_and_source_order_are_replay_stable_and_order_independent() -> None:
    config = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    ).config

    assert _seed(config, "n10_nearby_theorem", ("thm:b", "thm:a")) == _seed(
        config,
        "n10_nearby_theorem",
        ("thm:a", "thm:b"),
    )
    assert _selection_key(config.base_seed, "thm:a") == _selection_key(
        config.base_seed,
        "thm:a",
    )
    assert _selection_key(config.base_seed, "thm:a") != _selection_key(
        config.base_seed,
        "thm:b",
    )


def test_strict_theorem_loader_accepts_gate3_wrapper_and_rejects_duplicate_json_keys(
    tmp_path: Path,
) -> None:
    theorem = theorem_record()
    wrapped = tmp_path / "wrapped.jsonl"
    wrapped.write_text(
        '{"representation":{"ignored":true},"theorem":' + theorem.model_dump_json() + "}\n",
        encoding="utf-8",
    )

    assert _load_jsonl(wrapped, TheoremRecord, wrapper_key="theorem") == (theorem,)

    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text('{"theorem":{},"theorem":{}}\n', encoding="utf-8")
    with pytest.raises(DeterministicScaleError, match="duplicate JSON key"):
        _load_jsonl(duplicate, TheoremRecord, wrapper_key="theorem")


def test_immutable_writer_deduplicates_identical_replay_and_rejects_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal" / "00000000-source.json"
    expected = hashlib.sha256(b"one\n").hexdigest()

    assert _write_new_atomic(path, b"one\n") == expected
    assert _write_new_atomic(path, b"one\n") == expected
    with pytest.raises(DeterministicScaleError, match="other bytes"):
        _write_new_atomic(path, b"two\n")
    assert path.read_bytes() == b"one\n"


def test_canonical_projection_is_accepted_only_and_quarantine_is_non_training() -> None:
    _, _, accepted_shard = _accepted_source_shard()
    rejected_draft = _validation_draft()
    failure = ScaleFailure(
        stage="candidate_validation",
        code="candidate_lean_invalid",
        detail="fixture rejection",
        source_theorem_ids=rejected_draft.source_theorem_ids,
        rule_id=rejected_draft.rule_id,
        draft_id=rejected_draft.draft_id,
    )
    rejected_rule = ScaleRuleResult(
        status="candidate_invalid",
        rule_id=rejected_draft.rule_id,
        family_id=rejected_draft.family_id,
        polarity=Polarity.POSITIVE,
        seed=rejected_draft.seed,
        source_theorem_ids=rejected_draft.source_theorem_ids,
        draft_results=(
            ScaleDraftResult(
                status="candidate_invalid",
                draft=rejected_draft,
                failure=failure,
            ),
        ),
    )
    mixed = accepted_shard.model_copy(
        update={"rule_results": (*accepted_shard.rule_results, rejected_rule)}
    )

    projected = _project_records((mixed,))
    projected_drafts = cast(tuple[VariantDraft, ...], projected["drafts"])
    projected_candidates = cast(
        tuple[TheoremRecord, ...],
        projected["candidate_theorems"],
    )
    projected_quarantine = cast(
        tuple[ScaleQuarantineRecord, ...],
        projected["quarantine"],
    )

    assert [record.draft_id for record in projected_drafts] == [
        accepted_shard.rule_results[0].draft_results[0].persistent_draft_id
    ]
    assert rejected_draft.draft_id not in {record.draft_id for record in projected_drafts}
    assert len(projected_candidates) == 1
    assert projected_candidates[0].inline_elaboration_source is None
    assert len(projected["candidate_representations"]) == 1
    assert len(projected["audits"]) == 1
    assert len(projected["variants"]) == 1
    assert len(projected["pairs"]) == 1
    assert len(projected["quarantine"]) == 1
    quarantine = projected_quarantine[0]
    assert quarantine.draft_id == rejected_draft.draft_id
    assert quarantine.training_eligible is False
    assert quarantine.evaluation_eligible is False
    assert "candidate_code" not in quarantine.model_dump(mode="json")


def test_protected_overlap_journal_is_hash_only_and_redacted() -> None:
    draft = _validation_draft()
    failure = ScaleFailure(
        stage="candidate_admission",
        code="protected_benchmark_overlap",
        detail="protected fixture",
        source_theorem_ids=draft.source_theorem_ids,
        rule_id=draft.rule_id,
        draft_id=draft.draft_id,
    )
    redacted = ScaleDraftResult(
        status="protected_benchmark_overlap",
        redacted_draft_id=draft.draft_id,
        redacted_candidate_code_hash=draft.candidate_code_hash,
        candidate_content_redacted=True,
        failure=failure,
    )
    rule = ScaleRuleResult(
        status="protected_benchmark_overlap",
        rule_id=draft.rule_id,
        family_id=draft.family_id,
        polarity=Polarity.POSITIVE,
        seed=draft.seed,
        source_theorem_ids=draft.source_theorem_ids,
        draft_results=(redacted,),
    )
    shard = ScaleSourceShard(
        run_spec_hash="b" * 64,
        source_index=0,
        source_theorem_id=draft.source_theorem_ids[0],
        source_representation_id=draft.source_representation_ids[0],
        source_status="eligible",
        rule_results=(rule,),
    )

    persisted = _canonical_model_bytes(shard)
    projected = _project_records((shard,))
    projected_quarantine = cast(
        tuple[ScaleQuarantineRecord, ...],
        projected["quarantine"],
    )

    assert draft.candidate_code.encode("utf-8") not in persisted
    assert draft.candidate_code_hash.encode("ascii") in persisted
    assert projected["drafts"] == ()
    assert projected["candidate_theorems"] == ()
    assert projected_quarantine[0].candidate_content_redacted is True


def test_project_context_requires_clean_tree_and_detects_later_mutation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "mathlib"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "Source.lean").write_text("theorem source : True := by trivial\n")
    subprocess.run(["git", "add", "Source.lean"], cwd=project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=LeanFaith Test",
            "-c",
            "user.email=leanfaith@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=project,
        check=True,
    )
    revision, tree_hash = _clean_project_tree_hash(project)
    assert revision
    assert tree_hash
    assert _clean_project_tree_hash(
        project,
        expected_revision=revision,
        expected_tree_hash=tree_hash,
    ) == (revision, tree_hash)

    (project / "Source.lean").write_text("theorem source : False := by sorry\n")
    with pytest.raises(DeterministicScaleError, match="checkout is dirty"):
        _clean_project_tree_hash(
            project,
            expected_revision=revision,
            expected_tree_hash=tree_hash,
        )


def test_resume_semantics_reject_nested_accepted_lineage_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_materializer
    from leanfaith.transforms.registry import load_transformation_registry

    loaded_registry = load_transformation_registry(_ROOT)
    config = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    ).config.model_copy(update={"active_rule_ids": ("p01_alpha",)})
    seed = _seed(config, "p01_alpha", (theorem_record().theorem_id,))
    source, source_representation, shard = _accepted_source_shard(
        seed=seed,
        registry_hash=loaded_registry.registry_hash,
    )
    monkeypatch.setattr(scale_materializer, "_source_failure", lambda *args, **kwargs: None)
    benchmark = cast(ActiveBenchmarkRegistry, object())
    runtime = _runtime_for_shard(shard)
    _validate_resume_shard(
        shard=shard,
        expected_index=0,
        source=source,
        source_representation=source_representation,
        theorem_by_id={source.theorem_id: source},
        representation_by_theorem={source.theorem_id: source_representation},
        expected_donors=(),
        config=config,
        loaded_registry=loaded_registry,
        positive_runtime=runtime,
        negative_runtime=runtime,
        pair_rule=None,
        benchmark=benchmark,
        run_spec_hash="b" * 64,
    )

    accepted = shard.rule_results[0].draft_results[0]
    assert accepted.variant is not None
    tampered_variant = accepted.variant.model_copy(update={"polarity_metadata": Polarity.NEGATIVE})
    tampered_draft = accepted.model_copy(update={"variant": tampered_variant})
    tampered_rule = shard.rule_results[0].model_copy(update={"draft_results": (tampered_draft,)})
    tampered_shard = shard.model_copy(update={"rule_results": (tampered_rule,)})
    with pytest.raises(DeterministicScaleError, match="variant"):
        _validate_resume_shard(
            shard=tampered_shard,
            expected_index=0,
            source=source,
            source_representation=source_representation,
            theorem_by_id={source.theorem_id: source},
            representation_by_theorem={source.theorem_id: source_representation},
            expected_donors=(),
            config=config,
            loaded_registry=loaded_registry,
            positive_runtime=runtime,
            negative_runtime=runtime,
            pair_rule=None,
            benchmark=benchmark,
            run_spec_hash="b" * 64,
        )


def test_resume_semantics_accepts_empty_n10_donor_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_materializer
    from leanfaith.transforms.registry import load_transformation_registry

    loaded_registry = load_transformation_registry(_ROOT)
    config = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    ).config.model_copy(update={"active_rule_ids": ("n10_nearby_theorem",)})
    source, source_representation, base_shard = _accepted_source_shard(
        registry_hash=loaded_registry.registry_hash,
    )
    source_ids = (source.theorem_id,)
    no_donor = ScaleRuleResult(
        status="no_donor",
        rule_id="n10_nearby_theorem",
        family_id="n10_nearby_theorem",
        polarity=Polarity.NEGATIVE,
        seed=_seed(config, "n10_nearby_theorem", source_ids),
        source_theorem_ids=source_ids,
    )
    shard = base_shard.model_copy(update={"rule_results": (no_donor,)})
    monkeypatch.setattr(scale_materializer, "_source_failure", lambda *args, **kwargs: None)
    benchmark = cast(ActiveBenchmarkRegistry, object())

    _validate_resume_shard(
        shard=shard,
        expected_index=0,
        source=source,
        source_representation=source_representation,
        theorem_by_id={source.theorem_id: source},
        representation_by_theorem={source.theorem_id: source_representation},
        expected_donors=(),
        config=config,
        loaded_registry=loaded_registry,
        positive_runtime=object(),
        negative_runtime=object(),
        pair_rule=None,
        benchmark=benchmark,
        run_spec_hash="b" * 64,
    )

    tampered = no_donor.model_copy(update={"seed": no_donor.seed + 1})
    with pytest.raises(DeterministicScaleError, match="empty-donor N10"):
        _validate_resume_shard(
            shard=shard.model_copy(update={"rule_results": (tampered,)}),
            expected_index=0,
            source=source,
            source_representation=source_representation,
            theorem_by_id={source.theorem_id: source},
            representation_by_theorem={source.theorem_id: source_representation},
            expected_donors=(),
            config=config,
            loaded_registry=loaded_registry,
            positive_runtime=object(),
            negative_runtime=object(),
            pair_rule=None,
            benchmark=benchmark,
            run_spec_hash="b" * 64,
        )


def test_resume_rejects_fully_recomputed_wrong_generation_config_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_materializer
    from leanfaith.transforms.registry import load_transformation_registry

    loaded_registry = load_transformation_registry(_ROOT)
    config = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    ).config.model_copy(update={"active_rule_ids": ("p01_alpha",)})
    seed = _seed(config, "p01_alpha", (theorem_record().theorem_id,))
    source, source_representation, shard = _accepted_source_shard(
        seed=seed,
        registry_hash=loaded_registry.registry_hash,
        generation_config_hash="f" * 64,
    )
    runtime = _runtime_for_shard(shard)
    monkeypatch.setattr(scale_materializer, "_source_failure", lambda *args, **kwargs: None)

    with pytest.raises(DeterministicScaleError, match="attempt lineage"):
        _validate_resume_shard(
            shard=shard,
            expected_index=0,
            source=source,
            source_representation=source_representation,
            theorem_by_id={source.theorem_id: source},
            representation_by_theorem={source.theorem_id: source_representation},
            expected_donors=(),
            config=config,
            loaded_registry=loaded_registry,
            positive_runtime=runtime,
            negative_runtime=runtime,
            pair_rule=None,
            benchmark=cast(ActiveBenchmarkRegistry, object()),
            run_spec_hash="b" * 64,
        )


def test_exact_resume_replay_rejects_self_consistent_non_rule_candidate() -> None:
    source, representation, expected = _accepted_source_shard()
    forged_source, forged_representation, forged = _accepted_source_shard(
        candidate_code="theorem t : True ∧ True := by sorry",
    )
    assert source == forged_source
    assert representation == forged_representation
    assert expected.rule_results[0].attempt != forged.rule_results[0].attempt

    with pytest.raises(DeterministicScaleError, match="exact Lean-backed"):
        _require_exact_resume_replay(forged, expected)


def test_exact_resume_replay_rejects_rehashed_forged_signature_view() -> None:
    _, _, expected = _accepted_source_shard()
    accepted = expected.rule_results[0].draft_results[0]
    assert accepted.candidate_representation is not None
    forged_representation = accepted.candidate_representation.model_copy(
        update={"signature_pp": "False"}
    )
    forged_representation = forged_representation.model_copy(
        update={"content_hash": _representation_payload_hash(forged_representation)}
    )
    forged_result = accepted.model_copy(update={"candidate_representation": forged_representation})
    forged_rule = expected.rule_results[0].model_copy(update={"draft_results": (forged_result,)})
    forged_shard = expected.model_copy(update={"rule_results": (forged_rule,)})

    with pytest.raises(DeterministicScaleError, match="exact Lean-backed"):
        _require_exact_resume_replay(forged_shard, expected)


def test_protected_candidate_raw_artifacts_are_purged_without_touching_accepted(
    tmp_path: Path,
) -> None:
    _, _, shard = _accepted_source_shard()
    accepted = shard.rule_results[0].draft_results[0]
    assert accepted.draft is not None
    assert accepted.candidate_theorem is not None
    request_ids = _candidate_raw_request_ids(
        accepted.draft,
        candidate_theorem_id=accepted.candidate_theorem.theorem_id,
    )
    protected_ids = tuple(sorted(request_ids))[:2]
    accepted_request_id = "unrelated-accepted-candidate-request"

    for index, request_id in enumerate((*protected_ids, accepted_request_id)):
        payload = {
            "request": {
                "request_id": request_id,
                "code": (
                    accepted.draft.candidate_code
                    if request_id in request_ids
                    else "theorem accepted_candidate : True := by trivial"
                ),
            }
        }
        (tmp_path / f"{index}.json").write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )

    assert _purge_candidate_raw_artifacts(tmp_path, request_ids=request_ids) == len(protected_ids)
    remaining = tuple(tmp_path.glob("*.json"))
    assert len(remaining) == 1
    assert accepted_request_id in remaining[0].read_text(encoding="utf-8")
    assert accepted.draft.candidate_code not in remaining[0].read_text(encoding="utf-8")


def test_representation_overlap_is_redacted_before_audit_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_materializer
    from leanfaith.transforms.registry import load_transformation_registry

    source, source_representation, shard = _accepted_source_shard()
    rule = shard.rule_results[0]
    result = rule.draft_results[0]
    assert rule.attempt is not None
    assert result.draft is not None
    assert result.candidate_representation is not None
    config = load_config(
        _ROOT / "configs/transformations/deterministic_scale_v1.yaml",
        DeterministicScaleConfig,
    ).config
    audit_called = False
    purge_called = False

    class _Index:
        @staticmethod
        def contains_lean(text: str) -> bool:
            del text
            return False

    class _AuditMustNotRun:
        def audit(self, *args: object, **kwargs: object) -> object:
            nonlocal audit_called
            del args, kwargs
            audit_called = True
            raise AssertionError("audit ran before protected overlap redaction")

    def fake_purge(*args: object, **kwargs: object) -> int:
        nonlocal purge_called
        del args, kwargs
        purge_called = True
        return 1

    monkeypatch.setattr(
        scale_materializer,
        "_candidate_validation",
        lambda *args, **kwargs: _CandidateValidation(
            status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            diagnostics=(),
            request_hash="c" * 64,
        ),
    )
    monkeypatch.setattr(
        scale_materializer,
        "_candidate_representation",
        lambda *args, **kwargs: result.candidate_representation,
    )
    monkeypatch.setattr(scale_materializer, "_protected_overlap", lambda *args: True)
    monkeypatch.setattr(scale_materializer, "_purge_candidate_raw_artifacts", fake_purge)

    protected = _materialize_draft(
        backend=cast(LeanInteractBackend, object()),
        loaded_registry=load_transformation_registry(_ROOT),
        unary_runtime=_AuditMustNotRun(),
        pair_rule=None,
        benchmark=cast(
            ActiveBenchmarkRegistry,
            type("_Benchmark", (), {"index": _Index()})(),
        ),
        config=config,
        run_spec_hash="b" * 64,
        source_index=0,
        primary=source,
        primary_representation=source_representation,
        sources=(source,),
        source_representations=(source_representation,),
        attempt=rule.attempt,
        draft=result.draft,
        polarity=Polarity.POSITIVE,
        project_dir=tmp_path,
        import_header="import Mathlib",
        raw_response_dir=tmp_path / "raw",
        state=_AdmissionState(
            root_counts=Counter(),
            family_root_counts=Counter(),
            family_counts=Counter(),
            candidate_keys=set(),
            variant_ids=set(),
            pair_ids=set(),
        ),
    )

    assert protected.status == "protected_benchmark_overlap"
    assert protected.draft is None
    assert protected.candidate_content_redacted is True
    assert purge_called is True
    assert audit_called is False


def test_source_inventory_rejects_extraction_identity_and_representation_tampering() -> None:
    source, representation = _canonical_extracted_inventory()
    assert _validate_unique_inputs((source,), (representation,)) == {
        source.theorem_id: representation
    }

    with pytest.raises(DeterministicScaleError, match="extraction identity mismatch"):
        _validate_unique_inputs(
            (source.model_copy(update={"statement_content_hash": "0" * 64}),),
            (representation,),
        )
    with pytest.raises(DeterministicScaleError, match="representation ID mismatch"):
        _validate_unique_inputs(
            (source,),
            (
                representation.model_copy(
                    update={"representation_id": make_id("repr", {"tampered": True})}
                ),
            ),
        )
    with pytest.raises(DeterministicScaleError, match="content hash mismatch"):
        _validate_unique_inputs(
            (source,),
            (representation.model_copy(update={"content_hash": "0" * 64}),),
        )


def test_authoritative_inventory_manifest_rejects_self_consistent_partition_rewrite(
    tmp_path: Path,
) -> None:
    source, representation = _canonical_extracted_inventory()
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    manifest_path = tmp_path / "inventory.json"
    theorem_path.write_bytes(_canonical_model_bytes(source))
    representation_path.write_bytes(_canonical_model_bytes(representation))
    extraction_manifest, representation_manifest = _write_output_manifests(
        tmp_path,
        theorem_path=theorem_path,
        representation_path=representation_path,
        theorem=source,
    )
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_kind": "deterministic_scale_source_inventory_manifest",
                "context_id": source.context_id,
                "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
                "normalization_version": "repr_v3",
                "theorem_partition": {
                    "path": theorem_path.name,
                    "sha256": hashlib.sha256(theorem_path.read_bytes()).hexdigest(),
                    "record_count": 1,
                },
                "representation_partition": {
                    "path": representation_path.name,
                    "sha256": hashlib.sha256(representation_path.read_bytes()).hexdigest(),
                    "record_count": 1,
                },
                "theorem_upstream_manifest": {
                    "path": extraction_manifest.name,
                    "sha256": hash_file(extraction_manifest),
                    "manifest_kind": "output_manifest",
                },
                "representation_upstream_manifest": {
                    "path": representation_manifest.name,
                    "sha256": hash_file(representation_manifest),
                    "manifest_kind": "output_manifest",
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _load_source_inventory_manifest(
        manifest_path,
        theorem_path=theorem_path,
        representation_path=representation_path,
    )

    tampered_hash = "8" * 64
    tampered_id = make_id(
        "thm",
        {
            "source": source.source,
            "revision": source.source_revision,
            "context_id": source.context_id,
            "source_record_id": source.source_record,
            "declaration_ordinal": source.declaration_ordinal,
            "extracted_signature_hash": tampered_hash,
            "extraction_schema_version": EXTRACTION_SCHEMA_VERSION,
        },
    )
    tampered_source = source.model_copy(
        update={
            "theorem_id": tampered_id,
            "statement_content_hash": tampered_hash,
        }
    )
    tampered_representation = representation.model_copy(
        update={
            "theorem_id": tampered_id,
            "representation_id": make_id(
                "repr",
                {"theorem_id": tampered_id, "normalization_version": "repr_v3"},
            ),
        }
    )
    tampered_representation = tampered_representation.model_copy(
        update={"content_hash": _representation_payload_hash(tampered_representation)}
    )
    theorem_path.write_bytes(_canonical_model_bytes(tampered_source))
    representation_path.write_bytes(_canonical_model_bytes(tampered_representation))

    with pytest.raises(DeterministicScaleError, match="theorem partition hash"):
        _load_source_inventory_manifest(
            manifest_path,
            theorem_path=theorem_path,
            representation_path=representation_path,
        )


def test_candidate_inline_source_replaces_inline_declaration_once() -> None:
    source = theorem_record(
        proof_stripped_declaration="theorem source : True := by sorry",
        inline_elaboration_source=(
            "import Mathlib\n"
            "namespace SourceNamespace\n"
            "theorem source : True := by sorry\n"
            "end SourceNamespace\n"
        ),
    )
    candidate = "theorem source : False ∨ True := by sorry"

    rebuilt = _candidate_inline_source(
        source,
        candidate,
        project_dir=_ROOT,
        import_header="import Mathlib",
    )

    assert candidate in rebuilt
    assert source.proof_stripped_declaration not in rebuilt
    assert rebuilt.startswith("import Mathlib\nnamespace SourceNamespace\n")
    assert rebuilt.endswith("end SourceNamespace\n")


def test_candidate_inline_source_reconstructs_mathlib_prefix_without_old_declaration(
    tmp_path: Path,
) -> None:
    source_file = tmp_path / "Source.lean"
    source_file.write_text(
        "import Mathlib\n"
        "namespace PrefixContext\n"
        "variable (n : Nat)\n"
        "theorem source : n = n := by rfl\n"
        "end PrefixContext\n",
        encoding="utf-8",
    )
    source = theorem_record(
        source_file="Source.lean",
        source_range=(4, 4),
        declaration_name="source",
        declaration_full_name="PrefixContext.source",
        proof_stripped_declaration="theorem source : n = n := by sorry",
    )
    candidate = "theorem source : n ≤ n := by sorry"

    rebuilt = _candidate_inline_source(
        source,
        candidate,
        project_dir=tmp_path,
        import_header="import Mathlib",
    )

    assert rebuilt == (
        "import Mathlib\n"
        "namespace PrefixContext\n"
        "variable (n : Nat)\n"
        "theorem source : n ≤ n := by sorry\n"
    )
    assert "by rfl" not in rebuilt


def test_scale_cli_requires_all_immutable_inputs() -> None:
    result = CliRunner().invoke(
        app,
        ["generate-deterministic", "--materialize-scale"],
    )

    assert result.exit_code == 2
    assert "--theorems" in result.output
    assert "--representations" in result.output
    assert "--source-inventory-manifest" in result.output
    assert "--project-dir" in result.output
    assert "--output-dir" in result.output


def test_scale_inventory_freeze_cli_writes_and_replays_immutable_manifest(
    tmp_path: Path,
) -> None:
    source, representation = _canonical_extracted_inventory()
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    manifest_path = tmp_path / "inventory.json"
    theorem_path.write_bytes(_canonical_model_bytes(source))
    representation_path.write_bytes(_canonical_model_bytes(representation))
    extraction_manifest, representation_manifest = _write_output_manifests(
        tmp_path,
        theorem_path=theorem_path,
        representation_path=representation_path,
        theorem=source,
    )
    args = [
        "generate-deterministic",
        "--freeze-scale-inventory",
        "--root",
        str(tmp_path),
        "--theorems",
        str(theorem_path),
        "--representations",
        str(representation_path),
        "--source-inventory-manifest",
        str(manifest_path),
        "--theorem-upstream-manifest",
        str(extraction_manifest),
        "--representation-upstream-manifest",
        str(representation_manifest),
    ]

    first = CliRunner().invoke(app, args)
    second = CliRunner().invoke(app, args)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "theorems=1 representations=1" in first.output
    assert first.output == second.output
    frozen = _load_source_inventory_manifest(
        manifest_path,
        theorem_path=theorem_path,
        representation_path=representation_path,
    )
    assert frozen.context_id == source.context_id


def test_scale_inventory_freeze_rejects_claim_rewrite_before_trust_freeze(
    tmp_path: Path,
) -> None:
    source, representation = _canonical_extracted_inventory()
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    theorem_path.write_bytes(_canonical_model_bytes(source))
    representation_path.write_bytes(_canonical_model_bytes(representation))
    extraction_manifest, representation_manifest = _write_output_manifests(
        tmp_path,
        theorem_path=theorem_path,
        representation_path=representation_path,
        theorem=source,
    )

    changed = source.model_copy(
        update={"proof_stripped_declaration": "theorem t : False := by sorry"}
    )
    changed_representation = representation.model_copy(
        update={"raw_proof_stripped": changed.proof_stripped_declaration}
    )
    changed_representation = changed_representation.model_copy(
        update={"content_hash": _representation_payload_hash(changed_representation)}
    )
    theorem_path.write_bytes(_canonical_model_bytes(changed))
    representation_path.write_bytes(_canonical_model_bytes(changed_representation))

    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--freeze-scale-inventory",
            "--root",
            str(tmp_path),
            "--theorems",
            str(theorem_path),
            "--representations",
            str(representation_path),
            "--source-inventory-manifest",
            str(tmp_path / "inventory.json"),
            "--theorem-upstream-manifest",
            str(extraction_manifest),
            "--representation-upstream-manifest",
            str(representation_manifest),
        ],
    )

    assert result.exit_code == 1
    assert "does not bind the exact theorem partition/count" in result.output


def test_manifest_checksum_requires_reviewed_content_relocation(
    tmp_path: Path,
) -> None:
    original = tmp_path / "producer" / "records.jsonl"
    relocated = tmp_path / "consumer" / "records.jsonl"
    original.parent.mkdir()
    relocated.parent.mkdir()
    original.write_bytes(b'{"record":1}\n')
    relocated.write_bytes(original.read_bytes())
    digest = hash_file(original)
    checksums = {str(original): digest}

    assert (
        _path_checksum(
            checksums,
            supplied_path=relocated,
            repo_root=tmp_path,
        )
        is None
    )
    assert (
        _path_checksum(
            checksums,
            supplied_path=relocated,
            repo_root=tmp_path,
            allow_content_addressed_relocation=True,
        )
        == digest
    )

    relocated.write_bytes(b'{"record":2}\n')
    assert (
        _path_checksum(
            checksums,
            supplied_path=relocated,
            repo_root=tmp_path,
            allow_content_addressed_relocation=True,
        )
        is None
    )


def test_manifest_checksum_rejects_ambiguous_content_relocation(
    tmp_path: Path,
) -> None:
    relocated = tmp_path / "relocated.jsonl"
    relocated.write_bytes(b'{"record":1}\n')
    digest = hash_file(relocated)

    with pytest.raises(DeterministicScaleError, match="ambiguous content bindings"):
        _path_checksum(
            {
                "/producer/a.jsonl": digest,
                "/producer/b.jsonl": digest,
            },
            supplied_path=relocated,
            repo_root=tmp_path,
            allow_content_addressed_relocation=True,
        )


def test_manifest_checksum_does_not_override_wrong_exact_path_digest(
    tmp_path: Path,
) -> None:
    supplied = tmp_path / "records.jsonl"
    supplied.write_bytes(b'{"record":1}\n')
    correct_digest = hash_file(supplied)

    assert (
        _path_checksum(
            {
                str(supplied): "0" * 64,
                "/producer/other-copy.jsonl": correct_digest,
            },
            supplied_path=supplied,
            repo_root=tmp_path,
            allow_content_addressed_relocation=True,
        )
        == "0" * 64
    )


def test_scale_cli_delegates_and_reports_unresolved_provisional_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from leanfaith.transforms import scale_materializer

    artifacts = DeterministicScaleArtifacts(
        output_dir=tmp_path / "output",
        run_spec_path=tmp_path / "output/run_spec.json",
        manifest_path=tmp_path / "output/manifest.json",
        manifest_sha256="a" * 64,
        partition_paths={},
    )
    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> DeterministicScaleArtifacts:
        seen.update(kwargs)
        return artifacts

    monkeypatch.setattr(
        scale_materializer,
        "run_deterministic_scale_materialization",
        fake_run,
    )
    result = CliRunner().invoke(
        app,
        [
            "generate-deterministic",
            "--materialize-scale",
            "--root",
            str(tmp_path),
            "--theorems",
            str(tmp_path / "theorems.jsonl"),
            "--representations",
            str(tmp_path / "representations.jsonl"),
            "--source-inventory-manifest",
            str(tmp_path / "inventory.json"),
            "--project-dir",
            str(tmp_path / "mathlib"),
            "--output-dir",
            str(tmp_path / "output"),
            "--max-sources",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert "resolved_semantic_labels=0" in result.output
    assert "promoted_items=0" in result.output
    assert "output_tier=provisional" in result.output
    assert seen["paths"] == RepoPaths(root=tmp_path)
    assert seen["max_sources"] == 1
    assert seen["source_inventory_manifest"] == tmp_path / "inventory.json"
