"""Valid-record factories shared by LF-004 schema tests."""

from __future__ import annotations

import datetime
from typing import Any

from leanfaith.schemas import (
    CANONICAL_VIEW_NAMES,
    AnnotationAnswer,
    AnnotationRecord,
    ContextRecord,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceRecord,
    EvidenceTargetKind,
    FaithfulnessLevels,
    GeneratorKind,
    IntendedRelation,
    LLMCallRecord,
    LLMRole,
    NLPLeanRecord,
    NLTrust,
    PairRecord,
    ParseStatus,
    QualityTier,
    RelationLabel,
    RepresentationRecord,
    ResolutionOutcome,
    ResolvedLabel,
    SemanticLabelTargetKind,
    TheoremRecord,
    TypecheckValue,
    ValidationStatus,
    VariantRecord,
    ViewStatus,
    make_id,
)

UTC_NOW = datetime.datetime(2026, 7, 10, 12, 0, 0, tzinfo=datetime.UTC)

CTX_FINGERPRINT = "0" * 64
CTX_ID = f"ctx:{CTX_FINGERPRINT}"
THM_A = make_id("thm", {"fixture": "a"})
THM_B = make_id("thm", {"fixture": "b"})
ANC_A = make_id("anc", {"fixture": "a"})
ANC_B = make_id("anc", {"fixture": "b"})
PAIR_ID = make_id("pair", {"a": THM_A, "b": THM_B})
LABEL_ID = make_id("lbl", {"pair": PAIR_ID})
EV_ID = make_id("ev", {"pair": PAIR_ID, "kind": "typecheck"})
NL_ID = make_id("nllean", {"problem": "p1", "candidate": THM_A})
CALL_ID = make_id("call", {"n": 1})
ANN_ID = make_id("ann", {"n": 1})
VAR_ID = make_id("var", {"n": 1})


def context_record(**overrides: Any) -> ContextRecord:
    payload: dict[str, Any] = {
        "environment_schema_version": 1,
        "context_id": CTX_ID,
        "context_fingerprint": CTX_FINGERPRINT,
        "project_kind": "local",
        "project_uri": "tests/lean_fixtures",
        "project_revision": "fixture",
        "project_registry_key": "fixtures",
        "lean_version": "v4.31.0-rc1",
        "lean_interact_version": "0.11.4",
        "repl_revision": "pinned",
        "imports": ("Mathlib",),
        "header_hash": "1" * 64,
    }
    payload.update(overrides)
    return ContextRecord.model_validate(payload)


def theorem_record(**overrides: Any) -> TheoremRecord:
    payload: dict[str, Any] = {
        "theorem_id": THM_A,
        "ancestry_id": ANC_A,
        "root_ancestry_ids": (ANC_A,),
        "source": "fixtures",
        "source_revision": "r1",
        "context_id": CTX_ID,
        "declaration_kind": "theorem",
        "declaration_name": "t",
        "proof_stripped_declaration": "theorem t : True := sorry",
        "is_proposition": True,
        "elaboration_status": ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        "statement_content_hash": "2" * 64,
    }
    payload.update(overrides)
    return TheoremRecord.model_validate(payload)


def representation_record(**overrides: Any) -> RepresentationRecord:
    payload: dict[str, Any] = {
        "representation_id": make_id("repr", {"theorem": THM_A, "version": "repr_v1"}),
        "theorem_id": THM_A,
        "normalization_version": "repr_v1",
        "context_id": CTX_ID,
        "raw_proof_stripped": "theorem t : True := sorry",
        "headless": ": True",
        "signature_pp": "True",
        "view_status": {
            name: (
                ViewStatus.OK
                if name in ("raw_proof_stripped", "headless", "signature_pp")
                else ViewStatus.NOT_ATTEMPTED
            )
            for name in CANONICAL_VIEW_NAMES
        },
        "content_hash": "3" * 64,
        "created_at": UTC_NOW,
    }
    payload.update(overrides)
    return RepresentationRecord.model_validate(payload)


def variant_record(**overrides: Any) -> VariantRecord:
    payload: dict[str, Any] = {
        "variant_id": VAR_ID,
        "source_theorem_ids": (THM_A,),
        "generator_kind": GeneratorKind.DETERMINISTIC_TRANSFORM,
        "generator_id": "p01_alpha",
        "generation_config_hash": "4" * 64,
        "seed": 7,
        "extracted_statement": "theorem t2 : True := sorry",
        "intended_relation": IntendedRelation.EQUIVALENT,
        "candidate_pool": "main",
    }
    payload.update(overrides)
    return VariantRecord.model_validate(payload)


def pair_record(**overrides: Any) -> PairRecord:
    payload: dict[str, Any] = {
        "pair_id": PAIR_ID,
        "theorem_a_id": THM_A,
        "theorem_b_id": THM_B,
        "pair_source": "deterministic_transform",
        "split_group_ids": tuple(sorted((ANC_A, ANC_B))),
        "intended_relation": IntendedRelation.EQUIVALENT,
    }
    payload.update(overrides)
    return PairRecord.model_validate(payload)


def evidence_record(**overrides: Any) -> EvidenceRecord:
    payload: dict[str, Any] = {
        "evidence_id": EV_ID,
        "target_kind": EvidenceTargetKind.THEOREM,
        "target_id": THM_A,
        "kind": EvidenceKind.TYPECHECK,
        "status": EvidenceExecutionStatus.SUCCESS,
        "value": TypecheckValue(outcome="valid_with_sorry"),
        "method_version": "typecheck_v1",
        "created_at": UTC_NOW,
    }
    payload.update(overrides)
    return EvidenceRecord.model_validate(payload)


def resolved_label(**overrides: Any) -> ResolvedLabel:
    payload: dict[str, Any] = {
        "label_id": LABEL_ID,
        "target_kind": SemanticLabelTargetKind.LEAN_PAIR,
        "target_id": PAIR_ID,
        "same_claim": True,
        "resolution_outcome": ResolutionOutcome.SAME_CLAIM,
        "relation": RelationLabel.EQUIVALENT,
        "faithfulness_levels": FaithfulnessLevels(
            F0_representation_equivalent=True,
            F1_same_claim=True,
            F2_truth_equivalent=None,
        ),
        "error_types": (),
        "quality_tier": QualityTier.GOLD_CONSERVATIVE_TRANSFORM,
        "resolution_method": "p01_alpha_certificate",
        "requires_adjudication": False,
        "train_eligibility": True,
        "eval_eligibility": False,
        "policy_version": "semantic_policy_v1",
    }
    payload.update(overrides)
    return ResolvedLabel.model_validate(payload)


def nl_lean_record(**overrides: Any) -> NLPLeanRecord:
    payload: dict[str, Any] = {
        "nl_lean_id": NL_ID,
        "problem_id": "p1",
        "problem_group": "grp:p1",
        "source": "fixtures",
        "source_revision": "r1",
        "nl_statement": "Addition of naturals is commutative.",
        "nl_trust": NLTrust.TRUSTED,
        "candidate_theorem_id": THM_A,
        "split_group_ids": tuple(sorted((ANC_A, "grp:p1"))),
    }
    payload.update(overrides)
    return NLPLeanRecord.model_validate(payload)


def llm_call_record(**overrides: Any) -> LLMCallRecord:
    payload: dict[str, Any] = {
        "call_id": CALL_ID,
        "provider": "provider_a",
        "model": "model-x",
        "model_family": "family_a",
        "role": LLMRole.JUDGE,
        "request_date": UTC_NOW,
        "prompt_template_hash": "5" * 64,
        "prompt_render_hash": "6" * 64,
        "parse_status": ParseStatus.PARSED,
        "parsed_output": {"same_claim_answer": "same_claim"},
        "supervision_eligible": True,
        "private_source_content": False,
        "denylist_checked": True,
    }
    payload.update(overrides)
    return LLMCallRecord.model_validate(payload)


def annotation_record(**overrides: Any) -> AnnotationRecord:
    payload: dict[str, Any] = {
        "annotation_id": ANN_ID,
        "target_kind": SemanticLabelTargetKind.LEAN_PAIR,
        "target_id": PAIR_ID,
        "annotator_id": "annotator_1",
        "round_id": "pilot_1",
        "same_claim": AnnotationAnswer.SAME_CLAIM,
        "relation": RelationLabel.EQUIVALENT,
        "confidence": 4,
        "created_at": UTC_NOW,
    }
    payload.update(overrides)
    return AnnotationRecord.model_validate(payload)
