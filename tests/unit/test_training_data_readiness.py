"""Fail-closed training-data readiness tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leanfaith.config.loading import load_config
from leanfaith.models.data_readiness import (
    GoldPartitionManifest,
    TrainingAuditRecord,
    TrainingDataReadinessPolicy,
    audit_training_data_readiness,
    render_training_data_readiness_markdown,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs/models/training_data_readiness_v1.yaml"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _fixture_policy(tmp_path: Path, *, pair_count: int = 8) -> Path:
    raw = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    raw["inputs"]["prevalence_frame"] = "frame.jsonl"
    raw["inputs"]["prevalence_human_labels"] = "prevalence_labels.jsonl"
    raw["inputs"]["training_inventory"] = "inventory.jsonl"
    raw["inputs"]["human_products"] = {
        product: f"human/{product}.json"
        for product in (
            "training_gold",
            "selection_gold",
            "calibration_gold",
            "final_human_test",
        )
    }
    raw["inputs"]["lf022_required_artifacts"] = ["lf022/sci.json", "lf022/open.json"]
    raw["prevalence"] = {
        "minimum_frame_items": 2,
        "maximum_frame_items": 4,
        "minimum_generator_families": 2,
        "minimum_human_terminal_label_fraction": 1.0,
    }
    raw["pilot"]["confirmatory_pair_count"] = pair_count
    raw["pilot"]["reduced_data_minimum_pair_count"] = 2
    raw["pilot"]["maximum_unique_variants_per_component_per_arm"] = 4
    raw["selection_gold_minimum_groups"] = {
        "faithful": 1,
        "unfaithful": 1,
        "per_included_relation_class": 1,
    }
    raw["negative_arms"] = {
        arm: {"G_rule": 0.25, "G_sci": 0.25, "G_open": 0.25, "G_real": 0.25}
        for arm in ("D0", "D1", "D2", "D3", "D4", "D5")
    }
    raw["full_arm_positive_mix"] = {
        "certified_positive": 0.5,
        "human_or_promoted_faithful_real": 0.25,
        "promoted_llm_equivalent": 0.25,
    }
    raw["family_controls"] = {
        "apply_caps_to_arms": [],
        "deterministic_family_fraction_of_all_negative": 1.0,
        "minimum_llm_proposer_families": 1,
        "maximum_one_llm_family_fraction_of_llm_negative": 1.0,
        "minimum_real_generator_families": 1,
        "maximum_one_real_family_fraction_of_real_negative": 1.0,
    }
    raw["reports"] = {"json_path": "report.json", "markdown_path": "report.md"}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _gold_manifest(
    partition: str,
    *,
    component_prefix: str,
) -> dict[str, object]:
    final = partition == "final_human_test"
    records: list[dict[str, object]] = []
    for index, (same_claim, relation) in enumerate(((True, "equivalent"), (False, "unrelated"))):
        records.append(
            {
                "record_id": f"{partition}-{index}",
                "split_component_id": f"{component_prefix}-{index}",
                "adjudicated": True,
                "same_claim": None if final else same_claim,
                "relation": None if final else relation,
                "label_bases": [] if final else ["human_adjudication"],
            }
        )
    return {
        "schema_version": 1,
        "partition": partition,
        "sealed": final,
        "labels_exposed_to_audit": not final,
        "distribution": ("compiling_real_outputs" if partition == "calibration_gold" else "mixed"),
        "records": records,
    }


def _ready_fixture(tmp_path: Path) -> Path:
    policy_path = _fixture_policy(tmp_path)
    frame = [
        {
            "decision": "REVIEW",
            "same_claim": None,
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "population_item": {"representative_family_id": family},
        }
        for family in ("f1", "f2")
    ]
    for index, record in enumerate(frame):
        record["frame_record_id"] = f"frame-{index}"
    _write_jsonl(tmp_path / "frame.jsonl", frame)
    _write_jsonl(
        tmp_path / "prevalence_labels.jsonl",
        [
            {
                "schema_version": 1,
                "frame_record_id": f"frame-{index}",
                "adjudicated": True,
                "label_basis": "human_adjudication",
                "same_claim": same_claim,
                "resolution_outcome": ("same_claim" if same_claim else "not_same_claim"),
            }
            for index, same_claim in enumerate((True, False))
        ],
    )
    _write_json(tmp_path / "lf022/sci.json", {"ready": True})
    _write_json(tmp_path / "lf022/open.json", {"ready": True})
    for index, partition in enumerate(
        ("training_gold", "selection_gold", "calibration_gold", "final_human_test")
    ):
        _write_json(
            tmp_path / f"human/{partition}.json",
            _gold_manifest(partition, component_prefix=f"gold-{index}"),
        )

    records: list[dict[str, object]] = []
    arms = ["D0", "D1", "D2", "D3", "D4", "D5"]
    positives = (
        ("certified_positive", "human_adjudication"),
        ("certified_positive", "certified_conservative_transformation"),
        ("human_or_promoted_faithful_real", "human_adjudication"),
        ("promoted_llm_equivalent", "human_promoted_llm_variant"),
    )
    negatives = (
        ("G_rule", None, "N01", "human_promoted_transformation"),
        ("G_sci", "p1", None, "human_adjudication"),
        ("G_open", "p2", None, "human_adjudication"),
        ("G_real", "r1", None, "human_adjudication"),
    )
    for index, (positive_source, basis) in enumerate(positives):
        records.append(
            {
                "schema_version": 1,
                "pair_id": f"positive-{index}",
                "split_component_id": f"train-pos-{index}",
                "same_claim": True,
                "relation": "equivalent",
                "arm_memberships": arms,
                "label_bases": [basis],
                "positive_source": positive_source,
                "negative_source": None,
                "source_family": None,
                "transform_family": None,
                "duplicate_of": None,
            }
        )
    for index, (source, family, transform, basis) in enumerate(negatives):
        records.append(
            {
                "schema_version": 1,
                "pair_id": f"negative-{index}",
                "split_component_id": f"train-neg-{index}",
                "same_claim": False,
                "relation": "unrelated",
                "arm_memberships": arms,
                "label_bases": [basis],
                "positive_source": None,
                "negative_source": source,
                "source_family": family,
                "transform_family": transform,
                "duplicate_of": None,
            }
        )
    _write_jsonl(tmp_path / "inventory.jsonl", records)
    return policy_path


def test_current_repository_is_honestly_not_ready() -> None:
    report = audit_training_data_readiness(
        repo_root=ROOT,
        loaded_policy=load_config(POLICY, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert report.prevalence.frame_item_count == 240
    assert report.prevalence.frame_adequate_for_annotation
    assert report.prevalence.human_terminal_label_count == 0
    assert report.prevalence.unresolved_review_count == 240
    assert not report.prevalence.prevalence_estimate_ready
    assert not report.training.confirmatory_ready
    assert not report.training.lf022_artifacts_present
    assert sum(product.present for product in report.training.human_products) == 0
    codes = {blocker.code for blocker in report.blockers}
    assert "PREVALENCE_HUMAN_LABELS_MISSING" in codes
    assert "LF022_ARTIFACTS_MISSING" in codes
    assert "TRAINING_INVENTORY_MISSING" in codes
    markdown = render_training_data_readiness_markdown(report)
    assert "Prevalence frame adequate for human annotation: **TRUE**" in markdown
    assert "Confirmatory flagship training/model selection ready: **FALSE**" in markdown


def test_ready_fixture_passes_confirmatory_audit_deterministically(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    loaded = load_config(policy_path, TrainingDataReadinessPolicy)
    first = audit_training_data_readiness(repo_root=tmp_path, loaded_policy=loaded)
    second = audit_training_data_readiness(repo_root=tmp_path, loaded_policy=loaded)
    assert first == second
    assert first.status == "READY"
    assert first.training_execution_authorized
    assert first.audit_id == second.audit_id
    assert all(result.ready for result in first.training.arm_results)


def test_compilation_llm_and_proof_search_labels_are_rejected(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    inventory = (tmp_path / "inventory.jsonl").read_text(encoding="utf-8").splitlines()
    raw = json.loads(inventory[0])
    raw["label_bases"] = ["compilation", "llm_agreement", "proof_search"]
    inventory[0] = json.dumps(raw, sort_keys=True)
    (tmp_path / "inventory.jsonl").write_text(
        "\n".join(inventory) + "\n",
        encoding="utf-8",
    )
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert report.training.unsafe_f1_label_count == 1
    assert report.training.forbidden_label_basis_counts == {
        "compilation": 1,
        "llm_agreement": 1,
        "proof_search": 1,
    }
    assert "UNSAFE_F1_LABEL_BASIS" in {blocker.code for blocker in report.blockers}


def test_reduced_data_mode_is_explicit_and_does_not_waive_integrity(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    loaded = load_config(policy_path, TrainingDataReadinessPolicy)
    confirmatory = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=loaded,
    )
    assert confirmatory.status == "READY"

    # Raise only the confirmatory scale target. The same 8-pair corpus becomes an
    # explicitly named reduced-data ablation, not a silent substitute.
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    raw["pilot"]["confirmatory_pair_count"] = 50000
    policy_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    loaded = load_config(policy_path, TrainingDataReadinessPolicy)
    without_flag = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=loaded,
    )
    with_flag = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=loaded,
        reduced_data_ablation=True,
    )
    assert without_flag.status == "NOT_READY"
    assert with_flag.status == "READY_REDUCED_DATA_ABLATION"


def test_human_products_are_component_disjoint_and_final_labels_stay_hidden(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    final_path = tmp_path / "human/final_human_test.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final["records"][0]["split_component_id"] = "gold-1-0"
    _write_json(final_path, final)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training.human_partitions_component_disjoint
    assert "HUMAN_PARTITION_ANCESTRY_OVERLAP" in {blocker.code for blocker in report.blockers}

    final["records"][0]["same_claim"] = True
    final["records"][0]["relation"] = "equivalent"
    final["records"][0]["label_bases"] = ["human_adjudication"]
    with pytest.raises(ValueError, match="cannot expose labels"):
        GoldPartitionManifest.model_validate(final)


def test_training_record_rejects_incoherent_class_fields() -> None:
    with pytest.raises(ValueError, match="same-claim records"):
        TrainingAuditRecord.model_validate(
            {
                "schema_version": 1,
                "pair_id": "x",
                "split_component_id": "g",
                "same_claim": True,
                "relation": "A_stronger",
                "arm_memberships": ["D0"],
                "label_bases": ["human_adjudication"],
                "positive_source": "certified_positive",
            }
        )
