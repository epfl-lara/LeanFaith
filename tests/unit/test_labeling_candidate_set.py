"""Adversarial structural candidate-set tests for LF-024."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.labeling.candidate_set import (
    CANONICAL_RESOLUTION_SOURCES,
    CandidateEnumerationScope,
    CandidateSetManifest,
    CandidateSetStructureError,
    CandidateSourceClosureBinding,
    build_candidate_enumeration_scope,
    build_candidate_set_manifest,
    build_candidate_source_closure_binding,
    canonical_candidate_jsonl_bytes,
    verify_candidate_set_structure,
)
from leanfaith.labeling.quality import (
    ActiveLabelResolutionPolicy,
    AuthorityArtifactKind,
    CandidateCommitment,
    ResolutionCandidate,
    ResolutionSource,
    load_active_label_resolution_policy,
    make_authority_artifact_binding,
    make_resolution_candidate,
)
from leanfaith.labeling.resolution import VerifiedCandidateSet
from leanfaith.schemas.enums import (
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    SemanticLabelTargetKind,
    ValidationStatus,
)
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.pair import PairRecord
from leanfaith.schemas.theorem import ContextRecord, TheoremRecord

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def policy() -> ActiveLabelResolutionPolicy:
    return load_active_label_resolution_policy(REPO_ROOT)


@dataclass(frozen=True, slots=True)
class TargetFixture:
    pair: PairRecord
    theorems: tuple[TheoremRecord, ...]
    contexts: tuple[ContextRecord, ...]


def _target_fixture(*, suffix: str = "main") -> TargetFixture:
    fingerprint = hash_canonical({"fixture": "candidate-set", "suffix": suffix})
    context = ContextRecord(
        environment_schema_version=1,
        context_id=f"ctx:{fingerprint}",
        context_fingerprint=fingerprint,
        project_kind="fixture",
        project_uri="fixture-project",
        project_revision=f"revision-{suffix}",
        project_registry_key="fixture",
        lean_version="4.fixture",
        lean_interact_version="fixture",
        repl_revision="fixture",
        imports=("Mathlib",),
        header_text="import Mathlib\n",
        header_hash=hash_canonical("import Mathlib\n"),
    )

    def theorem(side: str) -> TheoremRecord:
        theorem_id = make_id("thm", {"fixture": suffix, "side": side})
        ancestry_id = make_id("anc", {"fixture": suffix, "side": side})
        statement = f"theorem candidate_set_{suffix}_{side} : True"
        return TheoremRecord(
            theorem_id=theorem_id,
            ancestry_id=ancestry_id,
            root_ancestry_ids=(ancestry_id,),
            source="fixture",
            source_revision=f"revision-{suffix}",
            context_id=context.context_id,
            declaration_kind="theorem",
            declaration_name=f"candidate_set_{suffix}_{side}",
            declaration_full_name=f"candidate_set_{suffix}_{side}",
            proof_stripped_declaration=statement,
            is_proposition=True,
            elaboration_status=ValidationStatus.ELABORATES,
            statement_content_hash=hash_canonical(statement),
        )

    theorem_a = theorem("a")
    theorem_b = theorem("b")
    groups = tuple(sorted({*theorem_a.root_ancestry_ids, *theorem_b.root_ancestry_ids}))
    pair = PairRecord(
        pair_id=make_id("pair", {"fixture": "candidate-set", "suffix": suffix}),
        theorem_a_id=theorem_a.theorem_id,
        theorem_b_id=theorem_b.theorem_id,
        pair_source="fixture",
        split_group_ids=groups,
    )
    return TargetFixture(pair=pair, theorems=(theorem_a, theorem_b), contexts=(context,))


def _candidate(
    policy: ActiveLabelResolutionPolicy,
    *,
    target_id: str,
    source: ResolutionSource,
) -> ResolutionCandidate:
    evidence_ids = tuple(
        make_id("ev", {"fixture": "candidate-set", "source": source.value, "index": index})
        for index in range(4)
    )
    if source is ResolutionSource.HUMAN_ADJUDICATION:
        tier = QualityTier.GOLD_HUMAN
        method = "expert_adjudication"
        artifact_kind = AuthorityArtifactKind.HUMAN_ADJUDICATION
        accepted_evidence_ids = evidence_ids[:1]
    elif source is ResolutionSource.FROZEN_BENCHMARK_POLICY:
        tier = QualityTier.BENCHMARK
        method = "benchmark_import"
        artifact_kind = AuthorityArtifactKind.FROZEN_BENCHMARK_LABEL
        accepted_evidence_ids = ()
    elif source is ResolutionSource.PROMOTED_CERTIFICATE_OR_SEPARATOR:
        tier = QualityTier.GOLD_CONSERVATIVE_TRANSFORM
        method = "p01_alpha_certificate"
        artifact_kind = AuthorityArtifactKind.CONSERVATIVE_FAMILY_PROMOTION
        accepted_evidence_ids = evidence_ids[:1]
    else:
        tier = QualityTier.SILVER_CONSENSUS
        method = "independent_consensus_audited"
        artifact_kind = AuthorityArtifactKind.INDEPENDENT_CONSENSUS_PROMOTION
        accepted_evidence_ids = evidence_ids
    artifact = make_authority_artifact_binding(
        artifact_kind=artifact_kind,
        artifact_id=make_id(
            "fixture_authority",
            {"target_id": target_id, "source": source.value},
        ),
        artifact_sha256=hash_canonical(
            {"fixture": "candidate-set-authority", "source": source.value}
        ),
    )
    return make_resolution_candidate(
        policy=policy,
        target_kind=SemanticLabelTargetKind.LEAN_PAIR,
        target_id=target_id,
        source=source,
        quality_tier=tier,
        resolution_method=method,
        authority_artifacts=(artifact,),
        accepted_evidence_ids=accepted_evidence_ids,
        commitment=CandidateCommitment.TERMINAL,
        same_claim=True,
        resolution_outcome=ResolutionOutcome.SAME_CLAIM,
        relation=RelationLabel.EQUIVALENT,
        provenance=(f"fixture:{source.value}",),
    )


def _scope(
    policy: ActiveLabelResolutionPolicy,
    fixture: TargetFixture,
) -> CandidateEnumerationScope:
    return build_candidate_enumeration_scope(
        target=fixture.pair,
        theorem_records=fixture.theorems,
        context_records=fixture.contexts,
        policy=policy,
    )


def _closures(
    scope: CandidateEnumerationScope,
    candidates: tuple[ResolutionCandidate, ...],
) -> tuple[CandidateSourceClosureBinding, ...]:
    by_source = {candidate.source: candidate.candidate_id for candidate in candidates}
    return tuple(
        build_candidate_source_closure_binding(
            scope=scope,
            source=source,
            adapter_method_version=f"fixture_adapter_{source.value}_v1",
            adapter_config_sha256=hash_canonical(
                {"fixture": "candidate-set-adapter", "source": source.value}
            ),
            authority_inventory_manifest_id=make_id(
                "fixture_authority_inventory",
                {"scope": scope.scope_id, "source": source.value},
            ),
            authority_inventory_manifest_sha256=hash_canonical(
                {"fixture": "authority-inventory", "source": source.value}
            ),
            closure_receipt_id=make_id(
                "fixture_closure_receipt",
                {"scope": scope.scope_id, "source": source.value},
            ),
            closure_receipt_sha256=hash_canonical(
                {"fixture": "closure-receipt", "source": source.value}
            ),
            candidate_ids=(() if source not in by_source else (by_source[source],)),
        )
        for source in CANONICAL_RESOLUTION_SOURCES
    )


def _all_candidates(
    policy: ActiveLabelResolutionPolicy,
    fixture: TargetFixture,
) -> tuple[ResolutionCandidate, ...]:
    return tuple(
        _candidate(policy, target_id=fixture.pair.pair_id, source=source)
        for source in CANONICAL_RESOLUTION_SOURCES
    )


def test_empty_candidate_set_is_stable_but_explicitly_not_production_closed(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    closures = _closures(scope, ())
    first = build_candidate_set_manifest(
        scope=scope,
        source_closures=tuple(reversed(closures)),
        candidates=(),
    )
    second = build_candidate_set_manifest(
        scope=scope,
        source_closures=closures,
        candidates=(),
    )

    assert first == second
    assert first.candidate_count == 0
    assert first.candidate_ids == ()
    assert first.candidate_records_sha256 == sha256_hex(b"")
    assert tuple(item.source for item in first.source_closures) == CANONICAL_RESOLUTION_SOURCES
    result = verify_candidate_set_structure(
        manifest=first,
        target=fixture.pair,
        theorem_records=fixture.theorems,
        context_records=fixture.contexts,
        candidates=(),
        policy=policy,
    )
    assert not isinstance(result, VerifiedCandidateSet)
    assert result.verification_level == "structural_diagnostic_only"
    assert result.authority_inventory_replays_verified is False
    assert result.production_candidate_set_closed is False


def test_nonempty_manifest_is_order_independent_and_round_trips(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    candidates = _all_candidates(policy, fixture)
    closures = _closures(scope, candidates)
    first = build_candidate_set_manifest(
        scope=scope,
        source_closures=tuple(reversed(closures)),
        candidates=tuple(reversed(candidates)),
    )
    second = build_candidate_set_manifest(
        scope=scope,
        source_closures=closures,
        candidates=candidates,
    )

    assert first == second
    assert CandidateSetManifest.model_validate_json(first.model_dump_json()) == first
    assert first.candidate_records_sha256 == sha256_hex(
        canonical_candidate_jsonl_bytes(tuple(reversed(candidates)))
    )
    result = verify_candidate_set_structure(
        manifest=first,
        target=fixture.pair,
        theorem_records=tuple(reversed(fixture.theorems)),
        context_records=fixture.contexts,
        candidates=tuple(reversed(candidates)),
        policy=policy,
    )
    assert result.candidate_ids == tuple(sorted(item.candidate_id for item in candidates))
    assert result.production_candidate_set_closed is False


def test_missing_or_duplicate_source_closure_fails(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    closures = _closures(scope, ())
    with pytest.raises(CandidateSetStructureError, match="all four"):
        build_candidate_set_manifest(
            scope=scope,
            source_closures=closures[:-1],
            candidates=(),
        )
    with pytest.raises(CandidateSetStructureError, match="duplicate candidate source"):
        build_candidate_set_manifest(
            scope=scope,
            source_closures=(*closures[:-1], closures[0]),
            candidates=(),
        )


def test_missing_supplied_candidate_fails_exact_union_verification(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    candidates = _all_candidates(policy, fixture)
    manifest = build_candidate_set_manifest(
        scope=scope,
        source_closures=_closures(scope, candidates),
        candidates=candidates,
    )
    with pytest.raises(CandidateSetStructureError, match="absent candidate"):
        verify_candidate_set_structure(
            manifest=manifest,
            target=fixture.pair,
            theorem_records=fixture.theorems,
            context_records=fixture.contexts,
            candidates=candidates[1:],
            policy=policy,
        )


def test_duplicate_candidate_and_duplicate_source_assignment_fail(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    candidates = _all_candidates(policy, fixture)
    closures = list(_closures(scope, candidates))
    with pytest.raises(CandidateSetStructureError, match="duplicate ResolutionCandidate"):
        build_candidate_set_manifest(
            scope=scope,
            source_closures=closures,
            candidates=(*candidates, candidates[0]),
        )

    duplicated = build_candidate_source_closure_binding(
        scope=scope,
        source=closures[1].source,
        adapter_method_version=closures[1].adapter_method_version,
        adapter_config_sha256=closures[1].adapter_config_sha256,
        authority_inventory_manifest_id=closures[1].authority_inventory_manifest_id,
        authority_inventory_manifest_sha256=(closures[1].authority_inventory_manifest_sha256),
        closure_receipt_id=closures[1].closure_receipt_id,
        closure_receipt_sha256=closures[1].closure_receipt_sha256,
        candidate_ids=(*closures[1].candidate_ids, candidates[0].candidate_id),
    )
    closures[1] = duplicated
    with pytest.raises(CandidateSetStructureError, match="wrong source closure"):
        build_candidate_set_manifest(
            scope=scope,
            source_closures=closures,
            candidates=candidates,
        )


def test_candidate_content_tampering_with_old_id_fails(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    candidates = _all_candidates(policy, fixture)
    manifest = build_candidate_set_manifest(
        scope=scope,
        source_closures=_closures(scope, candidates),
        candidates=candidates,
    )
    tampered = candidates[0].model_copy(update={"provenance": ("tampered",)})
    with pytest.raises(CandidateSetStructureError, match="content-addressed ID"):
        verify_candidate_set_structure(
            manifest=manifest,
            target=fixture.pair,
            theorem_records=fixture.theorems,
            context_records=fixture.contexts,
            candidates=(tampered, *candidates[1:]),
            policy=policy,
        )


def test_manifest_and_source_binding_tampering_fail_revalidation(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    candidates = _all_candidates(policy, fixture)
    manifest = build_candidate_set_manifest(
        scope=scope,
        source_closures=_closures(scope, candidates),
        candidates=candidates,
    )
    tampered_manifest = manifest.model_copy(update={"candidate_records_sha256": "f" * 64})
    with pytest.raises(CandidateSetStructureError, match="manifest is invalid"):
        verify_candidate_set_structure(
            manifest=tampered_manifest,
            target=fixture.pair,
            theorem_records=fixture.theorems,
            context_records=fixture.contexts,
            candidates=candidates,
            policy=policy,
        )

    bad_closure = manifest.source_closures[0].model_copy(
        update={"closure_receipt_sha256": "e" * 64}
    )
    bad_manifest = manifest.model_copy(
        update={"source_closures": (bad_closure, *manifest.source_closures[1:])}
    )
    with pytest.raises(CandidateSetStructureError, match="manifest is invalid"):
        verify_candidate_set_structure(
            manifest=bad_manifest,
            target=fixture.pair,
            theorem_records=fixture.theorems,
            context_records=fixture.contexts,
            candidates=candidates,
            policy=policy,
        )


@pytest.mark.parametrize("tamper", ["target", "context", "policy"])
def test_target_context_and_policy_drift_fail(
    policy: ActiveLabelResolutionPolicy,
    tamper: str,
) -> None:
    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    manifest = build_candidate_set_manifest(
        scope=scope,
        source_closures=_closures(scope, ()),
        candidates=(),
    )
    target = fixture.pair
    contexts = fixture.contexts
    active_policy = policy
    if tamper == "target":
        target = fixture.pair.model_copy(update={"pair_source": "changed"})
    elif tamper == "context":
        contexts = (fixture.contexts[0].model_copy(update={"project_revision": "changed"}),)
    else:
        active_policy = policy.model_copy(update={"gate_file_sha256": "f" * 64})
    with pytest.raises(CandidateSetStructureError, match="scope is stale"):
        verify_candidate_set_structure(
            manifest=manifest,
            target=target,
            theorem_records=fixture.theorems,
            context_records=contexts,
            candidates=(),
            policy=active_policy,
        )


def test_cross_target_candidate_and_scope_are_rejected(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    other = _target_fixture(suffix="other")
    scope = _scope(policy, fixture)
    cross_target = _candidate(
        policy,
        target_id=other.pair.pair_id,
        source=ResolutionSource.HUMAN_ADJUDICATION,
    )
    with pytest.raises(CandidateSetStructureError, match="targets another item"):
        build_candidate_set_manifest(
            scope=scope,
            source_closures=_closures(scope, (cross_target,)),
            candidates=(cross_target,),
        )

    other_scope = _scope(policy, other)
    closures = list(_closures(scope, ()))
    closures[0] = _closures(other_scope, ())[0]
    with pytest.raises(CandidateSetStructureError, match="another scope"):
        build_candidate_set_manifest(
            scope=scope,
            source_closures=closures,
            candidates=(),
        )


def test_context_and_theorem_partitions_must_be_exact(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    fixture = _target_fixture()
    with pytest.raises(CandidateSetStructureError, match="theorem partition is not exact"):
        build_candidate_enumeration_scope(
            target=fixture.pair,
            theorem_records=fixture.theorems[:1],
            context_records=fixture.contexts,
            policy=policy,
        )
    extra = _target_fixture(suffix="extra")
    with pytest.raises(CandidateSetStructureError, match="context partition is not exact"):
        build_candidate_enumeration_scope(
            target=fixture.pair,
            theorem_records=fixture.theorems,
            context_records=(*fixture.contexts, *extra.contexts),
            policy=policy,
        )


def test_self_consistent_omission_remains_structural_only_without_authority_replay(
    policy: ActiveLabelResolutionPolicy,
) -> None:
    """Structural equality cannot prove an upstream authority inventory complete."""

    fixture = _target_fixture()
    scope = _scope(policy, fixture)
    candidates = _all_candidates(policy, fixture)
    omitted = candidates[0]
    retained = tuple(item for item in candidates if item.candidate_id != omitted.candidate_id)
    structurally_self_consistent = build_candidate_set_manifest(
        scope=scope,
        source_closures=_closures(scope, retained),
        candidates=retained,
    )
    result = verify_candidate_set_structure(
        manifest=structurally_self_consistent,
        target=fixture.pair,
        theorem_records=fixture.theorems,
        context_records=fixture.contexts,
        candidates=retained,
        policy=policy,
    )

    assert omitted.candidate_id not in result.candidate_ids
    assert result.authority_inventory_replays_verified is False
    assert result.production_candidate_set_closed is False
