"""Live LeanInteract checks for the conservative P11/P12 E0 slice."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, cast

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas import TheoremRecord, ValidationStatus, make_id
from leanfaith.transforms.v2_e0_materializer import materialize_v2_e0_candidate
from leanfaith.transforms.v2_e0_runtime import build_v2_e0_runtime

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CONTEXT_FINGERPRINT = "0" * 64
_CONTEXT_ID = f"ctx:{_CONTEXT_FINGERPRINT}"
_CREATED_AT = datetime.datetime(2026, 8, 10, tzinfo=datetime.UTC)


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
    ("rule_id", "name", "code"),
    [
        (
            "p11_bounded_quantifiers",
            "v2_e0_live_bounded",
            "theorem v2_e0_live_bounded (s : List Nat) (P : Nat → Prop) : ∀ x ∈ s, P x := by sorry",
        ),
        (
            "p12_proof_arrow_binder",
            "v2_e0_live_arrow",
            "theorem v2_e0_live_arrow (P Q : Prop) : P → Q := by sorry",
        ),
    ],
)
def test_p11_p12_materialize_with_exact_live_identity(
    backend: LeanInteractBackend,
    rule_id: str,
    name: str,
    code: str,
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
        runtime=build_v2_e0_runtime(),
        theorem=source,
        representation=source_representation,
        rule_id=cast(
            Literal["p11_bounded_quantifiers", "p12_proof_arrow_binder"],
            rule_id,
        ),
        seed=11,
        project_dir=_FIXTURES,
        import_header="import LeanFaithFixtures",
    )

    assert result.terminal_status == "provisional_variant"
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
