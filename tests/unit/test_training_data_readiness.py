"""Fail-closed training-data readiness tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.models.data_readiness import (
    GoldPartitionManifest,
    TrainingAuditRecord,
    TrainingDataReadinessPolicy,
    annotation_content_sha256,
    audit_training_data_readiness,
    build_operator_attested_adjudication_record,
    render_training_data_readiness_markdown,
)
from leanfaith.schemas import make_id
from leanfaith.schemas.annotation import AnnotationRecord

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs/models/training_data_readiness_v1.yaml"
_OPERATOR_KEY = b"readiness-fixture-operator-key-v1!" * 2


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
    raw["artifact_class"] = "test_fixture"
    raw["inputs"]["prevalence_frame"] = "frame.jsonl"
    raw["inputs"]["prevalence_human_labels"] = "prevalence_labels.jsonl"
    raw["inputs"]["training_inventory"] = "inventory.jsonl"
    raw["inputs"]["training_ambiguity_inventory"] = "ambiguity_inventory.jsonl"
    raw["inputs"]["generator_holdout_manifest"] = "generator_holdout.json"
    raw["inputs"]["human_products"] = {
        product: f"human/{product}.json"
        for product in (
            "training_gold",
            "selection_gold",
            "calibration_gold",
            "final_human_test",
        )
    }
    raw["inputs"]["lf022_required_artifacts"] = {
        "G_sci": "lf022/sci.json",
        "G_open": "lf022/open.json",
    }
    raw["inputs"]["lineage"] = {
        "theorem_records": "lineage/theorems.jsonl",
        "pair_records": "lineage/pairs.jsonl",
        "resolved_labels": "lineage/labels.jsonl",
        "evidence_records": "lineage/evidence.jsonl",
        "promotion_decisions": "lineage/promotions.jsonl",
        "annotation_records": "lineage/annotations.jsonl",
        "adjudication_records": "lineage/adjudications.jsonl",
        "human_authentication_key": "lineage/operator.key",
        "allow_test_fixture_human_provenance": True,
        "split_assignments": "lineage/split_assignments.jsonl",
        "source_manifests": ["lineage/source.json"],
    }
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
    raw["statistical_adequacy"]["final_human_test_minimum_groups"] = 5
    raw["statistical_adequacy"]["calibration_gold_minimum_groups"] = 5
    raw["statistical_adequacy"]["calibration_design_method"] = "fixture_calibration_design_v1"
    raw["statistical_adequacy"]["final_design_method"] = "fixture_final_design_v1"
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


def _component_id(ancestry_id: str) -> str:
    return "split-component:" + hash_canonical(
        {
            "schema": "leanfaith_split_component_v1",
            "split_group_ids": [ancestry_id],
        }
    )


def _write_lf022_manifest(
    tmp_path: Path,
    source: str,
    pair_families: dict[str, str],
) -> set[str]:
    stem = "sci" if source == "G_sci" else "open"
    ordered_pairs = sorted(pair_families)
    payloads: dict[str, list[dict[str, object]]] = {
        "variants": [
            {
                "variant_id": make_id("var", {"lf022": stem, "index": index}),
                "validation_status": "elaborates",
            }
            for index in range(len(ordered_pairs))
        ],
        "pairs": [
            {
                "pair_id": pair_id,
                "negative_source": source,
                "proposer_family": pair_families[pair_id],
            }
            for pair_id in ordered_pairs
        ],
        "evidence": [
            {
                "evidence_id": make_id("ev", {"lf022": stem, "index": index}),
                "target_id": pair_id,
                "status": "success",
            }
            for index, pair_id in enumerate(ordered_pairs)
        ],
        "resolved_labels": [
            {
                "label_id": make_id("lbl", {"lf022": stem, "index": index}),
                "target_id": pair_id,
                "train_eligibility": True,
                "quality_tier": "silver_consensus",
                "resolution_outcome": "not_same_claim",
            }
            for index, pair_id in enumerate(ordered_pairs)
        ],
        "promotions": [
            {"pair_id": pair_id, "promoted": True, "promotion_status": "silver"}
            for pair_id in ordered_pairs
        ],
    }
    artifacts: dict[str, dict[str, object]] = {}
    for name, records in payloads.items():
        relative = f"lf022/{stem}_{name}.jsonl"
        path = tmp_path / relative
        _write_jsonl(path, records)
        artifacts[name] = {
            "path": relative,
            "sha256": hash_file(path),
            "record_count": len(records),
        }
    _write_json(
        tmp_path / f"lf022/{stem}.json",
        {
            "schema_version": 1,
            "negative_source": source,
            "artifact_class": "production",
            "promotion_status": "silver",
            "variant_count": len(ordered_pairs),
            "pair_count": len(ordered_pairs),
            "evidence_count": len(ordered_pairs),
            "resolved_label_count": len(ordered_pairs),
            "promoted_record_count": len(ordered_pairs),
            "promoted_pair_ids": ordered_pairs,
            "proposer_family_counts": dict(
                sorted(
                    {
                        family: sum(value == family for value in pair_families.values())
                        for family in set(pair_families.values())
                    }.items()
                )
            ),
            "artifacts": artifacts,
        },
    )
    return set(ordered_pairs)


def _write_lineage(
    tmp_path: Path,
    pair_specs: dict[str, dict[str, object]],
    inventory_records: list[dict[str, object]],
) -> dict[str, dict[str, str | list[str]]]:
    ctx = f"ctx:{'0' * 64}"
    theorems: list[dict[str, object]] = []
    pairs: list[dict[str, object]] = []
    labels: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    annotations: list[dict[str, object]] = []
    adjudications: list[dict[str, object]] = []
    assignments: list[dict[str, object]] = []
    promotion_ids: set[str] = set()
    lineage: dict[str, dict[str, str | list[str]]] = {}
    for pair_index, pair_id in enumerate(sorted(pair_specs)):
        spec = pair_specs[pair_id]
        same_claim = spec["same_claim"]
        relation = str(spec["relation"])
        basis = str(spec["label_basis"])
        sealed_final = bool(spec.get("sealed_final", False))
        ancestry_id = make_id("anc", {"pair": pair_id})
        component_id = _component_id(ancestry_id)
        theorem_a = make_id("thm", {"pair": pair_id, "side": "A"})
        theorem_b = make_id("thm", {"pair": pair_id, "side": "B"})
        for side, theorem_id in (("A", theorem_a), ("B", theorem_b)):
            name = f"fixture_{pair_index}_{side.lower()}"
            theorems.append(
                {
                    "schema_version": 1,
                    "theorem_id": theorem_id,
                    "ancestry_id": ancestry_id,
                    "root_ancestry_ids": [ancestry_id],
                    "parent_theorem_ids": [],
                    "source": "fixture",
                    "source_revision": "r1",
                    "context_id": ctx,
                    "declaration_kind": "theorem",
                    "declaration_name": name,
                    "proof_stripped_declaration": f"theorem {name} : True := sorry",
                    "is_proposition": True,
                    "elaboration_status": "elaborates_with_placeholder",
                    "statement_content_hash": hash_canonical(
                        {"pair": pair_id, "side": side, "signature": "True"}
                    ),
                }
            )
        label_id = make_id("lbl", {"pair": pair_id})
        certified = basis == "certified_conservative_transformation"
        evidence_ids: list[str] = []
        adjudication_id = ""
        if certified:
            evidence_id = make_id("ev", {"pair": pair_id, "audit": True})
            evidence_ids.append(evidence_id)
            evidence.append(
                {
                    "schema_version": 2,
                    "evidence_id": evidence_id,
                    "target_kind": "lean_pair",
                    "target_id": pair_id,
                    "kind": "transformation_audit",
                    "status": "success",
                    "value": {
                        "kind": "audit",
                        "checks": {"certificate_replayed": True},
                        "violation_codes": [],
                    },
                    "method_version": "fixture_transform_audit_v1",
                    "created_at": "2026-07-28T00:00:00Z",
                    "metadata": {"family_id": "p01_alpha"},
                }
            )
        elif not sealed_final:
            answer = (
                "same_claim"
                if same_claim is True
                else "not_same_claim"
                if same_claim is False
                else "ambiguous"
            )
            annotation_ids: list[str] = []
            pair_annotations: list[dict[str, object]] = []
            for annotator_index in range(2):
                annotation_id = make_id(
                    "ann",
                    {"pair": pair_id, "annotator": annotator_index},
                )
                annotation_ids.append(annotation_id)
                pair_annotations.append(
                    {
                        "schema_version": 2,
                        "annotation_id": annotation_id,
                        "target_kind": "lean_pair",
                        "target_id": pair_id,
                        "annotator_id": f"expert-{annotator_index}",
                        "round_id": "fixture-round-v1",
                        "same_claim": answer,
                        "relation": relation,
                        "error_types": [],
                        "confidence": 5,
                        "rationale": "" if same_claim is True else "fixture adjudication",
                        "reference_issue": "none",
                        "created_at": "2026-07-28T00:00:00Z",
                        "metadata": {
                            "campaign_id": "fixture-campaign-v1",
                            "annotator_slot": (f"independent_annotator_{annotator_index + 1}"),
                            "source_frame_record_id": spec.get(
                                "source_frame_record_id",
                                f"non-prevalence:{pair_id}",
                            ),
                            "import_role": "raw_annotation_test_fixture",
                            "human_assignment_id": (
                                f"fixture-assignment-{annotator_index + 1}-{pair_id}"
                            ),
                            "human_submission_attestation_id": (
                                f"fixture-attestation-{annotator_index + 1}-{pair_id}"
                            ),
                            "annotator_principal_hash": (str(annotator_index + 1) * 64),
                            "assignment_mode": "test_fixture",
                            "fixture_only": True,
                            "raw_vote_only": True,
                            "resolved_label_created": False,
                            "gold_label_created": False,
                            "training_eligible": False,
                        },
                    }
                )
            annotations.extend(pair_annotations)
            validated_annotations = [
                AnnotationRecord.model_validate(annotation) for annotation in pair_annotations
            ]
            adjudication_payload: dict[str, object] = {
                "schema_version": 1,
                "target_kind": "lean_pair",
                "target_id": pair_id,
                "annotation_ids": sorted(annotation_ids),
                "adjudicator_id": "fixture-adjudicator",
                "same_claim": same_claim,
                "relation": relation,
                "resolution_outcome": answer,
                "annotation_content_sha256": annotation_content_sha256(validated_annotations),
                "human_assignment_ids": sorted(
                    str(annotation.metadata["human_assignment_id"])
                    for annotation in validated_annotations
                ),
                "human_submission_attestation_ids": sorted(
                    str(annotation.metadata["human_submission_attestation_id"])
                    for annotation in validated_annotations
                ),
                "annotator_principal_hashes": sorted(
                    str(annotation.metadata["annotator_principal_hash"])
                    for annotation in validated_annotations
                ),
                "origin_assurance": "test_fixture",
                "operator_attestation_verified": True,
                "backend_origin_verified": False,
                "human_gold_eligible": False,
                "fixture_only": True,
                "backend_id": "pytest_fixture_backend",
                "adjudicator_principal_hash": "3" * 64,
                "backend_adjudication_record_id": f"fixture-backend-record:{pair_id}",
            }
            adjudication = build_operator_attested_adjudication_record(
                adjudication_payload,
                operator_key=_OPERATOR_KEY,
            )
            adjudication_id = adjudication.adjudication_id
            adjudications.append(adjudication.model_dump(mode="json"))
            evidence_id = make_id("ev", {"pair": pair_id, "human": True})
            evidence_ids.append(evidence_id)
            evidence.append(
                {
                    "schema_version": 2,
                    "evidence_id": evidence_id,
                    "target_kind": "lean_pair",
                    "target_id": pair_id,
                    "kind": "human_annotation",
                    "status": "success",
                    "value": {
                        "kind": "judgment",
                        "answer": answer,
                        "relation": relation,
                        "error_types": [],
                    },
                    "method_version": "fixture_human_v1",
                    "created_at": "2026-07-28T00:00:00Z",
                    "metadata": {"adjudication_id": adjudication_id},
                }
            )
        promotion_decision_ids: list[str] = []
        if certified:
            promotion_id = make_id("promotion", {"family": "p01_alpha"})
            promotion_ids.add(promotion_id)
            promotion_decision_ids.append(promotion_id)
        pairs.append(
            {
                "schema_version": 1,
                "pair_id": pair_id,
                "theorem_a_id": theorem_a,
                "theorem_b_id": theorem_b,
                "pair_source": "fixture",
                "split_group_ids": [ancestry_id],
                "generator_id": spec.get("generator_id"),
                "transformation_family": (
                    "p01_alpha" if certified else spec.get("transformation_family")
                ),
                "resolved_label_id": None if sealed_final else label_id,
                "evidence_ids": evidence_ids,
            }
        )
        if not sealed_final:
            labels.append(
                {
                    "schema_version": 2,
                    "label_id": label_id,
                    "target_kind": "lean_pair",
                    "target_id": pair_id,
                    "same_claim": same_claim,
                    "resolution_outcome": (
                        "same_claim"
                        if same_claim is True
                        else "not_same_claim"
                        if same_claim is False
                        else "ambiguous"
                    ),
                    "relation": relation,
                    "faithfulness_levels": {
                        "F0_representation_equivalent": None,
                        "F1_same_claim": same_claim,
                        "F2_truth_equivalent": None,
                    },
                    "truth_A_implies_B": None,
                    "truth_B_implies_A": None,
                    "error_types": [],
                    "quality_tier": ("gold_conservative_transform" if certified else "gold_human"),
                    "resolution_method": (
                        "p01_alpha_certificate" if certified else "human_adjudication"
                    ),
                    "evidence_ids_used": evidence_ids,
                    "requires_adjudication": False,
                    "train_eligibility": bool(spec.get("train_eligibility", False)),
                    "eval_eligibility": bool(spec.get("eval_eligibility", True)),
                    "policy_version": "fixture_policy_v1",
                }
            )
        assignments.append(
            {
                "schema_version": 1,
                "target_kind": "lean_pair",
                "target_id": pair_id,
                "split_component_id": component_id,
            }
        )
        lineage[pair_id] = {
            "component_id": component_id,
            "label_id": "" if sealed_final else label_id,
            "adjudication_id": adjudication_id,
            "evidence_ids": sorted(evidence_ids),
            "promotion_ids": sorted(promotion_decision_ids),
        }
    for inventory in inventory_records:
        item = lineage[str(inventory["pair_id"])]
        inventory["split_component_id"] = item["component_id"]
        inventory["resolved_label_id"] = item["label_id"]
        inventory["evidence_ids"] = item["evidence_ids"]
        inventory["promotion_decision_ids"] = item["promotion_ids"]
        inventory["normalized_arm_loss_weights"] = dict.fromkeys(
            inventory["arm_memberships"],  # type: ignore[arg-type]
            1.0,
        )
    promotions = [
        {
            "schema_version": 1,
            "decision_id": promotion_id,
            "family_id": "p01_alpha",
            "rule_id": "p01_alpha",
            "rule_version": "1.0.0",
            "policy_version": "fixture_promotion_v1",
            "audit_id": make_id("audit", {"family": "p01_alpha"}),
            "parent_registry_hash": "3" * 64,
            "promotion_policy_hash": "4" * 64,
            "audit_manifest_hash": "5" * 64,
            "audit_input_hash": "6" * 64,
            "audit_result_hash": "7" * 64,
            "selected_count": 200,
            "denominator_n": 200,
            "successes": 200,
            "point_precision": 1.0,
            "clopper_pearson_lower_95": 0.98,
            "blinded": True,
            "design_frozen_before_audit": True,
            "all_invariants_hold": True,
            "held_out_source_domain_audit_passed": True,
            "decision": "gold_promoted",
            "unlocked_quality_tier": "gold_conservative_transform",
        }
        for promotion_id in sorted(promotion_ids)
    ]
    _write_jsonl(tmp_path / "lineage/theorems.jsonl", theorems)
    _write_jsonl(tmp_path / "lineage/pairs.jsonl", pairs)
    _write_jsonl(tmp_path / "lineage/labels.jsonl", labels)
    _write_jsonl(tmp_path / "lineage/evidence.jsonl", evidence)
    _write_jsonl(tmp_path / "lineage/promotions.jsonl", promotions)
    _write_jsonl(tmp_path / "lineage/annotations.jsonl", annotations)
    _write_jsonl(tmp_path / "lineage/adjudications.jsonl", adjudications)
    operator_key_path = tmp_path / "lineage/operator.key"
    operator_key_path.write_bytes(_OPERATOR_KEY)
    operator_key_path.chmod(0o600)
    _write_jsonl(tmp_path / "lineage/split_assignments.jsonl", assignments)
    _write_json(
        tmp_path / "lineage/source.json",
        {
            "schema_version": 1,
            "source": "fixture",
            "kind": "local_fixture",
            "resolved_id": "fixture",
            "revision": "r1",
            "retrieval_date": "2026-07-28T00:00:00Z",
            "access_status": "public",
            "license": "MIT",
            "nl_trust": "trusted",
        },
    )
    return lineage


def _gold_manifest(
    tmp_path: Path,
    partition: str,
    pair_ids: list[str],
    lineage: dict[str, dict[str, str | list[str]]],
    pair_specs: dict[str, dict[str, object]],
) -> dict[str, object]:
    final = partition == "final_human_test"
    records: list[dict[str, object]] = []
    for index, pair_id in enumerate(pair_ids):
        spec = pair_specs[pair_id]
        same_claim = spec["same_claim"]
        relation = str(spec["relation"])
        item = lineage[pair_id]
        records.append(
            {
                "record_id": f"{partition}-{index}",
                "target_kind": "lean_pair",
                "target_id": pair_id,
                "resolved_label_id": None if final else item["label_id"],
                "adjudication_id": None if final else item["adjudication_id"],
                "sealed_label_vault_receipt_id": (
                    "sealed-label-vault-receipt:"
                    + hash_canonical(
                        {
                            "schema": "fixture_sealed_label_vault_receipt_v1",
                            "record_id": f"{partition}-{index}",
                            "target_id": pair_id,
                            "split_component_id": item["component_id"],
                        }
                    )
                    if final
                    else None
                ),
                "split_component_id": item["component_id"],
                "adjudicated": True,
                "sampling_stratum": f"fixture-{relation}",
                "inclusion_probability": 1.0,
                "design_weight": 1.0,
                "simple_random_real_output_subpanel": final and index == 0,
                "ambiguity_head_eligible": (not final and relation == "ambiguous"),
                "labels_hidden": final,
                "same_claim": None if final else same_claim,
                "relation": None if final else relation,
                "label_bases": [] if final else ["human_adjudication"],
            }
        )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "partition": partition,
        "sealed": final,
        "labels_exposed_to_audit": not final,
        "distribution": ("compiling_real_outputs" if partition == "calibration_gold" else "mixed"),
        "target_count": len(records),
        "realized_eligible_count": len(records),
        "sampling_design": "fixture_stratified_v1",
        "sampling_propensities_recorded": True,
        "statistical_adequacy_status": (
            "adequate" if partition in {"calibration_gold", "final_human_test"} else "unsupported"
        ),
        "statistical_assessment_artifact": None,
        "statistical_assessment_sha256": None,
        "calibration_k_folds": 2 if partition == "calibration_gold" else None,
        "simple_random_real_output_subpanel_count": 1 if final else None,
        "records": records,
    }
    if partition in {"calibration_gold", "final_human_test"}:
        design_payload = {
            "schema": "gold_partition_design_v1",
            "partition": partition,
            "distribution": manifest["distribution"],
            "target_count": manifest["target_count"],
            "sampling_design": manifest["sampling_design"],
            "calibration_k_folds": manifest["calibration_k_folds"],
            "simple_random_real_output_subpanel_count": manifest[
                "simple_random_real_output_subpanel_count"
            ],
            "records": [
                {
                    key: record[key]
                    for key in (
                        "record_id",
                        "target_kind",
                        "target_id",
                        "split_component_id",
                        "sampling_stratum",
                        "inclusion_probability",
                        "design_weight",
                        "simple_random_real_output_subpanel",
                    )
                }
                for record in records
            ],
        }
        required_claim = (
            "H4_Gate10" if partition == "calibration_gold" else "main_task_aggregate_95_precision"
        )
        assessment_payload = {
            "schema_version": 1,
            "assessment_kind": "preregistered_design_adequacy",
            "partition": partition,
            "status": "design_adequate",
            "component_count": len(records),
            "record_count": len(records),
            "supported_claims": [required_claim],
            "method": (
                "fixture_calibration_design_v1"
                if partition == "calibration_gold"
                else "fixture_final_design_v1"
            ),
            "partition_design_hash": hash_canonical(design_payload),
            "interval_method": "clopper_pearson",
            "confidence_level": 0.95,
            "target_accepted_precision": 0.95,
            "minimum_required_component_count": 5,
        }
        assessment = {
            **assessment_payload,
            "assessment_id": "statistical_design_v1:" + hash_canonical(assessment_payload),
        }
        assessment_path = f"human/{partition}_assessment.json"
        _write_json(tmp_path / assessment_path, assessment)
        manifest["statistical_assessment_artifact"] = assessment_path
        manifest["statistical_assessment_sha256"] = hash_file(tmp_path / assessment_path)
    return manifest


def _ready_fixture(tmp_path: Path) -> Path:
    policy_path = _fixture_policy(tmp_path)
    records: list[dict[str, object]] = []
    arms = ["D0", "D1", "D2", "D3", "D4", "D5"]
    positives = (
        ("certified_positive", "human_adjudication"),
        ("certified_positive", "certified_conservative_transformation"),
        ("human_or_promoted_faithful_real", "human_adjudication"),
        ("promoted_llm_equivalent", "human_adjudication"),
    )
    negatives = (
        ("G_rule", None, "N01", "human_adjudication"),
        ("G_sci", "p1", None, "human_adjudication"),
        ("G_open", "p2", None, "human_adjudication"),
        ("G_real", "r1", None, "human_adjudication"),
    )
    for index, (positive_source, basis) in enumerate(positives):
        records.append(
            {
                "schema_version": 1,
                "pair_id": make_id("pair", {"fixture": "positive", "index": index}),
                "split_component_id": "pending",
                "same_claim": True,
                "relation": "equivalent",
                "arm_memberships": arms,
                "label_bases": [basis],
                "resolved_label_id": "pending",
                "evidence_ids": [],
                "promotion_decision_ids": [],
                "training_gold_record_id": ("training_gold-0" if index == 0 else None),
                "arm_loss_weights": {"D5": 2.0} if index == 0 else {},
                "normalized_arm_loss_weights": {},
                "ancestry_oversampled_arms": [],
                "positive_source": positive_source,
                "negative_source": None,
                "source_family": None,
                "transform_family": None,
                "duplicate_of": None,
            }
        )
    negative_relations = ("A_stronger", "B_stronger", "incomparable", "unrelated")
    for index, (source, family, transform, basis) in enumerate(negatives):
        records.append(
            {
                "schema_version": 1,
                "pair_id": make_id("pair", {"fixture": "negative", "index": index}),
                "split_component_id": "pending",
                "same_claim": False,
                "relation": negative_relations[index],
                "arm_memberships": arms,
                "label_bases": [basis],
                "resolved_label_id": "pending",
                "evidence_ids": [],
                "promotion_decision_ids": [],
                "training_gold_record_id": f"training_gold-{index + 1}",
                "arm_loss_weights": {"D5": 2.0},
                "normalized_arm_loss_weights": {},
                "ancestry_oversampled_arms": [],
                "positive_source": None,
                "negative_source": source,
                "source_family": family,
                "transform_family": transform,
                "duplicate_of": None,
            }
        )

    pair_specs: dict[str, dict[str, object]] = {}
    for record in records:
        pair_specs[str(record["pair_id"])] = {
            "same_claim": record["same_claim"],
            "relation": record["relation"],
            "label_basis": record["label_bases"][0],  # type: ignore[index]
            "generator_id": record["source_family"],
            "transformation_family": record["transform_family"],
            "train_eligibility": True,
            "eval_eligibility": False,
        }
    relation_rows = (
        (True, "equivalent"),
        (False, "A_stronger"),
        (False, "B_stronger"),
        (False, "incomparable"),
        (False, "unrelated"),
    )
    product_pair_ids: dict[str, list[str]] = {
        "training_gold": [
            str(records[0]["pair_id"]),
            *(str(records[index]["pair_id"]) for index in range(4, 8)),
        ]
    }
    ambiguous_pair_id = make_id("pair", {"fixture": "training_gold", "ambiguous": True})
    pair_specs[ambiguous_pair_id] = {
        "same_claim": None,
        "relation": "ambiguous",
        "label_basis": "human_adjudication",
        "generator_id": "r1",
        "train_eligibility": True,
        "eval_eligibility": False,
    }
    product_pair_ids["training_gold"].append(ambiguous_pair_id)
    for partition in ("selection_gold", "calibration_gold", "final_human_test"):
        product_pair_ids[partition] = []
        for index, (same_claim, relation) in enumerate(relation_rows):
            pair_id = make_id("pair", {"fixture": partition, "index": index})
            product_pair_ids[partition].append(pair_id)
            pair_specs[pair_id] = {
                "same_claim": same_claim,
                "relation": relation,
                "label_basis": "human_adjudication",
                "generator_id": "r1",
                "train_eligibility": False,
                "eval_eligibility": True,
                "sealed_final": partition == "final_human_test",
            }

    pair_specs[product_pair_ids["selection_gold"][0]]["source_frame_record_id"] = "frame-0"
    pair_specs[ambiguous_pair_id]["source_frame_record_id"] = "frame-1"
    lineage = _write_lineage(tmp_path, pair_specs, records)
    for partition, pair_ids in product_pair_ids.items():
        _write_json(
            tmp_path / f"human/{partition}.json",
            _gold_manifest(tmp_path, partition, pair_ids, lineage, pair_specs),
        )

    ambiguity_item = lineage[ambiguous_pair_id]
    _write_jsonl(
        tmp_path / "ambiguity_inventory.jsonl",
        [
            {
                "schema_version": 1,
                "record_id": "ambiguity-training-0",
                "target_id": ambiguous_pair_id,
                "split_component_id": ambiguity_item["component_id"],
                "resolved_label_id": ambiguity_item["label_id"],
                "training_gold_record_id": "training_gold-5",
                "arm": "D5",
                "raw_human_gold_loss_weight": 2.0,
                "normalized_component_weight": 1.0,
            }
        ],
    )

    sci_pair = str(records[5]["pair_id"])
    open_pair = str(records[6]["pair_id"])
    _write_lf022_manifest(tmp_path, "G_sci", {sci_pair: "p1"})
    _write_lf022_manifest(tmp_path, "G_open", {open_pair: "p2"})

    source_artifact = tmp_path / "real_output_sources.json"
    _write_json(
        source_artifact,
        {"successful_generator_families": ["r1", "r2", "r3", "r4-heldout"]},
    )
    holdout_payload = {
        "schema_version": 1,
        "artifact_class": "production",
        "successful_generator_families": ["r1", "r2", "r3", "r4-heldout"],
        "supervision_generator_families": ["r1", "r2", "r3"],
        "heldout_generator_family": "r4-heldout",
        "source_artifact": "real_output_sources.json",
        "source_artifact_sha256": hash_file(source_artifact),
    }
    _write_json(
        tmp_path / "generator_holdout.json",
        {
            **holdout_payload,
            "manifest_id": "generator-holdout:" + hash_canonical(holdout_payload),
        },
    )

    frame = [
        {
            "frame_record_id": f"frame-{index}",
            "decision": "REVIEW",
            "same_claim": None,
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "population_item": {"representative_family_id": family},
        }
        for index, family in enumerate(("f1", "f2"))
    ]
    _write_jsonl(tmp_path / "frame.jsonl", frame)
    prevalence_labels: list[dict[str, object]] = []
    prevalence_pair_ids = [
        product_pair_ids["selection_gold"][0],
        ambiguous_pair_id,
    ]
    for index, pair_id in enumerate(prevalence_pair_ids):
        spec = pair_specs[pair_id]
        item = lineage[pair_id]
        prevalence_labels.append(
            {
                "schema_version": 1,
                "frame_record_id": f"frame-{index}",
                "target_pair_id": pair_id,
                "resolved_label_id": item["label_id"],
                "adjudication_id": item["adjudication_id"],
                "adjudicated": True,
                "label_basis": "human_adjudication",
                "same_claim": spec["same_claim"],
                "relation": spec["relation"],
                "resolution_outcome": (
                    "same_claim"
                    if spec["same_claim"] is True
                    else "not_same_claim"
                    if spec["same_claim"] is False
                    else "ambiguous"
                ),
            }
        )
    _write_jsonl(tmp_path / "prevalence_labels.jsonl", prevalence_labels)
    _write_jsonl(tmp_path / "inventory.jsonl", records)
    return policy_path


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _detach_training_gold_binding(
    tmp_path: Path,
    *,
    old_inventory_index: int,
    replacement_inventory_index: int,
) -> str:
    """Move one binary training-gold binding so a silver/counterexample test is valid."""

    inventory_path = tmp_path / "inventory.jsonl"
    inventory = _load_jsonl(inventory_path)
    old = inventory[old_inventory_index]
    replacement = inventory[replacement_inventory_index]
    gold_id = old["training_gold_record_id"]
    assert isinstance(gold_id, str)
    assert replacement["training_gold_record_id"] is None
    old["training_gold_record_id"] = None
    old["arm_loss_weights"] = {}
    replacement["training_gold_record_id"] = gold_id
    replacement["arm_loss_weights"] = {"D5": 2.0}
    _write_jsonl(inventory_path, inventory)

    replacement_pair_id = str(replacement["pair_id"])
    pairs = {str(item["pair_id"]): item for item in _load_jsonl(tmp_path / "lineage/pairs.jsonl")}
    adjudications = {
        str(item["target_id"]): item
        for item in _load_jsonl(tmp_path / "lineage/adjudications.jsonl")
    }
    assignments = {
        str(item["target_id"]): item
        for item in _load_jsonl(tmp_path / "lineage/split_assignments.jsonl")
    }
    training_path = tmp_path / "human/training_gold.json"
    training = json.loads(training_path.read_text(encoding="utf-8"))
    gold = next(item for item in training["records"] if item["record_id"] == gold_id)
    gold.update(
        {
            "target_id": replacement_pair_id,
            "resolved_label_id": pairs[replacement_pair_id]["resolved_label_id"],
            "adjudication_id": adjudications[replacement_pair_id]["adjudication_id"],
            "split_component_id": assignments[replacement_pair_id]["split_component_id"],
            "same_claim": replacement["same_claim"],
            "relation": replacement["relation"],
            "sampling_stratum": f"fixture-{replacement['relation']}",
            "ambiguity_head_eligible": False,
        }
    )
    _write_json(training_path, training)
    return str(old["pair_id"])


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
    assert first.status == "READY_TEST_FIXTURE"
    assert not first.training_execution_authorized
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
    assert confirmatory.status == "READY_TEST_FIXTURE"

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
    assert with_flag.status == "READY_REDUCED_DATA_TEST_FIXTURE"


def test_human_products_are_component_disjoint_and_final_labels_stay_hidden(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    training_path = tmp_path / "human/training_gold.json"
    training = json.loads(training_path.read_text(encoding="utf-8"))
    selection = json.loads((tmp_path / "human/selection_gold.json").read_text(encoding="utf-8"))
    training["records"][0]["split_component_id"] = selection["records"][0]["split_component_id"]
    _write_json(training_path, training)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training.human_partitions_component_disjoint
    assert "HUMAN_PARTITION_ANCESTRY_OVERLAP" in {blocker.code for blocker in report.blockers}

    final_path = tmp_path / "human/final_human_test.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final["records"][0]["same_claim"] = True
    final["records"][0]["relation"] = "equivalent"
    final["records"][0]["label_bases"] = ["human_adjudication"]
    with pytest.raises(ValueError, match="cannot expose semantic fields"):
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
                "resolved_label_id": "lbl:fixture",
                "positive_source": "certified_positive",
            }
        )


def test_d5_requires_training_gold_weight_two_and_no_ancestry_oversampling(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    lines = (tmp_path / "inventory.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["training_gold_record_id"] = None
    first["arm_loss_weights"] = {"D5": 2.0}
    first["ancestry_oversampled_arms"] = ["D5"]
    lines[0] = json.dumps(first, sort_keys=True)
    (tmp_path / "inventory.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert "D5_HUMAN_GOLD_CONTRACT_INVALID" in {blocker.code for blocker in report.blockers}


def test_lf022_requires_content_addressed_promoted_diverse_artifacts(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    artifact = tmp_path / "lf022/sci_variants.jsonl"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training.lf022_artifacts_present
    assert report.training.invalid_lf022_artifacts
    assert "LF022_ARTIFACTS_INVALID" in {blocker.code for blocker in report.blockers}


def test_lf022_placeholder_valid_production_variant_is_admitted(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    variants_path = tmp_path / "lf022/sci_variants.jsonl"
    variants = [json.loads(line) for line in variants_path.read_text(encoding="utf-8").splitlines()]
    variants[0]["validation_status"] = "elaborates_with_placeholder"
    _write_jsonl(variants_path, variants)
    manifest_path = tmp_path / "lf022/sci.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["variants"]["sha256"] = hash_file(variants_path)
    _write_json(manifest_path, manifest)

    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "READY_TEST_FIXTURE"
    assert report.training.lf022_artifacts_present


def test_lf022_family_diversity_and_promotion_are_mechanically_checked(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    policy_raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy_raw["family_controls"]["minimum_llm_proposer_families"] = 3
    policy_raw["family_controls"]["maximum_one_llm_family_fraction_of_llm_negative"] = 0.4
    policy_path.write_text(yaml.safe_dump(policy_raw, sort_keys=False), encoding="utf-8")
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert "LF022_FAMILY_DIVERSITY_INVALID" in {blocker.code for blocker in report.blockers}

    # Restore the fixture policy, then make the promotion ledger explicitly
    # unpromoted while preserving its content-addressed binding.
    policy_raw["family_controls"]["minimum_llm_proposer_families"] = 1
    policy_raw["family_controls"]["maximum_one_llm_family_fraction_of_llm_negative"] = 1.0
    policy_path.write_text(yaml.safe_dump(policy_raw, sort_keys=False), encoding="utf-8")
    promotions_path = tmp_path / "lf022/sci_promotions.jsonl"
    promotions = [
        json.loads(line) for line in promotions_path.read_text(encoding="utf-8").splitlines()
    ]
    promotions[0]["promoted"] = False
    _write_jsonl(promotions_path, promotions)
    manifest_path = tmp_path / "lf022/sci.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["promotions"]["sha256"] = hash_file(promotions_path)
    _write_json(manifest_path, manifest)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert "LF022_ARTIFACTS_INVALID" in {blocker.code for blocker in report.blockers}


def test_selection_gold_requires_every_canonical_nonambiguous_relation(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    path = tmp_path / "human/selection_gold.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["records"] = [
        record for record in manifest["records"] if record["relation"] != "A_stronger"
    ]
    manifest["target_count"] = 4
    manifest["realized_eligible_count"] = 4
    _write_json(path, manifest)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    blocker = next(
        blocker for blocker in report.blockers if blocker.code == "SELECTION_GOLD_MINIMA_NOT_MET"
    )
    assert "A_stronger=0" in blocker.observed


def test_calibration_and_final_products_require_hashed_statistical_adequacy(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    calibration_assessment = tmp_path / "human/calibration_gold_assessment.json"
    calibration_assessment.write_text(
        calibration_assessment.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    final_path = tmp_path / "human/final_human_test.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final["records"] = final["records"][:-1]
    final["target_count"] = 4
    final["realized_eligible_count"] = 4
    _write_json(final_path, final)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    codes = {blocker.code for blocker in report.blockers}
    assert report.status == "NOT_READY"
    assert "CALIBRATION_GOLD_INVALID" in codes
    assert "FINAL_HUMAN_TEST_INVALID" in codes


def test_inventory_label_basis_must_replay_from_underlying_lineage(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    lines = (tmp_path / "inventory.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["label_bases"] = ["certified_conservative_transformation"]
    lines[0] = json.dumps(first, sort_keys=True)
    (tmp_path / "inventory.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training.lineage_integrity_valid
    assert report.training.lineage_error_count > 0
    assert "TRAINING_LINEAGE_INVALID" in {blocker.code for blocker in report.blockers}


def test_terminal_ambiguity_counts_toward_prevalence_completion(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "READY_TEST_FIXTURE"
    assert report.prevalence.human_terminal_label_count == 2
    assert report.prevalence.human_binary_label_count == 1
    assert report.prevalence.human_ambiguous_label_count == 1
    assert report.prevalence.prevalence_estimate_ready


def test_missing_prevalence_labels_returns_not_ready_instead_of_crashing(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    (tmp_path / "prevalence_labels.jsonl").unlink()
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training_execution_authorized
    assert report.training.confirmatory_ready
    assert not report.prevalence.prevalence_estimate_ready
    assert "PREVALENCE_HUMAN_LABELS_MISSING" in {blocker.code for blocker in report.blockers}


def test_forged_human_json_cannot_unlock_readiness(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    annotations = _load_jsonl(tmp_path / "lineage/annotations.jsonl")
    annotations[0]["confidence"] = 1
    _write_jsonl(tmp_path / "lineage/annotations.jsonl", annotations)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training.lineage_integrity_valid
    assert "TRAINING_LINEAGE_INVALID" in {blocker.code for blocker in report.blockers}


def test_operator_hmac_cannot_unlock_production_human_gold_readiness(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    raw["artifact_class"] = "production"
    raw["inputs"]["lineage"]["allow_test_fixture_human_provenance"] = False
    policy_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training_execution_authorized
    assert not report.training.confirmatory_ready
    assert "HUMAN_GOLD_ADMISSION_DISABLED" in {blocker.code for blocker in report.blockers}


def test_prevalence_labels_cannot_be_reassigned_between_frame_rows(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    labels = _load_jsonl(tmp_path / "prevalence_labels.jsonl")
    first = labels[0]
    second = labels[1]
    for key in (
        "target_pair_id",
        "resolved_label_id",
        "adjudication_id",
        "same_claim",
        "relation",
        "resolution_outcome",
    ):
        first[key], second[key] = second[key], first[key]
    _write_jsonl(tmp_path / "prevalence_labels.jsonl", labels)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.prevalence.prevalence_estimate_ready
    assert "PREVALENCE_HUMAN_LABELS_MISSING" in {blocker.code for blocker in report.blockers}


def test_statistical_minimum_is_bound_by_policy_not_self_declared(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    assessment_path = tmp_path / "human/calibration_gold_assessment.json"
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["minimum_required_component_count"] = 1
    assessment_payload = {key: value for key, value in assessment.items() if key != "assessment_id"}
    assessment["assessment_id"] = "statistical_design_v1:" + hash_canonical(assessment_payload)
    _write_json(assessment_path, assessment)
    manifest_path = tmp_path / "human/calibration_gold.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["statistical_assessment_sha256"] = hash_file(assessment_path)
    _write_json(manifest_path, manifest)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert "CALIBRATION_GOLD_INVALID" in {blocker.code for blocker in report.blockers}


def test_sealed_final_manifest_exposes_no_label_or_adjudication_links(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    final_path = tmp_path / "human/final_human_test.json"
    final = json.loads(final_path.read_text(encoding="utf-8"))
    assert all(record["resolved_label_id"] is None for record in final["records"])
    assert all(record["adjudication_id"] is None for record in final["records"])
    final["records"][0]["resolved_label_id"] = make_id(
        "lbl",
        {"forbidden": "sealed-link"},
    )
    with pytest.raises(ValueError, match="cannot expose label or adjudication links"):
        GoldPartitionManifest.model_validate(final)
    assert (
        audit_training_data_readiness(
            repo_root=tmp_path,
            loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
        ).status
        == "READY_TEST_FIXTURE"
    )


def test_inventory_components_must_match_canonical_union_find_assignments(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    inventory = _load_jsonl(tmp_path / "inventory.jsonl")
    inventory[0]["split_component_id"] = "split-component:" + "f" * 64
    _write_jsonl(tmp_path / "inventory.jsonl", inventory)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training.lineage_integrity_valid
    assert "TRAINING_LINEAGE_INVALID" in {blocker.code for blocker in report.blockers}


def test_human_basis_requires_label_used_adjudication_evidence(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    pair_id = str(_load_jsonl(tmp_path / "inventory.jsonl")[0]["pair_id"])
    labels = _load_jsonl(tmp_path / "lineage/labels.jsonl")
    label = next(item for item in labels if item["target_id"] == pair_id)
    label["evidence_ids_used"] = []
    _write_jsonl(tmp_path / "lineage/labels.jsonl", labels)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training.lineage_integrity_valid
    assert "TRAINING_LINEAGE_INVALID" in {blocker.code for blocker in report.blockers}


def test_d5_ancestry_normalized_weights_are_recomputed_not_self_asserted(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    inventory = _load_jsonl(tmp_path / "inventory.jsonl")
    inventory[0]["normalized_arm_loss_weights"]["D5"] = 0.5  # type: ignore[index]
    _write_jsonl(tmp_path / "inventory.jsonl", inventory)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert "D5_HUMAN_GOLD_CONTRACT_INVALID" in {blocker.code for blocker in report.blockers}


def test_generator_holdout_is_content_addressed_and_absent_from_training(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    path = tmp_path / "generator_holdout.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["supervision_generator_families"] = ["r1", "r2", "r4-heldout"]
    _write_json(path, manifest)
    report = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert report.status == "NOT_READY"
    assert not report.training.generator_holdout_manifest_valid
    assert "GENERATOR_HOLDOUT_MANIFEST_INVALID" in {blocker.code for blocker in report.blockers}


def test_promoted_consensus_requires_two_used_independent_model_families(
    tmp_path: Path,
) -> None:
    policy_path = _ready_fixture(tmp_path)
    pair_id = _detach_training_gold_binding(
        tmp_path,
        old_inventory_index=5,
        replacement_inventory_index=2,
    )
    inventory = _load_jsonl(tmp_path / "inventory.jsonl")
    record = next(item for item in inventory if item["pair_id"] == pair_id)
    pairs = _load_jsonl(tmp_path / "lineage/pairs.jsonl")
    pair = next(item for item in pairs if item["pair_id"] == pair_id)
    labels = _load_jsonl(tmp_path / "lineage/labels.jsonl")
    label = next(item for item in labels if item["target_id"] == pair_id)
    evidence = _load_jsonl(tmp_path / "lineage/evidence.jsonl")
    new_ids: list[str] = []
    for index in range(2):
        evidence_id = make_id("ev", {"pair": pair_id, "consensus": index})
        new_ids.append(evidence_id)
        evidence.append(
            {
                "schema_version": 2,
                "evidence_id": evidence_id,
                "target_kind": "lean_pair",
                "target_id": pair_id,
                "kind": "llm_judgment",
                "status": "success",
                "value": {
                    "kind": "judgment",
                    "answer": "not_same_claim",
                    "relation": record["relation"],
                    "error_types": [],
                },
                "method_version": "fixture_llm_judge_v1",
                "created_at": "2026-07-28T00:00:00Z",
                "metadata": {"model_family": "judge-family-a"},
            }
        )
    all_evidence = sorted([*pair["evidence_ids"], *new_ids])
    pair["evidence_ids"] = all_evidence
    record["evidence_ids"] = all_evidence
    record["label_bases"] = ["promoted_independent_consensus"]
    label["evidence_ids_used"] = sorted(new_ids)
    label["quality_tier"] = "silver_consensus"
    label["resolution_method"] = "independent_llm_consensus"
    _write_jsonl(tmp_path / "inventory.jsonl", inventory)
    _write_jsonl(tmp_path / "lineage/pairs.jsonl", pairs)
    _write_jsonl(tmp_path / "lineage/labels.jsonl", labels)
    _write_jsonl(tmp_path / "lineage/evidence.jsonl", evidence)

    invalid = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert invalid.status == "NOT_READY"
    assert not invalid.training.lineage_integrity_valid

    evidence[-1]["metadata"]["model_family"] = "judge-family-b"  # type: ignore[index]
    _write_jsonl(tmp_path / "lineage/evidence.jsonl", evidence)
    valid = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert valid.status == "READY_TEST_FIXTURE"


def test_kernel_separator_requires_a_used_found_counterexample(tmp_path: Path) -> None:
    policy_path = _ready_fixture(tmp_path)
    pair_id = _detach_training_gold_binding(
        tmp_path,
        old_inventory_index=6,
        replacement_inventory_index=3,
    )
    inventory = _load_jsonl(tmp_path / "inventory.jsonl")
    record = next(item for item in inventory if item["pair_id"] == pair_id)
    pairs = _load_jsonl(tmp_path / "lineage/pairs.jsonl")
    pair = next(item for item in pairs if item["pair_id"] == pair_id)
    labels = _load_jsonl(tmp_path / "lineage/labels.jsonl")
    label = next(item for item in labels if item["target_id"] == pair_id)
    evidence = _load_jsonl(tmp_path / "lineage/evidence.jsonl")
    evidence_id = make_id("ev", {"pair": pair_id, "counterexample": True})
    evidence.append(
        {
            "schema_version": 2,
            "evidence_id": evidence_id,
            "target_kind": "lean_pair",
            "target_id": pair_id,
            "kind": "counterexample",
            "status": "success",
            "value": {
                "kind": "counterexample",
                "outcome": "not_found",
                "direction": "equivalence_only",
            },
            "method_version": "fixture_kernel_decide_v1",
            "created_at": "2026-07-28T00:00:00Z",
        }
    )
    all_evidence = sorted([*pair["evidence_ids"], evidence_id])
    pair["evidence_ids"] = all_evidence
    record["evidence_ids"] = all_evidence
    record["label_bases"] = ["accepted_kernel_separator"]
    label["evidence_ids_used"] = [evidence_id]
    label["quality_tier"] = "gold_counterexample"
    label["resolution_method"] = "kernel_counterexample"
    _write_jsonl(tmp_path / "inventory.jsonl", inventory)
    _write_jsonl(tmp_path / "lineage/pairs.jsonl", pairs)
    _write_jsonl(tmp_path / "lineage/labels.jsonl", labels)
    _write_jsonl(tmp_path / "lineage/evidence.jsonl", evidence)

    not_found = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert not_found.status == "NOT_READY"
    assert not not_found.training.lineage_integrity_valid

    evidence[-1]["value"] = {
        "kind": "counterexample",
        "outcome": "found",
        "direction": "equivalence_only",
        "domain": "Bool",
        "encoding": "kernel_decide_v1",
        "witness_artifact": "artifacts/fixture_counterexample.json",
        "axioms": [],
    }
    _write_jsonl(tmp_path / "lineage/evidence.jsonl", evidence)
    found = audit_training_data_readiness(
        repo_root=tmp_path,
        loaded_policy=load_config(policy_path, TrainingDataReadinessPolicy),
    )
    assert found.status == "READY_TEST_FIXTURE"
