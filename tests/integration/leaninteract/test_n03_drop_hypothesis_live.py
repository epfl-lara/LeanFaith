"""LF-018 N03 same-context re-elaboration and exact expression-erasure tests."""

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
from leanfaith.schemas import (
    QualityTier,
    RepresentationRecord,
    TheoremRecord,
    ValidationStatus,
    make_id,
)
from leanfaith.transforms.negatives.n03_drop_hypothesis import (
    N03DropHypothesisRule,
    erase_outer_forall,
)

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
            raw_response_dir=tmp_path_factory.mktemp("n03_raw"),
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
        source="n03_live_fixture",
        source_revision="n03_drop_hypothesis_v1",
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


def _representation(
    backend: LeanInteractBackend,
    theorem_id: str,
    name: str,
    code: str,
) -> RepresentationRecord:
    return build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=theorem_id,
                full_name=name,
                proof_stripped=code,
                context_id=_CTX,
                inline_declaration=True,
                inline_source=code,
            )
        ],
        imports="",
        created_at=_UTC,
    )[0]


@pytest.mark.parametrize(
    ("name", "conclusion"),
    [
        ("n03_live_true", "True"),
        ("n03_live_arrow", "P → P"),
    ],
)
def test_n03_candidate_reelaborates_as_exact_unused_binder_erasure_but_stays_provisional(
    backend: LeanInteractBackend,
    name: str,
    conclusion: str,
) -> None:
    source_code = f"theorem {name} (P : Prop) (h : P) : {conclusion} := by sorry"
    source_id = make_id("thm", {"n03_live": name, "side": "source"})
    ancestry_id = make_id("anc", {"n03_live": name})
    source = _theorem(
        source_code,
        theorem_id=source_id,
        name=name,
        ancestry_id=ancestry_id,
    )
    source_representation = _representation(
        backend,
        source_id,
        name,
        source_code,
    )
    rule = N03DropHypothesisRule.from_repository(registry_hash="7" * 64)
    applicability = rule.assess(source, source_representation)
    assert applicability.applicable

    (draft,) = rule.generate(source, source_representation, 23)
    assert "(h : P)" not in draft.candidate_code
    assert draft.metadata["failed_proof_search_consulted"] is False

    candidate_id = make_id("thm", {"n03_live": name, "draft": draft.draft_id})
    candidate = _theorem(
        draft.candidate_code,
        theorem_id=candidate_id,
        name=name,
        ancestry_id=ancestry_id,
        parents=(source_id,),
    )
    candidate_representation = _representation(
        backend,
        candidate_id,
        name,
        draft.candidate_code,
    )
    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert source_representation.operator_tree is not None
    assert candidate_representation.operator_tree is not None
    source_root = source_representation.operator_tree["root"]
    assert isinstance(source_root, dict)
    assert candidate_representation.operator_tree["root"] == erase_outer_forall(
        source_root,
        1,
    )
    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.structural_diff_ok is True
    assert audit.atom_mapping_ok is True
    assert audit.metadata["semantic_negative_resolved"] is False


def test_n03_rejects_hypothesis_used_by_later_binder(
    backend: LeanInteractBackend,
) -> None:
    name = "n03_live_dependent"
    code = f"theorem {name} (P : Prop) (h : P) (q : h = h) : True := by sorry"
    theorem_id = make_id("thm", {"n03_live": name})
    ancestry_id = make_id("anc", {"n03_live": name})
    theorem = _theorem(
        code,
        theorem_id=theorem_id,
        name=name,
        ancestry_id=ancestry_id,
    )
    representation = _representation(backend, theorem_id, name, code)

    applicability = N03DropHypothesisRule.from_repository(registry_hash="7" * 64).assess(
        theorem, representation
    )

    assert not applicability.applicable
    assert applicability.reason_codes == ("no_independent_prop_hypothesis",)
