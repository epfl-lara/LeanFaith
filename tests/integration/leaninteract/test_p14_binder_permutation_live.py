"""Live LeanInteract smoke for LF-033 P14 binder permutation."""

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
from leanfaith.transforms.positives.p14_binder_permutation import apply_p14_trace
from leanfaith.transforms.v2_e2_p14_runtime import build_v2_e2_p14_runtime
from leanfaith.transforms.v2_e2_scale import V2E2MaterializationInput, materialize_v2_e2_batch
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
_CASES = (
    (
        "p14_live_nat",
        "theorem p14_live_nat (x y : Nat) : x = y := by sorry",
        "theorem p14_live_nat (y x : Nat) : x = y := by sorry",
    ),
    (
        "p14_live_hidden",
        "theorem p14_live_hidden (x y : α) : x = y := by sorry",
        "theorem p14_live_hidden (y x : α) : x = y := by sorry",
    ),
    (
        "p14_live_prop",
        "theorem p14_live_prop (P Q : Prop) : P ↔ Q := by sorry",
        "theorem p14_live_prop (Q P : Prop) : P ↔ Q := by sorry",
    ),
)


def _source_theorem(name: str, source: str) -> TheoremRecord:
    theorem_id = make_id("thm", {"p14_live": name})
    ancestry_id = make_id("anc", {"p14_live": name})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=source,
        inline_elaboration_source="import LeanFaithFixtures\n" + source,
        statement_content_hash=hashlib.sha256(source.encode()).hexdigest(),
    )


def test_p14_live_exact_tree_certificate_and_pooled_materialization(tmp_path: Path) -> None:
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
        sources = tuple(_source_theorem(name, source) for name, source, _candidate in _CASES)
        representations = build_representations(
            backend,
            [
                TheoremForRepresentation(
                    theorem_id=source.theorem_id,
                    full_name=source.declaration_full_name or "",
                    proof_stripped=source.proof_stripped_declaration,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=source.inline_elaboration_source,
                )
                for source in sources
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        runtime = build_v2_e2_p14_runtime()
        executions = tuple(
            runtime.execute("p14_independent_binder_permutation", source, representation, seed=14)
            for source, representation in zip(sources, representations, strict=True)
        )
        for execution, (_name, source_text, candidate_text) in zip(executions, _CASES, strict=True):
            (draft,) = execution.drafts
            assert draft.candidate_code == candidate_text
            assert draft.inverse_trace is not None
            assert apply_p14_trace(draft.candidate_code, draft.inverse_trace) == source_text

        results = materialize_v2_e2_batch(
            backend=backend,
            runtime=runtime,
            inputs=tuple(
                V2E2MaterializationInput(
                    theorem=source,
                    representation=representation,
                    rule_id="p14_independent_binder_permutation",
                    seed=14,
                )
                for source, representation in zip(sources, representations, strict=True)
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert len(results) == len(_CASES)
        for result in results:
            assert result.profile_id == "deterministic_v2_e2_p14_experimental"
            assert result.rule_id == "p14_independent_binder_permutation"
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
