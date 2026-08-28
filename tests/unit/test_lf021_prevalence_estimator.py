from __future__ import annotations

import os
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.evaluation.prevalence import (
    RANDOMIZED_SAMPLING_METHOD,
    AdjudicationProjectionV1,
    IntervalStatus,
    PointEstimateScope,
    PrevalenceFrameUnitV2,
    PrevalenceInputError,
    VerifiedPrevalenceFrameBinding,
    estimate_prevalence,
    load_prevalence_design_policy,
    validate_prevalence_output_path,
    verify_prevalence_design_policy_v2,
    write_prevalence_report,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "policies" / "lf021_prevalence_design_v2.yaml"
FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)
ZERO_SHA = "0" * 64
ONE_SHA = "1" * 64


def _verified_binding(item_count: int) -> VerifiedPrevalenceFrameBinding:
    return VerifiedPrevalenceFrameBinding(
        frame_freeze_decision_id=f"lf021_frame_freeze_decision_v3:{ZERO_SHA}",
        frame_freeze_decision_sha256=ZERO_SHA,
        frame_id=f"lf021_prevalence_frame_v3:{ONE_SHA}",
        frame_artifact=f"frames/{ONE_SHA}.jsonl",
        frame_sha256=ONE_SHA,
        frame_item_count=item_count,
        population_id=f"lf021_eligible_population_v3:{'2' * 64}",
        population_manifest_sha256="3" * 64,
        population_artifact_sha256="4" * 64,
        sampling_method=RANDOMIZED_SAMPLING_METHOD,
        sampling_rank_algorithm="hmac_sha256_keyed_rank_v1",
        sampling_seed_sha256="5" * 64,
        sampling_seed_provenance_sha256="6" * 64,
        test_replay_only=False,
    )


def _frame_item(
    index: int,
    *,
    stratum: str,
    population: int,
    sample: int,
    family_counts: dict[str, int],
    problem_group: str | None = None,
    alpha: str | None = None,
) -> PrevalenceFrameUnitV2:
    positive_family_counts = {family: count for family, count in family_counts.items() if count > 0}
    member_count = sum(family_counts.values())
    return PrevalenceFrameUnitV2(
        frame_record_id=f"lf021_prevalence_item_v2:synthetic:{index}",
        problem_group=problem_group or f"problem_group:synthetic:{index}",
        alpha_identity_fingerprint=alpha or f"{index + 1:064x}",
        sampling_stratum=stratum,
        stratum_population_size=population,
        stratum_sample_size=sample,
        inclusion_probability_numerator=sample,
        inclusion_probability_denominator=population,
        member_count=member_count,
        member_count_by_family=positive_family_counts,
        member_count_by_source_proxy={"synthetic/source_proxy": member_count},
    )


def _frame() -> tuple[PrevalenceFrameUnitV2, ...]:
    return (
        _frame_item(
            0,
            stratum="h1",
            population=4,
            sample=2,
            family_counts={
                FAMILIES[0]: 4,
                FAMILIES[1]: 1,
                FAMILIES[2]: 0,
            },
        ),
        _frame_item(
            1,
            stratum="h1",
            population=4,
            sample=2,
            family_counts={
                FAMILIES[0]: 0,
                FAMILIES[1]: 0,
                FAMILIES[2]: 1,
            },
        ),
        _frame_item(
            2,
            stratum="h2",
            population=2,
            sample=2,
            family_counts={
                FAMILIES[0]: 0,
                FAMILIES[1]: 1,
                FAMILIES[2]: 0,
            },
        ),
        _frame_item(
            3,
            stratum="h2",
            population=2,
            sample=2,
            family_counts={
                FAMILIES[0]: 1,
                FAMILIES[1]: 1,
                FAMILIES[2]: 1,
            },
        ),
    )


def _adjudication(
    item: PrevalenceFrameUnitV2,
    outcome: str,
) -> AdjudicationProjectionV1:
    return AdjudicationProjectionV1.model_validate(
        {
            "schema_version": 1,
            "adjudication_id": f"synthetic-adjudication:{item.frame_record_id}",
            "frame_record_id": item.frame_record_id,
            "resolution_outcome": outcome,
            "terminal": outcome != "unresolved",
        }
    )


def _labels(
    frame: tuple[PrevalenceFrameUnitV2, ...],
) -> tuple[AdjudicationProjectionV1, ...]:
    outcomes = ("same_claim", "not_same_claim", "ambiguous", "same_claim")
    return tuple(
        _adjudication(item, outcome) for item, outcome in zip(frame, outcomes, strict=True)
    )


def _estimate(
    frame: tuple[PrevalenceFrameUnitV2, ...],
    labels: tuple[AdjudicationProjectionV1, ...],
):
    return estimate_prevalence(
        frame=frame,
        adjudications=labels,
        loaded_policy=load_prevalence_design_policy(POLICY),
        verified_frame_binding=_verified_binding(len(frame)),
        adjudication_projection_sha256=ONE_SHA,
    )


def test_prelabel_policy_freezes_estimands_and_three_family_limit() -> None:
    loaded = load_prevalence_design_policy(POLICY)
    verify_prevalence_design_policy_v2(repo_root=ROOT, loaded_policy=loaded)
    policy = loaded.config
    assert policy.status == "frozen_prelabel"
    assert policy.base_v1_design.sha256 == (
        "312bc2905eec9e9c30679aaecf1d12a90f46b7fa01f6e01bdfffe132cc584a27"
    )
    assert policy.target_population.frame_schema_version == 3
    assert policy.target_population.sampling_method == RANDOMIZED_SAMPLING_METHOD
    assert policy.target_population.primary_unit == "problem_group_x_alpha_identity"
    assert policy.primary.singleton_noncertainty_stratum == "supported_by_exact_inversion"
    assert (
        policy.secondary.singleton_noncertainty_stratum
        == "point_estimate_only_interval_unsupported_fail_closed"
    )
    assert policy.scope.required_scalable_families == FAMILIES
    assert policy.scope.three_family_collection_only
    assert not policy.scope.confirmatory_d4_d5_eligible
    assert not policy.scope.heldout_generator_claim_eligible
    assert not policy.semantic_labels_inspected_when_frozen
    assert not policy.gate_5_closed


def test_unequal_probability_primary_and_multiplicity_estimands() -> None:
    frame = _frame()
    report = _estimate(frame, _labels(frame))

    primary = report.primary_problem_claim
    assert primary.weighted_population_total == pytest.approx(6.0)
    assert primary.faithful.point_estimate == pytest.approx(0.5)
    assert primary.unfaithful.point_estimate == pytest.approx(1 / 3)
    assert primary.ambiguous.point_estimate == pytest.approx(1 / 6)
    assert primary.faithful_nonambiguous.point_estimate == pytest.approx(3 / 5)
    assert primary.ambiguous_as_unfaithful.point_estimate == pytest.approx(1 / 2)
    assert primary.faithful.interval.status is IntervalStatus.AVAILABLE
    assert primary.point_estimate_scope is PointEstimateScope.FULL_POPULATION

    invocation = report.secondary_retained_invocation
    assert invocation.weighted_population_total == pytest.approx(16.0)
    assert invocation.faithful.point_estimate == pytest.approx(13 / 16)
    assert invocation.unfaithful.point_estimate == pytest.approx(2 / 16)
    assert invocation.ambiguous.point_estimate == pytest.approx(1 / 16)
    assert invocation.faithful_nonambiguous.point_estimate == pytest.approx(13 / 15)
    assert invocation.faithful.interval.status is IntervalStatus.AVAILABLE

    by_family = report.per_family_retained_invocation
    assert by_family[FAMILIES[0]].faithful.point_estimate == pytest.approx(1.0)
    assert by_family[FAMILIES[1]].faithful.point_estimate == pytest.approx(3 / 4)
    assert by_family[FAMILIES[2]].faithful.point_estimate == pytest.approx(1 / 3)
    assert report.sampled_source_proxy_invocation_counts == {"synthetic/source_proxy": 10}
    assert report.source_proxy_interpretation.endswith("not an adjudicated semantic domain")
    assert report.scope_limitations.three_family_collection_only
    assert report.adjudication_accounting.terminal_record_count == 4
    assert report.adjudication_accounting.explicit_unresolved_record_count == 0
    assert not report.labels_created_by_estimator
    assert not report.gate_5g_closed
    assert not report.gate_5_closed


def test_explicit_unresolved_adjudication_is_nonresponse_with_bounds() -> None:
    frame = _frame()
    labels = (
        _adjudication(frame[0], "same_claim"),
        _adjudication(frame[1], "unresolved"),
        _adjudication(frame[2], "ambiguous"),
        _adjudication(frame[3], "same_claim"),
    )
    report = _estimate(frame, labels)
    primary = report.primary_problem_claim
    assert primary.nonresponse_weight_fraction == pytest.approx(2 / 6)
    assert primary.point_estimate_scope is PointEstimateScope.RESPONDENTS_ONLY_DESCRIPTIVE
    assert primary.faithful_nonambiguous.point_estimate == pytest.approx(1.0)
    assert primary.ambiguous_as_unfaithful.point_estimate == pytest.approx(3 / 4)
    assert primary.nonresponse_bounds.faithful_nonambiguous_lower == pytest.approx(3 / 5)
    assert primary.nonresponse_bounds.faithful_nonambiguous_upper == pytest.approx(1.0)
    assert primary.nonresponse_bounds.ambiguous_as_unfaithful_lower == pytest.approx(1 / 2)
    assert primary.nonresponse_bounds.ambiguous_as_unfaithful_upper == pytest.approx(5 / 6)

    invocation = report.secondary_retained_invocation
    assert invocation.nonresponse_weight_fraction == pytest.approx(2 / 16)
    assert invocation.nonresponse_bounds.faithful_nonambiguous_lower == pytest.approx(13 / 15)
    assert invocation.nonresponse_bounds.faithful_nonambiguous_upper == pytest.approx(1.0)
    assert invocation.nonresponse_bounds.ambiguous_as_unfaithful_lower == pytest.approx(13 / 16)
    assert invocation.nonresponse_bounds.ambiguous_as_unfaithful_upper == pytest.approx(15 / 16)
    assert report.adjudication_accounting.explicit_unresolved_record_count == 1
    assert report.adjudication_accounting.missing_record_count == 0


def test_missing_adjudication_fails_closed_with_reconciled_counts() -> None:
    frame = _frame()
    labels = (
        _adjudication(frame[0], "same_claim"),
        _adjudication(frame[1], "unresolved"),
        _adjudication(frame[2], "ambiguous"),
    )
    with pytest.raises(
        PrevalenceInputError,
        match=(
            r"incomplete adjudication projection: frame_items=4 "
            r"projection_records=3 missing=1 explicit_unresolved=1 terminal=2"
        ),
    ):
        _estimate(frame, labels)


def test_primary_exact_interval_supports_singleton_but_secondary_fails_closed() -> None:
    frame = (
        _frame_item(
            0,
            stratum="singleton",
            population=5,
            sample=1,
            family_counts=dict.fromkeys(FAMILIES, 1),
        ),
    )
    report = _estimate(frame, (_adjudication(frame[0], "same_claim"),))
    assert report.primary_problem_claim.faithful.interval.status is IntervalStatus.AVAILABLE
    assert (
        report.secondary_retained_invocation.faithful.interval.status
        is IntervalStatus.UNSUPPORTED_SINGLETON_NONCERTAINTY_STRATUM
    )
    assert report.secondary_retained_invocation.faithful.point_estimate == 1.0


def test_frame_validation_rejects_duplicate_primary_unit_and_bad_stratum_count() -> None:
    frame = _frame()
    duplicate_claim = frame[1].model_copy(
        update={
            "problem_group": frame[0].problem_group,
            "alpha_identity_fingerprint": frame[0].alpha_identity_fingerprint,
        }
    )
    with pytest.raises(PrevalenceInputError, match="duplicate problem-group"):
        _estimate((frame[0], duplicate_claim, frame[2], frame[3]), _labels(frame))

    with pytest.raises(PrevalenceInputError, match="expected n_h=2"):
        _estimate((frame[0], frame[2], frame[3]), ())


def test_report_is_content_addressed_and_deterministic() -> None:
    frame = _frame()
    labels = _labels(frame)
    first = _estimate(frame, labels)
    second = _estimate(tuple(reversed(frame)), tuple(reversed(labels)))
    assert first == second
    expected = "lf021_prevalence_report_v2:" + hash_canonical(
        {
            "schema": "lf021_prevalence_report_v2",
            **first.model_dump(mode="json", exclude={"report_id"}),
        }
    )
    assert first.report_id == expected


def test_adjudication_projection_rejects_nonterminal_semantic_outcome() -> None:
    with pytest.raises(ValueError, match="terminal must be false"):
        AdjudicationProjectionV1(
            adjudication_id="synthetic",
            frame_record_id="synthetic-frame",
            resolution_outcome="same_claim",
            terminal=False,
        )


def test_all_ambiguous_census_has_undefined_binary_denominator() -> None:
    frame = tuple(
        _frame_item(
            index,
            stratum="census",
            population=3,
            sample=3,
            family_counts={family: 1},
        )
        for index, family in enumerate(FAMILIES)
    )
    report = _estimate(
        frame,
        tuple(_adjudication(item, "ambiguous") for item in frame),
    )
    headline = report.primary_problem_claim.faithful_nonambiguous
    assert headline.point_estimate is None
    assert headline.interval.status is IntervalStatus.UNDEFINED_DENOMINATOR
    assert headline.interval.lower is None
    assert headline.interval.upper is None


def test_all_explicit_unresolved_is_complete_nonresponse_not_missing() -> None:
    frame = _frame()
    report = _estimate(
        frame,
        tuple(_adjudication(item, "unresolved") for item in frame),
    )
    primary = report.primary_problem_claim
    assert primary.point_estimate_scope is PointEstimateScope.RESPONDENTS_ONLY_DESCRIPTIVE
    assert primary.faithful.point_estimate is None
    assert primary.faithful_nonambiguous.point_estimate is None
    assert primary.nonresponse_weight_fraction == 1.0
    assert report.adjudication_accounting.explicit_unresolved_record_count == len(frame)
    assert report.adjudication_accounting.missing_record_count == 0


def test_sparse_multiplicity_maps_reject_zero_values() -> None:
    with pytest.raises(ValueError, match="positive sparse counts"):
        PrevalenceFrameUnitV2(
            frame_record_id="synthetic",
            problem_group="problem",
            alpha_identity_fingerprint="a" * 64,
            sampling_stratum="stratum",
            stratum_population_size=1,
            stratum_sample_size=1,
            inclusion_probability_numerator=1,
            inclusion_probability_denominator=1,
            member_count=1,
            member_count_by_family={"family": 1, "zero": 0},
            member_count_by_source_proxy={"source": 1},
        )


def test_prevalence_output_is_confined_to_dedicated_nonsemantic_root(
    tmp_path: Path,
) -> None:
    frame = _frame()
    report = _estimate(frame, _labels(frame))
    allowed = tmp_path / "reports/prevalence/estimate.json"
    output_sha = write_prevalence_report(report, allowed, repo_root=tmp_path)
    assert len(output_sha) == 64
    assert allowed.is_file()

    forbidden = (
        "reports/gates/gate_5.json",
        "reports/prevalence/labels/output.json",
        "reports/prevalence/annotation/input.json",
        "reports/prevalence/human_annotations/report.json",
        "reports/prevalence/supervision.json",
        "reports/prevalence/splits/report.json",
    )
    for relative in forbidden:
        with pytest.raises(PrevalenceInputError):
            validate_prevalence_output_path(
                repo_root=tmp_path,
                output_path=tmp_path / relative,
            )


def test_prevalence_writer_rejects_divergent_overwrite(tmp_path: Path) -> None:
    frame = _frame()
    report = _estimate(frame, _labels(frame))
    output = tmp_path / "reports/prevalence/estimate.json"
    output.parent.mkdir(parents=True)
    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(PrevalenceInputError, match="divergent"):
        write_prevalence_report(report, output, repo_root=tmp_path)


def test_prevalence_output_rejects_symlinked_namespace_root(
    tmp_path: Path,
) -> None:
    frame = _frame()
    report = _estimate(frame, _labels(frame))
    outside = tmp_path / "outside"
    outside.mkdir()
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "prevalence").symlink_to(outside, target_is_directory=True)
    output = reports / "prevalence/report.json"
    with pytest.raises(PrevalenceInputError, match=r"symlink|escapes"):
        write_prevalence_report(report, output, repo_root=tmp_path)
    assert not (outside / "report.json").exists()


def test_prevalence_output_rejects_symlinked_final_target(
    tmp_path: Path,
) -> None:
    frame = _frame()
    report = _estimate(frame, _labels(frame))
    output = tmp_path / "reports/prevalence/report.json"
    output.parent.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged\n", encoding="utf-8")
    output.symlink_to(outside)

    with pytest.raises(PrevalenceInputError, match=r"symlink"):
        write_prevalence_report(report, output, repo_root=tmp_path)

    assert outside.read_text(encoding="utf-8") == "unchanged\n"


def test_prevalence_output_directory_swap_race_fails_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame()
    report = _estimate(frame, _labels(frame))
    trusted = tmp_path / "reports/prevalence/run"
    trusted.mkdir(parents=True)
    moved = tmp_path / "moved-trusted"
    outside = tmp_path / "outside"
    outside.mkdir()
    output = trusted / "report.json"
    original_link = os.link
    swapped = False

    def swap_then_link(
        src: str,
        dst: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal swapped
        if not swapped:
            trusted.rename(moved)
            trusted.symlink_to(outside, target_is_directory=True)
            swapped = True
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", swap_then_link)
    with pytest.raises(PrevalenceInputError, match=r"symlink|changed"):
        write_prevalence_report(report, output, repo_root=tmp_path)
    assert not (outside / "report.json").exists()
    assert not (moved / "report.json").exists()
