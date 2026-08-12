from __future__ import annotations

import datetime
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.datasets import experimental_first_hop_projection as first_hop
from leanfaith.datasets import experimental_mixed_supervision as mixed
from leanfaith.datasets.denylist import DenylistIndex, FrozenBenchmark, FrozenRegistry
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditInput,
    LF022VerifiedCodexAuditJudgment,
)
from leanfaith.generation.lf022_lean_check import (
    LF022LeanCheckAttempt,
    LF022LeanCheckRecord,
)
from leanfaith.generation.weak_supervision import (
    JudgePresentation,
    JudgeResponse,
    PublicLeanJudgePair,
)
from leanfaith.lean.protocol import LeanStatus
from leanfaith.schemas.enums import RelationLabel, ViewStatus
from leanfaith.schemas.manifest import CodeState
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord


def _benchmark_registry(
    tmp_path: Path,
    *,
    protected_headless: str | None = None,
) -> Any:
    representation_hashes = (
        () if protected_headless is None else (mixed.signature_near_dup_hash(protected_headless),)
    )
    frozen = FrozenRegistry(
        frozen_at=datetime.datetime(2026, 8, 12, tzinfo=datetime.UTC),
        representation_signatures_appended=True,
        benchmarks=(
            FrozenBenchmark(
                registry_key="fixture",
                resolved=True,
                representation_hashes=representation_hashes,
            ),
        ),
    )
    active_path = tmp_path / "active_registry.json"
    active_path.write_bytes(mixed.canonical_json_bytes(frozen.model_dump(mode="json")) + b"\n")
    return cast(
        Any,
        SimpleNamespace(
            active_registry_path=active_path,
            index=DenylistIndex(frozen),
        ),
    )


def _model_field_values(model: Any, *, omit: set[str]) -> dict[str, object]:
    return {name: getattr(model, name) for name in type(model).model_fields if name not in omit}


def _config() -> mixed.ExperimentalMixedSupervisionConfig:
    return mixed.ExperimentalMixedSupervisionConfig(
        profile_id="fixture_mixed_v2",
        selection_seed="fixture-seed",
        first_hop_partition="omitted_not_bound",
        lf022_codex_partition="included",
        composition_partition="omitted_pending_receipt",
    )


def _source_records(
    *,
    theorem_digit: str = "1",
    ancestry_digit: str = "2",
) -> tuple[TheoremRecord, RepresentationRecord]:
    statement = "theorem source (n : Nat) : n = n := by sorry"
    theorem = TheoremRecord.model_construct(
        theorem_id="thm:" + theorem_digit * 64,
        ancestry_id="anc:" + ancestry_digit * 64,
        root_ancestry_ids=("anc:" + ancestry_digit * 64,),
        parent_theorem_ids=(),
        source="mathlib",
        source_revision="fixture-revision",
        context_id="ctx:" + "3" * 64,
        declaration_kind="theorem",
        declaration_name="source",
        proof_stripped_declaration=statement,
        statement_content_hash=sha256_hex(statement.encode()),
    )
    representation = RepresentationRecord.model_construct(
        representation_id="repr:" + theorem_digit * 64,
        theorem_id=theorem.theorem_id,
        normalization_version="repr_v3",
        context_id=theorem.context_id,
        headless="(n : Nat) : n = n",
        signature_pp="∀ (n : Nat), n = n",
        view_status={"signature_pp": ViewStatus.OK},
        alpha_identity_fingerprint="4" * 64,
    )
    return theorem, representation


def _lf022_fixture(
    *,
    index: int = 1,
    answer: str = "not_same_claim",
    needs_review: bool = False,
    candidate_rhs: int | None = 0,
    candidate_statement_override: str | None = None,
) -> tuple[
    LF022VerifiedCodexAuditJudgment,
    LF022CodexAuditInput,
    LF022LeanCheckRecord,
    TheoremRecord,
    RepresentationRecord,
]:
    theorem, representation = _source_records(
        theorem_digit=f"{index:x}"[-1],
        ancestry_digit=f"{index + 1:x}"[-1],
    )
    candidate_statement = candidate_statement_override or (
        "theorem candidate (n : Nat) : n = n"
        if candidate_rhs is None
        else f"theorem candidate (n : Nat) : n = {candidate_rhs}"
    )
    variant_id = "var:" + f"{index + 20:064x}"
    pair = PublicLeanJudgePair(
        pair_id="pair:" + f"{index + 30:064x}",
        canonical_lean_a="theorem source : ∀ (n : Nat), n = n",
        canonical_lean_b=candidate_statement,
        source_record_ids=tuple(sorted((theorem.theorem_id, variant_id))),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
    )
    audit_item_id = "lf022_codex_audit_item:" + f"{index + 40:064x}"
    check_id = "lf022_lean_check:" + f"{index + 50:064x}"
    presentation = JudgePresentation.model_construct(
        task_id="judge_task:" + f"{index + 60:064x}",
        opaque_task_token="lf022_judge_item_v1:" + f"{index + 70:064x}",
        pair_id=pair.pair_id,
        judge_slot="judge_A",
        orientation="AB",
        lean_a=pair.canonical_lean_a,
        lean_b=pair.canonical_lean_b,
        randomization_key_sha256="5" * 64,
        source_admission_sha256="6" * 64,
        external_transmission_allowed=True,
    )
    item = LF022CodexAuditInput.model_construct(
        audit_item_id=audit_item_id,
        lean_check_id=check_id,
        variant_id=variant_id,
        pair=pair,
        presentation=presentation,
    )
    attempt = LF022LeanCheckAttempt(
        attempt_index=0,
        request_hash="7" * 64,
        lean_status=LeanStatus.VALID,
        elapsed_ms=1,
        declarations=({"type": {"pp": "Nat → Prop"}},),
    )
    check = LF022LeanCheckRecord.model_construct(
        check_id=check_id,
        variant_id=variant_id,
        candidate_code_hash=sha256_hex(candidate_statement.encode()),
        context_id=theorem.context_id,
        source_id="mathlib",
        source_revision=theorem.source_revision,
        outcome="elaborates",
        declaration_verified=True,
        attempts=(attempt,),
    )
    relation = {
        "same_claim": RelationLabel.EQUIVALENT,
        "not_same_claim": RelationLabel.UNRELATED,
        "ambiguous": RelationLabel.AMBIGUOUS,
        "uncertain": None,
    }[answer]
    response = JudgeResponse(
        same_claim_answer=answer,  # type: ignore[arg-type]
        relation=relation,
        A_implies_B="unknown",
        B_implies_A="unknown",
        confidence=0.83,
        rationale="A bounded audit fixture.",
        needs_expert_review=needs_review,
    )
    judgment = LF022VerifiedCodexAuditJudgment(
        audit_item_id=audit_item_id,
        lean_check_id=check_id,
        pair_id=pair.pair_id,
        variant_id=variant_id,
        source_record_ids=pair.source_record_ids,
        source_theorem_id=theorem.theorem_id,
        source_representation_id=representation.representation_id,
        source_revision=theorem.source_revision,
        proposer_family_id="moonshot_kimi_k2",
        response=response,
        final_message_sha256="8" * 64,
        parsed_response_sha256="9" * 64,
    )
    return judgment, item, check, theorem, representation


def _first_hop_record() -> first_hop.ExperimentalFirstHopProjectionRecord:
    source_text = "(n : Nat) : n = n"
    candidate_text = "(n : Nat) : n = n ↔ n = n"
    source = first_hop.ExperimentalFirstHopStatementView(
        theorem_id="thm:first-hop-source",
        representation_id="repr:first-hop-source",
        context_id="ctx:" + "a" * 64,
        statement_content_hash="b" * 64,
        representation_content_hash="c" * 64,
        alpha_identity_fingerprint="d" * 64,
        normalized_headless_text_v1=source_text,
        normalized_headless_sha256=sha256_hex(source_text.encode()),
    )
    candidate = first_hop.ExperimentalFirstHopStatementView(
        theorem_id="thm:first-hop-candidate",
        representation_id="repr:first-hop-candidate",
        context_id=source.context_id,
        statement_content_hash="e" * 64,
        representation_content_hash="f" * 64,
        alpha_identity_fingerprint="1" * 64,
        normalized_headless_text_v1=candidate_text,
        normalized_headless_sha256=sha256_hex(candidate_text.encode()),
    )
    payload: dict[str, object] = {
        "projection_record_id": "experimental_first_hop_pair:" + "0" * 64,
        "unique_pair_id": "unique:first-hop",
        "exact_pair_key": "2" * 64,
        "observation_ids": ("observation:first-hop",),
        "selected_observation_id": "observation:first-hop",
        "provenance_count": 1,
        "root_binding_id": "root:first-hop",
        "result_id": "result:first-hop",
        "result_line_number": 1,
        "pair_id": "pair:first-hop",
        "family_ids": ("p14_independent_binder_permutation",),
        "rule_id": "p14_independent_binder_permutation",
        "intended_relations": ("equivalent",),
        "source_category": "mathlib",
        "source_root_ancestry_ids": ("anc:first-hop",),
        "evidence_tier": "E2",
        "pseudo_target": "same_claim",
        "certificate_kind": "fixture_certificate",
        "certificate_sha256": "3" * 64,
        "selection_status": "selectable",
        "source": source,
        "candidate": candidate,
        "private_source_content": False,
        "redistribution_allowed": True,
        "external_transmission_allowed": True,
        "release_eligible": True,
        "experimental_mixed_input_eligible": True,
    }
    provisional = first_hop.ExperimentalFirstHopProjectionRecord.model_construct(
        _fields_set=None,
        **payload,
    )
    canonical = provisional.model_dump(mode="json")
    canonical.pop("projection_record_id")
    payload["projection_record_id"] = "experimental_first_hop_pair:" + hash_canonical(canonical)
    return first_hop.ExperimentalFirstHopProjectionRecord.model_validate(payload)


def _adapt_lf022(
    benchmark_registry: Any,
    *,
    index: int = 1,
    answer: str = "not_same_claim",
    needs_review: bool = False,
    candidate_rhs: int | None = 0,
) -> mixed.ExperimentalMixedAdapterResult:
    judgment, item, check, theorem, representation = _lf022_fixture(
        index=index,
        answer=answer,
        needs_review=needs_review,
        candidate_rhs=candidate_rhs,
    )
    return mixed.adapt_verified_lf022_codex_judgment(
        judgment,
        item=item,
        check=check,
        source_theorem=theorem,
        source_representation=representation,
        benchmark_registry=benchmark_registry,
        judge_model="gpt-5.6-sol",
        judge_reasoning_effort="xhigh",
        response_artifact_set_sha256="a" * 64,
    )


def test_lf022_adapter_is_headless_only_and_preserves_audit_type_as_metadata(
    tmp_path: Path,
) -> None:
    result = _adapt_lf022(_benchmark_registry(tmp_path))

    assert not result.exclusions
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.pseudo_target == "not_same_claim"
    assert candidate.source.normalization_method == "normalized_headless_text_v1"
    assert candidate.candidate.headless == "(n : Nat) : n = 0"
    assert candidate.candidate.lean_check_type_pp == "Nat → Prop"
    assert "signature_explicit" not in candidate.candidate.model_dump(mode="json")
    assert candidate.signal.signal_kind == "codex_single_judge_ab"
    assert candidate.signal.human_label is False
    assert candidate.signal.semantic_label is False
    assert candidate.signal.silver_record is False
    assert any(group.startswith("statement-content:") for group in candidate.split_group_ids)


@pytest.mark.parametrize(
    ("answer", "needs_review", "reason"),
    [
        ("ambiguous", True, "codex_expert_review"),
        ("uncertain", True, "codex_expert_review"),
        ("same_claim", True, "codex_expert_review"),
    ],
)
def test_lf022_adapter_quarantines_unresolved_or_review_items(
    tmp_path: Path,
    answer: str,
    needs_review: bool,
    reason: str,
) -> None:
    result = _adapt_lf022(
        _benchmark_registry(tmp_path),
        answer=answer,
        needs_review=needs_review,
        candidate_rhs=None,
    )

    assert not result.candidates
    assert [item.reason for item in result.exclusions] == [reason]


def test_lf022_adapter_requires_exact_source_ancestry_and_representation(
    tmp_path: Path,
) -> None:
    judgment, item, check, theorem, representation = _lf022_fixture()
    wrong = representation.model_copy(update={"signature_pp": "False"})

    with pytest.raises(
        mixed.ExperimentalMixedSupervisionError,
        match="reconstructed named signature",
    ):
        mixed.adapt_verified_lf022_codex_judgment(
            judgment,
            item=item,
            check=check,
            source_theorem=theorem,
            source_representation=wrong,
            benchmark_registry=_benchmark_registry(tmp_path),
            judge_model="gpt-5.6-sol",
            judge_reasoning_effort="xhigh",
            response_artifact_set_sha256="a" * 64,
        )

    different_identity = representation.model_copy(update={"representation_id": "repr:" + "9" * 64})
    with pytest.raises(
        mixed.ExperimentalMixedSupervisionError,
        match="source theorem/view/check binding differs",
    ):
        mixed.adapt_verified_lf022_codex_judgment(
            judgment,
            item=item,
            check=check,
            source_theorem=theorem,
            source_representation=different_identity,
            benchmark_registry=_benchmark_registry(tmp_path),
            judge_model="gpt-5.6-sol",
            judge_reasoning_effort="xhigh",
            response_artifact_set_sha256="a" * 64,
        )

    wrong_revision = theorem.model_copy(update={"source_revision": "wrong-revision"})
    with pytest.raises(
        mixed.ExperimentalMixedSupervisionError,
        match="source theorem/view/check binding differs",
    ):
        mixed.adapt_verified_lf022_codex_judgment(
            judgment,
            item=item,
            check=check,
            source_theorem=wrong_revision,
            source_representation=representation,
            benchmark_registry=_benchmark_registry(tmp_path),
            judge_model="gpt-5.6-sol",
            judge_reasoning_effort="xhigh",
            response_artifact_set_sha256="a" * 64,
        )


def test_lf022_adapter_rechecks_candidate_against_current_registry(tmp_path: Path) -> None:
    protected = "(n : Nat) : n = 0"
    result = _adapt_lf022(_benchmark_registry(tmp_path, protected_headless=protected))

    assert not result.candidates
    assert [item.reason for item in result.exclusions] == ["benchmark_overlap"]


@pytest.mark.parametrize(
    "relation",
    [
        RelationLabel.A_STRONGER,
        RelationLabel.B_STRONGER,
        RelationLabel.INCOMPARABLE,
        RelationLabel.UNRELATED,
    ],
)
def test_lf022_adapter_retains_content_different_mutually_provable_claims(
    tmp_path: Path,
    relation: RelationLabel,
) -> None:
    judgment, item, check, theorem, representation = _lf022_fixture(
        answer="not_same_claim",
        candidate_statement_override="theorem candidate (n : Nat) : n ≤ n",
    )
    judgment = replace(
        judgment,
        response=judgment.response.model_copy(
            update={
                "relation": relation,
                "a_implies_b": "yes",
                "b_implies_a": "yes",
            }
        ),
    )
    result = mixed.adapt_verified_lf022_codex_judgment(
        judgment,
        item=item,
        check=check,
        source_theorem=theorem,
        source_representation=representation,
        benchmark_registry=_benchmark_registry(tmp_path),
        judge_model="gpt-5.6-sol",
        judge_reasoning_effort="xhigh",
        response_artifact_set_sha256="a" * 64,
    )

    assert not result.exclusions
    assert len(result.candidates) == 1
    assert result.candidates[0].pseudo_target == "not_same_claim"
    assert result.candidates[0].signal.judge_relation == relation.value


def test_lf022_adapter_does_not_use_f2_opinion_to_veto_same_claim(tmp_path: Path) -> None:
    judgment, item, check, theorem, representation = _lf022_fixture(
        answer="same_claim",
        candidate_rhs=None,
    )
    judgment = replace(
        judgment,
        response=judgment.response.model_copy(update={"a_implies_b": "no", "b_implies_a": "no"}),
    )

    result = mixed.adapt_verified_lf022_codex_judgment(
        judgment,
        item=item,
        check=check,
        source_theorem=theorem,
        source_representation=representation,
        benchmark_registry=_benchmark_registry(tmp_path),
        judge_model="gpt-5.6-sol",
        judge_reasoning_effort="xhigh",
        response_artifact_set_sha256="a" * 64,
    )

    assert not result.exclusions
    assert len(result.candidates) == 1
    assert result.candidates[0].pseudo_target == "same_claim"


@pytest.mark.parametrize(
    ("answer", "invalid_relation"),
    [
        ("same_claim", RelationLabel.A_STRONGER),
        ("not_same_claim", RelationLabel.EQUIVALENT),
    ],
)
def test_judge_response_schema_rejects_incoherent_f1_fields(
    answer: str,
    invalid_relation: RelationLabel,
) -> None:
    with pytest.raises(ValueError):
        JudgeResponse(
            same_claim_answer=answer,  # type: ignore[arg-type]
            relation=invalid_relation,
            A_implies_B="yes",
            B_implies_A="yes",
            confidence=0.9,
            rationale="The F1 verdict and relation intentionally disagree.",
            needs_expert_review=False,
        )


@pytest.mark.parametrize(
    ("answer", "invalid_relation"),
    [
        ("same_claim", RelationLabel.A_STRONGER),
        ("not_same_claim", RelationLabel.EQUIVALENT),
    ],
)
def test_lf022_adapter_quarantines_schema_bypassing_incoherent_f1_fields(
    tmp_path: Path,
    answer: str,
    invalid_relation: RelationLabel,
) -> None:
    judgment, item, check, theorem, representation = _lf022_fixture(answer=answer)
    judgment = replace(
        judgment,
        response=judgment.response.model_copy(update={"relation": invalid_relation}),
    )

    result = mixed.adapt_verified_lf022_codex_judgment(
        judgment,
        item=item,
        check=check,
        source_theorem=theorem,
        source_representation=representation,
        benchmark_registry=_benchmark_registry(tmp_path),
        judge_model="gpt-5.6-sol",
        judge_reasoning_effort="xhigh",
        response_artifact_set_sha256="a" * 64,
    )

    assert not result.candidates
    assert [item.reason for item in result.exclusions] == ["codex_incoherent"]


def test_first_hop_adapter_preserves_selectable_provisional_signal(tmp_path: Path) -> None:
    record = _first_hop_record()
    result = mixed.adapt_selectable_first_hop_projection(
        record,
        benchmark_registry=_benchmark_registry(tmp_path),
    )

    assert not result.exclusions
    candidate = result.candidates[0]
    assert candidate.pseudo_target == "same_claim"
    assert candidate.signal.signal_kind == "deterministic_first_hop_e2"
    assert candidate.signal.certificate_sha256s == (record.certificate_sha256,)
    assert record.source is not None
    assert candidate.source.headless == record.source.normalized_headless_text_v1
    assert candidate.private_source_content is False


def test_first_hop_adapter_revalidates_unsafe_projection(tmp_path: Path) -> None:
    unsafe = _first_hop_record().model_copy(update={"selection_status": "excluded"})

    with pytest.raises(
        mixed.ExperimentalMixedSupervisionError,
        match="invalid first-hop projection record",
    ):
        mixed.adapt_selectable_first_hop_projection(
            unsafe,
            benchmark_registry=_benchmark_registry(tmp_path),
        )


def test_new_candidates_reject_unchecked_denylist_flag(tmp_path: Path) -> None:
    candidate = _adapt_lf022(_benchmark_registry(tmp_path)).candidates[0]
    payload = candidate.model_dump(mode="json")
    payload["denylist_checked"] = False

    with pytest.raises(ValueError, match="denylist_checked"):
        mixed.ExperimentalMixedCandidate.model_validate(payload)


def test_pair_dedupe_merges_agreement_and_quarantines_conflict(tmp_path: Path) -> None:
    registry = _benchmark_registry(tmp_path)
    first = _adapt_lf022(registry, index=1, answer="not_same_claim").candidates[0]
    judgment, item, check, theorem, representation = _lf022_fixture(
        index=2,
        answer="not_same_claim",
        candidate_rhs=0,
    )
    # Reuse the same source and ancestry but retain a distinct verified signal.
    item = item.model_copy(
        update={
            "pair": item.pair.model_copy(
                update={
                    "canonical_lean_a": "theorem source : ∀ (n : Nat), n = n",
                }
            )
        }
    )
    theorem, representation = _source_records()
    judgment = replace(
        judgment,
        source_record_ids=tuple(sorted((theorem.theorem_id, judgment.variant_id))),
        source_theorem_id=theorem.theorem_id,
        source_representation_id=representation.representation_id,
        source_revision=theorem.source_revision,
    )
    item = item.model_copy(
        update={
            "pair": item.pair.model_copy(update={"source_record_ids": judgment.source_record_ids})
        }
    )
    second_result = mixed.adapt_verified_lf022_codex_judgment(
        judgment,
        item=item,
        check=check.model_copy(update={"context_id": theorem.context_id}),
        source_theorem=theorem,
        source_representation=representation,
        benchmark_registry=registry,
        judge_model="gpt-5.6-sol",
        judge_reasoning_effort="xhigh",
        response_artifact_set_sha256="b" * 64,
    )
    second = second_result.candidates[0]
    assert first.exact_pair_key == second.exact_pair_key

    records, exclusions = mixed._build_records((first, second), config=_config())
    assert len(records) == 1
    assert len(records[0].signals) == 2
    assert not exclusions

    conflicting_signal = second.signal.model_copy(update={"pseudo_target": "same_claim"})
    conflicting = second.model_copy(
        update={"pseudo_target": "same_claim", "signal": conflicting_signal}
    )
    # Re-content-address both changed models through the module constructors.
    conflicting_signal = mixed._make_signal(
        **_model_field_values(conflicting_signal, omit={"signal_id"})
    )
    conflicting = mixed._make_candidate(
        **{
            **_model_field_values(conflicting, omit={"candidate_id", "signal"}),
            "signal": conflicting_signal,
        }
    )
    third = _adapt_lf022(
        registry,
        index=7,
        answer="not_same_claim",
        candidate_rhs=7,
    ).candidates[0]
    records, exclusions = mixed._build_records((first, conflicting, third), config=_config())
    assert len(records) == 1
    assert [item.reason for item in exclusions] == ["conflicting_proxy_targets"]


def test_union_components_bridge_source_and_candidate_content(tmp_path: Path) -> None:
    registry = _benchmark_registry(tmp_path)
    first = _adapt_lf022(registry, index=1, candidate_rhs=0).candidates[0]
    second = _adapt_lf022(registry, index=7, candidate_rhs=7).candidates[0]
    bridge_groups = tuple(
        sorted(
            {
                *second.split_group_ids,
                next(
                    group
                    for group in first.split_group_ids
                    if group.startswith("statement-content:")
                ),
            }
        )
    )
    bridge = mixed._make_candidate(
        **{
            **_model_field_values(second, omit={"candidate_id", "split_group_ids"}),
            "split_group_ids": bridge_groups,
        }
    )

    records, _ = mixed._build_records((first, bridge), config=_config())
    assert len({record.split_component_id for record in records}) == 1
    assert len({record.split for record in records}) == 1


def test_partition_policy_requires_explicit_composition_omission() -> None:
    with pytest.raises(ValueError, match="omission must explicitly"):
        mixed.ExperimentalMixedSupervisionConfig(
            profile_id="bad",
            selection_seed="seed",
            first_hop_partition="omitted_not_bound",
            lf022_codex_partition="included",
            composition_partition="omitted_not_bound",
        )


def test_freeze_replay_verify_and_opt_in_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    input_path = tmp_path / "lf022_verified.json"
    input_path.write_text("verified\n", encoding="utf-8")
    binding = mixed.bind_experimental_mixed_input(
        input_path,
        partition="lf022_codex",
    )
    clean = CodeState(
        git_revision="b" * 40,
        git_dirty=False,
        base_git_commit="b" * 40,
        code_tree_hash="c" * 64,
    )
    monkeypatch.setattr(mixed, "collect_code_state", lambda _root: clean)
    registry = _benchmark_registry(tmp_path)
    candidate = _adapt_lf022(registry).candidates[0]
    output = tmp_path / "mixed"

    first = mixed.freeze_experimental_mixed_supervision(
        repo_root=repo,
        output_dir=output,
        config=_config(),
        candidates=(candidate,),
        adapter_exclusions=(),
        inputs={
            "active_benchmark_registry": mixed.bind_experimental_mixed_input(
                registry.active_registry_path,
                partition="policy",
            ),
            "lf022_verified": binding,
        },
    )
    second = mixed.freeze_experimental_mixed_supervision(
        repo_root=repo,
        output_dir=output,
        config=_config(),
        candidates=(candidate,),
        adapter_exclusions=(),
        inputs={
            "active_benchmark_registry": mixed.bind_experimental_mixed_input(
                registry.active_registry_path,
                partition="policy",
            ),
            "lf022_verified": binding,
        },
    )

    assert first.replayed is False
    assert second.replayed is True
    manifest = mixed.verify_experimental_mixed_supervision(output)
    assert manifest.composition_partition == "omitted_pending_receipt"
    assert manifest.lf022_codex_partition == "included"
    with pytest.raises(mixed.ExperimentalMixedSupervisionError, match="requires"):
        mixed.load_experimental_mixed_supervision(
            output,
            allow_experimental_mixed_supervision=False,
            purpose="smoke_training",
        )
    examples = mixed.load_experimental_mixed_supervision(
        output,
        allow_experimental_mixed_supervision=True,
        purpose="smoke_training",
    )
    assert len(examples) == 1
    dumped = examples[0].model_dump(mode="json")
    assert set(dumped) == {
        "schema_version",
        "record_id",
        "model_input_profile",
        "source_headless",
        "candidate_headless",
        "pseudo_target",
        "split",
    }

    records_path = output / "records.jsonl"
    records_path.write_bytes(records_path.read_bytes() + b"\n")
    with pytest.raises(mixed.ExperimentalMixedSupervisionError, match="hash differs"):
        mixed.verify_experimental_mixed_supervision(output)


def test_partition_policy_rejects_unbound_included_partition(tmp_path: Path) -> None:
    candidate = _adapt_lf022(_benchmark_registry(tmp_path)).candidates[0]
    input_path = tmp_path / "policy.json"
    input_path.write_text("policy\n", encoding="utf-8")
    binding = mixed.bind_experimental_mixed_input(input_path, partition="policy")
    records, _ = mixed._build_records((candidate,), config=_config())

    with pytest.raises(mixed.ExperimentalMixedSupervisionError, match="bound inputs"):
        mixed._validate_partition_policy(
            records,
            inputs={"policy": binding},
            config=_config(),
        )


def test_partition_policy_binds_the_registry_that_screened_records(tmp_path: Path) -> None:
    registry = _benchmark_registry(tmp_path)
    candidate = _adapt_lf022(registry).candidates[0]
    records, _ = mixed._build_records((candidate,), config=_config())
    lf022_path = tmp_path / "lf022.json"
    lf022_path.write_text("verified\n", encoding="utf-8")
    wrong_policy = tmp_path / "wrong_registry.json"
    wrong_policy.write_text("wrong\n", encoding="utf-8")

    with pytest.raises(
        mixed.ExperimentalMixedSupervisionError,
        match="exact active benchmark registry",
    ):
        mixed._validate_partition_policy(
            records,
            inputs={
                "lf022": mixed.bind_experimental_mixed_input(
                    lf022_path,
                    partition="lf022_codex",
                ),
                "policy": mixed.bind_experimental_mixed_input(
                    wrong_policy,
                    partition="policy",
                ),
            },
            config=_config(),
        )


def test_composition_replay_binding_requires_receipt_bound_artifacts() -> None:
    required = {
        "full_launch_spec_artifact",
        "full_receipt_artifact",
        "full_status_artifact",
        "export_manifest_artifact",
        "export_partition_artifact",
        "chain_manifest_artifact",
        "chain_records_artifact",
        "unique_pair_manifest_artifact",
        "unique_pair_records_artifact",
        "source_theorem_artifacts",
        "source_representation_artifacts",
        "second_hop_result_artifacts",
    }

    assert required.issubset(mixed.DeterministicCompositionReplayBinding.model_fields)
    with pytest.raises(ValueError):
        mixed.DeterministicCompositionReplayBinding.model_validate({})


def test_composition_batch_requires_complete_nonempty_receipt_export(tmp_path: Path) -> None:
    registry = _benchmark_registry(tmp_path)

    with pytest.raises(mixed.ExperimentalMixedSupervisionError, match="cannot be empty"):
        mixed.adapt_deterministic_composition_export_batch(
            (),
            export_partition_artifacts={},
            benchmark_registry=registry,
        )

    with pytest.raises(mixed.ExperimentalMixedSupervisionError, match="all three"):
        mixed.adapt_deterministic_composition_export_batch(
            cast(Any, ((object(), object()),)),
            export_partition_artifacts=cast(Any, {"inventory": object()}),
            benchmark_registry=registry,
        )
