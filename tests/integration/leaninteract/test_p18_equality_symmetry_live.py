"""Live LeanInteract smoke for P18 v1.0 root equality symmetry."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas.enums import Polarity, QualityTier
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.transforms.positives.p18_equality_symmetry import (
    P18EqualitySymmetryRule,
    apply_p18_trace,
)
from leanfaith.transforms.v2_e2_p18_runtime import build_v2_e2_p18_runtime
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
_NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)
_SOURCE = "theorem p18_live (x y : Nat) : x = y := by sorry"
_CANDIDATE = "theorem p18_live (x y : Nat) : y = x := by sorry"


def _source_theorem() -> TheoremRecord:
    theorem_id = make_id("thm", {"p18_live": "source"})
    ancestry_id = make_id("anc", {"p18_live": "source"})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name="p18_live",
        declaration_full_name="p18_live",
        proof_stripped_declaration=_SOURCE,
        inline_elaboration_source="import LeanFaithFixtures\n" + _SOURCE,
        statement_content_hash=hashlib.sha256(_SOURCE.encode()).hexdigest(),
    )


def test_p18_live_certificate_materialization_and_admission_free_iff(
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
        local_certificate = backend.run(
            LeanRequest(
                request_id="p18-live-local-certificate",
                context_id=_CTX,
                code=(
                    "import LeanFaithFixtures\n"
                    "universe u\n"
                    "example {α : Sort u} (lhs rhs : α) : "
                    "(lhs = rhs) ↔ (rhs = lhs) := by\n"
                    "  constructor\n"
                    "  · intro h\n"
                    "    exact h.symm\n"
                    "  · intro h\n"
                    "    exact h.symm\n"
                ),
                declarations=True,
                allow_sorry=False,
                timeout_seconds=120,
            )
        )
        assert local_certificate.status == LeanStatus.VALID

        source = _source_theorem()
        (source_representation,) = build_representations(
            backend,
            [
                TheoremForRepresentation(
                    theorem_id=source.theorem_id,
                    full_name="p18_live",
                    proof_stripped=_SOURCE,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=source.inline_elaboration_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        rule = P18EqualitySymmetryRule(
            generation_config_hash="e" * 64,
            candidate_pool="p18_live_fixture",
        )
        (draft,) = rule.generate(source, source_representation, seed=18)
        assert draft.candidate_code == _CANDIDATE
        assert draft.inverse_trace is not None
        assert apply_p18_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE

        (result,) = materialize_v2_e2_batch(
            backend=backend,
            runtime=build_v2_e2_p18_runtime(),
            inputs=(
                V2E2MaterializationInput(
                    theorem=source,
                    representation=source_representation,
                    rule_id="p18_root_equality_symmetry",
                    seed=18,
                ),
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert result.profile_id == "deterministic_v2_e2_p18_experimental"
        assert result.rule_id == "p18_root_equality_symmetry"
        assert result.evidence_class == "E2"
        assert result.terminal_status == "provisional_variant"
        assert result.variant is not None
        assert result.variant.polarity_metadata == Polarity.POSITIVE
        assert result.audit is not None
        assert result.audit.violation_codes == ()
        assert result.audit.structural_diff_ok is True
        assert result.audit.atom_mapping_ok is True
        assert result.audit.recommended_quality_tier == QualityTier.PROVISIONAL
        assert result.resolved_label_count == 0
        assert result.promoted_item_count == 0
        assert result.training_eligible is False
    finally:
        backend.close()
