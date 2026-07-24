"""Strict LF-016 transformation registry and guarded rule execution.

The registry is the runtime policy boundary described by PLAN.md §15.2 and
§15.8.  It validates the versioned family/rule inventory against the authored
promotion policy, binds every generated draft to the effective registry hash,
and fails closed before executing disabled or unlisted rules.  Generation
intentions remain provenance only: this module never constructs or infers a
``ResolvedLabel``.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.loading import load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import (
    IntendedRelation,
    Polarity,
    QualityTier,
    TransformationFamilyStatus,
)
from leanfaith.schemas.ids import HEX64_PATTERN, make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import (
    ECODE_PATTERN,
    FAMILY_ID_PATTERN,
    Applicability,
    TransformationAttempt,
    TransformationAudit,
    VariantDraft,
)
from leanfaith.transforms.protocol import (
    TransformationRule,
    build_transformation_attempt,
    verify_transformation_attempt_id,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
SemanticVersion = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True)]
RuleId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
FamilyId = Annotated[str, Field(pattern=FAMILY_ID_PATTERN, strict=True)]
Sha256 = Annotated[str, Field(pattern=HEX64_PATTERN, strict=True)]


class RegistryIntegrityError(ValueError):
    """A transformation config, policy binding, rule, draft, or audit is inconsistent."""


class TransformationOperation(StrEnum):
    REGISTER = "register"
    ASSESS = "assess"
    GENERATE = "generate"
    AUDIT = "audit"


class RejectionReason(StrEnum):
    FAMILY_UNLISTED = "family_unlisted"
    FAMILY_DISABLED = "family_disabled"
    FAMILY_NOT_ACTIVE = "family_not_active"
    RULE_UNLISTED = "rule_unlisted"
    RULE_NOT_REGISTERED = "rule_not_registered"
    RULE_ALREADY_REGISTERED = "rule_already_registered"
    IMPLEMENTATION_UNAVAILABLE = "implementation_unavailable"
    RULE_PROTOCOL_MISMATCH = "rule_protocol_mismatch"
    RULE_EXECUTION_ERROR = "rule_execution_error"
    RULE_METADATA_MISMATCH = "rule_metadata_mismatch"
    RULE_RESULT_MISMATCH = "rule_result_mismatch"
    INPUT_LINEAGE_MISMATCH = "input_lineage_mismatch"
    RULE_NOT_APPLICABLE = "rule_not_applicable"


class TransformationRejectionEvent(StrictModel):
    """Deterministic fail-closed event emitted before a prohibited call raises."""

    schema_version: Literal[1] = 1
    event_id: str
    operation: TransformationOperation
    reason_code: RejectionReason
    registry_hash: Sha256
    family_id: str | None = None
    rule_id: str | None = None
    rule_version: str | None = None
    details: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _event_id_matches(self) -> TransformationRejectionEvent:
        expected = make_id(
            "transform_rejection",
            {
                "schema_version": self.schema_version,
                "operation": self.operation.value,
                "reason_code": self.reason_code.value,
                "registry_hash": self.registry_hash,
                "family_id": self.family_id,
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "details": list(self.details),
            },
        )
        if self.event_id != expected:
            raise ValueError("transformation rejection event_id does not match its payload")
        return self


class TransformationRejected(RuntimeError):
    """Raised after a prohibited transformation operation emits an event."""

    def __init__(self, event: TransformationRejectionEvent) -> None:
        super().__init__(
            f"{event.operation.value} rejected: {event.reason_code.value}"
            f" (family={event.family_id!r}, rule={event.rule_id!r})"
        )
        self.event = event


class TransformationExecution(StrictModel):
    """One terminal applicability/generation attempt and its emitted drafts."""

    attempt: TransformationAttempt
    drafts: tuple[VariantDraft, ...]

    @model_validator(mode="after")
    def _coherent(self) -> TransformationExecution:
        try:
            verify_transformation_attempt_id(self.attempt)
        except ValueError as exc:
            raise ValueError("execution attempt_id does not match its semantic payload") from exc
        draft_ids = tuple(sorted(draft.draft_id for draft in self.drafts))
        if draft_ids != self.attempt.draft_ids:
            raise ValueError("execution drafts do not match attempt.draft_ids")
        if self.attempt.terminal_outcome == "generated" and not self.drafts:
            raise ValueError("generated execution requires at least one draft")
        if self.attempt.terminal_outcome != "generated" and self.drafts:
            raise ValueError("only a generated execution may carry drafts")
        for draft in self.drafts:
            try:
                verify_variant_draft_id(draft)
            except ValueError as exc:
                raise ValueError("execution draft_id does not match its semantic payload") from exc
            comparisons = (
                ("family_id", draft.family_id, self.attempt.family_id),
                ("rule_id", draft.rule_id, self.attempt.rule_id),
                ("rule_version", draft.rule_version, self.attempt.rule_version),
                (
                    "source_theorem_ids",
                    draft.source_theorem_ids,
                    self.attempt.source_theorem_ids,
                ),
                (
                    "source_representation_ids",
                    draft.source_representation_ids,
                    self.attempt.source_representation_ids,
                ),
                ("context_id", draft.context_id, self.attempt.context_id),
                (
                    "generation_config_hash",
                    draft.generation_config_hash,
                    self.attempt.generation_config_hash,
                ),
                ("seed", draft.seed, self.attempt.seed),
            )
            mismatches = [name for name, actual, expected in comparisons if actual != expected]
            if mismatches:
                raise ValueError(
                    "execution draft lineage differs from its attempt: "
                    + ", ".join(sorted(mismatches))
                )
        return self


class TransformationExecutionFailed(RuntimeError):
    """A rule attempt failed after dispatch and carries its terminal record."""

    def __init__(
        self,
        execution: TransformationExecution,
        *,
        stage: str,
        rejection_event: TransformationRejectionEvent,
    ) -> None:
        super().__init__(
            f"transformation {stage} failed: {rejection_event.reason_code.value}; "
            f"attempt={execution.attempt.attempt_id}"
        )
        self.execution = execution
        self.stage = stage
        self.rejection_event = rejection_event


class RuleImplementationStatus(StrEnum):
    """Code availability is explicit; YAML never names an importable module."""

    PENDING = "pending"
    AVAILABLE = "available"
    DISABLED = "disabled"


def _check_error_codes(codes: tuple[str, ...], *, location: str) -> None:
    if tuple(sorted(set(codes))) != codes:
        raise ValueError(f"{location} must be sorted and unique")
    for code in codes:
        if re.fullmatch(ECODE_PATTERN, code) is None:
            raise ValueError(f"{location} contains unknown error code {code!r}")


def _check_intentions(
    intentions: tuple[IntendedRelation, ...],
    *,
    polarity: Polarity,
    location: str,
) -> None:
    if tuple(sorted(set(intentions), key=str)) != intentions:
        raise ValueError(f"{location} must be sorted and unique")
    if not intentions:
        raise ValueError(f"{location} cannot be empty")
    if polarity == Polarity.POSITIVE and intentions != (IntendedRelation.EQUIVALENT,):
        raise ValueError(f"{location}: positive families may only intend equivalent")
    if polarity == Polarity.NEGATIVE and IntendedRelation.EQUIVALENT in intentions:
        raise ValueError(f"{location}: negative families cannot intend equivalent")


class TransformationRuleConfig(StrictModel):
    """One planned runtime rule; LF-017/LF-018 provide implementations later."""

    rule_id: RuleId
    rule_version: SemanticVersion
    family_id: FamilyId
    polarity: Polarity
    implementation_key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", strict=True)]
    implementation_status: RuleImplementationStatus
    allowed_intentions: tuple[IntendedRelation, ...]
    allowed_error_types: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _coherent(self) -> TransformationRuleConfig:
        if self.polarity not in (Polarity.POSITIVE, Polarity.NEGATIVE):
            raise ValueError("transformation rule polarity must be positive or negative")
        _check_intentions(
            self.allowed_intentions,
            polarity=self.polarity,
            location=f"rule {self.rule_id} allowed_intentions",
        )
        _check_error_codes(
            self.allowed_error_types,
            location=f"rule {self.rule_id} allowed_error_types",
        )
        if self.polarity == Polarity.POSITIVE and self.allowed_error_types:
            raise ValueError("positive rule allowed_error_types must be empty")
        return self


class TransformationFamilyConfig(StrictModel):
    """Family policy, provenance, and the exact rules admitted under it."""

    family_id: FamilyId
    family_version: SemanticVersion
    polarity: Polarity
    profile_id: NonEmptyStr
    status: TransformationFamilyStatus
    allowed_intentions: tuple[IntendedRelation, ...]
    allowed_error_types: tuple[str, ...] = ()
    invariants: tuple[NonEmptyStr, ...]
    required_evidence: tuple[NonEmptyStr, ...]
    audit_manifest: str | None = None
    audit_manifest_sha256: Sha256 | None = None
    promotion_decision: str | None = None
    promotion_decision_sha256: Sha256 | None = None
    policy_decision: NonEmptyStr
    rules: tuple[TransformationRuleConfig, ...]

    @model_validator(mode="after")
    def _coherent(self) -> TransformationFamilyConfig:
        if self.polarity not in (Polarity.POSITIVE, Polarity.NEGATIVE):
            raise ValueError("transformation family polarity must be positive or negative")
        _check_intentions(
            self.allowed_intentions,
            polarity=self.polarity,
            location=f"family {self.family_id} allowed_intentions",
        )
        _check_error_codes(
            self.allowed_error_types,
            location=f"family {self.family_id} allowed_error_types",
        )
        if self.polarity == Polarity.POSITIVE and self.allowed_error_types:
            raise ValueError("positive family allowed_error_types must be empty")
        if not self.invariants or len(set(self.invariants)) != len(self.invariants):
            raise ValueError("family invariants must be nonempty and unique")
        if not self.required_evidence or len(set(self.required_evidence)) != len(
            self.required_evidence
        ):
            raise ValueError("family required_evidence must be nonempty and unique")
        if (self.audit_manifest is None) != (self.audit_manifest_sha256 is None):
            raise ValueError("audit_manifest and audit_manifest_sha256 must be set together")
        if (self.promotion_decision is None) != (self.promotion_decision_sha256 is None):
            raise ValueError(
                "promotion_decision and promotion_decision_sha256 must be set together"
            )
        if self.status == TransformationFamilyStatus.SILVER:
            raise ValueError(
                "family status silver is not executable until a family-level silver "
                "promotion policy is specified"
            )
        if self.status == TransformationFamilyStatus.GOLD_PROMOTED and (
            self.audit_manifest is None or self.promotion_decision is None
        ):
            raise ValueError(
                "gold_promoted requires bound audit_manifest and promotion_decision artifacts"
            )
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if not rule_ids or len(set(rule_ids)) != len(rule_ids):
            raise ValueError("family rules must be nonempty with unique rule_id values")
        for rule in self.rules:
            if rule.family_id != self.family_id:
                raise ValueError(
                    f"rule {rule.rule_id} family_id {rule.family_id!r} does not match "
                    f"{self.family_id!r}"
                )
            if rule.polarity != self.polarity:
                raise ValueError(f"rule {rule.rule_id} polarity does not match its family")
            if not set(rule.allowed_intentions).issubset(self.allowed_intentions):
                raise ValueError(f"rule {rule.rule_id} admits a family-disallowed intention")
            if not set(rule.allowed_error_types).issubset(self.allowed_error_types):
                raise ValueError(f"rule {rule.rule_id} admits a family-disallowed error type")
            if self.status == TransformationFamilyStatus.DISABLED:
                if rule.implementation_status != RuleImplementationStatus.DISABLED:
                    raise ValueError(
                        "disabled family rules must have implementation_status=disabled"
                    )
            elif rule.implementation_status == RuleImplementationStatus.DISABLED:
                raise ValueError(
                    "non-disabled family rules cannot have implementation_status=disabled"
                )
        return self


class TransformationRegistryConfig(StrictModel):
    """Strict contents of ``configs/transformations/registry.yaml``."""

    schema_version: Literal[1] = 1
    registry_id: NonEmptyStr
    registry_version: SemanticVersion
    promotion_policy_version: NonEmptyStr
    profile_path: NonEmptyStr
    unlisted_family_status: Literal[TransformationFamilyStatus.DISABLED] = (
        TransformationFamilyStatus.DISABLED
    )
    disabled_families_non_executable: Literal[True] = True
    intention_is_never_resolved_label: Literal[True] = True
    families: tuple[TransformationFamilyConfig, ...]

    @model_validator(mode="after")
    def _unique_inventory(self) -> TransformationRegistryConfig:
        family_ids = tuple(family.family_id for family in self.families)
        if not family_ids or len(set(family_ids)) != len(family_ids):
            raise ValueError("registry families must be nonempty with unique family_id values")
        all_rule_ids = tuple(rule.rule_id for family in self.families for rule in family.rules)
        if len(set(all_rule_ids)) != len(all_rule_ids):
            raise ValueError("rule_id values must be globally unique")
        return self

    @property
    def rule_count(self) -> int:
        return sum(len(family.rules) for family in self.families)


class TransformationProfileConfig(StrictModel):
    """Strict contents of one execution profile such as ``v1.yaml``."""

    schema_version: Literal[1] = 1
    profile_id: NonEmptyStr
    profile_version: SemanticVersion
    registry_id: NonEmptyStr
    registry_version: SemanticVersion
    active_family_ids: tuple[FamilyId, ...]
    disabled_family_ids: tuple[FamilyId, ...]

    @model_validator(mode="after")
    def _partition(self) -> TransformationProfileConfig:
        for field_name, values in (
            ("active_family_ids", self.active_family_ids),
            ("disabled_family_ids", self.disabled_family_ids),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{field_name} must be sorted and unique")
        overlap = set(self.active_family_ids) & set(self.disabled_family_ids)
        if overlap:
            raise ValueError(f"profile active/disabled family sets overlap: {sorted(overlap)}")
        if not self.active_family_ids:
            raise ValueError("profile must contain at least one active family")
        return self


class LoadedTransformationRegistry(StrictModel):
    """Validated effective registry plus deterministic provenance hashes."""

    config: TransformationRegistryConfig
    profile: TransformationProfileConfig
    registry_path: Path
    profile_path: Path
    promotion_policy_path: Path
    registry_config_hash: Sha256
    profile_config_hash: Sha256
    promotion_policy_hash: Sha256
    registry_hash: Sha256

    @model_validator(mode="after")
    def _hashes_match_effective_payload(self) -> LoadedTransformationRegistry:
        if self.registry_config_hash != hash_canonical(self.config.model_dump(mode="json")):
            raise ValueError("registry_config_hash does not match validated config")
        if self.profile_config_hash != hash_canonical(self.profile.model_dump(mode="json")):
            raise ValueError("profile_config_hash does not match validated profile")
        if self.registry_hash != _effective_registry_hash(
            self.config,
            self.profile,
            self.promotion_policy_hash,
        ):
            raise ValueError("registry_hash does not match effective registry payload")
        return self


def _effective_registry_hash(
    config: TransformationRegistryConfig,
    profile: TransformationProfileConfig,
    promotion_policy_hash: str,
) -> str:
    return hash_canonical(
        {
            "schema": "leanfaith_transformation_registry_effective_v1",
            "registry": config.model_dump(mode="json"),
            "profile": profile.model_dump(mode="json"),
            "promotion_policy_hash": promotion_policy_hash,
        }
    )


def _validate_profile(
    config: TransformationRegistryConfig,
    profile: TransformationProfileConfig,
) -> None:
    if profile.registry_id != config.registry_id:
        raise RegistryIntegrityError("profile registry_id does not match registry config")
    if profile.registry_version != config.registry_version:
        raise RegistryIntegrityError("profile registry_version does not match registry config")
    inventory = {family.family_id: family for family in config.families}
    categorized = set(profile.active_family_ids) | set(profile.disabled_family_ids)
    if categorized != set(inventory):
        missing = sorted(set(inventory) - categorized)
        unknown = sorted(categorized - set(inventory))
        raise RegistryIntegrityError(
            f"profile must categorize every registry family exactly once; "
            f"missing={missing}, unknown={unknown}"
        )
    for family_id in profile.active_family_ids:
        family = inventory[family_id]
        if family.profile_id != profile.profile_id:
            raise RegistryIntegrityError(f"active family {family_id} has wrong profile_id")
        if family.status == TransformationFamilyStatus.DISABLED:
            raise RegistryIntegrityError(f"active family {family_id} has disabled status")
    for family_id in profile.disabled_family_ids:
        family = inventory[family_id]
        if family.status != TransformationFamilyStatus.DISABLED:
            raise RegistryIntegrityError(f"disabled family {family_id} is not status=disabled")


def _validate_policy_binding(
    config: TransformationRegistryConfig,
    policy: dict[str, object],
) -> None:
    if policy.get("policy_version") != config.promotion_policy_version:
        raise RegistryIntegrityError("promotion policy_version does not match registry binding")
    if policy.get("status") != "active_internal_research":
        raise RegistryIntegrityError(
            "promotion policy is not active under the Gate-0 internal-research scope"
        )
    defaults = policy.get("registry_defaults")
    if not isinstance(defaults, dict):
        raise RegistryIntegrityError("promotion policy registry_defaults must be a mapping")
    if defaults.get("unlisted_family_status") != config.unlisted_family_status.value:
        raise RegistryIntegrityError(
            "registry unlisted-family policy differs from promotion policy"
        )
    if defaults.get("disabled_families_non_executable") is not True:
        raise RegistryIntegrityError("promotion policy must make disabled families non-executable")
    if defaults.get("intention_is_never_resolved_label") is not True:
        raise RegistryIntegrityError("promotion policy must keep intentions separate from labels")

    policy_active = policy.get("active_families")
    policy_stubs = policy.get("stub_families")
    if not isinstance(policy_active, dict) or not isinstance(policy_stubs, dict):
        raise RegistryIntegrityError("promotion policy family inventories must be mappings")
    policy_families = {**policy_active, **policy_stubs}
    config_families = {family.family_id: family for family in config.families}
    if set(policy_families) != set(config_families):
        raise RegistryIntegrityError("registry family inventory differs from promotion policy")
    for family_id, family in config_families.items():
        raw = policy_families[family_id]
        if not isinstance(raw, dict):
            raise RegistryIntegrityError(f"promotion policy family {family_id} must be a mapping")
        if raw.get("polarity") != family.polarity.value:
            raise RegistryIntegrityError(f"family {family_id} polarity differs from policy")
        if raw.get("status") != family.status.value:
            raise RegistryIntegrityError(f"family {family_id} status differs from policy")
        policy_intention = raw.get("intended_relation")
        if policy_intention is not None and [policy_intention] != [
            item.value for item in family.allowed_intentions
        ]:
            raise RegistryIntegrityError(f"family {family_id} intention differs from policy")
        policy_errors = raw.get("intended_error_types", [])
        if not isinstance(policy_errors, list) or sorted(policy_errors) != list(
            family.allowed_error_types
        ):
            raise RegistryIntegrityError(f"family {family_id} error types differ from policy")


def load_transformation_registry(
    repo_root: Path | None = None,
    *,
    registry_path: Path | None = None,
    profile_path: Path | None = None,
    promotion_policy_path: Path | None = None,
) -> LoadedTransformationRegistry:
    """Load, bind, and hash the LF-016 registry, profile, and promotion policy."""

    root = find_repo_root(repo_root)
    resolved_root = root.resolve()
    resolved_registry = (registry_path or root / "configs/transformations/registry.yaml").resolve()
    if not resolved_registry.is_relative_to(resolved_root):
        raise RegistryIntegrityError("registry path escapes the repository")
    registry_loaded = load_config(resolved_registry, TransformationRegistryConfig)
    config = registry_loaded.config

    declared_profile = (root / config.profile_path).resolve()
    if not declared_profile.is_relative_to(resolved_root):
        raise RegistryIntegrityError("registry profile_path escapes the repository")
    resolved_profile = (profile_path or declared_profile).resolve()
    if resolved_profile.resolve() != declared_profile.resolve():
        raise RegistryIntegrityError(
            f"profile override {resolved_profile} differs from registry profile_path "
            f"{config.profile_path}"
        )
    profile_loaded = load_config(resolved_profile, TransformationProfileConfig)
    _validate_profile(config, profile_loaded.config)

    resolved_policy = (
        promotion_policy_path or root / "policies/transformation_promotion_v1.yaml"
    ).resolve()
    if not resolved_policy.is_relative_to(resolved_root):
        raise RegistryIntegrityError("promotion policy path escapes the repository")
    policy = load_yaml_mapping(resolved_policy)
    _validate_policy_binding(config, policy)
    policy_hash = hash_canonical(policy)
    effective_hash = _effective_registry_hash(
        config,
        profile_loaded.config,
        policy_hash,
    )
    return LoadedTransformationRegistry(
        config=config,
        profile=profile_loaded.config,
        registry_path=resolved_registry.resolve(),
        profile_path=resolved_profile.resolve(),
        promotion_policy_path=resolved_policy.resolve(),
        registry_config_hash=registry_loaded.config_hash,
        profile_config_hash=profile_loaded.config_hash,
        promotion_policy_hash=policy_hash,
        registry_hash=effective_hash,
    )


RejectionSink = Callable[[TransformationRejectionEvent], None]


class TransformationRegistry:
    """Guarded dispatcher for later LF-017/LF-018 rule implementations."""

    def __init__(
        self,
        loaded: LoadedTransformationRegistry,
        rejection_sink: RejectionSink | None = None,
    ) -> None:
        self.loaded = LoadedTransformationRegistry.model_validate(loaded.model_dump(mode="json"))
        self._families = {family.family_id: family for family in self.loaded.config.families}
        self._rules = {
            rule.rule_id: rule for family in self.loaded.config.families for rule in family.rules
        }
        self._implementations: dict[str, TransformationRule] = {}
        self._rejection_events: list[TransformationRejectionEvent] = []
        self._rejection_sink = rejection_sink
        self._lock = threading.RLock()

    @property
    def registry_hash(self) -> str:
        return self.loaded.registry_hash

    @property
    def rejection_events(self) -> tuple[TransformationRejectionEvent, ...]:
        with self._lock:
            return tuple(self._rejection_events)

    def _emit_rejection(
        self,
        operation: TransformationOperation,
        reason: RejectionReason,
        *,
        family_id: str | None,
        rule_id: str | None,
        rule_version: str | None,
        details: Sequence[str] = (),
    ) -> TransformationRejected:
        normalized_details = tuple(details)
        payload = {
            "schema_version": 1,
            "operation": operation.value,
            "reason_code": reason.value,
            "registry_hash": self.registry_hash,
            "family_id": family_id,
            "rule_id": rule_id,
            "rule_version": rule_version,
            "details": list(normalized_details),
        }
        event = TransformationRejectionEvent(
            event_id=make_id("transform_rejection", payload),
            operation=operation,
            reason_code=reason,
            registry_hash=self.registry_hash,
            family_id=family_id,
            rule_id=rule_id,
            rule_version=rule_version,
            details=normalized_details,
        )
        with self._lock:
            self._rejection_events.append(event)
        if self._rejection_sink is not None:
            self._rejection_sink(event)
        return TransformationRejected(event)

    def _configured_rule(
        self,
        operation: TransformationOperation,
        rule_id: str,
    ) -> tuple[TransformationFamilyConfig, TransformationRuleConfig, TransformationRule]:
        rule_config = self._rules.get(rule_id)
        implementation = self._implementations.get(rule_id)
        if rule_config is None:
            raise self._emit_rejection(
                operation,
                RejectionReason.RULE_UNLISTED,
                family_id=None,
                rule_id=rule_id,
                rule_version=None,
            )
        family = self._families[rule_config.family_id]
        if family.status == TransformationFamilyStatus.DISABLED:
            raise self._emit_rejection(
                operation,
                RejectionReason.FAMILY_DISABLED,
                family_id=family.family_id,
                rule_id=rule_id,
                rule_version=rule_config.rule_version,
            )
        if family.family_id not in self.loaded.profile.active_family_ids:
            raise self._emit_rejection(
                operation,
                RejectionReason.FAMILY_NOT_ACTIVE,
                family_id=family.family_id,
                rule_id=rule_id,
                rule_version=rule_config.rule_version,
            )
        if rule_config.implementation_status != RuleImplementationStatus.AVAILABLE:
            raise self._emit_rejection(
                operation,
                RejectionReason.IMPLEMENTATION_UNAVAILABLE,
                family_id=family.family_id,
                rule_id=rule_id,
                rule_version=rule_config.rule_version,
                details=(rule_config.implementation_status.value,),
            )
        if implementation is None:
            raise self._emit_rejection(
                operation,
                RejectionReason.RULE_NOT_REGISTERED,
                family_id=family.family_id,
                rule_id=rule_id,
                rule_version=rule_config.rule_version,
            )
        runtime_mismatches: list[str] = []
        for field_name, expected in (
            ("rule_id", rule_config.rule_id),
            ("rule_version", rule_config.rule_version),
            ("family_id", rule_config.family_id),
            ("polarity", rule_config.polarity),
            ("implementation_key", rule_config.implementation_key),
        ):
            if getattr(implementation, field_name, None) != expected:
                runtime_mismatches.append(field_name)
        if runtime_mismatches:
            raise self._emit_rejection(
                operation,
                RejectionReason.RULE_METADATA_MISMATCH,
                family_id=family.family_id,
                rule_id=rule_id,
                rule_version=rule_config.rule_version,
                details=tuple(sorted(runtime_mismatches)),
            )
        return family, rule_config, implementation

    def register(self, rule: TransformationRule) -> None:
        raw_family_id = getattr(rule, "family_id", None)
        raw_rule_id = getattr(rule, "rule_id", None)
        raw_rule_version = getattr(rule, "rule_version", None)
        family_id = raw_family_id if isinstance(raw_family_id, str) else None
        rule_id = raw_rule_id if isinstance(raw_rule_id, str) else None
        rule_version = raw_rule_version if isinstance(raw_rule_version, str) else None
        family = self._families.get(family_id) if family_id is not None else None
        if family is None:
            raise self._emit_rejection(
                TransformationOperation.REGISTER,
                RejectionReason.FAMILY_UNLISTED,
                family_id=family_id,
                rule_id=rule_id,
                rule_version=rule_version,
            )
        config = self._rules.get(rule_id) if rule_id is not None else None
        if config is None:
            raise self._emit_rejection(
                TransformationOperation.REGISTER,
                RejectionReason.RULE_UNLISTED,
                family_id=family_id,
                rule_id=rule_id,
                rule_version=rule_version,
            )
        if family.status == TransformationFamilyStatus.DISABLED:
            raise self._emit_rejection(
                TransformationOperation.REGISTER,
                RejectionReason.FAMILY_DISABLED,
                family_id=family_id,
                rule_id=rule_id,
                rule_version=rule_version,
            )
        if config.implementation_status != RuleImplementationStatus.AVAILABLE:
            raise self._emit_rejection(
                TransformationOperation.REGISTER,
                RejectionReason.IMPLEMENTATION_UNAVAILABLE,
                family_id=family_id,
                rule_id=rule_id,
                rule_version=rule_version,
                details=(config.implementation_status.value,),
            )
        if not isinstance(rule, TransformationRule):
            raise self._emit_rejection(
                TransformationOperation.REGISTER,
                RejectionReason.RULE_PROTOCOL_MISMATCH,
                family_id=family_id,
                rule_id=rule_id,
                rule_version=rule_version,
            )
        mismatches: list[str] = []
        if config.family_id != family_id:
            mismatches.append("family_id")
        if config.rule_version != rule_version:
            mismatches.append("rule_version")
        if config.polarity != getattr(rule, "polarity", None):
            mismatches.append("polarity")
        if config.implementation_key != getattr(rule, "implementation_key", None):
            mismatches.append("implementation_key")
        if mismatches:
            raise self._emit_rejection(
                TransformationOperation.REGISTER,
                RejectionReason.RULE_METADATA_MISMATCH,
                family_id=family_id,
                rule_id=rule_id,
                rule_version=rule_version,
                details=tuple(sorted(mismatches)),
            )
        with self._lock:
            if config.rule_id in self._implementations:
                raise self._emit_rejection(
                    TransformationOperation.REGISTER,
                    RejectionReason.RULE_ALREADY_REGISTERED,
                    family_id=family_id,
                    rule_id=rule_id,
                    rule_version=rule_version,
                )
            self._implementations[config.rule_id] = rule

    def _validate_input_lineage(
        self,
        operation: TransformationOperation,
        family: TransformationFamilyConfig,
        config: TransformationRuleConfig,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> None:
        violations: list[str] = []
        if representation.theorem_id != theorem.theorem_id:
            violations.append("representation.theorem_id")
        if representation.context_id != theorem.context_id:
            violations.append("representation.context_id")
        if violations:
            raise self._emit_rejection(
                operation,
                RejectionReason.INPUT_LINEAGE_MISMATCH,
                family_id=family.family_id,
                rule_id=config.rule_id,
                rule_version=config.rule_version,
                details=tuple(violations),
            )

    def assess(
        self,
        rule_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability:
        family, config, rule = self._configured_rule(TransformationOperation.ASSESS, rule_id)
        self._validate_input_lineage(
            TransformationOperation.ASSESS,
            family,
            config,
            theorem,
            representation,
        )
        try:
            applicability = rule.assess(theorem, representation)
        except Exception as exc:
            raise self._emit_rejection(
                TransformationOperation.ASSESS,
                RejectionReason.RULE_EXECUTION_ERROR,
                family_id=family.family_id,
                rule_id=config.rule_id,
                rule_version=config.rule_version,
                details=("assess", type(exc).__name__),
            ) from exc
        if not isinstance(applicability, Applicability):
            raise self._emit_rejection(
                TransformationOperation.ASSESS,
                RejectionReason.RULE_RESULT_MISMATCH,
                family_id=family.family_id,
                rule_id=config.rule_id,
                rule_version=config.rule_version,
                details=("applicability_type",),
            )
        return applicability

    def _execution_failure(
        self,
        *,
        family: TransformationFamilyConfig,
        config: TransformationRuleConfig,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
        applicability: Applicability | None,
        stage: str,
        cause: Exception,
    ) -> TransformationExecutionFailed:
        if isinstance(cause, TransformationRejected):
            rejected = cause
        else:
            rejected = self._emit_rejection(
                TransformationOperation.GENERATE,
                RejectionReason.RULE_EXECUTION_ERROR,
                family_id=family.family_id,
                rule_id=config.rule_id,
                rule_version=config.rule_version,
                details=(stage, type(cause).__name__),
            )
        outcome: Literal["generation_error", "infrastructure_error"] = (
            "infrastructure_error"
            if isinstance(cause, TimeoutError | OSError)
            else "generation_error"
        )
        attempt = build_transformation_attempt(
            family_id=family.family_id,
            rule_id=config.rule_id,
            rule_version=config.rule_version,
            source_theorem_ids=(theorem.theorem_id,),
            source_representation_ids=(representation.representation_id,),
            context_id=theorem.context_id,
            registry_hash=self.registry_hash,
            generation_config_hash=self.registry_hash,
            seed=seed,
            applicability=applicability,
            terminal_outcome=outcome,
            failure_codes=(f"{stage}_{rejected.event.reason_code.value}",),
            metadata={
                "exception_type": type(cause).__name__,
                "exception_detail": str(cause) or type(cause).__name__,
            },
        )
        execution = TransformationExecution(attempt=attempt, drafts=())
        return TransformationExecutionFailed(
            execution,
            stage=stage,
            rejection_event=rejected.event,
        )

    def _draft_violations(
        self,
        *,
        config: TransformationRuleConfig,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        draft: VariantDraft,
        seed: int | None,
    ) -> list[str]:
        violations: list[str] = []
        try:
            verify_variant_draft_id(draft)
        except ValueError:
            violations.append("draft_id")
        if (
            draft.rule_id != config.rule_id
            or draft.rule_version != config.rule_version
            or draft.family_id != config.family_id
        ):
            violations.append("rule_identity")
        if seed is not None and draft.seed != seed:
            violations.append("seed")
        if draft.generation_config_hash != self.registry_hash:
            violations.append("generation_config_hash")
        if draft.context_id != theorem.context_id:
            violations.append("context_id")
        if draft.source_theorem_ids != (theorem.theorem_id,):
            violations.append("source_theorem_ids")
        if draft.source_representation_ids != (representation.representation_id,):
            violations.append("source_representation_ids")
        if draft.intended_relation not in config.allowed_intentions:
            violations.append("intended_relation")
        if not set(draft.intended_error_types).issubset(config.allowed_error_types):
            violations.append("intended_error_types")
        return violations

    def _validate_generated_drafts(
        self,
        *,
        family: TransformationFamilyConfig,
        config: TransformationRuleConfig,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
        drafts: tuple[VariantDraft, ...],
    ) -> None:
        seen: set[str] = set()
        for draft in drafts:
            if not isinstance(draft, VariantDraft):
                raise self._emit_rejection(
                    TransformationOperation.GENERATE,
                    RejectionReason.RULE_RESULT_MISMATCH,
                    family_id=family.family_id,
                    rule_id=config.rule_id,
                    rule_version=config.rule_version,
                    details=("draft_type",),
                )
            violations = self._draft_violations(
                config=config,
                theorem=theorem,
                representation=representation,
                draft=draft,
                seed=seed,
            )
            if draft.draft_id in seen:
                violations.append("duplicate_draft_id")
            seen.add(draft.draft_id)
            if violations:
                raise self._emit_rejection(
                    TransformationOperation.GENERATE,
                    RejectionReason.RULE_RESULT_MISMATCH,
                    family_id=family.family_id,
                    rule_id=config.rule_id,
                    rule_version=config.rule_version,
                    details=tuple(sorted(set(violations))),
                )

    def execute(
        self,
        rule_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> TransformationExecution:
        """Assess then generate once, persisting non-applicability as an attempt."""

        family, config, rule = self._configured_rule(TransformationOperation.GENERATE, rule_id)
        self._validate_input_lineage(
            TransformationOperation.GENERATE,
            family,
            config,
            theorem,
            representation,
        )
        try:
            applicability = rule.assess(theorem, representation)
            if not isinstance(applicability, Applicability):
                rejected = self._emit_rejection(
                    TransformationOperation.GENERATE,
                    RejectionReason.RULE_RESULT_MISMATCH,
                    family_id=family.family_id,
                    rule_id=config.rule_id,
                    rule_version=config.rule_version,
                    details=("applicability_type",),
                )
                raise rejected
        except Exception as exc:
            failure = self._execution_failure(
                family=family,
                config=config,
                theorem=theorem,
                representation=representation,
                seed=seed,
                applicability=None,
                stage="assess",
                cause=exc,
            )
            raise failure from exc
        if not applicability.applicable:
            attempt = build_transformation_attempt(
                family_id=family.family_id,
                rule_id=config.rule_id,
                rule_version=config.rule_version,
                source_theorem_ids=(theorem.theorem_id,),
                source_representation_ids=(representation.representation_id,),
                context_id=theorem.context_id,
                registry_hash=self.registry_hash,
                generation_config_hash=self.registry_hash,
                seed=seed,
                applicability=applicability,
                terminal_outcome="not_applicable",
            )
            return TransformationExecution(attempt=attempt, drafts=())
        try:
            drafts = tuple(rule.generate(theorem, representation, seed))
            self._validate_generated_drafts(
                family=family,
                config=config,
                theorem=theorem,
                representation=representation,
                seed=seed,
                drafts=drafts,
            )
        except Exception as exc:
            failure = self._execution_failure(
                family=family,
                config=config,
                theorem=theorem,
                representation=representation,
                seed=seed,
                applicability=applicability,
                stage="generate",
                cause=exc,
            )
            raise failure from exc
        outcome: Literal["generated", "no_output"] = "generated" if drafts else "no_output"
        attempt = build_transformation_attempt(
            family_id=family.family_id,
            rule_id=config.rule_id,
            rule_version=config.rule_version,
            source_theorem_ids=(theorem.theorem_id,),
            source_representation_ids=(representation.representation_id,),
            context_id=theorem.context_id,
            registry_hash=self.registry_hash,
            generation_config_hash=self.registry_hash,
            seed=seed,
            applicability=applicability,
            terminal_outcome=outcome,
            draft_ids=tuple(draft.draft_id for draft in drafts),
        )
        return TransformationExecution(attempt=attempt, drafts=drafts)

    def generate(
        self,
        rule_id: str,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        """Compatibility view returning drafts from the persistent execution."""

        return self.execute(rule_id, theorem, representation, seed).drafts

    def audit(
        self,
        rule_id: str,
        source: TheoremRecord,
        source_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        family, config, rule = self._configured_rule(TransformationOperation.AUDIT, rule_id)
        self._validate_input_lineage(
            TransformationOperation.AUDIT,
            family,
            config,
            source,
            source_representation,
        )
        self._validate_input_lineage(
            TransformationOperation.AUDIT,
            family,
            config,
            candidate,
            candidate_representation,
        )
        preflight_violations = self._draft_violations(
            config=config,
            theorem=source,
            representation=source_representation,
            draft=draft,
            seed=None,
        )
        if candidate.context_id != source.context_id:
            preflight_violations.append("candidate_context_id")
        if preflight_violations:
            raise self._emit_rejection(
                TransformationOperation.AUDIT,
                RejectionReason.RULE_RESULT_MISMATCH,
                family_id=family.family_id,
                rule_id=rule_id,
                rule_version=config.rule_version,
                details=tuple(sorted(set(preflight_violations))),
            )
        try:
            audit = rule.audit(
                source,
                source_representation,
                candidate,
                candidate_representation,
                draft,
            )
        except Exception as exc:
            raise self._emit_rejection(
                TransformationOperation.AUDIT,
                RejectionReason.RULE_EXECUTION_ERROR,
                family_id=family.family_id,
                rule_id=config.rule_id,
                rule_version=config.rule_version,
                details=("audit", type(exc).__name__),
            ) from exc
        if not isinstance(audit, TransformationAudit):
            raise self._emit_rejection(
                TransformationOperation.AUDIT,
                RejectionReason.RULE_RESULT_MISMATCH,
                family_id=family.family_id,
                rule_id=config.rule_id,
                rule_version=config.rule_version,
                details=("audit_type",),
            )
        violations: list[str] = []
        if (
            draft.rule_id != config.rule_id
            or draft.rule_version != config.rule_version
            or draft.family_id != config.family_id
        ):
            violations.append("draft_rule_identity")
        if audit.draft_id != draft.draft_id:
            violations.append("draft_id")
        if audit.family_id != config.family_id:
            violations.append("audit_family_id")
        if audit.rule_id != config.rule_id or audit.rule_version != config.rule_version:
            violations.append("audit_rule_identity")
        if audit.context_id != source.context_id or candidate.context_id != source.context_id:
            violations.append("context_id")
        if draft.context_id != source.context_id:
            violations.append("draft_context_id")
        if draft.source_theorem_ids != (source.theorem_id,):
            violations.append("draft_source_theorem_ids")
        if draft.source_representation_ids != (source_representation.representation_id,):
            violations.append("draft_source_representation_ids")
        if audit.candidate_theorem_id != candidate.theorem_id:
            violations.append("candidate_theorem_id")
        if audit.candidate_representation_id != candidate_representation.representation_id:
            violations.append("candidate_representation_id")
        if audit.candidate_code_hash != draft.candidate_code_hash:
            violations.append("candidate_code_hash")
        try:
            verify_transformation_audit_id(audit)
        except ValueError:
            violations.append("audit_id")
        if audit.recommended_quality_tier not in (
            QualityTier.PROVISIONAL,
            QualityTier.UNKNOWN,
        ):
            violations.append("self_promotion")
        if violations:
            raise self._emit_rejection(
                TransformationOperation.AUDIT,
                RejectionReason.RULE_RESULT_MISMATCH,
                family_id=family.family_id,
                rule_id=rule_id,
                rule_version=config.rule_version,
                details=tuple(sorted(violations)),
            )
        return audit
