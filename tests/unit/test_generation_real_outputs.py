from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import cast

import pytest

import leanfaith.generation.real_outputs as real_outputs
from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.generation.prompts import parse_direct_autoformalization_output
from leanfaith.generation.real_outputs import (
    CandidateScreeningRecord,
    CandidateScreeningStatus,
    RealOutputFailureCode,
    RealOutputMaterializationError,
    RealOutputOutcomeCode,
    admit_screened_real_output_candidate,
    materialize_real_output_candidate,
)
from leanfaith.lean.extraction import PLACEHOLDER
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.pipeline import (
    RepresentationBatch,
    RepresentationBatchResult,
)
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    LLMCallStatus,
    LLMRole,
    NLTrust,
    ParseStatus,
    QualityTier,
    ValidationStatus,
    ViewStatus,
)
from leanfaith.schemas.ids import (
    ANCESTRY_PREFIX,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    make_id,
)
from leanfaith.schemas.llm import LLMCallRecord, make_llm_attempt_id, make_llm_call_id
from leanfaith.schemas.nl_lean import ProblemPoolRecord, make_problem_record_id
from leanfaith.schemas.theorem import (
    CANONICAL_VIEW_NAMES,
    ContextRecord,
    RepresentationRecord,
    TheoremRecord,
)

UTC = datetime.datetime(2026, 7, 23, 20, 0, tzinfo=datetime.UTC)
IMPORTS = "import LeanFaithFixtures.Basic"
RAW_ARTIFACT = "data/raw/real_outputs/call/attempt_0.json"
CONFIG_HASH = "c" * 64
CTX_FP = "0" * 64
CTX_ID = f"ctx:{CTX_FP}"
STATEMENT = "theorem generated_identity (n : Nat) : n = n"


class FakeBackend:
    def __init__(self, result: LeanResult | Exception) -> None:
        self.result = result
        self.requests: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


def _context() -> ContextRecord:
    return ContextRecord(
        environment_schema_version=1,
        context_id=CTX_ID,
        context_fingerprint=CTX_FP,
        project_kind="local",
        project_uri="tests/lean_fixtures",
        project_revision="fixture",
        project_registry_key="fixtures",
        lean_version="v4.31.0-rc1",
        lean_interact_version="0.11.4",
        repl_revision="fixture",
        imports=("LeanFaithFixtures.Basic",),
        header_text=IMPORTS,
        header_hash=sha256_hex(IMPORTS.encode("utf-8")),
    )


def _reference(name: str = "reference") -> TheoremRecord:
    theorem_id = make_id(THEOREM_PREFIX, {"reference": name})
    ancestry_id = make_id(ANCESTRY_PREFIX, {"reference": name})
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="fixtures",
        source_revision="fixture",
        source_record=f"{name}.lean",
        context_id=CTX_ID,
        declaration_kind="theorem",
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=f"theorem {name} (n : Nat) : n = n := by sorry",
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=sha256_hex(f"(n : Nat) : n = n:{name}".encode()),
    )


def _problem(reference: TheoremRecord) -> ProblemPoolRecord:
    fields = {
        "problem_id": "public-problem-1",
        "problem_group": "grp:public-problem-1",
        "source": "public_fixture",
        "source_revision": "fixture-r1",
        "source_split": "train",
        "source_record_id": "row-1",
        "source_record_content_hash": "1" * 64,
        "nl_statement": "For every natural number n, prove n = n.",
        "nl_trust": NLTrust.TRUSTED,
        "nl_source_link": "fixture://public/row-1",
        "context_id": CTX_ID,
        "import_header_artifact": "artifacts/generation/headers/row-1.lean",
        "import_header_hash": sha256_hex(IMPORTS.encode("utf-8")),
        "reference_theorem_ids": (reference.theorem_id,),
        "private_source_content": False,
        "external_provider_eligible": True,
        "release_eligible": True,
        "eligibility": "eligible",
        "denylist_checked": True,
    }
    return ProblemPoolRecord(
        problem_record_id=make_problem_record_id(
            source=str(fields["source"]),
            source_revision=str(fields["source_revision"]),
            source_split=str(fields["source_split"]),
            source_record_id=str(fields["source_record_id"]),
            problem_id=str(fields["problem_id"]),
        ),
        **fields,
    )


def _call(problem: ProblemPoolRecord, *, lean_statement: str = STATEMENT) -> LLMCallRecord:
    decoding = {"temperature": 0.0, "seed": 7}
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
    return LLMCallRecord(
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
        completed_at=UTC + datetime.timedelta(milliseconds=20),
        execution_mode="replay",
        prompt_template_id="direct_autoformalize",
        prompt_template_version="v1",
        prompt_template_hash="3" * 64,
        prompt_render_hash="4" * 64,
        request_artifact="data/raw/real_outputs/call/request.json",
        input_ids=(problem.problem_record_id,),
        decoding=decoding,
        raw_output_artifact=RAW_ARTIFACT,
        parsed_output={"lean_statement": lean_statement},
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
        latency_ms=20,
        provider_request_hash="5" * 64,
        request_artifact_sha256="6" * 64,
        raw_response_sha256="7" * 64,
    )


def _declaration(
    name: str = "generated_identity",
    *,
    full_name: str | None = None,
    kind: str = "theorem",
) -> dict:
    line = STATEMENT + PLACEHOLDER
    prefix = f"theorem {name} "
    return {
        "name": name,
        "full_name": full_name or name,
        "kind": kind,
        "range": {
            "start": {"line": 2, "column": 0},
            "finish": {"line": 2, "column": len(line)},
        },
        "signature": {
            "pp": "(n : Nat) : n = n",
            "range": {
                "start": {"line": 2, "column": len(prefix)},
                "finish": {"line": 2, "column": len(STATEMENT)},
            },
        },
        "type": {"pp": "n = n"},
    }


def _lean_result(
    *,
    status: LeanStatus = LeanStatus.VALID_WITH_SORRY,
    declarations: tuple[dict, ...] | None = None,
) -> LeanResult:
    return LeanResult(
        request_id="lf021",
        request_hash="a" * 64,
        context_id=CTX_ID,
        context_fingerprint=CTX_FP,
        status=status,
        declarations=(_declaration(),) if declarations is None else declarations,
        messages=({"severity": "error", "data": "fixture invalid"},)
        if status == LeanStatus.INVALID
        else (),
    )


def _representation(theorem_id: str) -> RepresentationRecord:
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for name in (
        "raw_proof_stripped",
        "headless",
        "signature_pp",
        "signature_explicit",
        "semantic_atoms",
        "operator_tree",
    ):
        statuses[name] = ViewStatus.OK
    return RepresentationRecord(
        representation_id=make_id(
            REPRESENTATION_PREFIX,
            {"theorem_id": theorem_id, "normalization_version": "repr_v2"},
        ),
        theorem_id=theorem_id,
        normalization_version="repr_v2",
        context_id=CTX_ID,
        raw_proof_stripped=STATEMENT + PLACEHOLDER,
        headless="(n : Nat) : n = n",
        signature_pp="(n : Nat) : n = n",
        signature_explicit="(n : Nat) : Eq Nat n n",
        semantic_atoms=("Eq", "Nat"),
        operator_tree={"kind": "const", "name": "Eq"},
        alpha_identity_fingerprint="d" * 64,
        view_status=statuses,
        content_hash=hash_canonical({"theorem_id": theorem_id, "views": "fixture"}),
        created_at=UTC,
    )


def _patch_representation_success(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[RepresentationBatch],
) -> None:
    def build(
        backend: LeanInteractBackend,
        batch: RepresentationBatch,
        *,
        created_at: datetime.datetime,
    ) -> RepresentationBatchResult:
        del backend, created_at
        captured.append(batch)
        theorem_id = batch.ordered_theorem_inputs[0].theorem_id
        return RepresentationBatchResult((_representation(theorem_id),), ())

    monkeypatch.setattr(real_outputs, "build_representation_batch", build)


def _materialize(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    *,
    call_statement: str = STATEMENT,
    raw_artifact: str = RAW_ARTIFACT,
    imports: str = IMPORTS,
) -> tuple[real_outputs.RealOutputMaterializationResult, list[RepresentationBatch]]:
    reference = _reference()
    problem = _problem(reference)
    captured: list[RepresentationBatch] = []
    _patch_representation_success(monkeypatch, captured)
    result = materialize_real_output_candidate(
        problem=problem,
        parsed=parse_direct_autoformalization_output(f"```lean4\n{STATEMENT}\n```"),
        call=_call(problem, lean_statement=call_statement),
        raw_output_artifact=raw_artifact,
        context=_context(),
        references=(reference,),
        imports=imports,
        backend=cast(LeanInteractBackend, backend),
        generation_config_hash=CONFIG_HASH,
        created_at=UTC,
    )
    return result, captured


def test_materializes_then_admits_only_after_clean_screening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(_lean_result())
    result, representation_batches = _materialize(monkeypatch, backend)

    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert request.allow_sorry is True
    assert request.declarations is True
    assert request.code is not None
    assert request.code.endswith(STATEMENT + PLACEHOLDER)
    assert request.code.count(PLACEHOLDER) == 1

    assert result.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING
    assert result.outcome.semantic_pool_eligible is False
    assert result.variant.generator_kind is GeneratorKind.AUTOFORMALIZER
    assert result.variant.intended_relation is IntendedRelation.UNKNOWN
    assert result.variant.quality_tier is QualityTier.PROVISIONAL
    assert result.variant.raw_output_artifact == RAW_ARTIFACT
    assert result.theorem is not None
    assert result.representation is not None
    assert len(representation_batches) == 1
    representation_input = representation_batches[0].ordered_theorem_inputs[0]
    assert representation_input.inline_declaration is True
    assert representation_input.inline_source == result.theorem.inline_elaboration_source
    assert representation_batches[0].import_header == ""

    assert result.pairs == ()
    assert result.nl_lean is None

    reference = _reference()
    problem = _problem(reference)
    registry_hash = "e" * 64
    screening = CandidateScreeningRecord.create(
        problem_record_id=problem.problem_record_id,
        call_id=result.outcome.call_id,
        theorem=result.theorem,
        representation=result.representation,
        frozen_registry_hash=registry_hash,
        created_at=UTC + datetime.timedelta(seconds=1),
    )
    assert screening.status is CandidateScreeningStatus.CLEAN
    admitted = admit_screened_real_output_candidate(
        materialized=result,
        screening=screening,
        problem=problem,
        references=(reference,),
        expected_frozen_registry_hash=registry_hash,
        created_at=UTC + datetime.timedelta(seconds=2),
    )

    assert admitted.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED
    assert admitted.outcome.semantic_pool_eligible is True
    assert admitted.outcome.screening_id == screening.screening_id
    assert len(admitted.pairs) == 1
    pair = admitted.pairs[0]
    assert pair.resolved_label_id is None
    assert pair.intended_relation is IntendedRelation.UNKNOWN
    assert pair.nl_problem_group in pair.split_group_ids
    assert pair.metadata["resolved_semantic_label"] is False
    assert admitted.nl_lean is not None
    assert admitted.nl_lean.schema_version == 2
    assert admitted.nl_lean.resolved_label_id is None
    assert admitted.nl_lean.reference_pairs[0].pair_id == pair.pair_id
    assert admitted.nl_lean.problem_group in admitted.nl_lean.split_group_ids
    assert admitted.theorem is not None
    assert admitted.theorem.parent_theorem_ids == (pair.theorem_a_id,)
    assert set(admitted.theorem.root_ancestry_ids) <= set(pair.split_group_ids)


def test_noncompiling_candidate_is_retained_but_not_pooled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(_lean_result(status=LeanStatus.INVALID, declarations=()))
    result, representation_batches = _materialize(monkeypatch, backend)

    assert result.outcome.outcome is RealOutputOutcomeCode.NONCOMPILING
    assert result.outcome.failure_code is RealOutputFailureCode.LEAN_INVALID
    assert result.outcome.semantic_pool_eligible is False
    assert result.variant.validation_status is ValidationStatus.INVALID
    assert result.variant.raw_output_artifact == RAW_ARTIFACT
    assert result.theorem is None
    assert result.representation is None
    assert result.pairs == ()
    assert result.nl_lean is None
    assert representation_batches == []


@pytest.mark.parametrize(
    ("declarations", "failure_code"),
    [
        ((), RealOutputFailureCode.DECLARATION_COUNT),
        (
            (_declaration(), _declaration("second")),
            RealOutputFailureCode.DECLARATION_COUNT,
        ),
        ((_declaration("wrong_name"),), RealOutputFailureCode.DECLARATION_NAME),
        ((_declaration(kind="definition"),), RealOutputFailureCode.DECLARATION_KIND),
    ],
)
def test_elaborating_but_wrong_declaration_shape_is_quarantined(
    monkeypatch: pytest.MonkeyPatch,
    declarations: tuple[dict, ...],
    failure_code: RealOutputFailureCode,
) -> None:
    backend = FakeBackend(_lean_result(declarations=declarations))
    result, representation_batches = _materialize(monkeypatch, backend)
    assert result.outcome.outcome is RealOutputOutcomeCode.QUARANTINED
    assert result.outcome.failure_code is failure_code
    assert not result.outcome.semantic_pool_eligible
    assert result.pairs == ()
    assert result.nl_lean is None
    assert representation_batches == []


def test_backend_exception_is_persistable_infrastructure_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _materialize(monkeypatch, FakeBackend(RuntimeError("worker died")))
    assert result.outcome.outcome is RealOutputOutcomeCode.INFRASTRUCTURE_ERROR
    assert result.outcome.failure_code is RealOutputFailureCode.LEAN_INFRASTRUCTURE
    assert "worker died" in (result.outcome.failure_detail or "")
    assert result.variant.validation_status is ValidationStatus.INFRASTRUCTURE_ERROR
    assert result.pairs == ()


def test_unsupported_backend_status_is_not_counted_as_noncompiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _materialize(
        monkeypatch,
        FakeBackend(_lean_result(status=LeanStatus.UNSUPPORTED, declarations=())),
    )
    assert result.outcome.outcome is RealOutputOutcomeCode.UNSUPPORTED
    assert result.outcome.failure_code is RealOutputFailureCode.LEAN_UNSUPPORTED
    assert result.outcome.validation_status is ValidationStatus.QUARANTINED


def test_namespace_qualified_full_name_preserves_syntactic_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(
        _lean_result(
            declarations=(
                _declaration(
                    name="generated_identity",
                    full_name="FixtureNamespace.generated_identity",
                ),
            )
        )
    )
    result, batches = _materialize(monkeypatch, backend)
    assert result.outcome.outcome is RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING
    assert batches[0].ordered_theorem_inputs[0].full_name == ("FixtureNamespace.generated_identity")


def test_representation_identity_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_theorem_id = make_id(THEOREM_PREFIX, {"wrong": "theorem"})

    def build(
        backend: LeanInteractBackend,
        batch: RepresentationBatch,
        *,
        created_at: datetime.datetime,
    ) -> RepresentationBatchResult:
        del backend, batch, created_at
        return RepresentationBatchResult((_representation(wrong_theorem_id),), ())

    monkeypatch.setattr(real_outputs, "build_representation_batch", build)
    reference = _reference()
    problem = _problem(reference)
    result = materialize_real_output_candidate(
        problem=problem,
        parsed=parse_direct_autoformalization_output(f"```lean4\n{STATEMENT}\n```"),
        call=_call(problem),
        raw_output_artifact=RAW_ARTIFACT,
        context=_context(),
        references=(reference,),
        imports=IMPORTS,
        backend=cast(LeanInteractBackend, FakeBackend(_lean_result())),
        generation_config_hash=CONFIG_HASH,
        created_at=UTC,
    )
    assert result.outcome.outcome is RealOutputOutcomeCode.INFRASTRUCTURE_ERROR
    assert result.outcome.failure_code is RealOutputFailureCode.REPRESENTATION_FAILED
    assert "theorem_id mismatch" in (result.outcome.failure_detail or "")


def test_context_header_bytes_are_bound_independently_of_problem_hash() -> None:
    reference = _reference()
    problem = _problem(reference)
    other_header = IMPORTS + "\nopen Nat"
    problem = ProblemPoolRecord.model_validate(
        {
            **problem.model_dump(mode="python"),
            "import_header_hash": sha256_hex(other_header.encode("utf-8")),
        }
    )
    backend = FakeBackend(_lean_result())
    with pytest.raises(RealOutputMaterializationError, match="ContextRecord header"):
        materialize_real_output_candidate(
            problem=problem,
            parsed=parse_direct_autoformalization_output(f"```lean4\n{STATEMENT}\n```"),
            call=_call(problem),
            raw_output_artifact=RAW_ARTIFACT,
            context=_context(),
            references=(reference,),
            imports=other_header,
            backend=cast(LeanInteractBackend, backend),
            generation_config_hash=CONFIG_HASH,
            created_at=UTC,
        )
    assert backend.requests == []


def test_rejected_or_wrongly_bound_screening_cannot_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _materialize(monkeypatch, FakeBackend(_lean_result()))
    assert result.theorem is not None
    assert result.representation is not None
    reference = _reference()
    problem = _problem(reference)
    registry_hash = "e" * 64
    rejected = CandidateScreeningRecord.create(
        problem_record_id=problem.problem_record_id,
        call_id=result.outcome.call_id,
        theorem=result.theorem,
        representation=result.representation,
        frozen_registry_hash=registry_hash,
        benchmark_hits=("benchmark:protected-signature",),
        created_at=UTC + datetime.timedelta(seconds=1),
    )
    assert rejected.status is CandidateScreeningStatus.REJECTED
    with pytest.raises(RealOutputMaterializationError, match="not clean"):
        admit_screened_real_output_candidate(
            materialized=result,
            screening=rejected,
            problem=problem,
            references=(reference,),
            expected_frozen_registry_hash=registry_hash,
            created_at=UTC + datetime.timedelta(seconds=2),
        )

    clean = CandidateScreeningRecord.create(
        problem_record_id=problem.problem_record_id,
        call_id=result.outcome.call_id,
        theorem=result.theorem,
        representation=result.representation,
        frozen_registry_hash=registry_hash,
        created_at=UTC + datetime.timedelta(seconds=1),
    )
    with pytest.raises(RealOutputMaterializationError, match="frozen_registry_hash"):
        admit_screened_real_output_candidate(
            materialized=result,
            screening=clean,
            problem=problem,
            references=(reference,),
            expected_frozen_registry_hash="f" * 64,
            created_at=UTC + datetime.timedelta(seconds=2),
        )


def test_screening_requires_all_minimum_admission_views(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _materialize(monkeypatch, FakeBackend(_lean_result()))
    assert result.theorem is not None
    assert result.representation is not None
    incomplete = RepresentationRecord.model_validate(
        {
            **result.representation.model_dump(mode="python"),
            "alpha_identity_fingerprint": None,
        }
    )
    with pytest.raises(
        RealOutputMaterializationError,
        match="alpha_identity_fingerprint",
    ):
        CandidateScreeningRecord.create(
            problem_record_id=result.outcome.problem_record_id,
            call_id=result.outcome.call_id,
            theorem=result.theorem,
            representation=incomplete,
            frozen_registry_hash="e" * 64,
            created_at=UTC + datetime.timedelta(seconds=1),
        )


def test_screening_round_trip_rejects_tampered_view_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _materialize(monkeypatch, FakeBackend(_lean_result()))
    assert result.theorem is not None
    assert result.representation is not None
    screening = CandidateScreeningRecord.create(
        problem_record_id=result.outcome.problem_record_id,
        call_id=result.outcome.call_id,
        theorem=result.theorem,
        representation=result.representation,
        frozen_registry_hash="e" * 64,
        created_at=UTC + datetime.timedelta(seconds=1),
    )

    assert CandidateScreeningRecord.model_validate_json(screening.model_dump_json()) == screening

    tampered = screening.model_dump(mode="python")
    tampered["headless_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="screening_id"):
        CandidateScreeningRecord.model_validate(tampered)


@pytest.mark.parametrize(
    ("call_statement", "raw_artifact", "imports", "message"),
    [
        ("theorem other : True", RAW_ARTIFACT, IMPORTS, "parsed_output"),
        (STATEMENT, "data/raw/other.json", IMPORTS, "raw artifact"),
        (STATEMENT, RAW_ARTIFACT, "import Other", "import_header_hash"),
    ],
)
def test_input_lineage_mismatch_fails_before_lean(
    monkeypatch: pytest.MonkeyPatch,
    call_statement: str,
    raw_artifact: str,
    imports: str,
    message: str,
) -> None:
    backend = FakeBackend(_lean_result())
    with pytest.raises(RealOutputMaterializationError, match=message):
        _materialize(
            monkeypatch,
            backend,
            call_statement=call_statement,
            raw_artifact=raw_artifact,
            imports=imports,
        )
    assert backend.requests == []
