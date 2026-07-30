"""LF-018 N10 explicit pair-rule and dual-ancestry audit tests."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas import (
    CANONICAL_VIEW_NAMES,
    IntendedRelation,
    QualityTier,
    ValidationStatus,
    ViewStatus,
    make_id,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import Applicability, TransformationAudit, VariantDraft
from leanfaith.transforms.n01_operator import load_n01_operator_config
from leanfaith.transforms.negatives.n10_nearby_theorem import (
    N10NearbyTheoremError,
    N10NearbyTheoremRule,
    apply_nearby_theorem_trace,
    enumerate_nearby_theorem_sites,
    load_n10_nearby_theorem_config,
    nearby_theorem_bucket_keys,
)
from leanfaith.transforms.pair_runtime import (
    PairTransformationDispatchError,
    audit_pair_transformation,
    execute_pair_transformation,
)
from leanfaith.transforms.protocol import PairTransformationRule, TransformationRule
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    TransformationRegistryConfig,
    load_transformation_registry,
)
from tests.unit.record_factories import representation_record, theorem_record

_GENERATION_HASH = "4" * 64
_PRIMARY_ID = make_id("thm", {"n10": "primary"})
_DONOR_ID = make_id("thm", {"n10": "donor"})
_CANDIDATE_ID = make_id("thm", {"n10": "candidate"})
_PRIMARY_REPR = make_id("repr", {"n10": "primary"})
_DONOR_REPR = make_id("repr", {"n10": "donor"})
_CANDIDATE_REPR = make_id("repr", {"n10": "candidate"})
_PRIMARY_ROOT = make_id("anc", {"n10": "primary"})
_DONOR_ROOT = make_id("anc", {"n10": "donor"})
_CANDIDATE_ANCESTRY = make_id("anc", {"n10": "candidate"})
_PRIMARY_ALPHA = "5" * 64
_DONOR_ALPHA = "6" * 64
_PRIMARY_CODE = "theorem n10_primary (m n : Nat) : m < n := by sorry"
_DONOR_CODE = "theorem n10_donor (m n : Nat) : m ≤ n := by sorry"
_PRIMARY_ATOMS = ("const:LT.lt", "const:Nat", "const:instLTNat")
_DONOR_ATOMS = ("const:LE.le", "const:Nat", "const:instLENat")
_PRIMARY_TREE: dict[str, Any] = {
    "atom_version": "atoms_v1",
    "node_count": 5,
    "depth": 3,
    "root": {"k": "app", "n": "LT.lt"},
}
_DONOR_TREE: dict[str, Any] = {
    "atom_version": "atoms_v1",
    "node_count": 5,
    "depth": 3,
    "root": {"k": "app", "n": "LE.le"},
}


def _theorem(
    *,
    theorem_id: str,
    ancestry_id: str,
    roots: tuple[str, ...],
    name: str,
    code: str,
    parents: tuple[str, ...] = (),
    context_id: str | None = None,
    valid: bool = True,
) -> TheoremRecord:
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=roots,
        parent_theorem_ids=parents,
        context_id=context_id or theorem_record().context_id,
        declaration_kind="theorem",
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=code,
        statement_content_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        elaboration_status=(
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER if valid else ValidationStatus.INVALID
        ),
    )


def _representation(
    *,
    representation_id: str,
    theorem_id: str,
    code: str,
    signature: str,
    atoms: tuple[str, ...],
    tree: dict[str, Any],
    alpha: str,
    context_id: str | None = None,
) -> RepresentationRecord:
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for view in (
        "raw_proof_stripped",
        "headless",
        "signature_pp",
        "signature_explicit",
        "semantic_atoms",
        "operator_tree",
    ):
        statuses[view] = ViewStatus.OK
    return representation_record(
        representation_id=representation_id,
        theorem_id=theorem_id,
        context_id=context_id or theorem_record().context_id,
        raw_proof_stripped=code,
        headless=signature,
        signature_pp=signature,
        signature_explicit=signature,
        semantic_atoms=atoms,
        operator_tree=tree,
        alpha_identity_fingerprint=alpha,
        view_status=statuses,
        content_hash=hash_canonical(
            {
                "alpha": alpha,
                "atoms": atoms,
                "code": code,
                "signature": signature,
                "theorem_id": theorem_id,
                "tree": tree,
            }
        ),
    )


def _sources() -> tuple[
    TheoremRecord,
    RepresentationRecord,
    TheoremRecord,
    RepresentationRecord,
]:
    primary = _theorem(
        theorem_id=_PRIMARY_ID,
        ancestry_id=_PRIMARY_ROOT,
        roots=(_PRIMARY_ROOT,),
        name="n10_primary",
        code=_PRIMARY_CODE,
    )
    donor = _theorem(
        theorem_id=_DONOR_ID,
        ancestry_id=_DONOR_ROOT,
        roots=(_DONOR_ROOT,),
        name="n10_donor",
        code=_DONOR_CODE,
    )
    primary_representation = _representation(
        representation_id=_PRIMARY_REPR,
        theorem_id=_PRIMARY_ID,
        code=_PRIMARY_CODE,
        signature="(m n : Nat) → m < n",
        atoms=_PRIMARY_ATOMS,
        tree=_PRIMARY_TREE,
        alpha=_PRIMARY_ALPHA,
    )
    donor_representation = _representation(
        representation_id=_DONOR_REPR,
        theorem_id=_DONOR_ID,
        code=_DONOR_CODE,
        signature="(m n : Nat) → m ≤ n",
        atoms=_DONOR_ATOMS,
        tree=_DONOR_TREE,
        alpha=_DONOR_ALPHA,
    )
    return primary, primary_representation, donor, donor_representation


def _rule() -> N10NearbyTheoremRule:
    return N10NearbyTheoremRule.from_repository(
        generation_config_hash=_GENERATION_HASH,
    )


def _available_registry() -> LoadedTransformationRegistry:
    loaded = load_transformation_registry()
    payload = loaded.config.model_dump(mode="json")
    for family in payload["families"]:
        if family["family_id"] != "n10_nearby_theorem":
            continue
        family["rules"][0]["implementation_status"] = "available"
        family["policy_decision"] = (
            "v1 curated pair implementation available; outputs remain provisional"
        )
    config = TransformationRegistryConfig.model_validate(payload)
    registry_config_hash = hash_canonical(config.model_dump(mode="json"))
    registry_hash = hash_canonical(
        {
            "schema": "leanfaith_transformation_registry_effective_v1",
            "registry": config.model_dump(mode="json"),
            "profile": loaded.profile.model_dump(mode="json"),
            "promotion_policy_hash": loaded.promotion_policy_hash,
        }
    )
    return LoadedTransformationRegistry.model_validate(
        {
            **loaded.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
            "registry_config_hash": registry_config_hash,
            "registry_hash": registry_hash,
        }
    )


def _candidate(
    primary: TheoremRecord,
    donor: TheoremRecord,
    code: str,
    *,
    roots: tuple[str, ...] | None = None,
    parents: tuple[str, ...] | None = None,
) -> TheoremRecord:
    return _theorem(
        theorem_id=_CANDIDATE_ID,
        ancestry_id=_CANDIDATE_ANCESTRY,
        roots=roots or tuple(sorted(set(primary.root_ancestry_ids) | set(donor.root_ancestry_ids))),
        parents=parents or tuple(sorted((primary.theorem_id, donor.theorem_id))),
        name=primary.declaration_name or "",
        code=code,
    )


def _candidate_representation(code: str) -> RepresentationRecord:
    return _representation(
        representation_id=_CANDIDATE_REPR,
        theorem_id=_CANDIDATE_ID,
        code=code,
        signature="(m n : Nat) → m ≤ n",
        atoms=_DONOR_ATOMS,
        tree=_DONOR_TREE,
        alpha=_DONOR_ALPHA,
    )


def test_shared_table_remains_backward_compatible_for_n01_and_adds_typed_n10() -> None:
    n01 = load_n01_operator_config()
    n10 = load_n10_nearby_theorem_config()

    assert tuple(entry.entry_id for entry in n01.table.entries) == (
        "nat_lt_to_le",
        "nat_le_to_lt",
        "prop_and_to_or",
        "prop_or_to_and",
    )
    assert tuple(entry.entry_id for entry in n10.nearby_entries) == (
        "n10_nat_lt_to_le",
        "n10_nat_le_to_lt",
        "n10_prop_and_to_or",
        "n10_prop_or_to_and",
    )
    assert n01.table_hash == n10.table_hash


def test_n10_implements_pair_contract_but_not_unary_registry_contract() -> None:
    rule = _rule()

    assert isinstance(rule, PairTransformationRule)
    assert not isinstance(rule, TransformationRule)


def test_pair_is_high_overlap_exactly_one_curated_component() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    loaded = load_n10_nearby_theorem_config()

    sites = enumerate_nearby_theorem_sites(
        primary,
        primary_representation,
        donor,
        donor_representation,
        loaded.config,
        loaded.table,
        loaded.nearby_entries,
    )

    assert len(sites) == 1
    assert sites[0].entry_id == "n10_nat_lt_to_le"
    assert sites[0].primary_token == "<"
    assert sites[0].donor_token == "≤"
    assert sites[0].signature_token_count == 10
    assert sites[0].positional_overlap_ppm == 900_000


def test_n10_bucket_index_finds_one_symbol_neighbor_without_claiming_applicability() -> None:
    primary, _, donor, _ = _sources()
    unrelated = _theorem(
        theorem_id=make_id("thm", {"n10": "unrelated"}),
        ancestry_id=make_id("anc", {"n10": "unrelated"}),
        roots=(make_id("anc", {"n10": "unrelated"}),),
        name="n10_unrelated",
        code="theorem n10_unrelated (m n : Nat) : m + n = n + m := by sorry",
    )

    primary_keys = set(nearby_theorem_bucket_keys(primary))
    donor_keys = set(nearby_theorem_bucket_keys(donor))
    unrelated_keys = set(nearby_theorem_bucket_keys(unrelated))

    assert primary_keys
    assert donor_keys
    assert primary_keys & donor_keys
    assert not (primary_keys & unrelated_keys)
    assert nearby_theorem_bucket_keys(primary) == tuple(sorted(primary_keys))


def test_generation_is_deterministic_dual_source_and_primary_identity_preserving() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    rule = _rule()

    (first,) = rule.generate_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        17,
    )
    (replay,) = rule.generate_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        17,
    )

    assert first == replay
    assert first.source_theorem_ids == tuple(sorted((_PRIMARY_ID, _DONOR_ID)))
    aligned = dict(zip(first.source_theorem_ids, first.source_representation_ids, strict=True))
    assert aligned == {_PRIMARY_ID: _PRIMARY_REPR, _DONOR_ID: _DONOR_REPR}
    assert first.candidate_code == _PRIMARY_CODE.replace("m < n", "m ≤ n")
    assert first.candidate_code.startswith("theorem n10_primary ")
    assert "n10_donor" not in first.candidate_code
    assert first.intended_relation == IntendedRelation.NEAR_MISS
    assert first.intended_error_types == ("E09", "E26")
    assert first.metadata["semantic_negative_resolved"] is False
    assert first.metadata["primary_theorem_id"] == _PRIMARY_ID
    assert first.metadata["donor_theorem_id"] == _DONOR_ID
    assert first.transformation_trace[0]["primary_root_ancestry_ids"] == [_PRIMARY_ROOT]
    assert first.transformation_trace[0]["donor_root_ancestry_ids"] == [_DONOR_ROOT]
    assert first.inverse_trace is not None
    assert (
        apply_nearby_theorem_trace(
            first.candidate_code,
            first.inverse_trace,
            expected_config_hash=rule.rule_config_hash,
            expected_table_hash=rule.table_hash,
        )
        == _PRIMARY_CODE
    )


def test_same_context_distinct_disjoint_ancestries_are_hard_preconditions() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    rule = _rule()
    other_context = f"ctx:{'9' * 64}"

    same_root_donor = donor.model_copy(update={"root_ancestry_ids": (_PRIMARY_ROOT,)})
    same_root = rule.assess_pair(
        primary,
        primary_representation,
        same_root_donor,
        donor_representation,
    )
    assert not same_root.applicable
    assert same_root.reason_codes == ("source_root_ancestries_not_disjoint",)

    other_context_donor = donor.model_copy(update={"context_id": other_context})
    other_context_representation = donor_representation.model_copy(
        update={"context_id": other_context}
    )
    cross_context = rule.assess_pair(
        primary,
        primary_representation,
        other_context_donor,
        other_context_representation,
    )
    assert not cross_context.applicable
    assert cross_context.reason_codes == ("source_contexts_differ",)


def test_two_signature_differences_are_not_a_nearby_component_pair() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    changed_code = _DONOR_CODE.replace("(m n : Nat)", "(k n : Nat)").replace("m ≤ n", "k ≤ n")
    changed_donor = donor.model_copy(
        update={
            "proof_stripped_declaration": changed_code,
            "statement_content_hash": hashlib.sha256(changed_code.encode("utf-8")).hexdigest(),
        }
    )
    changed_representation = donor_representation.model_copy(
        update={"raw_proof_stripped": changed_code}
    )

    applicability = _rule().assess_pair(
        primary,
        primary_representation,
        changed_donor,
        changed_representation,
    )

    assert not applicability.applicable
    assert applicability.reason_codes == ("no_curated_nearby_theorem_component",)


def test_clean_audit_requires_donor_signature_and_both_parent_ancestries() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    rule = _rule()
    (draft,) = rule.generate_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        17,
    )
    candidate = _candidate(primary, donor, draft.candidate_code)
    candidate_representation = _candidate_representation(draft.candidate_code)

    audit = rule.audit_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.violation_codes == ()
    assert audit.structural_diff_ok is True
    assert audit.atom_mapping_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.recommended_validation_status == ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["dual_ancestry_lineage_ok"] is True
    assert audit.metadata["candidate_matches_donor_signature"] is True
    assert audit.metadata["failed_proof_search_used"] is False
    assert audit.metadata["semantic_negative_resolved"] is False


def test_missing_donor_root_or_parent_quarantines_instead_of_resolving_negative() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    rule = _rule()
    (draft,) = rule.generate_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        17,
    )
    candidate = _candidate(
        primary,
        donor,
        draft.candidate_code,
        roots=(_PRIMARY_ROOT,),
        parents=(_PRIMARY_ID,),
    )
    candidate_representation = _candidate_representation(draft.candidate_code)

    audit = rule.audit_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert "candidate_dual_ancestry_lineage_mismatch" in audit.violation_codes
    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert audit.metadata["semantic_negative_resolved"] is False


def test_trace_hash_tampering_fails_closed() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    rule = _rule()
    (draft,) = rule.generate_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        17,
    )

    with pytest.raises(N10NearbyTheoremError, match="config_hash"):
        apply_nearby_theorem_trace(
            primary.proof_stripped_declaration,
            draft.transformation_trace,
            expected_config_hash="f" * 64,
            expected_table_hash=rule.table_hash,
        )


def test_pair_dispatcher_emits_generated_two_source_attempt() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    loaded = _available_registry()
    rule = N10NearbyTheoremRule.from_repository(
        generation_config_hash=loaded.registry_hash,
    )

    execution = execute_pair_transformation(
        loaded,
        rule,
        primary,
        primary_representation,
        donor,
        donor_representation,
        29,
    )

    assert execution.attempt.terminal_outcome == "generated"
    assert execution.attempt.registry_hash == loaded.registry_hash
    assert execution.attempt.generation_config_hash == loaded.registry_hash
    assert execution.attempt.source_theorem_ids == tuple(sorted((_PRIMARY_ID, _DONOR_ID)))
    aligned = dict(
        zip(
            execution.attempt.source_theorem_ids,
            execution.attempt.source_representation_ids,
            strict=True,
        )
    )
    assert aligned == {_PRIMARY_ID: _PRIMARY_REPR, _DONOR_ID: _DONOR_REPR}
    assert execution.attempt.metadata["source_arity"] == 2
    assert execution.attempt.draft_ids == (execution.drafts[0].draft_id,)


def test_pair_dispatcher_persists_nonapplicable_pair() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    donor = donor.model_copy(update={"root_ancestry_ids": (_PRIMARY_ROOT,)})
    loaded = _available_registry()
    rule = N10NearbyTheoremRule.from_repository(
        generation_config_hash=loaded.registry_hash,
    )

    execution = execute_pair_transformation(
        loaded,
        rule,
        primary,
        primary_representation,
        donor,
        donor_representation,
        29,
    )

    assert execution.attempt.terminal_outcome == "not_applicable"
    assert execution.attempt.applicability is not None
    assert execution.attempt.applicability.reason_codes == ("source_root_ancestries_not_disjoint",)
    assert execution.attempt.source_theorem_ids == tuple(sorted((_PRIMARY_ID, _DONOR_ID)))
    assert execution.drafts == ()


def test_pair_dispatcher_rejects_duplicate_sources_before_attempt_materialization() -> None:
    primary, primary_representation, _, _ = _sources()
    loaded = _available_registry()
    rule = N10NearbyTheoremRule.from_repository(
        generation_config_hash=loaded.registry_hash,
    )

    with pytest.raises(
        PairTransformationDispatchError,
        match="source_theorems_not_distinct",
    ) as caught:
        execute_pair_transformation(
            loaded,
            rule,
            primary,
            primary_representation,
            primary,
            primary_representation.model_copy(update={"representation_id": _DONOR_REPR}),
            29,
        )

    assert caught.value.execution is None
    assert caught.value.stage == "input"


def test_pair_dispatcher_rejects_wrong_registry_hash_with_two_source_failure_attempt() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    loaded = _available_registry()
    wrong_hash_rule = _rule()

    with pytest.raises(
        PairTransformationDispatchError,
        match="metadata_mismatch_generation_config_hash",
    ) as caught:
        execute_pair_transformation(
            loaded,
            wrong_hash_rule,
            primary,
            primary_representation,
            donor,
            donor_representation,
            29,
        )

    assert caught.value.execution is not None
    attempt = caught.value.execution.attempt
    assert attempt.terminal_outcome == "generation_error"
    assert attempt.source_theorem_ids == tuple(sorted((_PRIMARY_ID, _DONOR_ID)))
    assert attempt.failure_codes == ("configure_metadata_mismatch_generation_config_hash",)


class _DisallowedDraftPairRule:
    """Test proxy that tampers one generated draft after the real pair rule."""

    def __init__(self, delegate: N10NearbyTheoremRule) -> None:
        self.delegate = delegate
        self.rule_id = delegate.rule_id
        self.rule_version = delegate.rule_version
        self.family_id = delegate.family_id
        self.polarity = delegate.polarity
        self.implementation_key = delegate.implementation_key
        self.generation_config_hash = delegate.generation_config_hash

    def assess_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
    ) -> Applicability:
        return self.delegate.assess_pair(
            primary,
            primary_representation,
            donor,
            donor_representation,
        )

    def generate_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
        seed: int,
    ) -> Sequence[VariantDraft]:
        (draft,) = self.delegate.generate_pair(
            primary,
            primary_representation,
            donor,
            donor_representation,
            seed,
        )
        return (draft.model_copy(update={"intended_error_types": ("E30",)}),)

    def audit_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        return self.delegate.audit_pair(
            primary,
            primary_representation,
            donor,
            donor_representation,
            candidate,
            candidate_representation,
            draft,
        )


def test_pair_dispatcher_rejects_disallowed_or_identity_tampered_draft() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    loaded = _available_registry()
    delegate = N10NearbyTheoremRule.from_repository(
        generation_config_hash=loaded.registry_hash,
    )
    rule = _DisallowedDraftPairRule(delegate)

    with pytest.raises(PairTransformationDispatchError, match="draft_") as caught:
        execute_pair_transformation(
            loaded,
            rule,
            primary,
            primary_representation,
            donor,
            donor_representation,
            29,
        )

    assert caught.value.execution is not None
    attempt = caught.value.execution.attempt
    assert attempt.terminal_outcome == "generation_error"
    assert any("draft_id" in code for code in attempt.failure_codes)
    assert any("intended_error_types" in code for code in attempt.failure_codes)


def test_pair_audit_dispatcher_validates_registry_and_clean_result() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    loaded = _available_registry()
    rule = N10NearbyTheoremRule.from_repository(
        generation_config_hash=loaded.registry_hash,
    )
    execution = execute_pair_transformation(
        loaded,
        rule,
        primary,
        primary_representation,
        donor,
        donor_representation,
        29,
    )
    (draft,) = execution.drafts
    candidate = _candidate(primary, donor, draft.candidate_code)
    candidate_representation = _candidate_representation(draft.candidate_code)

    audit = audit_pair_transformation(
        loaded,
        rule,
        primary,
        primary_representation,
        donor,
        donor_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL


class _SelfPromotingAuditPairRule(_DisallowedDraftPairRule):
    def generate_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
        seed: int,
    ) -> Sequence[VariantDraft]:
        return self.delegate.generate_pair(
            primary,
            primary_representation,
            donor,
            donor_representation,
            seed,
        )

    def audit_pair(
        self,
        primary: TheoremRecord,
        primary_representation: RepresentationRecord,
        donor: TheoremRecord,
        donor_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        audit = self.delegate.audit_pair(
            primary,
            primary_representation,
            donor,
            donor_representation,
            candidate,
            candidate_representation,
            draft,
        )
        return audit.model_copy(
            update={"recommended_quality_tier": QualityTier.GOLD_COUNTEREXAMPLE}
        )


def test_pair_audit_dispatcher_rejects_self_promotion_and_tampered_audit_id() -> None:
    primary, primary_representation, donor, donor_representation = _sources()
    loaded = _available_registry()
    delegate = N10NearbyTheoremRule.from_repository(
        generation_config_hash=loaded.registry_hash,
    )
    (draft,) = delegate.generate_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        29,
    )
    candidate = _candidate(primary, donor, draft.candidate_code)
    candidate_representation = _candidate_representation(draft.candidate_code)
    rule = _SelfPromotingAuditPairRule(delegate)

    with pytest.raises(
        PairTransformationDispatchError,
        match=r"audit_id_self_promotion|self_promotion_audit_id",
    ):
        audit_pair_transformation(
            loaded,
            rule,
            primary,
            primary_representation,
            donor,
            donor_representation,
            candidate,
            candidate_representation,
            draft,
        )
