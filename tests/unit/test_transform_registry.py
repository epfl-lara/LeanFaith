"""Strict LF-016 registry loading, dispatch, and deterministic execution."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas import (
    Applicability,
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
    make_id,
)
from leanfaith.transforms import (
    LoadedTransformationRegistry,
    RegistryIntegrityError,
    RejectionReason,
    TransformationExecution,
    TransformationExecutionFailed,
    TransformationFamilyConfig,
    TransformationRegistry,
    TransformationRegistryConfig,
    TransformationRejected,
    build_transformation_audit,
    build_variant_draft,
    expected_transformation_attempt_id,
    expected_variant_draft_id,
    load_transformation_registry,
    verify_transformation_attempt_id,
    verify_variant_draft_id,
)
from tests.unit.record_factories import (
    ANC_B,
    CTX_ID,
    THM_B,
    representation_record,
    theorem_record,
)


def _runtime_with_available_p01() -> tuple[TransformationRegistry, Any]:
    loaded = load_transformation_registry()
    payload = loaded.config.model_dump(mode="python")
    for family in payload["families"]:
        for rule in family["rules"]:
            if rule["rule_id"] == "p01_alpha":
                rule["implementation_status"] = "available"
    config = TransformationRegistryConfig.model_validate(payload)
    registry_config_hash = hash_canonical(config.model_dump(mode="json"))
    registry_hash = hash_canonical(
        {
            "schema": "leanfaith_transformation_registry_effective_v1",
            "registry": config.model_dump(mode="json"),
            "profile": loaded.profile.model_dump(mode="json"),
            "promotion_policy_hash": loaded.promotion_policy_hash,
        }
    )
    loaded = loaded.model_copy(
        update={
            "config": config,
            "registry_config_hash": registry_config_hash,
            "registry_hash": registry_hash,
        }
    )
    rule = _Rule(registry_hash=loaded.registry_hash)
    runtime = TransformationRegistry(loaded)
    runtime.register(rule)
    return runtime, rule


def _loaded_with_rule_status(
    rule_id: str,
    implementation_status: str,
) -> LoadedTransformationRegistry:
    """Return a self-consistently rehashed registry for status-policy tests."""

    loaded = load_transformation_registry()
    payload = loaded.config.model_dump(mode="python")
    for family in payload["families"]:
        for rule in family["rules"]:
            if rule["rule_id"] == rule_id:
                rule["implementation_status"] = implementation_status
    config = TransformationRegistryConfig.model_validate(payload)
    registry_config_hash = hash_canonical(config.model_dump(mode="json"))
    registry_hash = hash_canonical(
        {
            "schema": "leanfaith_transformation_registry_effective_v1",
            "registry": config.model_dump(mode="json"),
            "profile": loaded.profile.model_dump(mode="json"),
            "promotion_policy_hash": loaded.promotion_policy_hash,
        }
    )
    return LoadedTransformationRegistry.model_validate(
        {
            **loaded.model_dump(mode="python"),
            "config": config,
            "registry_config_hash": registry_config_hash,
            "registry_hash": registry_hash,
        }
    )


class _Rule:
    rule_id = "p01_alpha"
    rule_version = "1.0.0"
    family_id = "p01_alpha"
    polarity = Polarity.POSITIVE
    implementation_key = "p01_alpha"

    def __init__(
        self,
        *,
        registry_hash: str,
        applicable: bool = True,
        malicious_source: bool = False,
    ) -> None:
        self.registry_hash = registry_hash
        self.applicable = applicable
        self.malicious_source = malicious_source
        self.assess_calls = 0
        self.generate_calls = 0
        self.audit_calls = 0

    def assess(self, theorem: Any, representation: Any) -> Applicability:
        self.assess_calls += 1
        return Applicability(
            applicable=self.applicable,
            reason_codes=() if self.applicable else ("no_eligible_binder",),
        )

    def generate(self, theorem: Any, representation: Any, seed: int):
        self.generate_calls += 1
        source_theorem = (
            make_id("thm", {"malicious": 1}) if self.malicious_source else theorem.theorem_id
        )
        source_representation = (
            make_id("repr", {"malicious": 1})
            if self.malicious_source
            else representation.representation_id
        )
        return (
            build_variant_draft(
                source_theorem_ids=(source_theorem,),
                source_representation_ids=(source_representation,),
                context_id=theorem.context_id,
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                family_id=self.family_id,
                seed=seed,
                candidate_code=f"theorem transformed_{seed} : True := by trivial",
                intended_relation=IntendedRelation.EQUIVALENT,
                candidate_pool="deterministic_positive",
                transformation_trace=({"operation": "rename", "seed": seed},),
                generation_config_hash=self.registry_hash,
            ),
        )

    def audit(
        self,
        source: Any,
        source_representation: Any,
        candidate: Any,
        candidate_representation: Any,
        draft: Any,
    ):
        self.audit_calls += 1
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(applicable=True, reason_codes=()),
            audit_config_hash="b" * 64,
            recommended_validation_status=ValidationStatus.ELABORATES,
            recommended_quality_tier=QualityTier.PROVISIONAL,
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
        )


def test_repository_registry_loads_and_binds_policy() -> None:
    loaded = load_transformation_registry()
    replay = load_transformation_registry()

    assert len(loaded.config.families) == 9
    assert loaded.config.rule_count == 9
    assert loaded.profile.disabled_family_ids == ("p00_cosmetic",)
    assert len(loaded.registry_hash) == 64
    assert loaded.registry_hash == replay.registry_hash


def test_pending_rule_rejects_before_implementation_runs() -> None:
    loaded = _loaded_with_rule_status("p01_alpha", "pending")
    runtime = TransformationRegistry(loaded)
    rule = _Rule(registry_hash=loaded.registry_hash)

    with pytest.raises(TransformationRejected) as caught:
        runtime.register(rule)

    assert caught.value.event.reason_code == RejectionReason.IMPLEMENTATION_UNAVAILABLE
    assert rule.assess_calls == rule.generate_calls == 0


def test_duplicate_runtime_registration_fails_closed() -> None:
    runtime, rule = _runtime_with_available_p01()

    with pytest.raises(TransformationRejected) as caught:
        runtime.register(rule)

    assert caught.value.event.reason_code == RejectionReason.RULE_ALREADY_REGISTERED


def test_runtime_object_missing_protocol_method_is_rejected() -> None:
    loaded = load_transformation_registry()
    payload = loaded.config.model_dump(mode="python")
    for family in payload["families"]:
        for rule in family["rules"]:
            if rule["rule_id"] == "p01_alpha":
                rule["implementation_status"] = "available"
    config = TransformationRegistryConfig.model_validate(payload)
    registry_config_hash = hash_canonical(config.model_dump(mode="json"))
    registry_hash = hash_canonical(
        {
            "schema": "leanfaith_transformation_registry_effective_v1",
            "registry": config.model_dump(mode="json"),
            "profile": loaded.profile.model_dump(mode="json"),
            "promotion_policy_hash": loaded.promotion_policy_hash,
        }
    )
    loaded = loaded.model_copy(
        update={
            "config": config,
            "registry_config_hash": registry_config_hash,
            "registry_hash": registry_hash,
        }
    )

    class IncompleteRule:
        rule_id = "p01_alpha"
        rule_version = "1.0.0"
        family_id = "p01_alpha"
        polarity = Polarity.POSITIVE
        implementation_key = "p01_alpha"

        def assess(self, theorem: Any, representation: Any) -> Applicability:
            return Applicability(applicable=True, reason_codes=())

        def generate(self, theorem: Any, representation: Any, seed: int):
            return ()

    runtime = TransformationRegistry(loaded)
    with pytest.raises(TransformationRejected) as caught:
        runtime.register(IncompleteRule())  # type: ignore[arg-type]

    assert caught.value.event.reason_code == RejectionReason.RULE_PROTOCOL_MISMATCH


def test_disabled_and_unlisted_rules_fail_closed() -> None:
    loaded = load_transformation_registry()
    runtime = TransformationRegistry(loaded)
    disabled = _Rule(registry_hash=loaded.registry_hash)
    disabled.rule_id = "p00_cosmetic"
    disabled.family_id = "p00_cosmetic"
    disabled.implementation_key = "p00_cosmetic_fixture"

    with pytest.raises(TransformationRejected) as disabled_error:
        runtime.register(disabled)
    assert disabled_error.value.event.reason_code == RejectionReason.FAMILY_DISABLED

    disabled.rule_id = "not_registered"
    disabled.family_id = "not_registered"
    with pytest.raises(TransformationRejected) as unknown_error:
        runtime.register(disabled)
    assert unknown_error.value.event.reason_code == RejectionReason.FAMILY_UNLISTED


def test_execute_records_non_applicability_without_calling_generate() -> None:
    runtime, rule = _runtime_with_available_p01()
    rule.applicable = False

    execution = runtime.execute("p01_alpha", theorem_record(), representation_record(), 7)

    assert execution.drafts == ()
    assert execution.attempt.terminal_outcome == "not_applicable"
    assert rule.assess_calls == 1
    assert rule.generate_calls == 0
    verify_transformation_attempt_id(execution.attempt)


def test_assessment_exception_carries_persistent_terminal_attempt() -> None:
    runtime, rule = _runtime_with_available_p01()

    def broken_assess(theorem: Any, representation: Any) -> Applicability:
        raise ValueError("fixture assessment failure")

    rule.assess = broken_assess
    with pytest.raises(TransformationExecutionFailed) as caught:
        runtime.execute("p01_alpha", theorem_record(), representation_record(), 7)

    execution = caught.value.execution
    assert execution.drafts == ()
    assert execution.attempt.applicability is None
    assert execution.attempt.terminal_outcome == "generation_error"
    assert caught.value.rejection_event.reason_code == RejectionReason.RULE_EXECUTION_ERROR
    verify_transformation_attempt_id(execution.attempt)


def test_generation_timeout_carries_infrastructure_attempt() -> None:
    runtime, rule = _runtime_with_available_p01()

    def timed_out(theorem: Any, representation: Any, seed: int):
        raise TimeoutError("fixture timeout")

    rule.generate = timed_out
    with pytest.raises(TransformationExecutionFailed) as caught:
        runtime.execute("p01_alpha", theorem_record(), representation_record(), 7)

    execution = caught.value.execution
    assert execution.attempt.applicability is not None
    assert execution.attempt.terminal_outcome == "infrastructure_error"
    assert caught.value.rejection_event.reason_code == RejectionReason.RULE_EXECUTION_ERROR
    verify_transformation_attempt_id(execution.attempt)


def test_execution_model_rejects_draft_attempt_lineage_mismatch() -> None:
    runtime, _ = _runtime_with_available_p01()
    execution = runtime.execute("p01_alpha", theorem_record(), representation_record(), 7)
    payload = execution.model_dump(mode="python")
    payload["attempt"]["seed"] = 99
    changed_attempt = execution.attempt.model_copy(update={"seed": 99})
    payload["attempt"]["attempt_id"] = expected_transformation_attempt_id(changed_attempt)

    with pytest.raises(ValidationError, match="lineage differs"):
        TransformationExecution.model_validate(payload)


def test_same_seed_replays_and_changed_seed_changes_draft() -> None:
    runtime, _ = _runtime_with_available_p01()
    theorem = theorem_record()
    representation = representation_record()

    first = runtime.execute("p01_alpha", theorem, representation, 7)
    replay = runtime.execute("p01_alpha", theorem, representation, 7)
    changed = runtime.execute("p01_alpha", theorem, representation, 8)

    assert first.model_dump(mode="json") == replay.model_dump(mode="json")
    assert first.drafts[0].draft_id != changed.drafts[0].draft_id
    assert first.attempt.attempt_id != changed.attempt.attempt_id


def test_draft_identity_excludes_metadata_but_includes_candidate_content() -> None:
    runtime, _ = _runtime_with_available_p01()
    original = runtime.generate("p01_alpha", theorem_record(), representation_record(), 7)[0]
    metadata_change = original.model_copy(update={"metadata": {"review_note": "mutable"}})
    content_change = original.model_copy(
        update={
            "candidate_code": "theorem transformed_7 : False := by sorry",
            "candidate_code_hash": "0" * 64,
        }
    )

    assert expected_variant_draft_id(metadata_change) == original.draft_id
    verify_variant_draft_id(metadata_change)
    assert expected_variant_draft_id(content_change) != original.draft_id
    with pytest.raises(ValueError, match="draft_id mismatch"):
        verify_variant_draft_id(content_change)


def test_malicious_rule_cannot_switch_source_lineage() -> None:
    runtime, rule = _runtime_with_available_p01()
    rule.malicious_source = True

    with pytest.raises(TransformationExecutionFailed) as caught:
        runtime.execute("p01_alpha", theorem_record(), representation_record(), 7)

    assert caught.value.rejection_event.reason_code == RejectionReason.RULE_RESULT_MISMATCH
    assert "source_theorem_ids" in caught.value.rejection_event.details
    assert caught.value.execution.attempt.terminal_outcome == "generation_error"


def test_mixed_theorem_representation_rejected_before_rule() -> None:
    runtime, rule = _runtime_with_available_p01()
    mismatched = representation_record(theorem_id=THM_B)

    with pytest.raises(TransformationRejected) as caught:
        runtime.execute("p01_alpha", theorem_record(), mismatched, 7)

    assert caught.value.event.reason_code == RejectionReason.INPUT_LINEAGE_MISMATCH
    assert rule.assess_calls == rule.generate_calls == 0


def test_runtime_rule_metadata_drift_is_rejected_before_rule() -> None:
    runtime, rule = _runtime_with_available_p01()
    rule.implementation_key = "mutated_after_registration"

    with pytest.raises(TransformationRejected) as caught:
        runtime.execute("p01_alpha", theorem_record(), representation_record(), 7)

    assert caught.value.event.reason_code == RejectionReason.RULE_METADATA_MISMATCH
    assert caught.value.event.details == ("implementation_key",)
    assert rule.assess_calls == rule.generate_calls == 0


def test_audit_is_rule_bound_and_never_promotes() -> None:
    runtime, rule = _runtime_with_available_p01()
    source = theorem_record()
    source_representation = representation_record()
    draft = runtime.generate("p01_alpha", source, source_representation, 7)[0]
    candidate = theorem_record(
        theorem_id=THM_B,
        ancestry_id=ANC_B,
        root_ancestry_ids=(ANC_B,),
        declaration_name="candidate",
        context_id=CTX_ID,
    )
    candidate_representation_id = make_id("repr", {"candidate": THM_B})
    candidate_representation = representation_record(
        representation_id=candidate_representation_id,
        theorem_id=THM_B,
    )

    audit = runtime.audit(
        "p01_alpha",
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.candidate_theorem_id == THM_B
    assert rule.audit_calls == 1


def test_audit_rejects_tampered_draft_before_rule() -> None:
    runtime, rule = _runtime_with_available_p01()
    source = theorem_record()
    source_representation = representation_record()
    draft = runtime.generate("p01_alpha", source, source_representation, 7)[0]
    tampered = draft.model_copy(update={"generation_config_hash": "0" * 64})
    candidate = theorem_record(
        theorem_id=THM_B,
        ancestry_id=ANC_B,
        root_ancestry_ids=(ANC_B,),
        declaration_name="candidate",
        context_id=CTX_ID,
    )
    candidate_representation = representation_record(
        representation_id=make_id("repr", {"candidate": THM_B}),
        theorem_id=THM_B,
    )

    with pytest.raises(TransformationRejected) as caught:
        runtime.audit(
            "p01_alpha",
            source,
            source_representation,
            candidate,
            candidate_representation,
            tampered,
        )

    assert caught.value.event.reason_code == RejectionReason.RULE_RESULT_MISMATCH
    assert {"draft_id", "generation_config_hash"} <= set(caught.value.event.details)
    assert rule.audit_calls == 0


def test_rule_config_rejects_import_path_implementation_key() -> None:
    loaded = load_transformation_registry()
    payload = loaded.config.families[0].model_dump(mode="python")
    payload["rules"][0]["implementation_key"] = "package.module.Rule"

    with pytest.raises(ValidationError, match="implementation_key"):
        TransformationFamilyConfig.model_validate(payload)


@pytest.mark.parametrize("status", ["silver", "gold_promoted"])
def test_unbound_promoted_family_status_rejected(status: str) -> None:
    loaded = load_transformation_registry()
    payload = loaded.config.families[0].model_dump(mode="python")
    payload["status"] = status

    with pytest.raises(ValidationError):
        TransformationFamilyConfig.model_validate(payload)


def test_registry_effective_hash_changes_with_profile() -> None:
    loaded = load_transformation_registry()
    modified = loaded.profile.model_copy(update={"profile_version": "1.0.1"})

    assert modified.model_dump(mode="json") != loaded.profile.model_dump(mode="json")
    # Registry hashing is performed by the strict loader; this guards against
    # accidentally treating just the registry YAML hash as the effective ID.
    assert loaded.registry_hash != loaded.registry_config_hash


def test_loaded_registry_rejects_tampered_effective_hash() -> None:
    loaded = load_transformation_registry()
    payload = loaded.model_dump(mode="python")
    payload["registry_hash"] = "0" * 64

    with pytest.raises(ValidationError, match="registry_hash"):
        LoadedTransformationRegistry.model_validate(payload)


def test_loader_rejects_policy_status_drift(tmp_path: Path) -> None:
    root = tmp_path
    (root / "configs/transformations").mkdir(parents=True)
    (root / "policies").mkdir()
    (root / "PLAN.md").write_text("# fixture\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    repo = load_transformation_registry()
    shutil.copy(repo.registry_path, root / "configs/transformations/registry.yaml")
    shutil.copy(repo.profile_path, root / "configs/transformations/v1.yaml")
    policy_text = repo.promotion_policy_path.read_text(encoding="utf-8").replace(
        "status: active_internal_research",
        "status: authored_pending_gate_0_review",
    )
    (root / "policies/transformation_promotion_v1.yaml").write_text(
        policy_text,
        encoding="utf-8",
    )

    with pytest.raises(RegistryIntegrityError, match="not active"):
        load_transformation_registry(root)


def test_mismatched_runtime_implementation_key_is_rejected() -> None:
    runtime, _rule = _runtime_with_available_p01()
    duplicate = _Rule(registry_hash=runtime.registry_hash)
    duplicate.implementation_key = "wrong_key"

    with pytest.raises(TransformationRejected) as caught:
        runtime.register(duplicate)

    assert caught.value.event.reason_code == RejectionReason.RULE_METADATA_MISMATCH
    assert "implementation_key" in caught.value.event.details
