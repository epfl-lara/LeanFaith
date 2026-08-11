"""Focused tests for the bounded audit-only LF-022 merged inventory."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation.lf022_codex_audit import LF022VerifiedCodexAuditJudgment
from leanfaith.generation.lf022_inventory_snapshot import (
    _pair_hash,
    source_candidate_pair_hash,
)
from leanfaith.generation.lf022_lean_check import LF022LeanCheckRecord
from leanfaith.generation.lf022_merged_inventory import (
    LF022MergedAuditDiagnostic,
    LF022MergedInventoryError,
    LF022MergedObservation,
    _conflicts,
    _counts,
    _load_variant,
    _observation,
    _pair_record,
    _safe_output_directory,
)
from leanfaith.generation.weak_supervision import JudgeResponse
from leanfaith.schemas.enums import (
    GeneratorKind,
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
)
from leanfaith.schemas.variant import VariantRecord


def _variant(
    index: int, *, source_index: int | None = None, model: str = "model/a"
) -> VariantRecord:
    source = index if source_index is None else source_index
    statement = f"theorem proposed_{index} (n : Nat) : n = {index}"
    return VariantRecord(
        variant_id="var:" + f"{index:064x}",
        source_theorem_ids=("thm:" + f"{source:064x}",),
        source_representation_ids=("repr:" + f"{source:064x}",),
        context_id="ctx:" + "1" * 64,
        generator_kind=GeneratorKind.LLM_PROPOSER,
        generator_id=model,
        generation_config_hash="f" * 64,
        seed=42,
        extracted_statement=statement,
        candidate_code_hash=sha256_hex(statement.encode()),
        intended_relation=IntendedRelation.NEAR_MISS,
        candidate_pool="G_open",
        validation_status=ValidationStatus.UNVALIDATED,
        quality_tier=QualityTier.PROVISIONAL,
        polarity_metadata=Polarity.NEGATIVE,
    )


def _check(
    variant: VariantRecord,
    *,
    index: int,
    artifact: str = "variants.jsonl",
    artifact_sha256: str = "a" * 64,
    line_sha256: str = "b" * 64,
    outcome: str = "elaborates_with_placeholder",
) -> LF022LeanCheckRecord:
    return LF022LeanCheckRecord.model_construct(
        schema_version=1,
        check_id="lf022_lean_check:" + f"{index:064x}",
        method_version="lf022_provisional_lean_check_v1",
        variant_id=variant.variant_id,
        source_variant_artifact=artifact,
        source_variant_artifact_sha256=artifact_sha256,
        source_variant_line_number=1,
        source_variant_line_sha256=line_sha256,
        candidate_code_hash=variant.candidate_code_hash,
        context_id=variant.context_id,
        outcome=outcome,
    )


def _judgment(
    variant: VariantRecord,
    *,
    index: int,
    same_claim: str,
    relation: str | None,
    a_implies_b: str,
    b_implies_a: str,
) -> LF022VerifiedCodexAuditJudgment:
    response = JudgeResponse.model_validate(
        {
            "same_claim_answer": same_claim,
            "relation": relation,
            "A_implies_B": a_implies_b,
            "B_implies_A": b_implies_a,
            "error_types": [],
            "confidence": 0.8,
            "rationale": "A bounded audit-only unit-test rationale.",
            "needs_expert_review": same_claim in {"ambiguous", "uncertain"},
        }
    )
    return LF022VerifiedCodexAuditJudgment(
        audit_item_id="lf022_codex_audit_item:" + f"{index:064x}",
        lean_check_id="lf022_lean_check:" + f"{index:064x}",
        pair_id="pair:" + f"{index:064x}",
        variant_id=variant.variant_id,
        source_record_ids=variant.source_theorem_ids,
        proposer_family_id="family/a",
        response=response,
        final_message_sha256="c" * 64,
        parsed_response_sha256="d" * 64,
    )


def test_public_pair_key_is_canonical_and_legacy_alias_is_preserved() -> None:
    variant = _variant(1)
    assert source_candidate_pair_hash(variant) == _pair_hash(variant)
    assert len(source_candidate_pair_hash(variant)) == 64


def test_merged_counts_and_conflicts_are_pair_key_based() -> None:
    first = _variant(1, source_index=7, model="model/a")
    duplicate = first.model_copy(
        update={
            "variant_id": "var:" + "2" * 64,
            "generator_id": "model/b",
        }
    )
    second = _variant(3, source_index=8, model="model/a")
    invalid = _variant(4, source_index=9, model="model/c")
    observations = (
        _observation(
            partition_id="one",
            check=_check(first, index=1),
            variant=first,
            judgment=_judgment(
                first,
                index=1,
                same_claim="same_claim",
                relation="equivalent",
                a_implies_b="yes",
                b_implies_a="yes",
            ),
        ),
        _observation(
            partition_id="two",
            check=_check(duplicate, index=2),
            variant=duplicate,
            judgment=_judgment(
                duplicate,
                index=2,
                same_claim="not_same_claim",
                relation="A_stronger",
                a_implies_b="yes",
                b_implies_a="no",
            ),
        ),
        _observation(
            partition_id="one",
            check=_check(second, index=3),
            variant=second,
            judgment=None,
        ),
        _observation(
            partition_id="three",
            check=_check(invalid, index=4, outcome="invalid"),
            variant=invalid,
            judgment=None,
        ),
    )
    grouped: dict[str, list[LF022MergedObservation]] = {}
    for item in observations:
        grouped.setdefault(item.pair_key, []).append(item)
    pairs = tuple(_pair_record(key, grouped[key]) for key in sorted(grouped))
    counts = _counts(observations)
    conflicts = _conflicts(pairs)

    assert counts.gross_observation_count == 4
    assert counts.unique_variant_id_count == 4
    assert counts.unique_pair_key_count == 3
    assert counts.duplicate_pair_observation_count == 1
    assert counts.cross_partition_pair_key_count == 1
    assert counts.cross_model_pair_key_count == 1
    assert counts.lean_valid_unique_pair_key_count == 2
    assert counts.audited_unique_pair_key_count == 1
    assert counts.lean_valid_unaudited_pair_key_count == 1
    assert conflicts.lean_outcome_conflict_pair_key_count == 0
    assert conflicts.audit_same_claim_conflict_pair_key_count == 1
    assert conflicts.audit_relation_conflict_pair_key_count == 1
    assert conflicts.audit_directional_conflict_pair_key_count == 1
    assert conflicts.audit_any_core_tuple_conflict_pair_key_count == 1
    assert all(not item.training_eligible for item in observations)
    assert all(not item.generation_complete for item in pairs)


def test_source_variant_loader_fails_closed_on_artifact_drift(tmp_path: Path) -> None:
    variant = _variant(1)
    artifact = tmp_path / "variants.jsonl"
    raw = canonical_json_bytes(variant.model_dump(mode="json")) + b"\n"
    artifact.write_bytes(raw)
    check = _check(
        variant,
        index=1,
        artifact="variants.jsonl",
        artifact_sha256=hash_file(artifact),
        line_sha256=sha256_hex(raw),
    )
    loaded = _load_variant(check, source_root=tmp_path, artifact_cache={})
    assert loaded == variant

    artifact.write_bytes(raw + b"{}\n")
    with pytest.raises(LF022MergedInventoryError, match="artifact hash differs"):
        _load_variant(check, source_root=tmp_path, artifact_cache={})


def test_observation_rejects_audit_on_invalid_check() -> None:
    variant = _variant(1)
    diagnostic = LF022MergedAuditDiagnostic(
        audit_item_id="lf022_codex_audit_item:" + "1" * 64,
        pair_id="pair:" + "2" * 64,
        same_claim_answer="same_claim",
        relation="equivalent",
        a_implies_b="yes",
        b_implies_a="yes",
        final_message_sha256="3" * 64,
        parsed_response_sha256="4" * 64,
    )
    values: dict[str, Any] = {
        "schema_version": 1,
        "observation_id": "lf022_merged_observation:" + "0" * 64,
        "partition_id": "p",
        "check_id": "lf022_lean_check:" + "5" * 64,
        "variant_id": variant.variant_id,
        "pair_key": source_candidate_pair_hash(variant),
        "candidate_code_hash": variant.candidate_code_hash,
        "source_theorem_ids": variant.source_theorem_ids,
        "proposer_model": variant.generator_id,
        "source_variant_artifact": "variants.jsonl",
        "source_variant_artifact_sha256": "6" * 64,
        "source_variant_line_number": 1,
        "source_variant_line_sha256": "7" * 64,
        "lean_outcome": "invalid",
        "lean_valid": False,
        "audit": diagnostic,
    }
    with pytest.raises(ValueError, match="non-Lean-valid observation cannot carry an audit"):
        LF022MergedObservation.model_validate(cast(dict[str, object], values))


def test_output_directory_rejects_symlinked_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(LF022MergedInventoryError, match="traverses a symlink"):
        _safe_output_directory(link / "inventory")
