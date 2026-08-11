"""Live LeanInteract smoke for N18 v1.0 root equality polarity."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.representations import TheoremForRepresentation, build_representations
from leanfaith.schemas.enums import IntendedRelation, Polarity, QualityTier
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.transforms.negatives.n18_equality_polarity import (
    N18EqualityPolarityRule,
    apply_n18_trace,
)
from leanfaith.transforms.v2_d0_n18_runtime import build_v2_d0_n18_runtime
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
_NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)
_SOURCE = "theorem n18_live (x y : Nat) : x = y := by sorry"
_CANDIDATE = "theorem n18_live (x y : Nat) : x ≠ y := by sorry"
_REVERSE_SOURCE = "theorem n18_reverse_live (x y : Nat) : x ≠ y := by sorry"
_REVERSE_CANDIDATE = "theorem n18_reverse_live (x y : Nat) : x = y := by sorry"
_COMPLEX_SOURCE = "theorem n18_complex_live (x y : Nat) : x + 1 = Nat.succ y := by sorry"
_COMPLEX_CANDIDATE = "theorem n18_complex_live (x y : Nat) : x + 1 ≠ Nat.succ y := by sorry"


def _source_theorem() -> TheoremRecord:
    theorem_id = make_id("thm", {"n18_live": "source"})
    ancestry_id = make_id("anc", {"n18_live": "source"})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name="n18_live",
        declaration_full_name="n18_live",
        proof_stripped_declaration=_SOURCE,
        inline_elaboration_source="import LeanFaithFixtures\n" + _SOURCE,
        statement_content_hash=hashlib.sha256(_SOURCE.encode()).hexdigest(),
    )


def _reverse_source_theorem() -> TheoremRecord:
    theorem_id = make_id("thm", {"n18_live": "reverse_source"})
    ancestry_id = make_id("anc", {"n18_live": "reverse_source"})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name="n18_reverse_live",
        declaration_full_name="n18_reverse_live",
        proof_stripped_declaration=_REVERSE_SOURCE,
        inline_elaboration_source="import LeanFaithFixtures\n" + _REVERSE_SOURCE,
        statement_content_hash=hashlib.sha256(_REVERSE_SOURCE.encode()).hexdigest(),
    )


def _complex_source_theorem() -> TheoremRecord:
    theorem_id = make_id("thm", {"n18_live": "complex_source"})
    ancestry_id = make_id("anc", {"n18_live": "complex_source"})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name="n18_complex_live",
        declaration_full_name="n18_complex_live",
        proof_stripped_declaration=_COMPLEX_SOURCE,
        inline_elaboration_source="import LeanFaithFixtures\n" + _COMPLEX_SOURCE,
        statement_content_hash=hashlib.sha256(_COMPLEX_SOURCE.encode()).hexdigest(),
    )


def test_n18_live_same_context_materialization_and_no_semantic_credit(
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
                    full_name="n18_live",
                    proof_stripped=_SOURCE,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=source.inline_elaboration_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        rule = N18EqualityPolarityRule(
            generation_config_hash="e" * 64,
            candidate_pool="n18_live_fixture",
        )
        (draft,) = rule.generate(source, source_representation, seed=18)
        assert draft.candidate_code == _CANDIDATE
        assert draft.intended_relation == IntendedRelation.NEAR_MISS
        assert draft.intended_error_types == ("E10", "E26")
        assert draft.inverse_trace is not None
        assert apply_n18_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE

        (result,) = materialize_v2_d0_batch(
            backend=backend,
            runtime=build_v2_d0_n18_runtime(),
            inputs=(
                V2D0MaterializationInput(
                    theorem=source,
                    representation=source_representation,
                    rule_id="n18_root_equality_polarity",
                    seed=18,
                ),
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert result.profile_id == "deterministic_v2_d0_n18_experimental"
        assert result.rule_id == "n18_root_equality_polarity"
        assert result.terminal_status == "provisional_variant"
        assert result.variant is not None
        assert result.variant.polarity_metadata == Polarity.NEGATIVE
        assert result.audit is not None
        assert result.audit.violation_codes == ()
        assert result.audit.structural_diff_ok is True
        assert result.audit.atom_mapping_ok is True
        assert result.audit.inverse_or_roundtrip_ok is True
        assert result.audit.recommended_quality_tier == QualityTier.PROVISIONAL
        assert result.audit.metadata["resolved_semantic_label"] is False
        assert result.resolved_label_count == 0
        assert result.promoted_item_count == 0
        assert result.training_eligible is False

        reverse_source = _reverse_source_theorem()
        (reverse_representation,) = build_representations(
            backend,
            [
                TheoremForRepresentation(
                    theorem_id=reverse_source.theorem_id,
                    full_name="n18_reverse_live",
                    proof_stripped=_REVERSE_SOURCE,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=reverse_source.inline_elaboration_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        (reverse_result,) = materialize_v2_d0_batch(
            backend=backend,
            runtime=build_v2_d0_n18_runtime(),
            inputs=(
                V2D0MaterializationInput(
                    theorem=reverse_source,
                    representation=reverse_representation,
                    rule_id="n18_root_equality_polarity",
                    seed=19,
                ),
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert reverse_result.terminal_status == "provisional_variant"
        assert reverse_result.draft is not None
        assert reverse_result.draft.candidate_code == _REVERSE_CANDIDATE
        assert reverse_result.audit is not None
        assert reverse_result.audit.violation_codes == ()
        assert reverse_result.audit.structural_diff_ok is True
        assert reverse_result.audit.atom_mapping_ok is True
        assert reverse_result.audit.metadata["structural_direction"] == "ne_to_eq"
        assert reverse_result.resolved_label_count == 0
        assert reverse_result.training_eligible is False

        complex_source = _complex_source_theorem()
        (complex_representation,) = build_representations(
            backend,
            [
                TheoremForRepresentation(
                    theorem_id=complex_source.theorem_id,
                    full_name="n18_complex_live",
                    proof_stripped=_COMPLEX_SOURCE,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=complex_source.inline_elaboration_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        (complex_result,) = materialize_v2_d0_batch(
            backend=backend,
            runtime=build_v2_d0_n18_runtime(),
            inputs=(
                V2D0MaterializationInput(
                    theorem=complex_source,
                    representation=complex_representation,
                    rule_id="n18_root_equality_polarity",
                    seed=20,
                ),
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert complex_result.terminal_status == "provisional_variant"
        assert complex_result.draft is not None
        assert complex_result.draft.candidate_code == _COMPLEX_CANDIDATE
        assert complex_result.audit is not None
        assert complex_result.audit.violation_codes == ()
        assert complex_result.audit.structural_diff_ok is True
        assert complex_result.audit.atom_mapping_ok is True
        assert complex_result.audit.metadata["structural_direction"] == "eq_to_ne"
        assert complex_result.resolved_label_count == 0
        assert complex_result.training_eligible is False
    finally:
        backend.close()
