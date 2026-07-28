"""CLI orchestration for fail-closed LF-022 config and replay validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from leanfaith.config.paths import RepoPaths
from leanfaith.generation.lf022_config import (
    LF022FoundationValidationReport,
    ReplayKind,
    validate_lf022_foundation,
)
from leanfaith.schemas.manifest import write_manifest


@dataclass(frozen=True, slots=True)
class LF022ValidationResult:
    report: LF022FoundationValidationReport
    report_path: Path
    report_sha256: str


def run_lf022_validation(
    *,
    paths: RepoPaths,
    variants_config_path: Path,
    judges_config_path: Path,
    report_path: Path,
    replay_kind: ReplayKind | None = None,
    request_path: Path | None = None,
    raw_response_root: Path | None = None,
) -> LF022ValidationResult:
    """Validate the foundation and optionally replay one immutable response."""

    report = validate_lf022_foundation(
        paths=paths,
        variants_path=variants_config_path,
        judges_path=judges_config_path,
        replay_kind=replay_kind,
        request_path=request_path,
        raw_response_root=raw_response_root,
    )
    report_sha256 = write_manifest(report, report_path)
    return LF022ValidationResult(
        report=report,
        report_path=report_path,
        report_sha256=report_sha256,
    )


__all__ = ["LF022ValidationResult", "run_lf022_validation"]
