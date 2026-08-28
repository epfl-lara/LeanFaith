"""Live LeanInteract smoke for LF-034 N15's exact projection certificate."""

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
from leanfaith.transforms.negatives.n15_conjunct_omission import N15ConjunctOmissionRule
from leanfaith.transforms.v2_d0_n15_runtime import build_v2_d0_n15_runtime
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
_SOURCE = "theorem n15_live (x : Nat) : x = 0 ∧ x = 1 := by sorry"


def _source_theorem() -> TheoremRecord:
    theorem_id = make_id("thm", {"n15_live": "source"})
    ancestry_id = make_id("anc", {"n15_live": "source"})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name="n15_live",
        declaration_full_name="n15_live",
        proof_stripped_declaration=_SOURCE,
        inline_elaboration_source="import LeanFaithFixtures\n" + _SOURCE,
        statement_content_hash=hashlib.sha256(_SOURCE.encode()).hexdigest(),
    )


def test_n15_live_pooled_materializer_certifies_only_provisional_variant(
    tmp_path: Path,
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
        source = _source_theorem()
        (source_representation,) = build_representations(
            backend,
            [
                TheoremForRepresentation(
                    theorem_id=source.theorem_id,
                    full_name="n15_live",
                    proof_stripped=_SOURCE,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=source.inline_elaboration_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        rule = N15ConjunctOmissionRule(
            generation_config_hash="d" * 64,
            candidate_pool="n15_live_fixture",
        )
        (draft,) = rule.generate(source, source_representation, seed=15)
        assert draft.candidate_code in {
            "theorem n15_live (x : Nat) : x = 0 := by sorry",
            "theorem n15_live (x : Nat) : x = 1 := by sorry",
        }

        (result,) = materialize_v2_d0_batch(
            backend=backend,
            runtime=build_v2_d0_n15_runtime(),
            inputs=(
                V2D0MaterializationInput(
                    theorem=source,
                    representation=source_representation,
                    rule_id="n15_conjunct_omission",
                    seed=15,
                ),
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert result.profile_id == "deterministic_v2_d0_n15_experimental"
        assert result.rule_id == "n15_conjunct_omission"
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
