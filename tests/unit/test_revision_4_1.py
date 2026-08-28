"""Executable Revision 4.1 migration, symmetry, identity, and pilot rules."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
import yaml

from leanfaith.models import (
    PilotCandidateResult,
    factor_relation_probabilities,
    select_backbone,
)
from leanfaith.representations import (
    ManualCollisionReview,
    TheoremForRepresentation,
    audit_representations,
    close_manual_collision_audit,
    compare_representation_replays,
)
from leanfaith.representations.pipeline import _build_record
from leanfaith.schemas import (
    AnnotationRecord,
    Decision,
    QualityTier,
    RelationLabel,
    ResolutionOutcome,
    ResolvedLabel,
    make_hf_source_record_id,
)
from leanfaith.sources.hf_sft_classic import parse_row
from tests.unit.record_factories import ANN_ID, LABEL_ID, PAIR_ID, variant_record

_UTC = datetime.datetime(2026, 7, 14, tzinfo=datetime.UTC)
_ROOT = Path(__file__).resolve().parents[2]


def _legacy_label(relation: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "label_id": LABEL_ID,
        "target_kind": "lean_pair",
        "target_id": PAIR_ID,
        "same_claim": False,
        "resolution_outcome": "not_same_claim",
        "relation": relation,
        "faithfulness_levels": {
            "F0_representation_equivalent": False,
            "F1_same_claim": False,
            "F2_truth_equivalent": None,
        },
        "quality_tier": "gold_human",
        "resolution_method": "legacy",
        "requires_adjudication": False,
        "train_eligibility": True,
        "eval_eligibility": True,
        "policy_version": "semantic_policy_v1",
    }


def test_legacy_near_miss_migrates_and_round_trips_v2() -> None:
    migrated = ResolvedLabel.model_validate(_legacy_label("incomparable_near_miss"))
    assert migrated.schema_version == 2
    assert migrated.relation is RelationLabel.INCOMPARABLE
    assert migrated.relation_provenance == ("near_miss",)
    assert ResolvedLabel.model_validate(migrated.model_dump(mode="json")) == migrated


def test_legacy_unknown_migrates_to_unresolved_review() -> None:
    payload = _legacy_label("unknown")
    migrated = ResolvedLabel.model_validate(payload)
    assert migrated.relation is None
    assert migrated.same_claim is None
    assert migrated.resolution_outcome is ResolutionOutcome.UNRESOLVED
    assert migrated.quality_tier is QualityTier.UNKNOWN
    assert migrated.decision is Decision.REVIEW
    assert migrated.requires_adjudication


def test_new_writer_rejects_legacy_relation() -> None:
    payload = _legacy_label("incomparable_near_miss")
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="relation"):
        ResolvedLabel.model_validate(payload)


def test_prediction_v2_writer_rejects_legacy_relation_score() -> None:
    from leanfaith.schemas import PredictionRecord

    with pytest.raises(ValueError, match="unknown relation"):
        PredictionRecord.model_validate(
            {
                "schema_version": 2,
                "record_id": PAIR_ID,
                "method": "fixture",
                "method_version": "v1",
                "same_claim_probability": 0.2,
                "ambiguity_probability": 0.1,
                "decision": "REVIEW",
                "relation_scores": {"incomparable_near_miss": 1.0},
                "model_version": "v1",
                "tokenizer_version": "v1",
                "representation_version": "v1",
                "calibration_version": "v1",
                "elapsed_ms": 1,
                "config_hash": "0" * 64,
            }
        )


def test_sci_provenance_keeps_requested_and_validated_categories_separate() -> None:
    record = variant_record(
        formalrx_sci_requested="S1.1",
        formalrx_sci_validated="C4",
        formalrx_sci_validation_status="retagged",
        formalrx_sci_proposer_family="family_a",
        formalrx_sci_validator_family="family_b",
    )
    assert record.formalrx_sci_requested == "S1.1"
    assert record.formalrx_sci_validated == "C4"


def test_sci_provenance_rejects_same_family_proposer_and_validator() -> None:
    with pytest.raises(ValueError, match="distinct model families"):
        variant_record(
            formalrx_sci_requested="S1.1",
            formalrx_sci_validation_status="pending",
            formalrx_sci_proposer_family="family_a",
            formalrx_sci_validator_family="family_a",
        )


@pytest.mark.parametrize(
    "overrides",
    (
        {
            "formalrx_sci_validation_status": "pending",
        },
        {
            "formalrx_sci_requested": "S1.1",
            "formalrx_sci_validated": "S1.2",
            "formalrx_sci_validation_status": "validated",
            "formalrx_sci_proposer_family": "family_a",
            "formalrx_sci_validator_family": "family_b",
        },
        {
            "formalrx_sci_requested": "S1.1",
            "formalrx_sci_validated": "S1.1",
            "formalrx_sci_validation_status": "retagged",
            "formalrx_sci_proposer_family": "family_a",
            "formalrx_sci_validator_family": "family_b",
        },
        {
            "formalrx_sci_requested": "S1.1",
            "formalrx_sci_validated": "S1.2",
            "formalrx_sci_validation_status": "rejected",
            "formalrx_sci_proposer_family": "family_a",
            "formalrx_sci_validator_family": "family_b",
        },
    ),
)
def test_sci_provenance_rejects_incoherent_state_shapes(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=r"SCI|sci"):
        variant_record(**overrides)


def test_formalrx_sci_crosswalk_pins_all_28_paper_categories() -> None:
    policy = yaml.safe_load(
        (_ROOT / "policies" / "formalrx_sci_crosswalk_v1.yaml").read_text(encoding="utf-8")
    )
    categories = policy["categories"]
    assert len(categories) == 28
    assert set(categories) == {
        "S1.1",
        "S1.2",
        "S1.3",
        "S2.1",
        "S2.2",
        "S2.3",
        "S2.4",
        "S2.5",
        "S2.6",
        "S2.7",
        "S3.1",
        "S3.2",
        "S3.3",
        "S3.4",
        "S3.5",
        "C1.1",
        "C1.2",
        "C1.3",
        "C1.4",
        "C2.1",
        "C2.2",
        "C3.1",
        "C3.2",
        "C3.3",
        "C4",
        "C5",
        "I1",
        "I2",
    }


def test_annotation_legacy_unknown_becomes_null() -> None:
    record = AnnotationRecord.model_validate(
        {
            "schema_version": 1,
            "annotation_id": ANN_ID,
            "target_kind": "lean_pair",
            "target_id": PAIR_ID,
            "annotator_id": "a",
            "round_id": "r",
            "same_claim": "cannot_assess_yet",
            "relation": "unknown",
            "confidence": 3,
            "rationale": "A required definition is not available yet.",
            "created_at": _UTC.isoformat(),
        }
    )
    assert record.relation is None
    assert record.metadata["source_schema_version"] == 1


def test_source_locator_ignores_content_and_uuid() -> None:
    source_id = make_hf_source_record_id("org/data", "abc", "train", 7)
    row_a = {
        "uuid": "duplicate",
        "question": "```lean4\ntheorem a : True := by sorry\n```",
        "lean_code": "theorem a : True := trivial",
    }
    row_b = dict(row_a, question="changed", lean_code="changed")
    parsed_a = parse_row(row_a, dataset_id="org/data", revision="abc", row_index=7)
    parsed_b = parse_row(row_b, dataset_id="org/data", revision="abc", row_index=7)
    assert parsed_a.source_record_id == parsed_b.source_record_id == source_id
    assert parsed_a.raw_row_hash != parsed_b.raw_row_hash
    assert make_hf_source_record_id("org/data", "abc", "train", 8) != source_id


def test_hierarchical_relation_factorization_and_swap() -> None:
    forward = factor_relation_probabilities(
        ambiguity_probability=0.1,
        equivalent_given_nonambiguous=0.5,
        non_equivalent_conditional={
            "A_stronger": 0.4,
            "B_stronger": 0.1,
            "incomparable": 0.3,
            "unrelated": 0.2,
        },
    )
    reverse = factor_relation_probabilities(
        ambiguity_probability=0.1,
        equivalent_given_nonambiguous=0.5,
        non_equivalent_conditional={
            "A_stronger": 0.1,
            "B_stronger": 0.4,
            "incomparable": 0.3,
            "unrelated": 0.2,
        },
    )
    assert reverse == forward.swapped()
    assert sum(
        (
            forward.equivalent,
            forward.A_stronger,
            forward.B_stronger,
            forward.incomparable,
            forward.unrelated,
            forward.ambiguous,
        )
    ) == pytest.approx(1.0)
    assert forward.same_claim_probability == forward.equivalent


def test_backbone_selection_applies_quality_then_efficiency_ties() -> None:
    winner = select_backbone(
        (
            PilotCandidateResult("fast-heavy", 0.005, 0.01, 100.0, 1000, 400),
            PilotCandidateResult("near-fast-light", 0.009, 0.019, 96.0, 700, 200),
            PilotCandidateResult("quality-fail", 0.011, 0.0, 200.0, 100, 10),
        )
    )
    assert winner.model_id == "near-fast-light"


def test_backbone_selection_fails_if_no_quality_survivor() -> None:
    with pytest.raises(ValueError, match="quality bounds"):
        select_backbone((PilotCandidateResult("x", 0.02, 0.03, 1.0, 1, 1),))


def test_gate3_audit_uses_frozen_denominator_and_enumerates_lossy_collisions() -> None:
    theorem_a = TheoremForRepresentation(
        theorem_id="thm:" + "a" * 64,
        full_name="a",
        proof_stripped="theorem a : True := by sorry",
        context_id="ctx:" + "0" * 64,
    )
    theorem_b = TheoremForRepresentation(
        theorem_id="thm:" + "b" * 64,
        full_name="b",
        proof_stripped="theorem b : False := by sorry",
        context_id="ctx:" + "0" * 64,
    )
    record_a = _build_record(
        theorem_a,
        "same lossy signature",
        "same lossy signature",
        {"k": "const", "n": "True", "us": "[]"},
        _UTC,
    )
    record_b = _build_record(
        theorem_b,
        "same lossy signature",
        "same lossy signature",
        {"k": "const", "n": "False", "us": "[]"},
        _UTC,
    )
    report = audit_representations(
        (record_a, record_b),
        source_by_theorem={theorem_a.theorem_id: "mathlib", theorem_b.theorem_id: "mathlib"},
    )
    assert report["record_count"] == 2
    assert report["coverage"]["mathlib"]["signature_pp"]["denominator"] == 2
    assert report["identity_fingerprint_coverage"]["mathlib"] == {
        "successes": 2,
        "denominator": 2,
        "rate": 1.0,
        "threshold": 1.0,
    }
    assert report["cryptographic_or_alpha_collisions"] == []
    assert report["lossy_collision_cluster_count"] >= 1
    assert report["manual_audit_status"] == "pending"
    assert not report["gate_pass"]

    selected_key = report["manual_audit_required"][0]
    selected = next(
        cluster
        for cluster in report["lossy_collision_clusters"]
        if cluster["view"] == selected_key["view"]
        and cluster["view_hash"] == selected_key["view_hash"]
    )
    review = ManualCollisionReview(
        view=selected["view"],
        view_hash=selected["view_hash"],
        reason_code=selected["reason_code"],
        theorem_ids=tuple(selected["theorem_ids"]),
        alpha_fingerprints=tuple(selected["alpha_fingerprints"]),
        disposition="expected_lossy_projection",
        reviewer_id="fixture-reviewer",
        notes="Distinct elaborated identities intentionally collapse in this lossy view.",
    )
    closed = close_manual_collision_audit(report, (review,))
    assert closed["manual_audit_status"] == "complete"
    assert closed["gate_pass"]

    pending = close_manual_collision_audit(report, ())
    assert pending["manual_audit_status"] == "failed"
    assert not pending["gate_pass"]

    defect = close_manual_collision_audit(
        report,
        (review.model_copy(update={"disposition": "representation_defect"}),),
    )
    assert not defect["gate_pass"]

    missing_record_report = audit_representations(
        (record_a,),
        source_by_theorem={
            theorem_a.theorem_id: "mathlib",
            theorem_b.theorem_id: "mathlib",
        },
    )
    assert missing_record_report["coverage"]["mathlib"]["signature_pp"]["denominator"] == 2
    assert missing_record_report["coverage"]["mathlib"]["signature_pp"]["successes"] == 1
    assert missing_record_report["identity_coverage_failures"]
    assert not missing_record_report["mechanical_pass"]


def test_gate3_audit_rejects_stale_normalization_version() -> None:
    theorem = TheoremForRepresentation(
        theorem_id="thm:" + "c" * 64,
        full_name="stale",
        proof_stripped="theorem stale : True := by sorry",
        context_id="ctx:" + "0" * 64,
    )
    current = _build_record(
        theorem,
        "True",
        "True",
        {"k": "const", "n": "True", "us": "[]"},
        _UTC,
    )
    stale = current.model_copy(update={"normalization_version": "repr_v1"})

    report = audit_representations(
        (stale,),
        source_by_theorem={theorem.theorem_id: "mathlib"},
    )

    assert report["expected_normalization_version"] == "repr_v3"
    assert report["normalization_version_errors"] == [
        f"normalization version mismatch: {theorem.theorem_id}:repr_v1!=repr_v3"
    ]
    assert not report["mechanical_pass"]


def test_representation_audit_fails_context_source_and_content_binding() -> None:
    theorem = TheoremForRepresentation(
        theorem_id="thm:" + "d" * 64,
        full_name="bound",
        proof_stripped="theorem bound : True := by sorry",
        context_id="ctx:" + "0" * 64,
        source_signature=": True",
    )
    record = _build_record(
        theorem,
        "True",
        "True",
        {"k": "const", "n": "True", "us": "[]"},
        _UTC,
    )
    report = audit_representations(
        (record.model_copy(update={"context_id": "ctx:" + "1" * 64}),),
        source_by_theorem={theorem.theorem_id: "mathlib"},
        expected_context_by_theorem={theorem.theorem_id: theorem.context_id},
        expected_raw_by_theorem={theorem.theorem_id: "different stripped declaration"},
        expected_headless_by_theorem={theorem.theorem_id: ": False"},
    )
    assert report["context_attachment_errors"]
    assert report["source_view_errors"]
    assert not report["proof_leakage_check"]["passed"]
    assert not report["mechanical_pass"]

    corrupted = record.model_copy(update={"content_hash": "f" * 64})
    hash_report = audit_representations(
        (corrupted,),
        source_by_theorem={theorem.theorem_id: "mathlib"},
    )
    assert hash_report["content_hash_errors"]
    assert not hash_report["mechanical_pass"]


def test_manifest_level_audit_failure_recomputes_gate_pass(tmp_path: Path) -> None:
    from leanfaith.cli.pipeline import run_audit_representations
    from leanfaith.schemas import make_id
    from tests.unit.record_factories import theorem_record

    expected_theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"manifest_gate": "expected"}),
        full_name="expected",
        proof_stripped="theorem expected : True := by sorry",
        context_id="ctx:" + "0" * 64,
        source_signature=": True",
    )
    unexpected_theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"manifest_gate": "unexpected"}),
        full_name="unexpected",
        proof_stripped="theorem unexpected : False := by sorry",
        context_id="ctx:" + "0" * 64,
        source_signature=": False",
    )
    expected_record = _build_record(
        expected_theorem,
        "True",
        "True",
        {"k": "const", "n": "True", "us": "[]"},
        _UTC,
    )
    unexpected_record = _build_record(
        unexpected_theorem,
        "False",
        "False",
        {"k": "const", "n": "False", "us": "[]"},
        _UTC,
    )
    ancestry_id = make_id("anc", {"manifest_gate": "expected"})
    theorem = theorem_record(
        theorem_id=expected_theorem.theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="mathlib",
        context_id=expected_theorem.context_id,
        declaration_name=expected_theorem.full_name,
        declaration_full_name=expected_theorem.full_name,
        proof_stripped_declaration=expected_theorem.proof_stripped,
    )
    theorem_path = tmp_path / "theorems.jsonl"
    theorem_path.write_text(
        json.dumps(
            {
                "theorem": theorem.model_dump(mode="json"),
                "representation": {"headless": expected_theorem.source_signature},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    representation_path = tmp_path / "representations.jsonl"
    representation_path.write_text(
        "".join(
            json.dumps(record.model_dump(mode="json")) + "\n"
            for record in (expected_record, unexpected_record)
        ),
        encoding="utf-8",
    )
    failure_path = tmp_path / "failures.jsonl"
    failure_path.write_text("", encoding="utf-8")

    _, report = run_audit_representations(
        representation_jsonl=representation_path,
        theorem_jsonl=theorem_path,
        failure_jsonl=failure_path,
        frozen_manifest_path=None,
        out_path=tmp_path / "audit.json",
    )
    assert report["manual_audit_status"] == "not_required"
    assert report["unexpected_theorem_ids"] == [unexpected_theorem.theorem_id]
    assert not report["mechanical_pass"]
    assert not report["gate_pass"]


def test_representation_replay_ignores_timestamp_but_not_content(tmp_path: Path) -> None:
    theorem = TheoremForRepresentation(
        theorem_id="thm:" + "c" * 64,
        full_name="c",
        proof_stripped="theorem c : True := by sorry",
        context_id="ctx:" + "0" * 64,
    )
    first = _build_record(
        theorem,
        "True",
        "True",
        {"k": "const", "n": "True", "us": "[]"},
        _UTC,
    )
    second = first.model_copy(update={"created_at": _UTC + datetime.timedelta(days=1)})
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    left.write_text(json.dumps(first.model_dump(mode="json")) + "\n", encoding="utf-8")
    right.write_text(json.dumps(second.model_dump(mode="json")) + "\n", encoding="utf-8")
    assert compare_representation_replays(left, right).ok

    changed = second.model_copy(update={"content_hash": "f" * 64})
    right.write_text(json.dumps(changed.model_dump(mode="json")) + "\n", encoding="utf-8")
    assert not compare_representation_replays(left, right).ok
