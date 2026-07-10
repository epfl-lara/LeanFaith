"""LF-004: Context/Theorem/Representation/Variant/Pair/Evidence record invariants."""

from __future__ import annotations

import pytest

from leanfaith.schemas import (
    DefeqValue,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
    GeneratorKind,
    QualityTier,
    TypecheckValue,
    ViewStatus,
    check_pair_groups,
    make_id,
)
from tests.unit.record_factories import (
    ANC_A,
    ANC_B,
    CTX_FINGERPRINT,
    PAIR_ID,
    THM_A,
    THM_B,
    context_record,
    evidence_record,
    pair_record,
    representation_record,
    theorem_record,
    variant_record,
)


def test_valid_records_construct() -> None:
    context_record()
    theorem_record()
    representation_record()
    variant_record()
    pair_record()
    evidence_record()


def test_context_id_must_match_fingerprint() -> None:
    with pytest.raises(ValueError, match="context_fingerprint"):
        context_record(context_id="ctx:" + "9" * 64)


def test_context_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="surprise"):
        context_record(surprise=1)


def test_theorem_source_requires_single_root_ancestry() -> None:
    with pytest.raises(ValueError, match="exactly one root ancestry"):
        theorem_record(root_ancestry_ids=tuple(sorted((ANC_A, ANC_B))))


def test_theorem_root_ancestries_sorted_unique() -> None:
    parents = (THM_A,)
    unsorted = tuple(sorted((ANC_A, ANC_B), reverse=True))
    with pytest.raises(ValueError, match="sorted and unique"):
        theorem_record(parent_theorem_ids=parents, root_ancestry_ids=unsorted)


def test_theorem_variant_inherits_multiple_roots() -> None:
    record = theorem_record(
        parent_theorem_ids=(THM_A, THM_B),
        root_ancestry_ids=tuple(sorted((ANC_A, ANC_B))),
    )
    assert len(record.root_ancestry_ids) == 2


def test_theorem_rejects_bad_parent_id() -> None:
    with pytest.raises(ValueError, match="parent theorem ID"):
        theorem_record(parent_theorem_ids=("not-an-id",), root_ancestry_ids=(ANC_A,))


def test_representation_null_view_cannot_be_ok() -> None:
    with pytest.raises(ValueError, match="null but marked ok"):
        representation_record(headless=None)


def test_representation_present_view_cannot_be_failed() -> None:
    status = dict(representation_record().view_status)
    status["headless"] = ViewStatus.FAILED
    with pytest.raises(ValueError, match="present but marked"):
        representation_record(view_status=status)


def test_representation_requires_full_view_status_cover() -> None:
    status = dict(representation_record().view_status)
    del status["operator_tree"]
    with pytest.raises(ValueError, match="missing"):
        representation_record(view_status=status)


def test_representation_rejects_unknown_view_name() -> None:
    status = dict(representation_record().view_status)
    status["proof_body"] = ViewStatus.OK
    with pytest.raises(ValueError, match="unknown view names"):
        representation_record(view_status=status)


def test_variant_stays_provisional() -> None:
    with pytest.raises(ValueError, match="provisional until resolution"):
        variant_record(quality_tier=QualityTier.GOLD_CONSERVATIVE_TRANSFORM)


def test_variant_deterministic_requires_sources() -> None:
    with pytest.raises(ValueError, match="requires source theorem IDs"):
        variant_record(
            source_theorem_ids=(),
            generator_kind=GeneratorKind.DETERMINISTIC_TRANSFORM,
        )


def test_variant_rejects_unknown_error_code() -> None:
    with pytest.raises(ValueError, match="E31"):
        variant_record(intended_error_types=("E31",))


def test_pair_split_groups_sorted_unique() -> None:
    groups = tuple(sorted((ANC_A, ANC_B), reverse=True))
    with pytest.raises(ValueError, match="sorted and unique"):
        pair_record(split_group_ids=groups)


def test_check_pair_groups_passes_for_consistent_records() -> None:
    theorem_a = theorem_record()
    theorem_b = theorem_record(theorem_id=THM_B, ancestry_id=ANC_B, root_ancestry_ids=(ANC_B,))
    assert check_pair_groups(pair_record(), theorem_a, theorem_b) == []


def test_check_pair_groups_flags_missing_root() -> None:
    theorem_a = theorem_record()
    theorem_b = theorem_record(theorem_id=THM_B, ancestry_id=ANC_B, root_ancestry_ids=(ANC_B,))
    pair = pair_record(split_group_ids=(ANC_A,))
    violations = check_pair_groups(pair, theorem_a, theorem_b)
    assert any("side B" in violation for violation in violations)


def test_check_pair_groups_flags_missing_nl_group() -> None:
    theorem_a = theorem_record()
    theorem_b = theorem_record(theorem_id=THM_B, ancestry_id=ANC_B, root_ancestry_ids=(ANC_B,))
    pair = pair_record(nl_problem_group="grp:p9")
    violations = check_pair_groups(pair, theorem_a, theorem_b)
    assert any("grp:p9" in violation for violation in violations)


def test_check_pair_groups_flags_id_mismatch() -> None:
    theorem_a = theorem_record()
    violations = check_pair_groups(pair_record(), theorem_a, theorem_a)
    assert any("theorem_b_id" in violation for violation in violations)


def test_evidence_success_requires_value() -> None:
    with pytest.raises(ValueError, match="must carry a value"):
        evidence_record(value=None)


def test_evidence_kind_value_type_must_match() -> None:
    with pytest.raises(ValueError, match="requires value type"):
        evidence_record(kind=EvidenceKind.DEFEQ, value=TypecheckValue(outcome="valid"))


def test_evidence_not_run_carries_no_value() -> None:
    with pytest.raises(ValueError, match="not_run"):
        evidence_record(status=EvidenceExecutionStatus.NOT_RUN)


def test_evidence_failure_statuses_allow_missing_value() -> None:
    record = evidence_record(status=EvidenceExecutionStatus.TIMEOUT, value=None)
    assert record.value is None


def test_evidence_target_prefix_must_match_kind() -> None:
    with pytest.raises(ValueError, match="does not match target_kind"):
        evidence_record(target_kind=EvidenceTargetKind.LEAN_PAIR, target_id=THM_A)


def test_evidence_pair_kind_accepts_pair_target() -> None:
    record = evidence_record(
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=PAIR_ID,
        kind=EvidenceKind.DEFEQ,
        value=DefeqValue(outcome="not_equal"),
    )
    assert record.value is not None


def test_evidence_family_target_accepts_registry_key() -> None:
    record = evidence_record(
        target_kind=EvidenceTargetKind.TRANSFORMATION_FAMILY,
        target_id="p01_alpha",
        kind=EvidenceKind.TRANSFORMATION_AUDIT,
        status=EvidenceExecutionStatus.NOT_RUN,
        value=None,
    )
    assert record.target_id == "p01_alpha"


def test_evidence_family_target_rejects_pair_kind() -> None:
    with pytest.raises(ValueError, match="cannot target a transformation family"):
        evidence_record(
            target_kind=EvidenceTargetKind.TRANSFORMATION_FAMILY,
            target_id="p01_alpha",
            kind=EvidenceKind.DEFEQ,
            status=EvidenceExecutionStatus.NOT_RUN,
            value=None,
        )


def test_ids_are_deterministic_for_records() -> None:
    assert make_id("ctx", {"f": CTX_FINGERPRINT}) == make_id("ctx", {"f": CTX_FINGERPRINT})
