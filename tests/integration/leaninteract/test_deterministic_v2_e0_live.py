"""Live LeanInteract checks for the conservative P11/P12 E0 slice."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas import TheoremRecord, ValidationStatus, make_id
from leanfaith.transforms.v2_e0_materializer import materialize_v2_e0_candidate
from leanfaith.transforms.v2_e0_runtime import V2E0RuleId, build_v2_e0_runtime

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CONTEXT_FINGERPRINT = "0" * 64
_CONTEXT_ID = f"ctx:{_CONTEXT_FINGERPRINT}"
_CREATED_AT = datetime.datetime(2026, 8, 10, tzinfo=datetime.UTC)
_PROFILE = Path("configs/transformations/v2_e0_lf032_experimental.yaml")


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CONTEXT_FINGERPRINT,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("v2_e0_live_raw"),
            enable_parallel_elaboration=False,
        )
    )
    yield instance
    instance.close()


def _source(code: str, name: str) -> TheoremRecord:
    theorem_id = make_id("thm", {"v2_e0_live": name})
    ancestry_id = make_id("anc", {"v2_e0_live": name})
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        parent_theorem_ids=(),
        source="v2_e0_live_fixture",
        source_revision="v2_e0_live_v1",
        context_id=_CONTEXT_ID,
        declaration_kind="theorem",
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=code,
        inline_elaboration_source="import LeanFaithFixtures\n" + code,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
    )


@pytest.mark.parametrize(
    ("rule_id", "name", "code", "expected_status"),
    [
        (
            "p06_implicit_arguments",
            "v2_e0_live_implicit",
            "theorem v2_e0_live_implicit (xs : List Nat) : "
            "@List.length Nat xs = xs.length := by sorry",
            "provisional_variant",
        ),
        (
            "p07_coercion_surface",
            "v2_e0_live_coercion",
            "theorem v2_e0_live_coercion (n : Nat) : (↑n : Int) = 0 := by sorry",
            "not_applicable",
        ),
        (
            "p09_projections",
            "v2_e0_live_projection",
            "theorem v2_e0_live_projection (p : Nat × Nat) : p.1 = 0 := by sorry",
            "provisional_variant",
        ),
        (
            "p10_constructors",
            "v2_e0_live_constructor",
            "theorem v2_e0_live_constructor (a : Nat) (b : Bool) : "
            "(⟨a, b⟩ : Nat × Bool) = (a, b) := by sorry",
            "provisional_variant",
        ),
        (
            "p11_bounded_quantifiers",
            "v2_e0_live_bounded",
            "theorem v2_e0_live_bounded (s : List Nat) (P : Nat → Prop) : ∀ x ∈ s, P x := by sorry",
            "provisional_variant",
        ),
        (
            "p12_proof_arrow_binder",
            "v2_e0_live_arrow",
            "theorem v2_e0_live_arrow (P Q : Prop) : P → Q := by sorry",
            "provisional_variant",
        ),
    ],
)
def test_lf032_materializes_only_with_exact_live_identity(
    backend: LeanInteractBackend,
    rule_id: str,
    name: str,
    code: str,
    expected_status: str,
) -> None:
    source = _source(code, name)
    source_representation = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=source.theorem_id,
                full_name=name,
                proof_stripped=code,
                context_id=_CONTEXT_ID,
                inline_declaration=True,
                inline_source=source.inline_elaboration_source,
            )
        ],
        imports="",
        created_at=_CREATED_AT,
    )[0]

    result = materialize_v2_e0_candidate(
        backend=backend,
        runtime=build_v2_e0_runtime(path=_PROFILE),
        theorem=source,
        representation=source_representation,
        rule_id=cast(V2E0RuleId, rule_id),
        seed=11,
        project_dir=_FIXTURES,
        import_header="import LeanFaithFixtures",
    )

    assert result.terminal_status == expected_status
    if expected_status == "not_applicable":
        assert result.variant is None
        assert result.attempt.applicability.reason_codes
        return
    assert result.variant is not None
    assert result.candidate_representation is not None
    assert result.audit is not None
    assert result.audit.violation_codes == ()
    assert source_representation.alpha_identity_fingerprint == (
        result.candidate_representation.alpha_identity_fingerprint
    )
    assert source_representation.signature_explicit == (
        result.candidate_representation.signature_explicit
    )
    assert source_representation.semantic_atoms == result.candidate_representation.semantic_atoms
    assert result.resolved_label_count == 0
    assert result.promoted_item_count == 0
    assert result.training_eligible is False
