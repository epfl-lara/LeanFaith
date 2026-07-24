"""LF-018 N10 same-context dual-source elaboration and representation audit."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations.pipeline import TheoremForRepresentation, build_representations
from leanfaith.schemas import QualityTier, TheoremRecord, ValidationStatus, make_id
from leanfaith.transforms.negatives.n10_nearby_theorem import N10NearbyTheoremRule

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CTX_FP = "0" * 64
_CTX = f"ctx:{_CTX_FP}"
_UTC = datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC)


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CTX_FP,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("n10_raw"),
        )
    )
    yield instance
    instance.close()


def _record(
    code: str,
    *,
    name: str,
    theorem_id: str,
    ancestry_id: str,
    roots: tuple[str, ...],
    parents: tuple[str, ...] = (),
) -> TheoremRecord:
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=roots,
        parent_theorem_ids=parents,
        source="n10_live_fixture",
        source_revision="n10_nearby_theorem_v1",
        context_id=_CTX,
        declaration_kind="theorem",
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=code,
        inline_elaboration_source=code,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
    )


def _representation_input(
    theorem_id: str,
    name: str,
    code: str,
) -> TheoremForRepresentation:
    return TheoremForRepresentation(
        theorem_id=theorem_id,
        full_name=name,
        proof_stripped=code,
        context_id=_CTX,
        inline_declaration=True,
        inline_source=code,
    )


def _assert_reelaborates(
    backend: LeanInteractBackend,
    code: str,
    request_id: str,
) -> None:
    result = backend.run(
        LeanRequest(
            request_id=request_id,
            context_id=_CTX,
            code=code,
            declarations=True,
            allow_sorry=True,
        )
    )
    assert result.status == LeanStatus.VALID_WITH_SORRY, result.messages
    assert len(result.declarations) == 1


@pytest.mark.parametrize(
    ("case_id", "binders", "primary_claim", "donor_claim", "entry_id"),
    [
        ("nat_order", "(m n : Nat)", "m < n", "m ≤ n", "n10_nat_lt_to_le"),
        ("prop_connective", "(P Q : Prop)", "P ∧ Q", "P ∨ Q", "n10_prop_and_to_or"),
    ],
)
def test_n10_pair_reelaborates_matches_donor_and_preserves_both_ancestries(
    backend: LeanInteractBackend,
    case_id: str,
    binders: str,
    primary_claim: str,
    donor_claim: str,
    entry_id: str,
) -> None:
    primary_name = f"n10_live_{case_id}_primary"
    donor_name = f"n10_live_{case_id}_donor"
    primary_code = f"theorem {primary_name} {binders} : {primary_claim} := by sorry"
    donor_code = f"theorem {donor_name} {binders} : {donor_claim} := by sorry"
    primary_id = make_id("thm", {"n10_live": case_id, "role": "primary"})
    donor_id = make_id("thm", {"n10_live": case_id, "role": "donor"})
    primary_root = make_id("anc", {"n10_live": case_id, "role": "primary"})
    donor_root = make_id("anc", {"n10_live": case_id, "role": "donor"})
    primary = _record(
        primary_code,
        name=primary_name,
        theorem_id=primary_id,
        ancestry_id=primary_root,
        roots=(primary_root,),
    )
    donor = _record(
        donor_code,
        name=donor_name,
        theorem_id=donor_id,
        ancestry_id=donor_root,
        roots=(donor_root,),
    )
    _assert_reelaborates(backend, primary_code, f"n10-{case_id}-primary")
    _assert_reelaborates(backend, donor_code, f"n10-{case_id}-donor")
    primary_representation = build_representations(
        backend,
        [_representation_input(primary_id, primary_name, primary_code)],
        imports="",
        created_at=_UTC,
    )[0]
    donor_representation = build_representations(
        backend,
        [_representation_input(donor_id, donor_name, donor_code)],
        imports="",
        created_at=_UTC,
    )[0]

    rule = N10NearbyTheoremRule.from_repository(generation_config_hash="4" * 64)
    applicability = rule.assess_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
    )
    assert applicability.applicable
    (draft,) = rule.generate_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        31,
    )
    assert draft.transformation_trace[0]["entry_id"] == entry_id
    assert draft.candidate_code.startswith(f"theorem {primary_name} ")
    assert donor_claim in draft.candidate_code
    _assert_reelaborates(backend, draft.candidate_code, f"n10-{case_id}-candidate")

    candidate_id = make_id("thm", {"n10_live": case_id, "draft": draft.draft_id})
    candidate_roots = tuple(sorted((primary_root, donor_root)))
    candidate_parents = tuple(sorted((primary_id, donor_id)))
    candidate = _record(
        draft.candidate_code,
        name=primary_name,
        theorem_id=candidate_id,
        ancestry_id=make_id("anc", {"n10_live": case_id, "role": "candidate"}),
        roots=candidate_roots,
        parents=candidate_parents,
    )
    candidate_representation = build_representations(
        backend,
        [_representation_input(candidate_id, primary_name, draft.candidate_code)],
        imports="",
        created_at=_UTC,
    )[0]
    audit = rule.audit_pair(
        primary,
        primary_representation,
        donor,
        donor_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert primary_representation.alpha_identity_fingerprint != (
        donor_representation.alpha_identity_fingerprint
    )
    assert candidate_representation.alpha_identity_fingerprint == (
        donor_representation.alpha_identity_fingerprint
    )
    assert candidate_representation.signature_explicit == (donor_representation.signature_explicit)
    assert candidate_representation.operator_tree == donor_representation.operator_tree
    assert candidate.root_ancestry_ids == candidate_roots
    assert candidate.parent_theorem_ids == candidate_parents
    assert audit.violation_codes == ()
    assert audit.structural_diff_ok is True
    assert audit.atom_mapping_ok is True
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["semantic_negative_resolved"] is False
