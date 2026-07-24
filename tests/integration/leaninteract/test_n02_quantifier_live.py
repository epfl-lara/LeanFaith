"""LF-018 N02 source/candidate re-elaboration and provisional audit."""

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
from leanfaith.schemas import QualityTier, TheoremRecord, ValidationStatus, make_id
from leanfaith.transforms.negatives.n02_quantifier import N02QuantifierRule

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
            raw_response_dir=tmp_path_factory.mktemp("n02_raw"),
        )
    )
    yield instance
    instance.close()


def _theorem(
    code: str,
    *,
    theorem_id: str,
    name: str,
    ancestry_id: str,
    parents: tuple[str, ...] = (),
) -> TheoremRecord:
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        parent_theorem_ids=parents,
        source="n02_live_fixture",
        source_revision="n02_quantifier_v1",
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


@pytest.mark.parametrize(
    ("name", "source_quantifier", "candidate_quantifier"),
    [
        ("n02_live_forall", "∀", "∃"),
        ("n02_live_exists", "∃", "∀"),
    ],
)
def test_n02_candidate_reelaborates_but_remains_semantically_unresolved(
    backend: LeanInteractBackend,
    name: str,
    source_quantifier: str,
    candidate_quantifier: str,
) -> None:
    source_code = f"theorem {name} : ({source_quantifier} n : Nat, n = n) := by sorry"
    source_id = make_id("thm", {"n02_live": name, "side": "source"})
    ancestry_id = make_id("anc", {"n02_live": name})
    source = _theorem(
        source_code,
        theorem_id=source_id,
        name=name,
        ancestry_id=ancestry_id,
    )
    (source_representation,) = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=source_id,
                full_name=name,
                proof_stripped=source_code,
                context_id=_CTX,
                inline_declaration=True,
                inline_source=source_code,
            )
        ],
        imports="",
        created_at=_UTC,
    )
    rule = N02QuantifierRule.from_repository(registry_hash="4" * 64)
    (draft,) = rule.generate(source, source_representation, 23)
    assert f"({candidate_quantifier} n : Nat, n = n)" in draft.candidate_code

    candidate_id = make_id("thm", {"n02_live": name, "draft": draft.draft_id})
    candidate = _theorem(
        draft.candidate_code,
        theorem_id=candidate_id,
        name=name,
        ancestry_id=ancestry_id,
        parents=(source_id,),
    )
    (candidate_representation,) = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=candidate_id,
                full_name=name,
                proof_stripped=draft.candidate_code,
                context_id=_CTX,
                inline_declaration=True,
                inline_source=draft.candidate_code,
            )
        ],
        imports="",
        created_at=_UTC,
    )

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
    assert source_representation.operator_tree != candidate_representation.operator_tree
    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["semantic_negative_resolved"] is False
