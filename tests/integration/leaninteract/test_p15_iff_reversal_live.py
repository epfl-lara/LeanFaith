"""Live LeanInteract smoke for LF-033 P15's exact root-Iff reversal."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas.enums import Polarity, QualityTier
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.transforms.positives.p15_iff_reversal import P15IffReversalRule
from leanfaith.transforms.v2_e2_p15_runtime import build_v2_e2_p15_runtime
from leanfaith.transforms.v2_e2_scale import (
    V2E2MaterializationInput,
    materialize_v2_e2_batch,
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
_SOURCE = "theorem p15_live (P Q : Prop) : P ↔ Q := by sorry"
_CANDIDATE = "theorem p15_live (P Q : Prop) : Q ↔ P := by sorry"


def _source_theorem() -> TheoremRecord:
    theorem_id = make_id("thm", {"p15_live": "source"})
    ancestry_id = make_id("anc", {"p15_live": "source"})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name="p15_live",
        declaration_full_name="p15_live",
        proof_stripped_declaration=_SOURCE,
        inline_elaboration_source="import LeanFaithFixtures\n" + _SOURCE,
        statement_content_hash=hashlib.sha256(_SOURCE.encode()).hexdigest(),
    )


def test_p15_live_pooled_materializer_certifies_only_provisional_e2_variant(
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
                    full_name="p15_live",
                    proof_stripped=_SOURCE,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=source.inline_elaboration_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        rule = P15IffReversalRule(
            generation_config_hash="e" * 64,
            candidate_pool="p15_live_fixture",
        )
        (draft,) = rule.generate(source, source_representation, seed=15)
        assert draft.candidate_code == _CANDIDATE

        (result,) = materialize_v2_e2_batch(
            backend=backend,
            runtime=build_v2_e2_p15_runtime(),
            inputs=(
                V2E2MaterializationInput(
                    theorem=source,
                    representation=source_representation,
                    rule_id="p15_root_iff_reversal",
                    seed=15,
                ),
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert result.profile_id == "deterministic_v2_e2_p15_experimental"
        assert result.rule_id == "p15_root_iff_reversal"
        assert result.evidence_class == "E2"
        assert result.terminal_status == "provisional_variant"
        assert result.variant is not None
        assert result.variant.polarity_metadata == Polarity.POSITIVE
        assert result.audit is not None
        assert result.audit.violation_codes == ()
        assert result.audit.structural_diff_ok is True
        assert result.audit.recommended_quality_tier == QualityTier.PROVISIONAL
        assert result.resolved_label_count == 0
        assert result.promoted_item_count == 0
        assert result.training_eligible is False
    finally:
        backend.close()
