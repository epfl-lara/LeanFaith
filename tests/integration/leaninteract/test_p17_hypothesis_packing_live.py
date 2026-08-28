"""Live LeanInteract smoke for LF-033 P17 final-hypothesis packing."""

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
from leanfaith.transforms.positives.p17_hypothesis_packing import (
    P17HypothesisPackingRule,
    apply_p17_trace,
)
from leanfaith.transforms.v2_e2_p17_runtime import build_v2_e2_p17_runtime
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
_PACK_SOURCE = "theorem p17_live_pack (P Q R : Prop) (hP : P) (hQ : Q) : R := by sorry"
_PACK_CANDIDATE = "theorem p17_live_pack (P Q R : Prop) (h_p17 : P ∧ Q) : R := by sorry"
_UNPACK_SOURCE = "theorem p17_live_unpack (P Q R : Prop) (h : P ∧ Q) : R := by sorry"
_UNPACK_CANDIDATE = (
    "theorem p17_live_unpack (P Q R : Prop) (h_p17_left : P) (h_p17_right : Q) : R := by sorry"
)


def _source_theorem(source: str, name: str) -> TheoremRecord:
    theorem_id = make_id("thm", {"p17_live": name})
    ancestry_id = make_id("anc", {"p17_live": name})
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


def test_p17_live_pack_unpack_inverse_and_pooled_materialization(tmp_path: Path) -> None:
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
        sources = (
            _source_theorem(_PACK_SOURCE, "p17_live_pack"),
            _source_theorem(_UNPACK_SOURCE, "p17_live_unpack"),
        )
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
        rule = P17HypothesisPackingRule(
            generation_config_hash="e" * 64,
            candidate_pool="p17_live_fixture",
        )
        expected = (_PACK_CANDIDATE, _UNPACK_CANDIDATE)
        drafts = []
        for source, representation, expected_candidate in zip(
            sources,
            representations,
            expected,
            strict=True,
        ):
            (draft,) = rule.generate(source, representation, seed=17)
            drafts.append(draft)
            assert draft.candidate_code == expected_candidate
            assert draft.inverse_trace is not None
            inverse_source = apply_p17_trace(draft.candidate_code, draft.inverse_trace)
            assert inverse_source == source.proof_stripped_declaration
            for request_id, code in (
                (f"p17-live-{source.declaration_name}-forward", draft.candidate_code),
                (f"p17-live-{source.declaration_name}-inverse", inverse_source),
            ):
                check = backend.run(
                    LeanRequest(
                        request_id=request_id,
                        context_id=_CTX,
                        code="import LeanFaithFixtures\n" + code,
                        declarations=True,
                        allow_sorry=True,
                    )
                )
                assert check.status == LeanStatus.VALID_WITH_SORRY

        results = materialize_v2_e2_batch(
            backend=backend,
            runtime=build_v2_e2_p17_runtime(),
            inputs=tuple(
                V2E2MaterializationInput(
                    theorem=source,
                    representation=representation,
                    rule_id="p17_hypothesis_packing",
                    seed=17,
                )
                for source, representation in zip(sources, representations, strict=True)
            ),
            context_id=_CTX,
            project_dir=_FIXTURES,
            import_header="import LeanFaithFixtures",
        )
        assert len(results) == 2
        for result in results:
            assert result.profile_id == "deterministic_v2_e2_p17_experimental"
            assert result.rule_id == "p17_hypothesis_packing"
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
