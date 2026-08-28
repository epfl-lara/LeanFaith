"""Stable orchestration for authenticated LF-023 operator actions."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Literal, cast

from leanfaith.annotation_support.operations import (
    AdjudicationArtifactRun,
    AgreementArtifactRun,
    HumanAssignmentRun,
    SubmissionAttestationRun,
    attest_human_submission,
    create_authenticated_human_assignment,
    write_authenticated_adjudication_queue,
    write_authenticated_agreement,
)
from leanfaith.config.paths import RepoPaths
from leanfaith.schemas.manifest import require_utc


def parse_utc_timestamp(value: str) -> datetime.datetime:
    """Parse an explicit ISO-8601 UTC timestamp without using local time."""

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.datetime.fromisoformat(normalized)
        require_utc(result)
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601 and timezone-aware UTC") from exc
    return result


def run_create_human_assignment(
    *,
    paths: RepoPaths,
    public_bundle_manifest_path: Path,
    private_linkage_manifest_path: Path,
    authentication_key_path: Path,
    round_id: str,
    annotator_slot: str,
    annotator_id: str,
    annotator_principal_hash: str,
    backend_id: str,
    assigned_at: str,
    output_path: Path,
) -> HumanAssignmentRun:
    """Create a pre-response verified-human assignment."""

    if annotator_slot not in {"independent_annotator_1", "independent_annotator_2"}:
        raise ValueError("annotator slot must be one of the two independent slots")
    slot = cast(
        Literal["independent_annotator_1", "independent_annotator_2"],
        annotator_slot,
    )
    return create_authenticated_human_assignment(
        repo_root=paths.root,
        public_bundle_manifest_path=public_bundle_manifest_path,
        private_linkage_manifest_path=private_linkage_manifest_path,
        authentication_key_path=authentication_key_path,
        round_id=round_id,
        annotator_slot=slot,
        annotator_id=annotator_id,
        annotator_principal_hash=annotator_principal_hash,
        backend_id=backend_id,
        assigned_at=parse_utc_timestamp(assigned_at),
        output_path=output_path,
    )


def run_attest_human_submission(
    *,
    paths: RepoPaths,
    human_assignment_path: Path,
    response_path: Path,
    authentication_key_path: Path,
    backend_export_id: str,
    verifier_id: str,
    attested_at: str,
    confirm_operator_human_origin_assertion: bool,
    confirm_backend_export_locked: bool,
    output_path: Path,
) -> SubmissionAttestationRun:
    """Authenticate one exact locked backend response export."""

    return attest_human_submission(
        repo_root=paths.root,
        human_assignment_path=human_assignment_path,
        response_path=response_path,
        authentication_key_path=authentication_key_path,
        backend_export_id=backend_export_id,
        verifier_id=verifier_id,
        attested_at=parse_utc_timestamp(attested_at),
        confirm_operator_human_origin_assertion=confirm_operator_human_origin_assertion,
        confirm_backend_export_locked=confirm_backend_export_locked,
        output_path=output_path,
    )


def run_write_annotation_agreement(
    *,
    paths: RepoPaths,
    first_import_manifest_path: Path,
    second_import_manifest_path: Path,
    authentication_key_path: Path,
    output_path: Path,
) -> AgreementArtifactRun:
    """Reverify two imports and write a raw agreement artifact."""

    return write_authenticated_agreement(
        repo_root=paths.root,
        first_import_manifest_path=first_import_manifest_path,
        second_import_manifest_path=second_import_manifest_path,
        authentication_key_path=authentication_key_path,
        output_path=output_path,
    )


def run_write_adjudication_queue(
    *,
    paths: RepoPaths,
    first_import_manifest_path: Path,
    second_import_manifest_path: Path,
    authentication_key_path: Path,
    output_path: Path,
    policy_trigger_set_path: Path | None = None,
) -> AdjudicationArtifactRun:
    """Reverify two imports and write an unresolved human-routing queue."""

    return write_authenticated_adjudication_queue(
        repo_root=paths.root,
        first_import_manifest_path=first_import_manifest_path,
        second_import_manifest_path=second_import_manifest_path,
        authentication_key_path=authentication_key_path,
        output_path=output_path,
        policy_trigger_set_path=policy_trigger_set_path,
    )


__all__ = [
    "parse_utc_timestamp",
    "run_attest_human_submission",
    "run_create_human_assignment",
    "run_write_adjudication_queue",
    "run_write_annotation_agreement",
]
