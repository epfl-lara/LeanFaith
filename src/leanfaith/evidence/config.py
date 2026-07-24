"""Strict authoritative LF-020 evidence policy configurations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths


class CertificatePolicy(StrictModel):
    replay_required: bool
    reject_unresolved_metavariables: bool
    reject_source_proof_constants: bool
    reject_candidate_proof_constants: bool
    reject_vacuity_or_ex_falso_for_claim_relations: bool
    allowed_standard_axioms: tuple[str, ...] = ()
    forbidden_axioms: tuple[str, ...] = ()


class ProofMethodConfig(StrictModel):
    method_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    order: int = Field(ge=0)
    enabled: bool
    comparison_modes: tuple[Literal["closed_truth", "binder_aligned_claim"], ...]
    tactic_body: str
    discovery_tactic: str | None = None
    replay_policy: Literal["fixed_tactic", "exact_term", "exact_suggestion", "same_tactic"]
    timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _mvp_replay_is_exact(self) -> ProofMethodConfig:
        if len(self.comparison_modes) != len(set(self.comparison_modes)):
            raise ValueError("proof comparison_modes must be unique")
        if self.enabled and not self.tactic_body.strip():
            raise ValueError("enabled proof methods require a complete tactic_body")
        if self.enabled and self.discovery_tactic is not None:
            raise ValueError(
                "LF-020 MVP enabled methods must be direct replay methods; suggestion "
                "tactics are discovery-only and need a separate exact replay stage"
            )
        if self.enabled and self.replay_policy != "fixed_tactic":
            raise ValueError("LF-020 MVP enabled methods require replay_policy=fixed_tactic")
        return self


class AlignmentTemplatePolicy(StrictModel):
    template_version: str
    require_total_binder_map: bool
    require_total_premise_map: bool
    require_conclusion_role_map: bool
    reject_unused_mapped_premises: bool
    reject_inconsistent_source_premises: bool


class PortfolioConfig(StrictModel):
    schema_version: Literal[1] = 1
    portfolio_id: str
    portfolio_version: str
    default_timeout_seconds: float = Field(gt=0)
    allow_sorry: Literal[False] = False
    certificate_policy: CertificatePolicy
    methods: tuple[ProofMethodConfig, ...] = Field(min_length=1)
    alignment_templates: AlignmentTemplatePolicy

    @model_validator(mode="after")
    def _ordered_unique(self) -> PortfolioConfig:
        enabled = [method for method in self.methods if method.enabled]
        if not enabled:
            raise ValueError("portfolio must enable at least one proof method")
        ids = [method.method_id for method in self.methods]
        orders = [method.order for method in self.methods]
        if len(ids) != len(set(ids)):
            raise ValueError("proof method IDs must be unique")
        if orders != sorted(set(orders)):
            raise ValueError("proof method order values must be unique and sorted")
        if set(self.certificate_policy.allowed_standard_axioms) & set(
            self.certificate_policy.forbidden_axioms
        ):
            raise ValueError("an axiom cannot be both allowed and forbidden")
        policy = self.certificate_policy
        if not (
            policy.replay_required
            and policy.reject_unresolved_metavariables
            and policy.reject_source_proof_constants
            and policy.reject_candidate_proof_constants
            and policy.reject_vacuity_or_ex_falso_for_claim_relations
        ):
            raise ValueError("LF-020 certificate safety policies must remain enabled")
        if not any("sorryAx" in axiom for axiom in policy.forbidden_axioms):
            raise ValueError("LF-020 must explicitly forbid sorryAx")
        return self

    @property
    def method_version(self) -> str:
        return f"{self.portfolio_id}@{self.portfolio_version}"


class CounterexampleEngineConfig(StrictModel):
    engine_id: str
    order: int = Field(ge=0)
    enabled: bool
    implementation: Literal["decide", "native_decide"]
    trust_tier: Literal["kernel_checked", "lower_trust"]
    may_support_gold_negative: bool

    @model_validator(mode="after")
    def _native_is_never_gold(self) -> CounterexampleEngineConfig:
        if self.implementation == "native_decide" and (
            self.enabled or self.may_support_gold_negative
        ):
            raise ValueError("native_decide must remain disabled and non-gold in LF-020 v1")
        return self


class DomainEncodingConfig(StrictModel):
    encoding_id: str
    domain: str
    enabled: bool
    max_cardinality: int | None = Field(default=None, gt=0)
    lower_bound: int | None = None
    upper_bound: int | None = None

    @model_validator(mode="after")
    def _implemented_v1_only(self) -> DomainEncodingConfig:
        if self.enabled and self.domain not in {"Bool", "Fin"}:
            raise ValueError("LF-020 v1 enables only implemented Bool/Fin encodings")
        return self


class CounterexampleCertificatePolicy(StrictModel):
    persist_helper_theorem: bool
    persist_witness: bool
    persist_axiom_audit: bool
    require_allow_sorry_false: bool
    unsupported_is_not_negative: bool
    not_found_is_not_negative: bool
    native_decide_never_sole_gold_negative: bool


class CounterexampleConfig(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: str
    profile_version: str
    scope: Literal["decidable_bounded_fragments_only"]
    default_timeout_seconds: float = Field(gt=0)
    max_enumerated_assignments: int = Field(gt=0)
    engines: tuple[CounterexampleEngineConfig, ...] = Field(min_length=1)
    domain_encodings: tuple[DomainEncodingConfig, ...] = Field(min_length=1)
    certificate_policy: CounterexampleCertificatePolicy

    @model_validator(mode="after")
    def _kernel_only(self) -> CounterexampleConfig:
        engine_ids = [engine.engine_id for engine in self.engines]
        if len(engine_ids) != len(set(engine_ids)):
            raise ValueError("counterexample engine IDs must be unique")
        engines = [engine for engine in self.engines if engine.enabled]
        if len(engines) != 1 or engines[0].implementation != "decide":
            raise ValueError("counterexample_v1 must enable exactly kernel decide")
        orders = [engine.order for engine in self.engines]
        if orders != sorted(set(orders)):
            raise ValueError("counterexample engine order must be unique and sorted")
        encoding_ids = [encoding.encoding_id for encoding in self.domain_encodings]
        if len(encoding_ids) != len(set(encoding_ids)):
            raise ValueError("counterexample domain encoding IDs must be unique")
        enabled_encodings = [encoding for encoding in self.domain_encodings if encoding.enabled]
        if not enabled_encodings:
            raise ValueError("counterexample v1 must enable at least one bounded encoding")
        for encoding in enabled_encodings:
            if (
                encoding.max_cardinality is not None
                and encoding.max_cardinality > self.max_enumerated_assignments
            ):
                raise ValueError(
                    f"encoding {encoding.encoding_id} exceeds max_enumerated_assignments"
                )
        policy = self.certificate_policy
        if not (
            policy.persist_helper_theorem
            and policy.persist_witness
            and policy.persist_axiom_audit
            and policy.require_allow_sorry_false
            and policy.unsupported_is_not_negative
            and policy.not_found_is_not_negative
            and policy.native_decide_never_sole_gold_negative
        ):
            raise ValueError("counterexample v1 negative-safety policies must remain enabled")
        return self

    @property
    def method_version(self) -> str:
        return f"{self.profile_id}@{self.profile_version}"


class TrainingEvidenceSample(StrictModel):
    enabled: bool
    strategy: Literal["stratified_hash_v1"]
    hash_seed: str
    fraction_per_stratum: float = Field(gt=0, lt=1)
    minimum_per_stratum: int = Field(ge=0)
    maximum_per_stratum: int = Field(gt=0)
    strata: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _bounds(self) -> TrainingEvidenceSample:
        if self.minimum_per_stratum > self.maximum_per_stratum:
            raise ValueError("minimum_per_stratum cannot exceed maximum_per_stratum")
        if len(self.strata) != len(set(self.strata)):
            raise ValueError("training evidence strata must be unique")
        if "intended_relation" in self.strata:
            raise ValueError("mutation intended_relation cannot control evidence sampling")
        return self


class EvidenceApplicability(StrictModel):
    defeq: str
    directional_proof: str
    claim_alignment: str
    counterexample: str


class EvidenceFailurePolicy(StrictModel):
    persist_every_attempt: bool
    timeout_is_unknown: bool
    proof_not_proved_is_unknown: bool
    counterexample_not_found_is_unknown: bool
    unsupported_is_unknown: bool

    @model_validator(mode="after")
    def _all_failures_unknown(self) -> EvidenceFailurePolicy:
        if not all(self.model_dump(mode="python").values()):
            raise ValueError("all LF-020 failure/absence outcomes must remain unknown")
        return self


class EvidenceSamplingConfig(StrictModel):
    schema_version: Literal[1] = 1
    policy_id: str
    policy_version: str
    mandatory_for: tuple[str, ...]
    training_sample: TrainingEvidenceSample
    applicability: EvidenceApplicability
    failure_policy: EvidenceFailurePolicy

    @model_validator(mode="after")
    def _mandatory_coverage(self) -> EvidenceSamplingConfig:
        required = {
            "evaluation_pairs",
            "calibration_pairs",
            "gold_promotion_candidates",
            "gold_counterexample_candidates",
        }
        if len(self.mandatory_for) != len(set(self.mandatory_for)):
            raise ValueError("mandatory evidence scopes must be unique")
        missing = required - set(self.mandatory_for)
        if missing:
            raise ValueError(f"mandatory evidence scopes missing: {sorted(missing)}")
        return self


@dataclass(frozen=True, slots=True)
class LoadedEvidenceConfigs:
    portfolio: LoadedConfig[PortfolioConfig]
    counterexample: LoadedConfig[CounterexampleConfig]
    sampling: LoadedConfig[EvidenceSamplingConfig]


def load_evidence_configs(paths: RepoPaths) -> LoadedEvidenceConfigs:
    directory = paths.configs / "evidence"
    return LoadedEvidenceConfigs(
        portfolio=load_config(directory / "portfolio_v1.yaml", PortfolioConfig),
        counterexample=load_config(
            directory / "counterexample_v1.yaml",
            CounterexampleConfig,
        ),
        sampling=load_config(directory / "sampling_v1.yaml", EvidenceSamplingConfig),
    )


def load_evidence_configs_from_root(root: Path) -> LoadedEvidenceConfigs:
    return load_evidence_configs(RepoPaths(root=root))
