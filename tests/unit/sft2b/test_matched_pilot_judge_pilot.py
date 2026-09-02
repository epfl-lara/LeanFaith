from __future__ import annotations

from pathlib import Path

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.sft2b import matched_pilot_judge_pilot as pilot
from leanfaith.sft2b.judges import (
    JudgeCallResult,
    JudgesConfig,
    LoadedJudges,
)
from leanfaith.sft2b.schemas import (
    CandidateRecord,
    JudgeDecision,
    JudgeId,
    JudgeVote,
    SourceRecord,
    stable_id,
)


def _source(index: int) -> SourceRecord:
    return SourceRecord.model_construct(
        source_id=f"sft2b_source:{index:064x}",
        nl_statement=f"Natural-language statement {index}",
    )


def _candidate(index: int, *, source_index: int, signature: str | None = None) -> CandidateRecord:
    return CandidateRecord.model_construct(
        candidate_id=f"sft2b_candidate:{index:064x}",
        source_id=f"sft2b_source:{source_index:064x}",
        signature_sha256=signature or f"{index + 1000:064x}",
    )


def test_selection_is_hash_ranked_representation_valid_and_signature_unique() -> None:
    sources = [_source(index) for index in range(4)]
    duplicate_signature = "f" * 64
    candidates = [
        _candidate(1, source_index=0, signature=duplicate_signature),
        _candidate(2, source_index=1, signature=duplicate_signature),
        _candidate(3, source_index=2),
        _candidate(4, source_index=3),
    ]
    source_rows = [
        {
            "source_id": source.source_id,
            "reference_elaborated": True,
            "reference": {"status": "valid", "goal_v1": f"reference {index}"},
        }
        for index, source in enumerate(sources)
    ]
    candidate_rows = [
        {
            "candidate_id": candidate.candidate_id,
            "source_class": "theorem_problem",
            "elaboration_status": "valid",
            "status": "valid",
            "goal_v1": f"candidate {index}",
        }
        for index, candidate in enumerate(candidates)
    ]

    first = pilot.select_candidates(
        sources=sources,
        candidates=candidates,
        source_rows=source_rows,
        candidate_rows=candidate_rows,
        count=3,
    )
    second = pilot.select_candidates(
        sources=sources,
        candidates=candidates,
        source_rows=source_rows,
        candidate_rows=candidate_rows,
        count=3,
    )

    assert first == second
    assert len({item.signature_sha256 for item in first}) == 3
    assert [item.selection_key for item in first] == sorted(item.selection_key for item in first)


def _loaded() -> LoadedJudges:
    providers = []
    models = {
        JudgeId.CODEX: "gpt-5.6-terra",
        JudgeId.LEMEX: "moonshotai/Kimi-K2.7-Code",
        JudgeId.CLAUDE: "opus",
    }
    for judge in JudgeId:
        providers.append(
            {
                "judge": judge.value,
                "provider": f"{judge.value}_provider",
                "binary_path": f"/bin/{judge.value}",
                "binary_sha256": "1" * 64,
                "cli_version": "test",
                "model_id": models[judge],
                "effort": "high",
                "prompt_path": f"prompts/{judge.value}.md",
                "prompt_sha256": "2" * 64,
                "timeout_seconds": 30,
            }
        )
    config = JudgesConfig.model_validate(
        {
            "schema_version": "sft2b_judges_v1",
            "rubric_version": "intended_claim_consistency_v1",
            "output_schema_path": "schema.json",
            "output_schema_sha256": "3" * 64,
            "blinded_to_expected_label": True,
            "blinded_to_other_votes": True,
            "providers": providers,
        }
    )
    return LoadedJudges(
        config=config,
        repo_root=Path("/repo"),
        output_schema_path=Path("/repo/schema.json"),
        output_schema_sha256="3" * 64,
    )


def _selected(index: int = 1) -> pilot.SelectedCandidate:
    nl = "A natural-language statement"
    reference = "x : Nat\n⊢ x = x"
    candidate = "y : Nat\n⊢ y = y"
    return pilot.SelectedCandidate(
        selection_rank=0,
        selection_key="4" * 64,
        source_id=f"sft2b_source:{index:064x}",
        candidate_id=f"sft2b_candidate:{index:064x}",
        source_class="theorem_problem",
        signature_sha256="5" * 64,
        nl_statement=nl,
        nl_statement_sha256=sha256_hex(nl.encode()),
        reference_goal_v1=reference,
        reference_goal_v1_sha256=sha256_hex(reference.encode()),
        candidate_goal_v1=candidate,
        candidate_goal_v1_sha256=sha256_hex(candidate.encode()),
    )


def _vote(loaded: LoadedJudges, item: pilot.SelectedCandidate, judge: JudgeId) -> JudgeVote:
    provider = loaded.provider(judge)
    input_sha = hash_canonical(
        {
            "schema_version": "sft2b_judge_input_v1",
            "nl_statement": item.nl_statement,
            "reference": item.reference_goal_v1,
            "candidate": item.candidate_goal_v1,
        }
    )
    vote_id = stable_id(
        "sft2b_vote",
        {
            "candidate_id": item.candidate_id,
            "judge": judge,
            "model_id": provider.model_id,
            "prompt_sha256": provider.prompt_sha256,
            "judge_input_sha256": input_sha,
        },
    )
    return JudgeVote(
        vote_id=vote_id,
        candidate_id=item.candidate_id,
        judge=judge,
        provider=provider.provider,
        model_id=provider.model_id,
        cli_version=provider.cli_version,
        prompt_sha256=provider.prompt_sha256,
        judge_input_sha256=input_sha,
        response_sha256="6" * 64,
        decision=JudgeDecision.EQUIVALENT,
        probability_equivalent=0.9,
        rationale="The propositions have the same quantified identity claim.",
        relation_class="same_claim",
        saw_expected_label=False,
        saw_other_votes=False,
    )


def test_vote_cache_restart_makes_no_second_provider_call(
    tmp_path: Path, monkeypatch: object
) -> None:
    loaded = _loaded()
    item = _selected()
    config = pilot.MatchedPilotJudgeConfig.model_construct(output_parent=tmp_path)
    calls = 0

    def fake_run(*args: object, **kwargs: object) -> JudgeCallResult:
        nonlocal calls
        calls += 1
        vote = _vote(loaded, item, JudgeId.CODEX)
        return JudgeCallResult(
            vote=vote,
            elapsed_seconds=1.25,
            stdout=b"stdout",
            stderr=b"",
            provider_payload=b"{" + b'"decision":"equivalent"' + b"}",
        )

    monkeypatch.setattr(pilot, "run_judge", fake_run)  # type: ignore[attr-defined]
    first, first_key, first_called = pilot._call_or_load_vote(
        config=config,
        loaded=loaded,
        item=item,
        judge=JudgeId.CODEX,
    )
    second, second_key, second_called = pilot._call_or_load_vote(
        config=config,
        loaded=loaded,
        item=item,
        judge=JudgeId.CODEX,
    )

    assert first == second
    assert first_key == second_key
    assert first_called is True
    assert second_called is False
    assert calls == 1


def test_pairwise_agreement_counts_all_three_blinded_pairs() -> None:
    loaded = _loaded()
    first = _selected(1)
    second = _selected(2)
    all_equal = tuple(_vote(loaded, first, judge) for judge in JudgeId)
    split = [_vote(loaded, second, judge) for judge in JudgeId]
    split[1] = split[1].model_copy(update={"decision": JudgeDecision.NON_EQUIVALENT})
    split[2] = split[2].model_copy(update={"decision": JudgeDecision.UNKNOWN})

    assert pilot.pairwise_agreement([all_equal, tuple(split)]) == 0.5  # type: ignore[list-item]
