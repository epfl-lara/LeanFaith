from __future__ import annotations

import datetime
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.datasets import experimental_first_hop_projection as projection
from leanfaith.datasets.denylist import DenylistIndex, FrozenBenchmark, FrozenRegistry
from leanfaith.datasets.experimental_machine_supervision import (
    ExperimentalMachineSupervisionRecord,
    ExperimentalStatementView,
)
from leanfaith.representations.views import normalize_headless, signature_near_dup_hash
from leanfaith.schemas.enums import IntendedRelation
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_seed import CompositionSeedRecord
from leanfaith.transforms.provisional_pair_combine import (
    MaterializationRootBinding,
    ProvisionalPairCombinationManifest,
    ProvisionalPairObservation,
    UniqueProvisionalPair,
)

SHA = "a" * 64
CONTEXT = f"ctx:{'1' * 64}"
ANCESTRY = f"anc:{'2' * 64}"


def _config() -> projection.ExperimentalFirstHopProjectionConfig:
    return projection.ExperimentalFirstHopProjectionConfig(
        profile_id="fixture_full_first_hop",
        expected_gross_observation_count=3,
        expected_unique_pair_count=3,
        expected_counts_by_source={"mathlib": 2, "sft_classic": 1},
        audit_manifest_sha256=SHA,
        audit_gross_observations_sha256=SHA,
        audit_unique_pairs_sha256=SHA,
        positive_seed_manifest_sha256=SHA,
        positive_seed_records_sha256=SHA,
        positive_seed_theorems_sha256=SHA,
        positive_seed_representations_sha256=SHA,
        benchmark_manifest_sha256=SHA,
        benchmark_active_registry_sha256=SHA,
        benchmark_authorization_sha256=SHA,
    )


def _observation(
    suffix: str,
    *,
    family: str,
    relation: IntendedRelation,
    source: str,
    root_id: str,
) -> ProvisionalPairObservation:
    polarity = "positive" if relation == IntendedRelation.EQUIVALENT else "negative"
    return ProvisionalPairObservation.model_construct(
        observation_id=f"detprov_observation:{suffix * 64}",
        root_binding_id=root_id,
        result_id=f"result:{suffix}",
        result_line_number=1,
        profile_id=f"profile:{suffix}",
        family_id=family,
        rule_id=family,
        context_id=CONTEXT,
        source_theorem_ids=(f"thm:source-{suffix}",),
        source_representation_ids=(f"repr:source-{suffix}",),
        source_categories=(source,),
        source_root_ancestry_ids=(ANCESTRY,),
        pair_id=f"pair:{suffix}",
        attempt_id=f"attempt:{suffix}",
        draft_id=f"draft:{suffix}",
        audit_id=f"audit:{suffix}",
        variant_id=f"variant:{suffix}",
        candidate_theorem_id=f"thm:candidate-{suffix}",
        candidate_representation_id=f"repr:candidate-{suffix}",
        candidate_code_hash=suffix * 64,
        candidate_alpha_identity_fingerprint=(suffix.upper() * 64).lower(),
        intended_relation=relation,
        polarity_metadata=polarity,
        exact_pair_key=suffix * 64,
        candidate_code_key=(suffix.upper() * 64).lower(),
        ancestry_candidate_key="b" * 64,
        alpha_candidate_key="c" * 64,
    )


def _unique(observation: ProvisionalPairObservation) -> UniqueProvisionalPair:
    return UniqueProvisionalPair.model_construct(
        unique_pair_id=f"detprov_pair:{observation.exact_pair_key}",
        exact_pair_key=observation.exact_pair_key,
        context_id=observation.context_id,
        source_theorem_ids=observation.source_theorem_ids,
        candidate_code_hash=observation.candidate_code_hash,
        observation_ids=(observation.observation_id,),
        provenance_count=1,
        family_ids=(observation.family_id,),
        source_categories=observation.source_categories,
        intended_relations=(observation.intended_relation,),
        polarity_metadata=(observation.polarity_metadata,),
        conflicting_intentions=False,
    )


def _root(observation: ProvisionalPairObservation, *, kind: str) -> MaterializationRootBinding:
    return MaterializationRootBinding.model_construct(
        root_binding_id=observation.root_binding_id,
        run_kind=kind,
        context_id=observation.context_id,
    )


def _seed(
    observation: ProvisionalPairObservation,
    unique: UniqueProvisionalPair,
    *,
    combination_hash: str,
) -> CompositionSeedRecord:
    return CompositionSeedRecord.model_construct(
        input_combination_hash=combination_hash,
        unique_pair_id=unique.unique_pair_id,
        exact_pair_key=unique.exact_pair_key,
        first_hop_observation_ids=unique.observation_ids,
        selected_observation_id=observation.observation_id,
        first_hop_root_binding_id=observation.root_binding_id,
        first_hop_result_id=observation.result_id,
        first_hop_result_line_number=observation.result_line_number,
        first_hop_profile_id=observation.profile_id,
        first_hop_rule_id=observation.rule_id,
        first_hop_family_id=observation.family_id,
        first_hop_attempt_id=observation.attempt_id,
        first_hop_draft_id=observation.draft_id,
        first_hop_audit_id=observation.audit_id,
        first_hop_variant_id=observation.variant_id,
        source_theorem_id=observation.source_theorem_ids[0],
        source_representation_id=observation.source_representation_ids[0],
        intermediate_theorem_id=observation.candidate_theorem_id,
        intermediate_representation_id=observation.candidate_representation_id,
        context_id=observation.context_id,
        root_ancestry_ids=observation.source_root_ancestry_ids,
        intermediate_candidate_code_hash=observation.candidate_code_hash,
        intermediate_alpha_identity_fingerprint=(observation.candidate_alpha_identity_fingerprint),
        certificate_kind="binder_permutation_certificate",
        certificate_sha256="d" * 64,
    )


def _audit_manifest(
    observations: tuple[ProvisionalPairObservation, ...],
    *,
    combination_hash: str,
) -> ProvisionalPairCombinationManifest:
    roots = tuple(
        sorted(
            (
                _root(
                    observation,
                    kind=(
                        "e2"
                        if observation.family_id.startswith("p14")
                        else "d0"
                        if observation.family_id.startswith("n")
                        else "e0"
                    ),
                )
                for observation in observations
            ),
            key=lambda item: item.root_binding_id,
        )
    )
    return ProvisionalPairCombinationManifest.model_construct(
        combination_hash=combination_hash,
        root_bindings=roots,
    )


def _material(
    suffix: str,
    *,
    source: str,
    declaration_name: str,
    proposition: str,
    proof: str,
) -> projection._SideMaterial:
    declaration = f"@[simp]\ntheorem {declaration_name} {proposition} := {proof}"
    normalized = normalize_headless(declaration)
    assert normalized is not None
    theorem = TheoremRecord.model_construct(
        theorem_id=f"thm:{suffix}",
        source=source,
        source_record=f"row:{suffix}",
        source_record_id=None,
        upstream_uuid=None,
        context_id=CONTEXT,
        root_ancestry_ids=(ANCESTRY,),
        proof_stripped_declaration=declaration,
        statement_content_hash="3" * 64,
    )
    representation = RepresentationRecord.model_construct(
        theorem_id=theorem.theorem_id,
        representation_id=f"repr:{suffix}",
        context_id=CONTEXT,
        content_hash="4" * 64,
        alpha_identity_fingerprint="5" * 64,
        headless=normalized,
        signature_pp=normalized,
        signature_explicit=f"explicit:{normalized}",
    )
    return projection._SideMaterial(theorem=theorem, representation=representation)


def _empty_registry() -> Any:
    registry = FrozenRegistry(
        frozen_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        representation_signatures_appended=True,
        benchmarks=(),
    )
    return SimpleNamespace(index=DenylistIndex(registry))


def _record(
    *,
    source_category: str = "mathlib",
    tier: str | None = "D0",
    target: str | None = "not_same_claim",
    reasons: tuple[str, ...] = (),
) -> projection.ExperimentalFirstHopProjectionRecord:
    source = _material(
        "source",
        source=source_category,
        declaration_name="reference_name",
        proposition="(x : Nat) : x = x",
        proof="by exact rfl",
    )
    candidate = _material(
        "candidate",
        source="generated",
        declaration_name="candidate_name",
        proposition="(x : Nat) : x != x",
        proof="by sorry",
    )
    source_view, _ = projection._project_view(source)
    candidate_view, _ = projection._project_view(candidate)
    assert source_view is not None and candidate_view is not None
    selectable = tier is not None and not reasons
    private = source_category == "sft_classic"
    payload: dict[str, object] = {
        "projection_record_id": f"experimental_first_hop_pair:{'0' * 64}",
        "unique_pair_id": "detprov_pair:fixture",
        "exact_pair_key": "6" * 64,
        "observation_ids": ("observation:fixture",),
        "selected_observation_id": "observation:fixture",
        "provenance_count": 1,
        "root_binding_id": "root:fixture",
        "result_id": "result:fixture",
        "result_line_number": 1,
        "pair_id": "pair:fixture",
        "family_ids": ("n11_bound_variable_substitution",),
        "rule_id": "n11_bound_variable_substitution",
        "intended_relations": ("near_miss",),
        "source_category": source_category,
        "source_root_ancestry_ids": (ANCESTRY,),
        "evidence_tier": tier,
        "pseudo_target": target,
        "certificate_kind": None,
        "certificate_sha256": None,
        "selection_status": "selectable" if selectable else "excluded",
        "exclusion_reasons": reasons,
        "source": source_view,
        "candidate": candidate_view,
        "private_source_content": private,
        "redistribution_allowed": not private,
        "external_transmission_allowed": not private,
        "release_eligible": not private,
        "experimental_mixed_input_eligible": selectable,
    }
    provisional = projection.ExperimentalFirstHopProjectionRecord.model_construct(
        _fields_set=None, **payload
    )
    payload["projection_record_id"] = "experimental_first_hop_pair:" + (
        hash_canonical(
            projection._without_id(provisional.model_dump(mode="json"), "projection_record_id")
        )
    )
    return projection.ExperimentalFirstHopProjectionRecord.model_validate(payload)


def test_projection_config_fixes_clean_e2_and_d0_family_registries() -> None:
    config = _config()

    assert config.e2_positive_families == (
        "p14_independent_binder_permutation",
        "p15_root_iff_reversal",
        "p16_conjunction_reassociation",
        "p17_hypothesis_packing",
        "p18_root_equality_symmetry",
    )
    assert len(config.d0_negative_families) == 8
    assert config.admitted_source_categories == ("mathlib", "sft_classic")

    with pytest.raises(ValueError, match="exceeds gross"):
        _config().model_copy(update={"expected_unique_pair_count": 4}).model_validate(
            _config().model_copy(update={"expected_unique_pair_count": 4})
        )


def test_headless_projection_is_name_and_proof_placeholder_invariant() -> None:
    left = _material(
        "left",
        source="mathlib",
        declaration_name="first_name",
        proposition="(x : Nat) : x + 0 = x",
        proof="by sorry",
    )
    right = _material(
        "right",
        source="mathlib",
        declaration_name="completely_different_name",
        proposition="(x : Nat) : x + 0 = x",
        proof="sorry",
    )

    left_view, left_reasons = projection._project_view(left)
    right_view, right_reasons = projection._project_view(right)

    assert left_reasons == right_reasons == set()
    assert left_view is not None and right_view is not None
    assert left_view.normalized_headless_text_v1 == right_view.normalized_headless_text_v1
    assert "first_name" not in left_view.normalized_headless_text_v1
    assert "different_name" not in right_view.normalized_headless_text_v1
    assert "sorry" not in right_view.normalized_headless_text_v1
    assert ":=" not in left_view.normalized_headless_text_v1


def test_build_locators_exposes_e2_d0_and_inventory_only_legacy_positive() -> None:
    positive = _observation(
        "1",
        family="p14_independent_binder_permutation",
        relation=IntendedRelation.EQUIVALENT,
        source="mathlib",
        root_id="root:p14",
    )
    negative = _observation(
        "2",
        family="n11_bound_variable_substitution",
        relation=IntendedRelation.NEAR_MISS,
        source="sft_classic",
        root_id="root:n11",
    )
    legacy = _observation(
        "3",
        family="p12_proof_arrow_binder",
        relation=IntendedRelation.EQUIVALENT,
        source="mathlib",
        root_id="root:p12",
    )
    uniques = tuple(_unique(item) for item in (positive, negative, legacy))
    combination_hash = "9" * 64
    seed = _seed(positive, uniques[0], combination_hash=combination_hash)
    manifest = _audit_manifest((positive, negative, legacy), combination_hash=combination_hash)

    locators = projection._build_locators(
        manifest,
        (positive, negative, legacy),
        uniques,
        {uniques[0].unique_pair_id: seed},
        _config(),
    )

    by_family = {item.observation.family_id: item for item in locators}
    assert by_family[positive.family_id].evidence_tier == "E2"
    assert by_family[positive.family_id].pseudo_target == "same_claim"
    assert by_family[negative.family_id].evidence_tier == "D0"
    assert by_family[negative.family_id].pseudo_target == "not_same_claim"
    assert by_family[legacy.family_id].evidence_tier is None
    assert by_family[legacy.family_id].initial_exclusions == ("unsupported_evidence_tier",)

    with pytest.raises(projection.ExperimentalFirstHopProjectionError, match="lacks a seed"):
        projection._build_locators(
            manifest,
            (positive, negative, legacy),
            uniques,
            {},
            _config(),
        )


def test_make_record_screens_both_sides_and_preserves_private_policy() -> None:
    observation = _observation(
        "2",
        family="n11_bound_variable_substitution",
        relation=IntendedRelation.NEAR_MISS,
        source="sft_classic",
        root_id="root:n11",
    )
    unique = _unique(observation)
    locator = projection._Locator(
        unique=unique,
        observation=observation,
        root=_root(observation, kind="d0"),
        evidence_tier="D0",
        pseudo_target="not_same_claim",
        seed=None,
        initial_exclusions=(),
    )
    source = _material(
        "source-2",
        source="sft_classic",
        declaration_name="source_name",
        proposition="(x : Nat) : x = x",
        proof="by sorry",
    )
    candidate = _material(
        "candidate-2",
        source="generated",
        declaration_name="candidate_name",
        proposition="(x : Nat) : x != x",
        proof="by sorry",
    )
    source = replace(
        source,
        theorem=source.theorem.model_copy(
            update={
                "theorem_id": observation.source_theorem_ids[0],
                "root_ancestry_ids": observation.source_root_ancestry_ids,
            }
        ),
        representation=source.representation.model_copy(
            update={
                "theorem_id": observation.source_theorem_ids[0],
                "representation_id": observation.source_representation_ids[0],
            }
        ),
    )
    candidate = replace(
        candidate,
        theorem=candidate.theorem.model_copy(
            update={
                "theorem_id": observation.candidate_theorem_id,
                "root_ancestry_ids": observation.source_root_ancestry_ids,
                "statement_content_hash": observation.candidate_code_hash,
            }
        ),
        representation=candidate.representation.model_copy(
            update={
                "theorem_id": observation.candidate_theorem_id,
                "representation_id": observation.candidate_representation_id,
                "alpha_identity_fingerprint": (observation.candidate_alpha_identity_fingerprint),
            }
        ),
    )
    protected_source_row = cast(str, source.theorem.source_record)
    protected_candidate = cast(str, candidate.representation.signature_explicit)
    frozen = FrozenRegistry(
        frozen_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        representation_signatures_appended=True,
        benchmarks=(
            FrozenBenchmark(
                registry_key="fixture",
                resolved=True,
                row_ids=(protected_source_row,),
                representation_hashes=(signature_near_dup_hash(protected_candidate),),
            ),
        ),
    )
    registry = cast(Any, SimpleNamespace(index=DenylistIndex(frozen)))

    record = projection._make_record(
        locator,
        projection._prepare_side(source, registry),
        projection._prepare_side(candidate, registry),
    )

    assert record.selection_status == "excluded"
    assert record.exclusion_reasons == (
        "benchmark_overlap_candidate",
        "benchmark_overlap_source",
    )
    assert record.private_source_content
    assert not record.redistribution_allowed
    assert not record.external_transmission_allowed
    assert not record.release_eligible
    assert record.semantic_label is record.human_label is record.silver_record is False
    assert record.split_assignment_id is None


def test_unsupported_legacy_p11_with_missing_headless_remains_excluded_inventory() -> None:
    observation = _observation(
        "7",
        family="p11_bounded_quantifiers",
        relation=IntendedRelation.EQUIVALENT,
        source="mathlib",
        root_id="root:p11",
    )
    locator = projection._Locator(
        unique=_unique(observation),
        observation=observation,
        root=_root(observation, kind="e0"),
        evidence_tier=None,
        pseudo_target=None,
        seed=None,
        initial_exclusions=("unsupported_evidence_tier",),
    )
    source = _material(
        "legacy-source",
        source="mathlib",
        declaration_name="source_name",
        proposition="(x : Nat) : x = x",
        proof="by exact rfl",
    )
    candidate = _material(
        "legacy-candidate",
        source="generated",
        declaration_name="candidate_name",
        proposition="(x : Nat) : x = x",
        proof="by exact rfl",
    )
    source = replace(
        source,
        theorem=source.theorem.model_copy(
            update={
                "theorem_id": observation.source_theorem_ids[0],
                "root_ancestry_ids": observation.source_root_ancestry_ids,
            }
        ),
        representation=source.representation.model_copy(
            update={
                "theorem_id": observation.source_theorem_ids[0],
                "representation_id": observation.source_representation_ids[0],
            }
        ),
    )
    candidate = replace(
        candidate,
        theorem=candidate.theorem.model_copy(
            update={
                "theorem_id": observation.candidate_theorem_id,
                "root_ancestry_ids": observation.source_root_ancestry_ids,
                "statement_content_hash": observation.candidate_code_hash,
                "proof_stripped_declaration": "nonrec theorem malformed",
            }
        ),
        representation=candidate.representation.model_copy(
            update={
                "theorem_id": observation.candidate_theorem_id,
                "representation_id": observation.candidate_representation_id,
                "alpha_identity_fingerprint": (observation.candidate_alpha_identity_fingerprint),
                "headless": None,
            }
        ),
    )

    record = projection._make_record(
        locator,
        projection._prepare_side(source, _empty_registry()),
        projection._prepare_side(candidate, _empty_registry()),
    )

    assert record.source is not None
    assert record.candidate is None
    assert record.selection_status == "excluded"
    assert record.exclusion_reasons == (
        "headless_normalization_failed",
        "missing_required_representation_view",
        "unsupported_evidence_tier",
    )
    assert not record.experimental_mixed_input_eligible
    with pytest.raises(ValueError, match="selectable records require both statement views"):
        projection.ExperimentalFirstHopProjectionRecord.model_validate(
            record.model_copy(
                update={
                    "selection_status": "selectable",
                    "experimental_mixed_input_eligible": True,
                }
            ).model_dump(mode="json")
        )


def test_projection_record_rejects_semantic_strengthening_and_unsafe_private_use() -> None:
    record = _record(source_category="sft_classic")

    with pytest.raises(ValueError, match="private source policy"):
        projection.ExperimentalFirstHopProjectionRecord.model_validate(
            record.model_copy(update={"external_transmission_allowed": True}).model_dump(
                mode="json"
            )
        )
    with pytest.raises(ValueError, match="E2 requires"):
        projection.ExperimentalFirstHopProjectionRecord.model_validate(
            record.model_copy(update={"evidence_tier": "E2"}).model_dump(mode="json")
        )


def test_mathlib_v1_differential_checks_proxy_semantics_not_old_split() -> None:
    new = _record()
    assert new.source is not None and new.candidate is not None
    old_source = ExperimentalStatementView.model_construct(
        theorem_id=new.source.theorem_id,
        representation_id=new.source.representation_id,
        context_id=new.source.context_id,
        alpha_identity_fingerprint=new.source.alpha_identity_fingerprint,
        headless=f"  {new.source.normalized_headless_text_v1}  ",
    )
    old_candidate = ExperimentalStatementView.model_construct(
        theorem_id=new.candidate.theorem_id,
        representation_id=new.candidate.representation_id,
        context_id=new.candidate.context_id,
        alpha_identity_fingerprint=new.candidate.alpha_identity_fingerprint,
        headless=new.candidate.normalized_headless_text_v1,
    )
    old = ExperimentalMachineSupervisionRecord.model_construct(
        unique_pair_id=new.unique_pair_id,
        family_id=new.family_ids[0],
        evidence_class=new.evidence_tier,
        pseudo_target=new.pseudo_target,
        intended_relation=new.intended_relations[0],
        split_group_ids=new.source_root_ancestry_ids,
        source=old_source,
        candidate=old_candidate,
        certificate_kind=new.certificate_kind,
        certificate_sha256=new.certificate_sha256,
        split="test",
        split_component_id="deliberately-not-reused",
    )

    result = projection.differential_check_mathlib_v1_records((new,), (old,))

    assert result.compared_count == 1
    changed = old.model_copy(update={"pseudo_target": "same_claim"})
    with pytest.raises(projection.ExperimentalFirstHopProjectionError, match="target"):
        projection.differential_check_mathlib_v1_records((new,), (changed,))


def test_projection_output_writer_replays_and_rejects_tamper(tmp_path: Path) -> None:
    payloads = {name: f"{name}\n".encode() for name in projection._OUTPUT_FILES}
    output = tmp_path / "projection"

    assert projection._write_or_replay(output, payloads) is False
    assert projection._write_or_replay(output, payloads) is True

    (output / "selectable.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(projection.ExperimentalFirstHopProjectionError, match="differs"):
        projection._write_or_replay(output, payloads)


def test_loader_requires_explicit_experimental_opt_in(tmp_path: Path) -> None:
    with pytest.raises(projection.ExperimentalFirstHopProjectionError, match="requires"):
        projection.load_selectable_experimental_first_hop_projection(
            tmp_path,
            allow_experimental_first_hop_projection=False,
            purpose="mixed_proxy_construction",
        )
    with pytest.raises(projection.ExperimentalFirstHopProjectionError, match="forbidden"):
        projection.load_selectable_experimental_first_hop_projection(
            tmp_path,
            allow_experimental_first_hop_projection=True,
            purpose="model_selection",
        )
