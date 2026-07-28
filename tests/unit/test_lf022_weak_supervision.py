from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from leanfaith.generation.providers import (
    DeterministicFixtureProvider,
    ProviderError,
    ProviderIdentity,
    ProviderRequest,
    bridge_provider_result_to_generic_llm_lineage,
    persist_provider_request,
)
from leanfaith.generation.weak_supervision import (
    FamilySeparationMatrix,
    JudgeOutputErrorCode,
    JudgeOutputParseError,
    PublicLeanJudgePair,
    build_weak_consensus_candidate,
    judge_provider_input_ids,
    make_swapped_presentations,
    materialize_judgment_evidence,
    materialize_verified_judgment_evidence,
    parse_blinded_judge_output,
    render_blinded_judge_prompt,
    validate_family_separation,
)
from leanfaith.schemas import JudgmentValue, LLMRole, ParseStatus

PAIR_ID = "pair:" + "a" * 64
NOW = datetime.datetime(2026, 7, 28, tzinfo=datetime.UTC)


def _source(**overrides: object) -> PublicLeanJudgePair:
    values: dict[str, object] = {
        "pair_id": PAIR_ID,
        "canonical_lean_a": "∀ n : Nat, n = n",
        "canonical_lean_b": "∀ m : Nat, m = m",
        "optional_natural_language": "Every natural number equals itself.",
        "source_record_ids": ("public:fixture",),
        "source_is_public": True,
        "private_source_content": False,
        "external_transmission_allowed": True,
        "denylist_checked": True,
        "denylist_hits": (),
    }
    values.update(overrides)
    return PublicLeanJudgePair.model_validate(values)


MATRIX = FamilySeparationMatrix(
    proposer_family="moonshot_kimi_k2",
    judge_a_family="qwen3",
    judge_b_family="deepseek",
    primary_eval_judge_family="openai_codex",
)


def _response(
    *,
    answer: str = "not_same_claim",
    relation: str | None = "A_stronger",
    a_to_b: str = "yes",
    b_to_a: str = "no",
    review: bool = False,
) -> str:
    return json.dumps(
        {
            "same_claim_answer": answer,
            "relation": relation,
            "A_implies_B": a_to_b,
            "B_implies_A": b_to_a,
            "error_types": ["E01"],
            "confidence": 0.8,
            "rationale": "One premise is missing.",
            "needs_expert_review": review,
        }
    )


def _evidence(
    *,
    family: str,
    slot: str,
    orientation: str,
    call_digit: str,
    raw: str,
):
    response = parse_blinded_judge_output(raw)
    return materialize_judgment_evidence(
        pair_id=PAIR_ID,
        call_id="call:" + call_digit * 64,
        judge_family=family,
        judge_slot=slot,  # type: ignore[arg-type]
        proposer_family="moonshot_kimi_k2",
        orientation=orientation,  # type: ignore[arg-type]
        response=response,
        method_version="weak_judge_v1",
        config_hash="b" * 64,
        raw_artifact=f"raw/{call_digit}.json",
        created_at=NOW,
    )


def test_swapped_presentations_are_reproducible_blinded_and_inverse() -> None:
    tasks = make_swapped_presentations(
        source=_source(),
        judge_slot="judge_A",
        randomization_key=b"x" * 32,
    )
    repeated = make_swapped_presentations(
        source=_source(),
        judge_slot="judge_A",
        randomization_key=b"x" * 32,
    )
    assert tasks == repeated
    assert {task.orientation for task in tasks} == {"AB", "BA"}
    ab = next(task for task in tasks if task.orientation == "AB")
    ba = next(task for task in tasks if task.orientation == "BA")
    assert ab.lean_a == ba.lean_b
    assert ab.lean_b == ba.lean_a

    rendered = render_blinded_judge_prompt(ab)
    assert PAIR_ID not in rendered.text
    assert "judge_A" not in rendered.text
    assert ab.randomization_key_sha256 not in rendered.text
    assert '"opaque_task_token"' in rendered.text


def test_judge_task_rejects_forbidden_external_transmission() -> None:
    with pytest.raises(ValueError, match="external_transmission_allowed"):
        _source(
            canonical_lean_a="True",
            canonical_lean_b="False",
            optional_natural_language=None,
            external_transmission_allowed=False,
        )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", JudgeOutputErrorCode.EMPTY_OUTPUT),
        ("```json\n{}\n```", JudgeOutputErrorCode.INVALID_JSON),
        ('{"same_claim_answer":"same_claim"}', JudgeOutputErrorCode.INVALID_SCHEMA),
        (
            '{"same_claim_answer":"same_claim","relation":"equivalent",'
            '"A_implies_B":"yes","B_implies_A":"yes","error_types":[],'
            '"confidence":0.9,"confidence":0.8,"rationale":"x",'
            '"needs_expert_review":false}',
            JudgeOutputErrorCode.INVALID_JSON,
        ),
        (
            _response(answer="same_claim", relation="A_stronger"),
            JudgeOutputErrorCode.INCOHERENT,
        ),
        (
            _response(answer="uncertain", relation=None, review=False),
            JudgeOutputErrorCode.INCOHERENT,
        ),
    ],
)
def test_judge_parser_fails_closed(raw: str, code: JudgeOutputErrorCode) -> None:
    with pytest.raises(JudgeOutputParseError) as caught:
        parse_blinded_judge_output(raw)
    assert caught.value.code is code


def test_ba_judgment_is_remapped_to_canonical_direction() -> None:
    evidence = _evidence(
        family="qwen3",
        slot="judge_A",
        orientation="BA",
        call_digit="1",
        raw=_response(
            relation="B_stronger",
            a_to_b="no",
            b_to_a="yes",
        ),
    )
    assert isinstance(evidence.value, JudgmentValue)
    assert evidence.value.relation == "A_stronger"
    assert evidence.value.a_implies_b == "yes"
    assert evidence.value.b_implies_a == "no"
    assert evidence.metadata["semantic_label_created"] is False


def test_two_family_swapped_consensus_stays_non_trainable_candidate() -> None:
    judgments = (
        _evidence(
            family="qwen3",
            slot="judge_A",
            orientation="AB",
            call_digit="1",
            raw=_response(),
        ),
        _evidence(
            family="qwen3",
            slot="judge_A",
            orientation="BA",
            call_digit="2",
            raw=_response(relation="B_stronger", a_to_b="no", b_to_a="yes"),
        ),
        _evidence(
            family="deepseek",
            slot="judge_B",
            orientation="AB",
            call_digit="3",
            raw=_response(),
        ),
        _evidence(
            family="deepseek",
            slot="judge_B",
            orientation="BA",
            call_digit="4",
            raw=_response(relation="B_stronger", a_to_b="no", b_to_a="yes"),
        ),
    )
    candidate = build_weak_consensus_candidate(
        pair_id=PAIR_ID,
        proposer_family="moonshot_kimi_k2",
        family_matrix=MATRIX,
        judgments=judgments,
        created_at=NOW,
    )
    assert candidate.status == "candidate_consensus"
    assert candidate.consensus_value is not None
    assert candidate.consensus_value.relation == "A_stronger"
    assert candidate.semantic_label_created is False
    assert candidate.silver_promoted is False
    assert candidate.train_eligible is False
    assert candidate.eval_eligible is False
    assert candidate.requires_adjudication is True


def test_swap_inconsistency_is_retained_without_consensus() -> None:
    judgments = (
        _evidence(
            family="qwen3",
            slot="judge_A",
            orientation="AB",
            call_digit="1",
            raw=_response(),
        ),
        _evidence(
            family="qwen3",
            slot="judge_A",
            orientation="BA",
            call_digit="2",
            raw=_response(relation="A_stronger", a_to_b="yes", b_to_a="no"),
        ),
        _evidence(
            family="deepseek",
            slot="judge_B",
            orientation="AB",
            call_digit="3",
            raw=_response(),
        ),
        _evidence(
            family="deepseek",
            slot="judge_B",
            orientation="BA",
            call_digit="4",
            raw=_response(relation="B_stronger", a_to_b="no", b_to_a="yes"),
        ),
    )
    candidate = build_weak_consensus_candidate(
        pair_id=PAIR_ID,
        proposer_family="moonshot_kimi_k2",
        family_matrix=MATRIX,
        judgments=judgments,
        created_at=NOW,
    )
    assert candidate.status == "swap_inconsistent"
    assert candidate.consensus_value is None
    assert "swap_inconsistent" in candidate.promotion_blockers


def test_aggregation_rejects_same_slot_and_mismatched_proposer_lineage() -> None:
    judgments = (
        _evidence(
            family="qwen3",
            slot="judge_A",
            orientation="AB",
            call_digit="1",
            raw=_response(),
        ),
        _evidence(
            family="qwen3",
            slot="judge_A",
            orientation="BA",
            call_digit="2",
            raw=_response(relation="B_stronger", a_to_b="no", b_to_a="yes"),
        ),
        _evidence(
            family="deepseek",
            slot="judge_A",
            orientation="AB",
            call_digit="3",
            raw=_response(),
        ),
        _evidence(
            family="deepseek",
            slot="judge_A",
            orientation="BA",
            call_digit="4",
            raw=_response(relation="B_stronger", a_to_b="no", b_to_a="yes"),
        ),
    )
    with pytest.raises(ValueError, match="family/slot"):
        build_weak_consensus_candidate(
            pair_id=PAIR_ID,
            proposer_family="moonshot_kimi_k2",
            family_matrix=MATRIX,
            judgments=judgments,
            created_at=NOW,
        )

    correctly_slotted = tuple(
        record.model_copy(
            update={
                "metadata": {
                    **record.metadata,
                    "judge_slot": (
                        "judge_A" if record.metadata["judge_family"] == "qwen3" else "judge_B"
                    ),
                    "proposer_family": "different_proposer",
                }
            }
        )
        for record in judgments
    )
    with pytest.raises(ValueError, match="proposer lineage"):
        build_weak_consensus_candidate(
            pair_id=PAIR_ID,
            proposer_family="moonshot_kimi_k2",
            family_matrix=MATRIX,
            judgments=correctly_slotted,
            created_at=NOW,
        )


def test_unanimous_ambiguity_is_not_misreported_as_disagreement() -> None:
    ambiguous = _response(
        answer="ambiguous",
        relation="ambiguous",
        a_to_b="unknown",
        b_to_a="unknown",
        review=True,
    )
    judgments = (
        _evidence(
            family="qwen3",
            slot="judge_A",
            orientation="AB",
            call_digit="1",
            raw=ambiguous,
        ),
        _evidence(
            family="qwen3",
            slot="judge_A",
            orientation="BA",
            call_digit="2",
            raw=ambiguous,
        ),
        _evidence(
            family="deepseek",
            slot="judge_B",
            orientation="AB",
            call_digit="3",
            raw=ambiguous,
        ),
        _evidence(
            family="deepseek",
            slot="judge_B",
            orientation="BA",
            call_digit="4",
            raw=ambiguous,
        ),
    )
    candidate = build_weak_consensus_candidate(
        pair_id=PAIR_ID,
        proposer_family="moonshot_kimi_k2",
        family_matrix=MATRIX,
        judgments=judgments,
        created_at=NOW,
    )
    assert candidate.status == "ambiguous_consensus"
    assert candidate.consensus_value is None


def test_family_separation_requires_four_distinct_families() -> None:
    validate_family_separation(
        FamilySeparationMatrix(
            proposer_family="moonshot_kimi_k2",
            judge_a_family="qwen3",
            judge_b_family="deepseek",
            primary_eval_judge_family="openai_codex",
        )
    )
    with pytest.raises(ValueError, match="four distinct"):
        validate_family_separation(
            FamilySeparationMatrix(
                proposer_family="moonshot_kimi_k2",
                judge_a_family="qwen3",
                judge_b_family="qwen3",
                primary_eval_judge_family="openai_codex",
            )
        )


def test_verified_judgment_materializer_requires_raw_bound_public_lineage(
    tmp_path: Path,
) -> None:
    source = _source()
    task = next(
        task
        for task in make_swapped_presentations(
            source=source,
            judge_slot="judge_A",
            randomization_key=b"verified-judge-materializer-key!!",
        )
        if task.orientation == "AB"
    )
    rendered = render_blinded_judge_prompt(task)
    identity = ProviderIdentity(
        provider="fixture",
        model="fixture/judge",
        revision="fixture-revision",
        transport="fixture",
    )
    provider_request = ProviderRequest.create(
        identity=identity,
        prompt_template_hash=rendered.template_sha256,
        rendered_prompt=rendered.text,
        decoding={"temperature": 0.0},
        input_ids=judge_provider_input_ids(task),
        private_source_content=False,
    )
    request_path = tmp_path / "requests" / "request.json"
    persist_provider_request(provider_request, request_path)
    raw_output = _response()
    result = DeterministicFixtureProvider(
        identity=identity,
        raw_response_root=tmp_path / "raw",
        responses={provider_request.request_hash: raw_output},
    ).generate(provider_request)
    parsed = parse_blinded_judge_output(raw_output)
    config_hash = "8" * 64
    lineage = bridge_provider_result_to_generic_llm_lineage(
        request=provider_request,
        result=result,
        request_artifact_path=request_path,
        artifact_root=tmp_path,
        role=LLMRole.JUDGE,
        provider_slot="judge_A",
        model_family="qwen3",
        prompt_template_id="lean_pair_blinded",
        prompt_template_version="v1",
        execution_mode="replay",
        parse_status=ParseStatus.PARSED,
        parsed_output=parsed.model_dump(mode="json", by_alias=True),
        private_source_content=False,
        denylist_checked=True,
        denylist_hits=(),
        started_at=NOW,
        completed_at=NOW,
        supervision_eligible=True,
        metadata={
            "weak_supervision_config_hash": config_hash,
            "proposer_family": MATRIX.proposer_family,
        },
    )
    evidence = materialize_verified_judgment_evidence(
        call=lineage.call,
        task=task,
        source=source,
        family_matrix=MATRIX,
        proposer_family=MATRIX.proposer_family,
        method_version="judge_v1",
        config_hash=config_hash,
        artifact_root=tmp_path,
        created_at=NOW,
    )
    assert evidence.raw_artifact == lineage.call.raw_output_artifact
    assert evidence.metadata["llm_call_id"] == lineage.call.call_id

    forged = lineage.call.model_copy(
        update={"parsed_output": {**lineage.call.parsed_output, "confidence": 0.1}}
    )
    with pytest.raises(ProviderError, match="parsed judge payload"):
        materialize_verified_judgment_evidence(
            call=forged,
            task=task,
            source=source,
            family_matrix=MATRIX,
            proposer_family=MATRIX.proposer_family,
            method_version="judge_v1",
            config_hash=config_hash,
            artifact_root=tmp_path,
            created_at=NOW,
        )
