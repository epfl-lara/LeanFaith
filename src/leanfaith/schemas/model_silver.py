"""Training-only model-adjudicated silver records for LF-022.

These records are deliberately separate from ``ResolvedLabel``.  They allow a
frozen weak-supervision training arm to consume exact Sol/Fable AB/BA
consensus while mechanically forbidding selection, calibration, evaluation,
or human-gold use.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import QualityTier, RelationLabel
from leanfaith.schemas.ids import EVIDENCE_PREFIX, LLM_CALL_PREFIX, id_pattern, make_id


class ModelAdjudicatedSilverCellV1(StrictModel):
    """One canonicalized cell in the required two-family AB/BA matrix."""

    schema_version: Literal[1] = 1
    judge_slot: Literal["judge_A", "judge_B"]
    orientation: Literal["AB", "BA"]
    judge_family: Literal["openai_codex_sol", "anthropic_fable"]
    provider: Literal["openai_codex_exec", "anthropic_claude_code"]
    model: Literal["openai/gpt-5.6-sol", "anthropic/claude-fable-5"]
    model_revision: str = Field(min_length=1)
    effort: Literal["xhigh", "max"]
    judge_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_id: str = Field(pattern=id_pattern(EVIDENCE_PREFIX))
    call_id: str = Field(pattern=id_pattern(LLM_CALL_PREFIX))
    answer: Literal["same_claim", "not_same_claim"]
    canonical_relation: Literal[
        "equivalent",
        "A_stronger",
        "B_stronger",
        "incomparable",
        "unrelated",
    ]
    # These are model-judgment metadata used only to enforce swapped-order and
    # cross-family agreement.  They are never trusted F2 proof evidence.
    a_implies_b: Literal["yes", "no", "unknown"]
    b_implies_a: Literal["yes", "no", "unknown"]
    confidence: float = Field(ge=0.0, le=1.0)
    needs_expert_review: Literal[False] = False
    private_source_content: Literal[False] = False
    denylist_hits: tuple[()] = ()

    @model_validator(mode="after")
    def _slot_family_model_effort(self) -> Self:
        expected = {
            "judge_A": (
                "openai_codex_sol",
                "openai_codex_exec",
                "openai/gpt-5.6-sol",
                "xhigh",
            ),
            "judge_B": (
                "anthropic_fable",
                "anthropic_claude_code",
                "anthropic/claude-fable-5",
                "max",
            ),
        }[self.judge_slot]
        observed = (self.judge_family, self.provider, self.model, self.effort)
        if observed != expected:
            raise ValueError("judge cell differs from the registered Sol/Fable slot")
        if self.answer == "same_claim" and self.canonical_relation != "equivalent":
            raise ValueError("same-claim cell requires relation=equivalent")
        if self.answer == "not_same_claim" and self.canonical_relation == "equivalent":
            raise ValueError("not-same cell cannot use relation=equivalent")
        return self


class ModelAdjudicatedSilverPromotionRecordV1(StrictModel):
    """One exact four-cell consensus admitted for weak training only."""

    schema_version: Literal[1] = 1
    promotion_id: str = Field(pattern=id_pattern("model_silver"))
    promotion_profile: Literal["sol_fable_abba_model_adjudicated_training_silver_v1"] = (
        "sol_fable_abba_model_adjudicated_training_silver_v1"
    )
    label_basis: Literal["model_adjudicated_training_silver"] = "model_adjudicated_training_silver"
    resolution_method: Literal["sol_fable_abba_model_consensus_v1"] = (
        "sol_fable_abba_model_consensus_v1"
    )
    pair_id: str = Field(pattern=id_pattern("pair"))
    weak_candidate_id: str = Field(pattern=id_pattern("weak_consensus"))
    source_batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    source_execution_id: str = Field(pattern=id_pattern("lf022_weak_execution"))
    source_finalization_id: str = Field(pattern=id_pattern("lf022_weak_finalization"))
    source_authoring_id: str = Field(pattern=id_pattern("lf022_sol_fable_authoring"))
    source_authoring_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_sol_xhigh_registry_id: str = Field(pattern=id_pattern("lf022_sol_history_registry"))
    historical_sol_xhigh_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_sol_fable_ledger_id: str = Field(pattern=id_pattern("lf022_sol_fable_exclusion"))
    completed_sol_fable_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_record_id: str = Field(pattern=id_pattern("lf022_supervision_candidate"))
    source_theorem_lineage_id: str = Field(pattern=id_pattern("thm"))
    source_line_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_visible_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    freshness_status: Literal["verified_authoring_history_and_completed_ledger_v1"] = (
        "verified_authoring_history_and_completed_ledger_v1"
    )
    dispatch_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finalization_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    weak_candidates_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judgment_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calls_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotion_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposer_family: str = Field(min_length=1)
    heldout_evaluation_family: str = Field(min_length=1)
    cells: tuple[ModelAdjudicatedSilverCellV1, ...]
    evidence_ids: tuple[str, ...]
    llm_call_ids: tuple[str, ...]
    same_claim: bool
    relation: RelationLabel
    minimum_self_reported_confidence: float = Field(ge=0.0, le=1.0)
    quality_tier: Literal[QualityTier.SILVER_CONSENSUS] = QualityTier.SILVER_CONSENSUS
    accepted_strong_evidence_ids: tuple[()] = ()
    strong_evidence_conflict_status: Literal["none_in_bound_evidence"] = "none_in_bound_evidence"
    train_eligibility: Literal[True] = True
    eval_eligibility: Literal[False] = False
    selection_eligibility: Literal[False] = False
    calibration_eligibility: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    human_adjudication_status: Literal["not_performed"] = "not_performed"
    resolved_label_created: Literal[False] = False
    gate_6_human_audit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _four_cell_consensus(self) -> Self:
        keys = tuple((cell.judge_slot, cell.orientation) for cell in self.cells)
        expected_keys = (
            ("judge_A", "AB"),
            ("judge_A", "BA"),
            ("judge_B", "AB"),
            ("judge_B", "BA"),
        )
        if keys != expected_keys:
            raise ValueError("promotion requires exactly the canonical four Sol/Fable cells")
        if self.proposer_family in {cell.judge_family for cell in self.cells}:
            raise ValueError("proposer family cannot adjudicate its own output")
        if self.heldout_evaluation_family in {cell.judge_family for cell in self.cells}:
            raise ValueError("held-out evaluation family cannot enter training supervision")
        semantics = {
            (
                cell.answer,
                cell.canonical_relation,
                cell.a_implies_b,
                cell.b_implies_a,
            )
            for cell in self.cells
        }
        if len(semantics) != 1:
            raise ValueError("all four canonicalized cells must agree exactly")
        answer, relation, _, _ = next(iter(semantics))
        if self.same_claim != (answer == "same_claim") or self.relation.value != relation:
            raise ValueError("promotion target differs from four-cell consensus")
        expected_evidence = tuple(sorted(cell.evidence_id for cell in self.cells))
        expected_calls = tuple(sorted(cell.call_id for cell in self.cells))
        if self.evidence_ids != expected_evidence or self.llm_call_ids != expected_calls:
            raise ValueError("promotion evidence/call IDs differ from the four cells")
        confidence = min(cell.confidence for cell in self.cells)
        if self.minimum_self_reported_confidence != confidence:
            raise ValueError("minimum confidence must be recomputed over all four cells")
        expected_id = make_id(
            "model_silver", self.model_dump(mode="json", exclude={"promotion_id"})
        )
        if self.promotion_id != expected_id:
            raise ValueError("promotion_id differs from promotion content")
        return self


class ModelAdjudicatedSilverRejectionV1(StrictModel):
    """Fail-closed audit row for a pair not admitted by the promotion policy."""

    schema_version: Literal[1] = 1
    rejection_id: str = Field(pattern=id_pattern("model_silver_rejection"))
    pair_id: str = Field(pattern=id_pattern("pair"))
    weak_candidate_id: str = Field(pattern=id_pattern("weak_consensus"))
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reasons(self) -> Self:
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("rejection reasons must be sorted and unique")
        if any(re.fullmatch(r"[a-z0-9_]+", reason) is None for reason in self.reasons):
            raise ValueError("rejection reasons must be canonical snake_case")
        expected_id = make_id(
            "model_silver_rejection",
            self.model_dump(mode="json", exclude={"rejection_id"}),
        )
        if self.rejection_id != expected_id:
            raise ValueError("rejection_id differs from rejection content")
        return self


class ModelAdjudicatedSilverPromotionManifestV1(StrictModel):
    """Complete immutable partition of a finalized batch into promote/reject rows."""

    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=id_pattern("model_silver_manifest"))
    promotion_profile: Literal["sol_fable_abba_model_adjudicated_training_silver_v1"] = (
        "sol_fable_abba_model_adjudicated_training_silver_v1"
    )
    source_batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    source_execution_id: str = Field(pattern=id_pattern("lf022_weak_execution"))
    source_finalization_id: str = Field(pattern=id_pattern("lf022_weak_finalization"))
    source_authoring_id: str = Field(pattern=id_pattern("lf022_sol_fable_authoring"))
    source_authoring_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_sol_xhigh_registry_id: str = Field(pattern=id_pattern("lf022_sol_history_registry"))
    historical_sol_xhigh_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_sol_fable_ledger_id: str = Field(pattern=id_pattern("lf022_sol_fable_exclusion"))
    completed_sol_fable_ledger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    freshness_verified: Literal[True] = True
    dispatch_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finalization_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotion_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    promotions_artifact: Literal["promotions.jsonl"] = "promotions.jsonl"
    promotions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rejections_artifact: Literal["rejections.jsonl"] = "rejections.jsonl"
    rejections_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_pair_count: int = Field(ge=0, strict=True)
    promotion_count: int = Field(ge=0, strict=True)
    rejection_count: int = Field(ge=0, strict=True)
    rejection_reason_counts: dict[str, int]
    complete_pair_partition: Literal[True] = True
    model_adjudicated_silver_records_created: Literal[True] = True
    promotion_record_policy_train_eligible: Literal[True] = True
    contains_train_eligible_records: bool = Field(strict=True)
    eval_eligibility: Literal[False] = False
    selection_eligibility: Literal[False] = False
    calibration_eligibility: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    resolved_label_created: Literal[False] = False
    gate_6_human_audit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _complete_and_content_addressed(self) -> Self:
        if self.input_pair_count != self.promotion_count + self.rejection_count:
            raise ValueError("promotion manifest does not partition every input pair")
        if self.contains_train_eligible_records != (self.promotion_count > 0):
            raise ValueError("train-eligible-record presence must match promotion count")
        if list(self.rejection_reason_counts) != sorted(self.rejection_reason_counts):
            raise ValueError("rejection reason counts must use sorted keys")
        if any(
            re.fullmatch(r"[a-z0-9_]+", reason) is None or count < 1
            for reason, count in self.rejection_reason_counts.items()
        ):
            raise ValueError("rejection reason counts must be canonical and positive")
        expected_id = make_id(
            "model_silver_manifest",
            self.model_dump(mode="json", exclude={"manifest_id"}),
        )
        if self.manifest_id != expected_id:
            raise ValueError("manifest_id differs from promotion-manifest content")
        return self


__all__ = [
    "ModelAdjudicatedSilverCellV1",
    "ModelAdjudicatedSilverPromotionManifestV1",
    "ModelAdjudicatedSilverPromotionRecordV1",
    "ModelAdjudicatedSilverRejectionV1",
]
