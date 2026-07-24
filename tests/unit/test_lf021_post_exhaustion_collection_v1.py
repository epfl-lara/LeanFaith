from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.generation import post_exhaustion_collection_v1 as collection
from leanfaith.generation import post_exhaustion_extension as extension
from leanfaith.generation import tranche_expansion as tranche
from tests.unit.test_lf021_post_exhaustion_extension import _synthetic_context

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs/generation/lf021_post_exhaustion_collection_v1.yaml"
FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)


def _write_extension_decision(
    *,
    path: Path,
    decision: extension.PostExhaustionExtensionDecisionV1,
) -> Path:
    path.write_bytes(canonical_json_bytes(decision.model_dump(mode="json")))
    return path


def test_policy_is_local_and_execution_disabled() -> None:
    policy = collection.load_post_exhaustion_collection_policy_v1(POLICY).config
    assert policy.required_families == FAMILIES
    assert policy.required_transport == "local"
    assert policy.future_collector_schema_version == 6
    assert policy.required_future_postprocess_schema_version == 7
    assert not policy.collector_v5_directly_compatible
    assert not policy.postprocess_v6_directly_compatible
    assert policy.config_plan_adapter_only
    assert not policy.execution_enabled
    assert not policy.semantic_labels_inspected
    assert not policy.semantic_labels_created
    assert not policy.supervision_eligible
    assert not policy.gate_5g_credit_claimed
    assert not policy.gate_5_closed


def test_collect_next_decision_creates_exact_reviewed_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, activation, _, _ = _synthetic_context(tmp_path, monkeypatch)
    decision = extension.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=(),
    )
    decision_path = _write_extension_decision(
        path=tmp_path / "decision.json",
        decision=decision,
    )
    run = collection.write_reviewed_extension_collection_authorization_v1(
        repo_root=ROOT,
        authorization_policy_path=POLICY,
        extension_decision_path=decision_path,
        output_root=tmp_path / "authorizations",
    )
    record = run.authorization
    assert record.authorized_tranche.tranche_id == "algebra_s6"
    assert record.authorized_tranche.order == 12
    assert record.extension_prefix_length == 0
    assert tuple(item.family_id for item in record.family_pins) == FAMILIES
    assert not record.collector_v5_compatible
    assert not record.postprocess_v6_compatible
    assert record.config_plan_adapter_only
    assert not record.executable_collection_adapter_available
    assert not record.semantic_labels_inspected
    assert not record.semantic_labels_created
    assert not record.supervision_eligible
    assert not record.gate_5g_credit_claimed
    assert not record.gate_5_closed

    replayed = collection.load_verified_reviewed_extension_collection_authorization_v1(
        repo_root=ROOT,
        authorization_policy_path=POLICY,
        authorization_path=run.path,
    )
    assert replayed.authorization == record
    assert replayed.binding == run.binding


def test_preferred_stop_requires_exact_ordered_authorization_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, activation, extension_paths, _ = _synthetic_context(tmp_path, monkeypatch)
    authorization_paths: list[Path] = []
    for prefix_length in range(2):
        decision = extension.evaluate_post_exhaustion_extension(
            repo_root=ROOT,
            loaded_policy=loaded,
            activation_v2_decision_path=activation,
            extension_observed_manifests=extension_paths[:prefix_length],
        )
        decision_path = _write_extension_decision(
            path=tmp_path / f"decision-{prefix_length}.json",
            decision=decision,
        )
        run = collection.write_reviewed_extension_collection_authorization_v1(
            repo_root=ROOT,
            authorization_policy_path=POLICY,
            extension_decision_path=decision_path,
            output_root=tmp_path / "authorizations",
        )
        authorization_paths.append(run.path)

    stop = extension.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=extension_paths[:2],
    )
    stop_path = _write_extension_decision(
        path=tmp_path / "preferred-stop.json",
        decision=stop,
    )
    verified = collection.verify_extension_collection_authorizations_v1(
        repo_root=ROOT,
        policy_path=POLICY,
        extension_stop_decision_path=stop_path,
        authorization_paths=tuple(authorization_paths),
    )
    assert [item.authorized_tranche.tranche_id for item in verified.records] == [
        "algebra_s6",
        "cross_domain_s6",
    ]
    assert verified.postprocess_observations == stop.extension_observations
    assert len(verified.bindings) == 2

    with pytest.raises(
        collection.PostExhaustionCollectionV1Error,
        match="authorization sequence differs",
    ):
        collection.verify_extension_collection_authorizations_v1(
            repo_root=ROOT,
            policy_path=POLICY,
            extension_stop_decision_path=stop_path,
            authorization_paths=tuple(reversed(authorization_paths)),
        )


def test_stop_decision_cannot_authorize_another_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, activation, extension_paths, _ = _synthetic_context(tmp_path, monkeypatch)
    stop = extension.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=extension_paths[:2],
    )
    stop_path = _write_extension_decision(
        path=tmp_path / "preferred-stop.json",
        decision=stop,
    )
    with pytest.raises(
        collection.PostExhaustionCollectionV1Error,
        match="does not authorize extension collection",
    ):
        collection.review_extension_collect_next_decision_v1(
            repo_root=ROOT,
            authorization_policy_path=POLICY,
            extension_decision_path=stop_path,
        )


def test_config_plan_adapter_is_exact_and_cannot_execute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_observation_loader = tranche.load_postprocess_observation
    loaded, activation, _, _ = _synthetic_context(tmp_path, monkeypatch)
    synthetic_observation_loader = tranche.load_postprocess_observation

    def load_synthetic_or_real_observation(**kwargs: object) -> tranche.LoadedObservation:
        try:
            return synthetic_observation_loader(**kwargs)
        except KeyError:
            return original_observation_loader(**kwargs)

    monkeypatch.setattr(
        tranche,
        "load_postprocess_observation",
        load_synthetic_or_real_observation,
    )
    decision = extension.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=(),
    )
    decision_path = _write_extension_decision(
        path=tmp_path / "decision.json",
        decision=decision,
    )
    authorization = collection.write_reviewed_extension_collection_authorization_v1(
        repo_root=ROOT,
        authorization_policy_path=POLICY,
        extension_decision_path=decision_path,
        output_root=tmp_path / "authorizations",
    )
    run = collection.write_post_exhaustion_collection_config_plan_v1(
        repo_root=ROOT,
        authorization_policy_path=POLICY,
        authorization_path=authorization.path,
        frozen_at=datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC),
        output_root=tmp_path / "plans",
    )
    assert run.config.tranche_id == "algebra_s6"
    assert run.config.authorization_id == authorization.authorization.authorization_id
    assert tuple(item.family_id for item in run.config.families) == FAMILIES
    assert [item.seeds for item in run.config.families] == [(36,), (6,), (6,)]
    assert run.plan.problem_count == 40
    assert run.plan.expected_candidate_count == 120
    assert len(run.plan.invocations) == 120
    assert run.plan.planning_only
    assert not run.config.execution_enabled
    assert not run.plan.execution_enabled
    assert not run.plan.actual_collection_performed
    assert not run.config.semantic_labels_created
    assert not run.plan.semantic_labels_created
    assert not run.config.supervision_eligible
    assert not run.plan.supervision_eligible
    assert not run.config.gate_5g_credit_claimed
    assert not run.plan.gate_5g_credit_claimed


def test_legacy_collectors_remain_incompatible() -> None:
    assert not hasattr(collection, "execute_post_exhaustion_collection_v1")
    assert not hasattr(collection, "postprocess_post_exhaustion_collection_v1")
