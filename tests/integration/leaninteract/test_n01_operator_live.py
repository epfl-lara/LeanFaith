"""LF-018 N01 same-context elaboration and exact atom-delta checks."""

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
from leanfaith.representations.pipeline import (
    TheoremForRepresentation,
    build_representations,
)
from leanfaith.schemas import QualityTier, TheoremRecord, ValidationStatus, make_id
from leanfaith.transforms.n01_operator import N01OperatorRule

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
            raw_response_dir=tmp_path_factory.mktemp("n01_raw"),
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
    parents: tuple[str, ...] = (),
) -> TheoremRecord:
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=make_id("anc", {"n01_live_record": theorem_id}),
        root_ancestry_ids=(root_ancestry_id,),
        parent_theorem_ids=parents,
        source="n01_live_fixture",
        source_revision="n01_operator_v1",
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
    ("case_id", "binders", "source_claim", "candidate_claim", "entry_id"),
    [
        ("nat_lt", "(m n : Nat)", "m < n", "m ≤ n", "nat_lt_to_le"),
        ("nat_le", "(m n : Nat)", "m ≤ n", "m < n", "nat_le_to_lt"),
        ("prop_and", "(P Q : Prop)", "P ∧ Q", "P ∨ Q", "prop_and_to_or"),
        ("prop_or", "(P Q : Prop)", "P ∨ Q", "P ∧ Q", "prop_or_to_and"),
    ],
)
def test_n01_all_finite_directions_reelaborate_and_remain_provisional(
    backend: LeanInteractBackend,
    case_id: str,
    binders: str,
    source_claim: str,
    candidate_claim: str,
    entry_id: str,
) -> None:
    name = f"n01_live_{case_id}"
    source_code = f"theorem {name} {binders} : {source_claim} := by sorry"
    source_id = make_id("thm", {"n01_live": case_id, "kind": "source"})
    root_ancestry_id = make_id("anc", {"n01_live": case_id})
    source = _record(
        source_code,
        name=name,
        theorem_id=source_id,
        root_ancestry_id=root_ancestry_id,
    )
    _assert_reelaborates(backend, source_code, f"n01-{case_id}-source")
    source_representation = build_representations(
        backend,
        [_representation_input(source_id, name, source_code)],
        imports="",
        created_at=_UTC,
    )[0]

    rule = N01OperatorRule.from_repository(generation_config_hash="4" * 64)
    (draft,) = rule.generate(source, source_representation, 23)
    assert candidate_claim in draft.candidate_code
    assert draft.transformation_trace[0]["entry_id"] == entry_id
    _assert_reelaborates(
        backend,
        draft.candidate_code,
        f"n01-{case_id}-candidate",
    )

    candidate_id = make_id("thm", {"n01_live": case_id, "draft": draft.draft_id})
    candidate = _record(
        draft.candidate_code,
        name=name,
        theorem_id=candidate_id,
        root_ancestry_id=root_ancestry_id,
        parents=(source_id,),
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

    assert source_representation.alpha_identity_fingerprint != (
        candidate_representation.alpha_identity_fingerprint
    )
    assert audit.violation_codes == ()
    assert audit.atom_mapping_ok is True
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["failed_proof_search_used"] is False
    assert audit.metadata["semantic_negative_established"] is False
