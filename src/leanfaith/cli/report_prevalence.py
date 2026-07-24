"""CLI service for the frozen LF-021 prevalence estimator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from leanfaith.evaluation.prevalence import (
    PrevalenceReportV2,
    estimate_prevalence_from_files,
    write_prevalence_report,
)


@dataclass(frozen=True, slots=True)
class PrevalenceCLIResult:
    report: PrevalenceReportV2
    output_path: Path
    output_sha256: str


def run_report_prevalence(
    *,
    repo_root: Path,
    frame_decision_path: Path,
    adjudication_path: Path,
    policy_path: Path,
    frame_freeze_policy_path: Path,
    output_path: Path,
) -> PrevalenceCLIResult:
    """Estimate and write one deterministic report.

    The caller supplies already-frozen inputs.  This function does not create
    or mutate annotations, supervision records, milestones, or gate reports.
    """

    report = estimate_prevalence_from_files(
        repo_root=repo_root,
        frame_decision_path=frame_decision_path,
        adjudication_path=adjudication_path,
        policy_path=policy_path,
        frame_freeze_policy_path=frame_freeze_policy_path,
    )
    output_sha256 = write_prevalence_report(
        report,
        output_path,
        repo_root=repo_root,
    )
    return PrevalenceCLIResult(
        report=report,
        output_path=output_path,
        output_sha256=output_sha256,
    )


__all__ = ["PrevalenceCLIResult", "run_report_prevalence"]
