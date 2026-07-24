"""Static LF-020 evidence-policy configuration invariants."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config import load_yaml_mapping
from leanfaith.evidence.certificates import ClaimAlignmentSpec
from leanfaith.evidence.config import (
    CounterexampleConfig,
    EvidenceSamplingConfig,
    TrainingEvidenceSample,
    load_evidence_configs_from_root,
)
from leanfaith.evidence.sampling import select_training_evidence_pairs
from leanfaith.schemas.ids import PAIR_PREFIX, make_id
from leanfaith.schemas.pair import PairRecord
from tests.unit.record_factories import pair_record

ROOT = Path(__file__).resolve().parents[2]


def _config(name: str) -> dict[str, object]:
    return load_yaml_mapping(ROOT / "configs" / "evidence" / name)


def test_portfolio_v1_is_versioned_ordered_and_admission_free() -> None:
    config = _config("portfolio_v1.yaml")
    assert config["schema_version"] == 1
    assert config["portfolio_id"] == "portfolio_v1"
    assert config["portfolio_version"] == "1.0.0"
    assert config["allow_sorry"] is False
    methods = config["methods"]
    assert isinstance(methods, list) and methods
    identifiers = [method["method_id"] for method in methods]
    orders = [method["order"] for method in methods]
    assert len(identifiers) == len(set(identifiers))
    assert orders == sorted(set(orders))
    for method in methods:
        assert method["timeout_seconds"] > 0


def test_counterexample_v1_keeps_negative_search_failures_unknown() -> None:
    config = _config("counterexample_v1.yaml")
    assert config["schema_version"] == 1
    assert config["scope"] == "decidable_bounded_fragments_only"
    policy = config["certificate_policy"]
    assert isinstance(policy, dict)
    assert policy["unsupported_is_not_negative"] is True
    assert policy["not_found_is_not_negative"] is True
    engines = config["engines"]
    assert isinstance(engines, list)
    native = next(engine for engine in engines if engine["engine_id"] == "native_decide_v1")
    assert native["may_support_gold_negative"] is False


def test_sampling_v1_is_bounded_and_nonexhaustive_for_training() -> None:
    config = _config("sampling_v1.yaml")
    assert config["schema_version"] == 1
    assert config["policy_id"] == "evidence_sampling_v1"
    training = config["training_sample"]
    assert isinstance(training, dict)
    assert 0 < training["fraction_per_stratum"] < 1
    assert training["minimum_per_stratum"] <= training["maximum_per_stratum"]
    failure = config["failure_policy"]
    assert isinstance(failure, dict)
    assert failure["proof_not_proved_is_unknown"] is True
    assert failure["counterexample_not_found_is_unknown"] is True


def test_strict_evidence_configs_load_and_hash() -> None:
    configs = load_evidence_configs_from_root(ROOT)
    assert configs.portfolio.config.method_version == "portfolio_v1@1.0.0"
    assert configs.counterexample.config.method_version == "counterexample_v1@1.0.0"
    assert len(configs.portfolio.config_hash) == 64
    assert len(configs.counterexample.config_hash) == 64
    assert len(configs.sampling.config_hash) == 64


def test_counterexample_config_rejects_enabled_native_decide() -> None:
    payload = _config("counterexample_v1.yaml")
    engines = payload["engines"]
    assert isinstance(engines, list)
    for engine in engines:
        if engine["engine_id"] == "native_decide_v1":
            engine["enabled"] = True
    with pytest.raises(ValidationError, match="native_decide"):
        CounterexampleConfig.model_validate(payload)


def test_sampling_config_rejects_intended_relation_stratum() -> None:
    payload = _config("sampling_v1.yaml")
    training = payload["training_sample"]
    assert isinstance(training, dict)
    training["strata"] = [*training["strata"], "intended_relation"]
    with pytest.raises(ValidationError, match="intended_relation"):
        EvidenceSamplingConfig.model_validate(payload)


def _small_sampling_config() -> EvidenceSamplingConfig:
    loaded = load_evidence_configs_from_root(ROOT).sampling.config
    payload = loaded.model_dump(mode="python")
    payload["training_sample"] = TrainingEvidenceSample(
        enabled=True,
        strategy="stratified_hash_v1",
        hash_seed="unit-test",
        fraction_per_stratum=0.25,
        minimum_per_stratum=0,
        maximum_per_stratum=10,
        strata=("pair_source",),
    )
    return EvidenceSamplingConfig.model_validate(payload)


def test_evidence_sampling_is_deterministic_and_uses_ceiling() -> None:
    config = _small_sampling_config()
    records = tuple(
        pair_record(
            pair_id=make_id(PAIR_PREFIX, {"sampling": index}),
            pair_source="source-a",
        )
        for index in range(5)
    )

    def key(pair: PairRecord) -> tuple[str, ...]:
        return (pair.pair_source,)

    selected = select_training_evidence_pairs(records, config=config, stratum_key=key)
    reversed_selected = select_training_evidence_pairs(
        reversed(records),
        config=config,
        stratum_key=key,
    )

    assert selected == reversed_selected
    assert len(selected) == 2


def test_evidence_sampling_rejects_duplicate_ids_and_bad_strata() -> None:
    config = _small_sampling_config()
    record = pair_record()
    with pytest.raises(ValueError, match="duplicate pair_id"):
        select_training_evidence_pairs(
            (record, record),
            config=config,
            stratum_key=lambda pair: (pair.pair_source,),
        )
    with pytest.raises(ValueError, match="stratum arity"):
        select_training_evidence_pairs(
            (record,),
            config=config,
            stratum_key=lambda _pair: (),
        )


def test_claim_alignment_indices_are_typed_and_injective() -> None:
    pair_id = pair_record().pair_id
    spec = ClaimAlignmentSpec(
        pair_id=pair_id,
        alignment_version="alignment_v1",
        template_id="alpha_identity_assumption_v1",
        binder_map={"binder:0": "binder:1"},
        premise_map={"premise:0": "premise:0"},
        conclusion_role_map={"A": "B"},
        direction="both",
    )
    assert spec.binder_map == {"binder:0": "binder:1"}

    with pytest.raises(ValidationError, match="binder:<ordinal>"):
        ClaimAlignmentSpec(
            **{
                **spec.model_dump(mode="python"),
                "binder_map": {"binder:0": "premise:0"},
            }
        )
    with pytest.raises(ValidationError, match="one-to-one"):
        ClaimAlignmentSpec(
            **{
                **spec.model_dump(mode="python"),
                "binder_map": {
                    "binder:0": "binder:1",
                    "binder:2": "binder:1",
                },
            }
        )
