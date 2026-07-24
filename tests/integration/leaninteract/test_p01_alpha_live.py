"""LF-017 P01 re-elaboration and alpha-identity integration test."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.representations.pipeline import TheoremForRepresentation, build_representations
from leanfaith.schemas import TheoremRecord, ValidationStatus, make_id
from leanfaith.transforms.p01_alpha import P01AlphaRule

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
            raw_response_dir=tmp_path_factory.mktemp("p01_raw"),
        )
    )
    yield instance
    instance.close()


def _statement_hash(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def _theorem_record(
    *,
    theorem_id: str,
    ancestry_id: str,
    source: str,
    declaration_name: str = "lf_p01_live",
    parent_theorem_ids: tuple[str, ...] = (),
) -> TheoremRecord:
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        parent_theorem_ids=parent_theorem_ids,
        source="p01_live_fixture",
        source_revision="v1",
        context_id=_CTX,
        declaration_kind="theorem",
        declaration_name=declaration_name,
        declaration_full_name=declaration_name,
        proof_stripped_declaration=source,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=_statement_hash(source),
    )


def test_p01_candidate_reelaborates_and_preserves_alpha_identity(
    backend: LeanInteractBackend,
) -> None:
    source_code = (
        "theorem lf_p01_live {α : Type} [inst : Inhabited α] (x : α) "
        ": (∀ x : α, x = x) ∧ x = x := by sorry"
    )
    source_id = make_id("thm", {"p01": "source"})
    ancestry_id = make_id("anc", {"p01": "source"})
    source_input = TheoremForRepresentation(
        theorem_id=source_id,
        full_name="lf_p01_live",
        proof_stripped=source_code,
        context_id=_CTX,
        inline_declaration=True,
    )
    (source_representation,) = build_representations(
        backend,
        [source_input],
        imports="import LeanFaithFixtures",
        created_at=_UTC,
    )
    assert source_representation.alpha_identity_fingerprint is not None
    source_theorem = _theorem_record(
        theorem_id=source_id,
        ancestry_id=ancestry_id,
        source=source_code,
    )

    rule = P01AlphaRule.from_repository(generation_config_hash="b" * 64)
    (draft,) = rule.generate(source_theorem, source_representation, 31)
    candidate_id = make_id("thm", {"p01": draft.draft_id})
    candidate_input = TheoremForRepresentation(
        theorem_id=candidate_id,
        full_name="lf_p01_live",
        proof_stripped=draft.candidate_code,
        context_id=_CTX,
        inline_declaration=True,
    )
    (candidate_representation,) = build_representations(
        backend,
        [candidate_input],
        imports="import LeanFaithFixtures",
        created_at=_UTC,
    )
    candidate_theorem = _theorem_record(
        theorem_id=candidate_id,
        ancestry_id=ancestry_id,
        source=draft.candidate_code,
        parent_theorem_ids=(source_id,),
    )

    assert candidate_representation.signature_explicit is not None
    assert candidate_representation.alpha_identity_fingerprint == (
        source_representation.alpha_identity_fingerprint
    )
    assert candidate_representation.semantic_atoms == source_representation.semantic_atoms

    audit = rule.audit(
        source_theorem,
        source_representation,
        candidate_theorem,
        candidate_representation,
        draft,
    )
    assert audit.violation_codes == ()
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.atom_mapping_ok is True
    assert audit.recommended_quality_tier.value == "provisional"


@pytest.mark.parametrize(
    ("name", "signature", "seed"),
    [
        ("lf_p01_shadow_fun", "(x : Nat) : (fun x : Nat => x) x = x", 100),
        ("lf_p01_shadow_exists", "(x : Nat) : (∃ x : Nat, x = x) ∧ x = x", 101),
        (
            "lf_p01_dependent_forall",
            "(α : Type) (x : α) : (∀ y : α, y = x → y = x)",
            102,
        ),
        (
            "lf_p01_shadow_hypothesis",
            "(p q : Prop) (h : p) : p ∧ (∀ h : q, q)",
            103,
        ),
        (
            "lf_p01_instance",
            "{α : Type} [inst : Inhabited α] (x : α) : x = x",
            104,
        ),
        (
            "lf_p01_guillemet",
            "(«δ value» : Nat) : «δ value» = «δ value»",
            105,
        ),
    ],
)
def test_p01_adversarial_scopes_preserve_elaborated_type(
    backend: LeanInteractBackend,
    name: str,
    signature: str,
    seed: int,
) -> None:
    """Exercise shadowing, dependencies, instances, and quoted identifiers.

    The lexical transformer is not trusted as the certificate: both sides are
    independently elaborated and their proof-free Expr fingerprints must be
    identical.
    """

    source_code = f"theorem {name} {signature} := by sorry"
    source_id = make_id("thm", {"p01_adversarial": name, "side": "source"})
    ancestry_id = make_id("anc", {"p01_adversarial": name})
    (source_representation,) = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=source_id,
                full_name=name,
                proof_stripped=source_code,
                context_id=_CTX,
                inline_declaration=True,
            )
        ],
        imports="import LeanFaithFixtures",
        created_at=_UTC,
    )
    source_theorem = _theorem_record(
        theorem_id=source_id,
        ancestry_id=ancestry_id,
        source=source_code,
        declaration_name=name,
    )

    (draft,) = P01AlphaRule.from_repository(generation_config_hash="b" * 64).generate(
        source_theorem, source_representation, seed
    )
    candidate_id = make_id("thm", {"p01_adversarial": name, "side": "candidate"})
    (candidate_representation,) = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=candidate_id,
                full_name=name,
                proof_stripped=draft.candidate_code,
                context_id=_CTX,
                inline_declaration=True,
            )
        ],
        imports="import LeanFaithFixtures",
        created_at=_UTC,
    )

    assert candidate_representation.alpha_identity_fingerprint == (
        source_representation.alpha_identity_fingerprint
    )
    assert candidate_representation.semantic_atoms == source_representation.semantic_atoms
