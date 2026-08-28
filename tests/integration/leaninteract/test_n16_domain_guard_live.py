"""Live LeanInteract smoke for LF-034 N16's exact guard-removal certificate."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas.enums import QualityTier
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.transforms.negatives.n16_domain_guard import N16DomainGuardRule
from leanfaith.transforms.v2_d0_n16_runtime import build_v2_d0_n16_runtime
from leanfaith.transforms.v2_d0_scale import (
    V2D0MaterializationInput,
    materialize_v2_d0_batch,
)
from tests.unit.record_factories import theorem_record

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_ROOT = find_repo_root(Path(__file__).parent)
_FIXTURES = _ROOT / "tests" / "lean_fixtures"
_CTX_FP = "0" * 64
_CTX = f"ctx:{_CTX_FP}"
_NOW = datetime.datetime(2026, 8, 10, tzinfo=datetime.UTC)
_EXPLICIT_SOURCE = "theorem n16_live (s : List Nat) : ∀ x : Nat, x ∈ s → x = 0 := by sorry"
_BOUNDED_SOURCE = "theorem n16_bounded (s : List Nat) : ∀ x ∈ s, x = 0 := by sorry"


def _source_theorem(name: str, source_code: str) -> TheoremRecord:
    theorem_id = make_id("thm", {name: "source"})
    ancestry_id = make_id("anc", {name: "source"})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=source_code,
        inline_elaboration_source="import LeanFaithFixtures\n" + source_code,
        statement_content_hash=hashlib.sha256(source_code.encode()).hexdigest(),
    )


@pytest.mark.parametrize(
    ("name", "source_code", "candidate_code"),
    (
        (
            "n16_live",
            _EXPLICIT_SOURCE,
            "theorem n16_live (s : List Nat) : ∀ x : Nat, x = 0 := by sorry",
        ),
        (
            "n16_bounded",
            _BOUNDED_SOURCE,
            "theorem n16_bounded (s : List Nat) : ∀ x, x = 0 := by sorry",
        ),
    ),
)
def test_n16_live_pooled_materializer_certifies_only_provisional_variant(
    tmp_path: Path,
    name: str,
    source_code: str,
    candidate_code: str,
) -> None:
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CTX_FP,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            enable_parallel_elaboration=False,
        )
    )
    try:
        source = _source_theorem(name, source_code)
        (source_representation,) = build_representations(
            backend,
            [
                TheoremForRepresentation(
                    theorem_id=source.theorem_id,
                    full_name=name,
                    proof_stripped=source_code,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=source.inline_elaboration_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        rule = N16DomainGuardRule(
            generation_config_hash="d" * 64,
            candidate_pool="n16_live_fixture",
        )
        (draft,) = rule.generate(source, source_representation, seed=16)
        assert draft.candidate_code == candidate_code

        (result,) = materialize_v2_d0_batch(
            backend=backend,
            runtime=build_v2_d0_n16_runtime(),
            inputs=(
                V2D0MaterializationInput(
                    theorem=source,
                    representation=source_representation,
                    rule_id="n16_domain_guard_removal",
                    seed=16,
                ),
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert result.profile_id == "deterministic_v2_d0_n16_experimental"
        assert result.rule_id == "n16_domain_guard_removal"
        assert result.terminal_status == "provisional_variant"
        assert result.variant is not None
        assert result.audit is not None
        assert result.audit.violation_codes == ()
        assert result.audit.structural_diff_ok is True
        assert result.audit.recommended_quality_tier == QualityTier.PROVISIONAL
        assert result.resolved_label_count == 0
        assert result.promoted_item_count == 0
        assert result.training_eligible is False
    finally:
        backend.close()
