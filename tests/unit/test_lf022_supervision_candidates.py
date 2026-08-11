"""Fail-closed tests for LF-022 provisional supervision candidate inventories."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditInput,
    LF022VerifiedCodexAuditJudgment,
)
from leanfaith.generation.lf022_supervision_candidates import (
    CandidateArtifactBinding,
    LF022SupervisionCandidateError,
    LF022SupervisionCandidateRecord,
    LF022SupervisionCandidateSpec,
    _judge_visible_payload_hash,
    _lexical_no_symlink_components,
    _load_spec,
    _record_values,
    _resolve_bound_path,
    _validate_variant_proposer_binding,
)
from leanfaith.generation.weak_supervision import JudgeResponse, PublicLeanJudgePair
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    Polarity,
    QualityTier,
    RelationLabel,
    ValidationStatus,
)
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.variant import VariantRecord


def _spec(**overrides: object) -> LF022SupervisionCandidateSpec:
    values: dict[str, object] = {
        "collection_id": "fixture",
        "proposer_family_id": "moonshot_kimi_k2",
        "proposer_model": "moonshotai/Kimi-K2.7-Code",
        "judge_a_family_id": "qwen3",
        "judge_b_family_id": "deepseek_v4",
        "primary_eval_judge_family_id": "openai_codex",
        "checks": {"path": "checks.jsonl", "sha256": "a" * 64},
        "codex_audit_manifest": {"path": "manifest.json", "sha256": "b" * 64},
    }
    values.update(overrides)
    return LF022SupervisionCandidateSpec.model_validate(values)


def _pair(*, optional_natural_language: str | None = None) -> PublicLeanJudgePair:
    return PublicLeanJudgePair(
        pair_id="pair:" + "a" * 64,
        canonical_lean_a="theorem source (n : Nat) : n = n",
        canonical_lean_b="theorem candidate (m : Nat) : m = m",
        optional_natural_language=optional_natural_language,
        source_record_ids=("thm:" + "b" * 64, "var:" + "c" * 64),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
    )


def _item(audit_digit: str) -> LF022CodexAuditInput:
    return LF022CodexAuditInput.model_construct(
        audit_item_id="lf022_codex_audit_item:" + audit_digit * 64,
        lean_check_id="lf022_lean_check:" + "d" * 64,
        variant_id="var:" + "c" * 64,
        pair=_pair(),
    )


def _judgment(audit_digit: str) -> LF022VerifiedCodexAuditJudgment:
    return LF022VerifiedCodexAuditJudgment(
        audit_item_id="lf022_codex_audit_item:" + audit_digit * 64,
        lean_check_id="lf022_lean_check:" + "d" * 64,
        pair_id="pair:" + "a" * 64,
        variant_id="var:" + "c" * 64,
        source_record_ids=("thm:" + "b" * 64, "var:" + "c" * 64),
        proposer_family_id="moonshot_kimi_k2",
        response=JudgeResponse(
            same_claim_answer="same_claim",
            relation=RelationLabel.EQUIVALENT,
            A_implies_B="yes",
            B_implies_A="yes",
            confidence=0.9,
            rationale="The binder rename preserves the claim.",
            needs_expert_review=False,
        ),
        final_message_sha256="e" * 64,
        parsed_response_sha256="f" * 64,
    )


def _variant(**overrides: object) -> VariantRecord:
    statement = "theorem candidate (m : Nat) : m = m"
    values: dict[str, object] = {
        "variant_id": "var:" + "c" * 64,
        "source_theorem_ids": ("thm:" + "b" * 64,),
        "source_representation_ids": ("repr:" + "1" * 64,),
        "context_id": "ctx:" + "2" * 64,
        "generator_kind": GeneratorKind.LLM_PROPOSER,
        "generator_id": "moonshotai/Kimi-K2.7-Code",
        "generation_config_hash": "3" * 64,
        "extracted_statement": statement,
        "candidate_code_hash": sha256_hex(statement.encode("utf-8")),
        "intended_relation": IntendedRelation.EQUIVALENT,
        "candidate_pool": "G_open",
        "validation_status": ValidationStatus.UNVALIDATED,
        "quality_tier": QualityTier.PROVISIONAL,
        "polarity_metadata": Polarity.POSITIVE,
        "metadata": {"proposer_family": "moonshotai/kimi-k2"},
    }
    values.update(overrides)
    return VariantRecord.model_validate(values)


def test_same_pair_id_uses_one_canonical_audit_item_for_dispatch() -> None:
    canonical = "lf022_codex_audit_item:" + "1" * 64
    values_a = _record_values(
        spec=_spec(),
        item=_item("1"),
        judgment=_judgment("1"),
        canonical_pair_id=_pair().pair_id,
        canonical_audit_item_id=canonical,
        codex_model="gpt-5.6-sol",
        codex_reasoning_effort="xhigh",
    )
    values_b = _record_values(
        spec=_spec(),
        item=_item("2"),
        judgment=_judgment("2"),
        canonical_pair_id=_pair().pair_id,
        canonical_audit_item_id=canonical,
        codex_model="gpt-5.6-sol",
        codex_reasoning_effort="xhigh",
    )

    record_a = LF022SupervisionCandidateRecord.model_validate(
        {
            **values_a,
            "candidate_inventory_record_id": make_id("lf022_supervision_candidate", values_a),
        }
    )
    record_b = LF022SupervisionCandidateRecord.model_validate(
        {
            **values_b,
            "candidate_inventory_record_id": make_id("lf022_supervision_candidate", values_b),
        }
    )
    assert record_a.dispatch_status == "ready_for_two_family_judging"
    assert record_a.required_judgment_cells == (
        "judge_A:AB",
        "judge_A:BA",
        "judge_B:AB",
        "judge_B:BA",
    )
    assert record_b.dispatch_status == "exact_duplicate_not_dispatched"
    assert record_b.required_judgment_cells == ()


def test_visible_payload_hash_keeps_identical_lean_with_different_nl_distinct() -> None:
    without_nl = _pair(optional_natural_language=None)
    with_nl = _pair(optional_natural_language="Every natural number equals itself.")
    assert without_nl.canonical_lean_a == with_nl.canonical_lean_a
    assert without_nl.canonical_lean_b == with_nl.canonical_lean_b
    assert _judge_visible_payload_hash(without_nl) != _judge_visible_payload_hash(with_nl)


def test_bound_artifact_rejects_a_final_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(LF022SupervisionCandidateError, match="symlinked component"):
        _resolve_bound_path(
            CandidateArtifactBinding(path=str(link), sha256=hash_file(target)),
            repo_root=tmp_path,
        )


def test_spec_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    spec = _spec()
    spec_path = real / "spec.json"
    spec_path.write_bytes(canonical_json_bytes(spec.model_dump(mode="json")) + b"\n")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(LF022SupervisionCandidateError, match="symlinked component"):
        _load_spec(
            repo_root=tmp_path,
            spec_path=alias / "spec.json",
            expected_spec_sha256=hash_file(spec_path),
        )


def test_output_directory_rejects_a_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(LF022SupervisionCandidateError, match="symlinked component"):
        _lexical_no_symlink_components(
            alias / "inventory",
            base=tmp_path,
            label="candidate inventory output directory",
            allow_missing_leaf=True,
        )


def test_variant_model_and_family_are_bound_to_frozen_spec() -> None:
    variant = _variant()
    _validate_variant_proposer_binding(
        variant=variant,
        judgment_proposer_family_id="moonshot_kimi_k2",
        judgment_variant_id=variant.variant_id,
        spec=_spec(),
    )

    with pytest.raises(LF022SupervisionCandidateError, match="model/family"):
        _validate_variant_proposer_binding(
            variant=variant,
            judgment_proposer_family_id="moonshot_kimi_k2",
            judgment_variant_id=variant.variant_id,
            spec=_spec(proposer_model="moonshotai/Kimi-K2.6"),
        )
    with pytest.raises(LF022SupervisionCandidateError, match="audit proposer family"):
        _validate_variant_proposer_binding(
            variant=variant,
            judgment_proposer_family_id="qwen3",
            judgment_variant_id=variant.variant_id,
            spec=_spec(),
        )


def test_candidate_spec_requires_four_distinct_families() -> None:
    with pytest.raises(ValueError, match="four distinct families"):
        _spec(judge_b_family_id="qwen3")
