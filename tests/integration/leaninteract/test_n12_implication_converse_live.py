"""Live LeanInteract smoke for LF-034 N12's root-converse certificate."""

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
from leanfaith.schemas.enums import QualityTier, ValidationStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import TheoremRecord
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.negatives.n12_implication_converse import (
    N12ImplicationConverseRule,
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
_SOURCE = "theorem n12_live (Premise Goal : Prop) (h : Premise) : Goal := by sorry"


def _source_theorem() -> TheoremRecord:
    theorem_id = make_id("thm", {"n12_live": "source"})
    ancestry_id = make_id("anc", {"n12_live": "source"})
    return theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        context_id=_CTX,
        declaration_name="n12_live",
        declaration_full_name="n12_live",
        proof_stripped_declaration=_SOURCE,
        inline_elaboration_source="import LeanFaithFixtures\n" + _SOURCE,
        statement_content_hash=hashlib.sha256(_SOURCE.encode()).hexdigest(),
    )


def test_n12_live_candidate_elaborates_and_passes_exact_converse_audit(
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
                    full_name="n12_live",
                    proof_stripped=_SOURCE,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=source.inline_elaboration_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        rule = N12ImplicationConverseRule(
            generation_config_hash="b" * 64,
            candidate_pool="n12_live_fixture",
        )
        draft = rule.generate(source, source_representation, seed=7)[0]
        inline_source = "import LeanFaithFixtures\n" + draft.candidate_code
        result = backend.run(
            LeanRequest(
                request_id="n12-live-candidate",
                context_id=_CTX,
                code=inline_source,
                declarations=True,
                allow_sorry=True,
                timeout_seconds=120,
            )
        )
        assert result.status == LeanStatus.VALID_WITH_SORRY
        candidate = build_derived_theorem_record(
            draft=draft,
            sources=(source,),
            primary_source_id=source.theorem_id,
            elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            inline_elaboration_source=inline_source,
        )
        assert candidate.declaration_full_name is not None
        (candidate_representation,) = build_representations(
            backend,
            [
                TheoremForRepresentation(
                    theorem_id=candidate.theorem_id,
                    full_name=candidate.declaration_full_name,
                    proof_stripped=draft.candidate_code,
                    context_id=_CTX,
                    inline_declaration=True,
                    inline_source=inline_source,
                )
            ],
            imports="import LeanFaithFixtures",
            created_at=_NOW,
        )
        audit = rule.audit(
            source,
            source_representation,
            candidate,
            candidate_representation,
            draft,
        )
        assert audit.violation_codes == ()
        assert audit.structural_diff_ok is True
        assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
        assert audit.metadata["resolved_semantic_label"] is False
        assert audit.metadata["training_eligible"] is False
    finally:
        backend.close()
