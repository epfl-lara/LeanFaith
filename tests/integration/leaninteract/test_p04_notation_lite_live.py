"""LF-017 P04-lite same-context LeanInteract identity checks."""

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
from leanfaith.transforms.positives.p04_notation_lite import P04NotationLiteRule

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
            raw_response_dir=tmp_path_factory.mktemp("p04_raw"),
        )
    )
    yield instance
    instance.close()


def _record(
    code: str,
    *,
    name: str,
    theorem_id: str,
    root_ancestry_id: str,
    parent_theorem_ids: tuple[str, ...] = (),
    elaboration_status: ValidationStatus = ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
) -> TheoremRecord:
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=make_id("anc", {"p04_live_record": theorem_id}),
        root_ancestry_ids=(root_ancestry_id,),
        parent_theorem_ids=parent_theorem_ids,
        source="p04_live_fixture",
        source_revision="p04_notation_lite_v1",
        context_id=_CTX,
        declaration_kind="theorem",
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=code,
        inline_elaboration_source=code,
        is_proposition=True,
        elaboration_status=elaboration_status,
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
            code="import LeanFaithFixtures\n" + code,
            declarations=True,
            allow_sorry=True,
        )
    )
    assert result.status == LeanStatus.VALID_WITH_SORRY, result.messages
    assert len(result.declarations) == 1


@pytest.mark.parametrize(
    ("case_id", "source_token", "target_token", "direction"),
    [
        ("nat_direct", "Nat", "ℕ", "direct_to_notation"),
        ("nat_notation", "ℕ", "Nat", "notation_to_direct"),
        ("int_direct", "Int", "ℤ", "direct_to_notation"),
        ("int_notation", "ℤ", "Int", "notation_to_direct"),
    ],
)
def test_p04_all_directions_reelaborate_with_exact_identity(
    backend: LeanInteractBackend,
    case_id: str,
    source_token: str,
    target_token: str,
    direction: str,
) -> None:
    name = f"p04_live_{case_id}"
    source_code = (
        f"set_option autoImplicit false\ntheorem {name} (x : {source_token}) : x = x := by sorry"
    )
    source_id = make_id("thm", {"p04_live": case_id, "kind": "source"})
    root_ancestry_id = make_id("anc", {"p04_live": case_id})
    source = _record(
        source_code,
        name=name,
        theorem_id=source_id,
        root_ancestry_id=root_ancestry_id,
    )
    _assert_reelaborates(backend, source_code, f"p04-{case_id}-source")
    source_representation = build_representations(
        backend,
        [_representation_input(source_id, name, source_code)],
        imports="import LeanFaithFixtures",
        created_at=_UTC,
    )[0]

    rule = P04NotationLiteRule.from_repository(generation_config_hash="f" * 64)
    draft = rule.generate(source, source_representation, 23)[0]
    assert f"(x : {target_token})" in draft.candidate_code
    assert draft.transformation_trace[0]["direction"] == direction
    _assert_reelaborates(backend, draft.candidate_code, f"p04-{case_id}-candidate")

    candidate_id = make_id("thm", {"p04_live": case_id, "draft": draft.draft_id})
    candidate = _record(
        draft.candidate_code,
        name=name,
        theorem_id=candidate_id,
        root_ancestry_id=root_ancestry_id,
        parent_theorem_ids=(source_id,),
    )
    candidate_representation = build_representations(
        backend,
        [_representation_input(candidate_id, name, draft.candidate_code)],
        imports="import LeanFaithFixtures",
        created_at=_UTC,
    )[0]

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert source_representation.alpha_identity_fingerprint == (
        candidate_representation.alpha_identity_fingerprint
    )
    assert source_representation.signature_explicit == (candidate_representation.signature_explicit)
    assert source_representation.semantic_atoms == candidate_representation.semantic_atoms
    assert source_representation.operator_tree == candidate_representation.operator_tree
    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["elaborated_identity_exact"] is True


def test_p04_unavailable_target_notation_is_rejected_by_live_reelaboration(
    backend: LeanInteractBackend,
) -> None:
    name = "p04_live_unavailable"
    source_code = f"set_option autoImplicit false\ntheorem {name} (x : Nat) : x = x := by sorry"
    source_id = make_id("thm", {"p04_live": "unavailable", "kind": "source"})
    root_ancestry_id = make_id("anc", {"p04_live": "unavailable"})
    source = _record(
        source_code,
        name=name,
        theorem_id=source_id,
        root_ancestry_id=root_ancestry_id,
    )
    source_representation = build_representations(
        backend,
        [_representation_input(source_id, name, source_code)],
        imports="",
        created_at=_UTC,
    )[0]
    rule = P04NotationLiteRule.from_repository(generation_config_hash="f" * 64)
    draft = rule.generate(source, source_representation, 5)[0]
    assert "(x : ℕ)" in draft.candidate_code

    failed = backend.run(
        LeanRequest(
            request_id="p04-unavailable-candidate",
            context_id=_CTX,
            code=draft.candidate_code,
            declarations=True,
            allow_sorry=True,
        )
    )
    assert failed.status == LeanStatus.INVALID

    candidate_id = make_id("thm", {"p04_live": "unavailable", "draft": draft.draft_id})
    candidate = _record(
        draft.candidate_code,
        name=name,
        theorem_id=candidate_id,
        root_ancestry_id=root_ancestry_id,
        parent_theorem_ids=(source_id,),
        elaboration_status=ValidationStatus.INVALID,
    )
    candidate_representation = build_representations(
        backend,
        [_representation_input(candidate_id, name, draft.candidate_code)],
        imports="",
        created_at=_UTC,
    )[0]
    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert "candidate_not_elaborated" in audit.violation_codes
    assert "target_notation_unavailable_or_invalid" in audit.violation_codes
