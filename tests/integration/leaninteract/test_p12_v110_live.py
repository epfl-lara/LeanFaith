"""Live LeanInteract identity check for the versioned P12 v1.1 expansion."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path

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
_PROFILE = Path("configs/transformations/v2_e0_p12_v110_experimental.yaml")


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CONTEXT_FINGERPRINT,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("p12_v110_live_raw"),
            enable_parallel_elaboration=False,
        )
    )
    yield instance
    instance.close()


def test_p12_v110_complex_root_prop_is_exact_e0(backend: LeanInteractBackend) -> None:
    code = "theorem p12_v110_live (x : Nat) : 0 < x ∧ x ≤ 10 → x = x := by sorry"
    theorem_id = make_id("thm", {"p12_v110_live": code})
    ancestry_id = make_id("anc", {"p12_v110_live": code})
    source = TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        parent_theorem_ids=(),
        source="p12_v110_live_fixture",
        source_revision="p12_v110_live_v1",
        context_id=_CONTEXT_ID,
        declaration_kind="theorem",
        declaration_name="p12_v110_live",
        declaration_full_name="p12_v110_live",
        proof_stripped_declaration=code,
        inline_elaboration_source="import LeanFaithFixtures\n" + code,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=hashlib.sha256(code.encode()).hexdigest(),
    )
    source_representation = build_representations(
        backend,
        [
            TheoremForRepresentation(
                theorem_id=source.theorem_id,
                full_name=source.declaration_full_name,
                proof_stripped=code,
                context_id=_CONTEXT_ID,
                inline_declaration=True,
                inline_source=source.inline_elaboration_source,
            )
        ],
        imports="",
        created_at=datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC),
    )[0]

    result = materialize_v2_e0_candidate(
        backend=backend,
        runtime=build_v2_e0_runtime(path=_PROFILE),
        theorem=source,
        representation=source_representation,
        rule_id="p12_proof_arrow_binder",
        seed=11,
        project_dir=_FIXTURES,
        import_header="import LeanFaithFixtures",
    )

    assert result.terminal_status == "provisional_variant"
    assert result.variant is not None
    assert result.candidate_representation is not None
    assert result.audit is not None
    assert result.audit.violation_codes == ()
    assert result.variant.rule_version == "1.1.0"
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
