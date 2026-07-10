"""Shared enums (PLAN.md §7.1, §11.1).

Only these enums may be serialized into persisted records (§14.1). External
spellings (proposer intentions, judge answers, UI labels, imported benchmarks)
map through explicit tables and never create new persisted spellings.
"""

from __future__ import annotations

from enum import StrEnum

# --- §11.1 canonical semantic enums (verbatim values) ---


class ResolutionOutcome(StrEnum):
    SAME_CLAIM = "same_claim"
    NOT_SAME_CLAIM = "not_same_claim"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class RelationLabel(StrEnum):
    EQUIVALENT = "equivalent"
    A_STRONGER = "A_stronger"
    B_STRONGER = "B_stronger"
    INCOMPARABLE_NEAR_MISS = "incomparable_near_miss"
    UNRELATED = "unrelated"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class IntendedRelation(StrEnum):
    """Generation intention only; resolved labels use ``RelationLabel`` (§11.1)."""

    EQUIVALENT = "equivalent"
    A_STRONGER = "A_stronger"
    B_STRONGER = "B_stronger"
    NEAR_MISS = "near_miss"
    UNRELATED = "unrelated"
    UNKNOWN = "unknown"


class QualityTier(StrEnum):
    GOLD_HUMAN = "gold_human"
    GOLD_CONSERVATIVE_TRANSFORM = "gold_conservative_transform"
    GOLD_COUNTEREXAMPLE = "gold_counterexample"
    BENCHMARK = "benchmark"
    SILVER_CONSENSUS = "silver_consensus"
    PROVISIONAL = "provisional"
    UNKNOWN = "unknown"


class ValidationStatus(StrEnum):
    UNVALIDATED = "unvalidated"
    ELABORATES = "elaborates"
    ELABORATES_WITH_PLACEHOLDER = "elaborates_with_placeholder"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    QUARANTINED = "quarantined"


class TransformationFamilyStatus(StrEnum):
    GOLD_PROMOTED = "gold_promoted"
    SILVER = "silver"
    EXPERIMENTAL = "experimental"
    DISABLED = "disabled"


class EvidenceExecutionStatus(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"
    UNSUPPORTED = "unsupported"
    ABSTAIN = "abstain"
    NOT_RUN = "not_run"


class SemanticLabelTargetKind(StrEnum):
    LEAN_PAIR = "lean_pair"
    NL_LEAN = "nl_lean"


class EvidenceTargetKind(StrEnum):
    THEOREM = "theorem"
    LEAN_PAIR = "lean_pair"
    NL_LEAN = "nl_lean"
    TRANSFORMATION_DRAFT = "transformation_draft"
    TRANSFORMATION_FAMILY = "transformation_family"


# --- Supporting operational enums ---


class ArtifactClass(StrEnum):
    """Run/artifact class (§5.1, §22.7): smoke artifacts are barred from releases."""

    PRODUCTION = "production"
    SMOKE = "smoke"
    DIAGNOSTIC = "diagnostic"


class DataStage(StrEnum):
    """Immutable data lifecycle stages (§10)."""

    RAW = "raw"
    PARSED = "parsed"
    ELABORATED = "elaborated"
    REPRESENTED = "represented"
    GENERATED = "generated"
    VALIDATED = "validated"
    EVIDENCE_COLLECTED = "evidence_collected"
    LABELED = "labeled"
    SPLIT = "split"
    FROZEN = "frozen"


class NLTrust(StrEnum):
    """NL provenance trust (§9.4)."""

    TRUSTED = "trusted"
    SYNTHETIC = "synthetic"
    UNCERTAIN = "uncertain"


class ViewStatus(StrEnum):
    """Per-view derivation status in a RepresentationRecord (§11.4)."""

    OK = "ok"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"
    UNSUPPORTED = "unsupported"


class GeneratorKind(StrEnum):
    """How a variant was produced (§11.5)."""

    DETERMINISTIC_TRANSFORM = "deterministic_transform"
    AUTOFORMALIZER = "autoformalizer"
    LLM_PROPOSER = "llm_proposer"
    REPAIR = "repair"


class Polarity(StrEnum):
    """Variant polarity metadata (§11.5)."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    """Evidence jobs (§16.2) plus judgment/audit kinds (§11.7)."""

    TYPECHECK = "typecheck"
    DEFEQ = "defeq"
    PROOF_A_IMPLIES_B = "proof_a_implies_b"
    PROOF_B_IMPLIES_A = "proof_b_implies_a"
    CLAIM_ALIGNMENT = "claim_alignment"
    COUNTEREXAMPLE = "counterexample"
    AXIOM_AUDIT = "axiom_audit"
    LLM_JUDGMENT = "llm_judgment"
    HUMAN_ANNOTATION = "human_annotation"
    TRANSFORMATION_AUDIT = "transformation_audit"


class LLMRole(StrEnum):
    """Provider slot roles (§17.1, §17.2)."""

    AUTOFORMALIZER = "autoformalizer"
    PROPOSER = "proposer"
    REPAIR = "repair"
    JUDGE = "judge"
    PRIMARY_EVAL_JUDGE = "primary_eval_judge"


class ParseStatus(StrEnum):
    """LLM output parse state (§11.11)."""

    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"
    EMPTY = "empty"


class AnnotationAnswer(StrEnum):
    """Human annotation same-claim field (§18.5)."""

    SAME_CLAIM = "same_claim"
    NOT_SAME_CLAIM = "not_same_claim"
    AMBIGUOUS = "ambiguous"
    CANNOT_ASSESS_YET = "cannot_assess_yet"


class ReferenceIssue(StrEnum):
    """Reference-defect flag (§18.5)."""

    NONE = "none"
    SUSPECTED = "suspected"
    DEFINITE = "definite"


class Decision(StrEnum):
    """Selective decision (§20.6)."""

    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class SourceKind(StrEnum):
    """Source manifest kind (§9.5)."""

    HF_DATASET = "hf_dataset"
    GIT_REPOSITORY = "git_repository"
    LLM_PROVIDER = "llm_provider"
    LOCAL_FIXTURE = "local_fixture"


class AccessStatus(StrEnum):
    """Source access outcome (§9.1, §9.2)."""

    PUBLIC = "public"
    PRIVATE_AUTHENTICATED = "private_authenticated"
    BLOCKED = "blocked"
