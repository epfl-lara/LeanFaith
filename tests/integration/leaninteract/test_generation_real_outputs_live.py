"""LF-021 live validation: parsed statement to provisional semantic-pool records."""

from __future__ import annotations

import datetime
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.prompts import parse_direct_autoformalization_output
from leanfaith.generation.real_outputs import (
    CandidateScreeningRecord,
    RealOutputOutcomeCode,
    admit_screened_real_output_candidate,
    materialize_real_output_candidate,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.schemas.enums import (
    IntendedRelation,
    LLMCallStatus,
    LLMRole,
    NLTrust,
    ParseStatus,
    ValidationStatus,
)
from leanfaith.schemas.ids import ANCESTRY_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.llm import LLMCallRecord, make_llm_attempt_id, make_llm_call_id
from leanfaith.schemas.nl_lean import ProblemPoolRecord, make_problem_record_id
from leanfaith.schemas.theorem import ContextRecord, TheoremRecord

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

ROOT = find_repo_root(Path(__file__).parent)
FIXTURES = ROOT / "tests" / "lean_fixtures"
UTC = datetime.datetime(2026, 7, 23, 20, 0, tzinfo=datetime.UTC)
CTX_FP = "0" * 64
CTX_ID = f"ctx:{CTX_FP}"
IMPORTS = "import LeanFaithFixtures.Basic\nnamespace LF021Generated"
STATEMENT = "theorem lf021_generated_identity (n : Nat) : n = n"
RAW_ARTIFACT = "data/raw/real_outputs/live/attempt_0.json"


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=FIXTURES,
            context_fingerprint=CTX_FP,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
        )
    )
    yield instance
    instance.close()


def test_live_candidate_materializes_without_a_semantic_label(
    backend: LeanInteractBackend,
) -> None:
    reference_id = make_id(THEOREM_PREFIX, {"lf021": "reference"})
    ancestry_id = make_id(ANCESTRY_PREFIX, {"lf021": "reference"})
    reference = TheoremRecord(
        theorem_id=reference_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        source_revision="workspace",
        source_record="lf021-reference",
        context_id=CTX_ID,
        declaration_kind="theorem",
        declaration_name="reference",
        declaration_full_name="reference",
        proof_stripped_declaration="theorem reference (n : Nat) : n = n := by sorry",
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=sha256_hex(b"(n : Nat) : n = n"),
    )
    problem_id = make_problem_record_id(
        source="public_fixture",
        source_revision="workspace",
        source_split="test",
        source_record_id="lf021-live",
        problem_id="lf021-live",
    )
    problem = ProblemPoolRecord(
        problem_record_id=problem_id,
        problem_id="lf021-live",
        problem_group="grp:lf021-live",
        source="public_fixture",
        source_revision="workspace",
        source_split="test",
        source_record_id="lf021-live",
        source_record_content_hash="1" * 64,
        nl_statement="For every natural number n, prove n equals itself.",
        nl_trust=NLTrust.TRUSTED,
        nl_source_link="fixture://lf021-live",
        context_id=CTX_ID,
        import_header_artifact="artifacts/generation/lf021-live.lean",
        import_header_hash=sha256_hex(IMPORTS.encode()),
        reference_theorem_ids=(reference.theorem_id,),
        private_source_content=False,
        external_provider_eligible=False,
        release_eligible=True,
        eligibility="eligible",
        denylist_checked=True,
    )
    decoding = {"temperature": 0.0, "seed": 0}
    call_id = make_llm_call_id(
        provider="fixture",
        provider_slot="offline_fixture",
        model="fixture-model",
        model_family="fixture-family",
        model_revision="fixture-r1",
        role=LLMRole.AUTOFORMALIZER,
        problem_record_id=problem.problem_record_id,
        prompt_template_hash="3" * 64,
        prompt_render_hash="4" * 64,
        input_ids=(problem.problem_record_id,),
        decoding=decoding,
    )
    call = LLMCallRecord(
        schema_version=2,
        call_id=call_id,
        provider="fixture",
        provider_slot="offline_fixture",
        model="fixture-model",
        model_family="fixture-family",
        role=LLMRole.AUTOFORMALIZER,
        model_revision="fixture-r1",
        request_date=UTC,
        started_at=UTC,
        completed_at=UTC + datetime.timedelta(milliseconds=1),
        execution_mode="replay",
        prompt_template_id="direct_autoformalize",
        prompt_template_version="v1",
        prompt_template_hash="3" * 64,
        prompt_render_hash="4" * 64,
        request_artifact="data/raw/real_outputs/live/request.json",
        input_ids=(problem.problem_record_id,),
        decoding=decoding,
        raw_output_artifact=RAW_ARTIFACT,
        parsed_output={"lean_statement": STATEMENT},
        parse_status=ParseStatus.PARSED,
        retry_count=0,
        supervision_eligible=True,
        private_source_content=False,
        denylist_checked=True,
        problem_record_id=problem.problem_record_id,
        problem_id=problem.problem_id,
        problem_group=problem.problem_group,
        terminal_status=LLMCallStatus.COMPLETED,
        attempt_ids=(make_llm_attempt_id(call_id, 0),),
        latency_ms=1,
        provider_request_hash="5" * 64,
        request_artifact_sha256="6" * 64,
        raw_response_sha256="7" * 64,
    )
    context = ContextRecord(
        environment_schema_version=1,
        context_id=CTX_ID,
        context_fingerprint=CTX_FP,
        project_kind="local",
        project_uri="tests/lean_fixtures",
        project_revision="workspace",
        project_registry_key="fixtures",
        lean_version="v4.31.0-rc1",
        lean_interact_version="0.11.4",
        repl_revision="fixture",
        imports=("LeanFaithFixtures.Basic",),
        header_text=IMPORTS,
        header_hash=sha256_hex(IMPORTS.encode()),
    )

    result = materialize_real_output_candidate(
        problem=problem,
        parsed=parse_direct_autoformalization_output(f"```lean4\n{STATEMENT}\n```"),
        call=call,
        raw_output_artifact=RAW_ARTIFACT,
        context=context,
        references=(reference,),
        imports=IMPORTS,
        backend=backend,
        generation_config_hash="c" * 64,
        created_at=UTC,
    )

    assert result.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING
    assert not result.outcome.semantic_pool_eligible
    assert result.theorem is not None
    assert result.theorem.declaration_name == "lf021_generated_identity"
    assert result.theorem.declaration_full_name == ("LF021Generated.lf021_generated_identity")
    assert result.representation is not None
    assert result.representation.signature_explicit is not None
    assert result.representation.alpha_identity_fingerprint is not None
    assert result.variant.intended_relation is IntendedRelation.UNKNOWN
    assert result.pairs == ()
    assert result.nl_lean is None

    registry_hash = "e" * 64
    screening = CandidateScreeningRecord.create(
        problem_record_id=problem.problem_record_id,
        call_id=result.outcome.call_id,
        theorem=result.theorem,
        representation=result.representation,
        frozen_registry_hash=registry_hash,
        created_at=UTC + datetime.timedelta(seconds=1),
    )
    admitted = admit_screened_real_output_candidate(
        materialized=result,
        screening=screening,
        problem=problem,
        references=(reference,),
        expected_frozen_registry_hash=registry_hash,
        created_at=UTC + datetime.timedelta(seconds=2),
    )
    assert admitted.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED
    assert admitted.outcome.semantic_pool_eligible
    assert len(admitted.pairs) == 1
    assert admitted.pairs[0].resolved_label_id is None
    assert admitted.nl_lean is not None
    assert admitted.nl_lean.resolved_label_id is None
