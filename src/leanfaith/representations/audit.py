"""Mechanical Gate-3 coverage and collision audit."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.representations.pipeline import alpha_canonical_bytes
from leanfaith.representations.views import NORMALIZATION_VERSION, representation_content_hash
from leanfaith.schemas.enums import ViewStatus
from leanfaith.schemas.ids import HEX64_PATTERN
from leanfaith.schemas.theorem import RepresentationRecord

_THRESHOLDS = {
    "raw_proof_stripped": 1.0,
    "headless": 1.0,
    "signature_pp": 0.99,
    "signature_explicit": 0.99,
    "semantic_atoms": 0.99,
    "operator_tree": 0.98,
}
_IDENTITY_FINGERPRINT_THRESHOLD = 1.0


@dataclass(frozen=True, slots=True)
class LossyCollisionCluster:
    view: str
    view_hash: str
    theorem_ids: tuple[str, ...]
    alpha_fingerprints: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True, slots=True)
class RepresentationReplayReport:
    left_records: int
    right_records: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


class ManualCollisionReview(StrictModel):
    """One terminal human review of a mechanically selected lossy cluster."""

    schema_version: int = 1
    view: str = Field(min_length=1)
    view_hash: str = Field(pattern=HEX64_PATTERN)
    reason_code: str = Field(min_length=1)
    theorem_ids: tuple[str, ...] = Field(min_length=2)
    alpha_fingerprints: tuple[str, ...] = Field(min_length=2)
    disposition: Literal["expected_lossy_projection", "representation_defect"]
    reviewer_id: str = Field(min_length=1)
    notes: str = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_members(self) -> ManualCollisionReview:
        if list(self.theorem_ids) != sorted(set(self.theorem_ids)):
            raise ValueError("theorem_ids must be sorted and unique")
        if list(self.alpha_fingerprints) != sorted(set(self.alpha_fingerprints)):
            raise ValueError("alpha_fingerprints must be sorted and unique")
        return self


def close_manual_collision_audit(
    mechanical_report: Mapping[str, Any],
    reviews: tuple[ManualCollisionReview, ...],
) -> dict[str, Any]:
    """Bind terminal reviews to the exact deterministic Gate-3 sample.

    A lossy collision is not itself a defect. Gate closure requires every
    selected cluster to be reviewed and classified, while any discovered
    representation defect keeps the gate closed.
    """

    required_rows = mechanical_report.get("manual_audit_required")
    cluster_rows = mechanical_report.get("lossy_collision_clusters")
    if not isinstance(required_rows, list) or not isinstance(cluster_rows, list):
        raise ValueError("mechanical report lacks collision audit rows")

    clusters: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in cluster_rows:
        if not isinstance(row, Mapping):
            raise ValueError("lossy collision cluster must be an object")
        key = (str(row.get("view", "")), str(row.get("view_hash", "")))
        if not all(key) or key in clusters:
            raise ValueError("lossy collision clusters contain an invalid or duplicate key")
        clusters[key] = row

    required: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in required_rows:
        if not isinstance(row, Mapping):
            raise ValueError("manual audit requirement must be an object")
        key = (str(row.get("view", "")), str(row.get("view_hash", "")))
        cluster = clusters.get(key)
        if cluster is None or key in required:
            raise ValueError("manual audit requirements do not identify unique clusters")
        required[key] = cluster

    errors: list[str] = []
    reviews_by_key: dict[tuple[str, str], ManualCollisionReview] = {}
    for review in reviews:
        key = (review.view, review.view_hash)
        if key in reviews_by_key:
            errors.append(f"duplicate review: {review.view}:{review.view_hash}")
        reviews_by_key[key] = review

    for key in sorted(required):
        cluster = required[key]
        selected_review = reviews_by_key.get(key)
        if selected_review is None:
            errors.append(f"missing review: {key[0]}:{key[1]}")
            continue
        expected_reason = str(cluster.get("reason_code", ""))
        expected_theorems = tuple(str(value) for value in cluster.get("theorem_ids", ()))
        expected_fingerprints = tuple(str(value) for value in cluster.get("alpha_fingerprints", ()))
        if selected_review.reason_code != expected_reason:
            errors.append(f"reason code mismatch: {key[0]}:{key[1]}")
        if selected_review.theorem_ids != expected_theorems:
            errors.append(f"theorem membership mismatch: {key[0]}:{key[1]}")
        if selected_review.alpha_fingerprints != expected_fingerprints:
            errors.append(f"alpha membership mismatch: {key[0]}:{key[1]}")
        if selected_review.disposition == "representation_defect":
            errors.append(f"representation defect: {key[0]}:{key[1]}")

    for key in sorted(set(reviews_by_key) - set(required)):
        errors.append(f"unexpected review: {key[0]}:{key[1]}")

    mechanical_pass = mechanical_report.get("mechanical_pass") is True
    if not mechanical_pass:
        errors.append("mechanical representation audit did not pass")
    if required:
        status = "complete" if not errors else "failed"
    else:
        status = "not_required" if not errors else "failed"
    return {
        "schema_version": 1,
        "audit_version": "manual_lossy_collision_v1",
        "required_count": len(required),
        "reviewed_count": len(reviews_by_key),
        "manual_audit_status": status,
        "review_errors": errors,
        "gate_pass": mechanical_pass and not errors,
        "reviewed_clusters": [
            review.model_dump(mode="json")
            for review in sorted(reviews, key=lambda item: (item.view, item.view_hash))
        ],
    }


def _canonical_identity(record: RepresentationRecord) -> bytes | None:
    tree = record.operator_tree
    if not isinstance(tree, dict):
        return None
    root = tree.get("root")
    if not isinstance(root, dict):
        return None
    return alpha_canonical_bytes(root)


def _view_bytes(record: RepresentationRecord, view: str) -> bytes | None:
    value = getattr(record, view)
    return None if value is None else canonical_json_bytes(value)


def _replay_projection(record: RepresentationRecord) -> bytes:
    """Deterministic identity/content projection; timestamps are not gate identity."""

    return canonical_json_bytes(
        {
            "representation_id": record.representation_id,
            "theorem_id": record.theorem_id,
            "normalization_version": record.normalization_version,
            "context_id": record.context_id,
            "content_hash": record.content_hash,
            "alpha_identity_fingerprint": record.alpha_identity_fingerprint,
            "view_status": {
                name: status.value for name, status in sorted(record.view_status.items())
            },
            "views": {
                view: sha256_hex(value)
                for view in _THRESHOLDS
                if (value := _view_bytes(record, view)) is not None
            },
        }
    )


def compare_representation_replays(
    left_path: Path,
    right_path: Path,
) -> RepresentationReplayReport:
    """Require exact representation ID/hash/status replay in frozen order."""

    def load(path: Path) -> list[RepresentationRecord]:
        return [
            RepresentationRecord.model_validate(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    left = load(left_path)
    right = load(right_path)
    errors: list[str] = []
    if len(left) != len(right):
        errors.append(f"record count differs: {len(left)} != {len(right)}")
    for index, (left_record, right_record) in enumerate(zip(left, right, strict=False)):
        if _replay_projection(left_record) != _replay_projection(right_record):
            errors.append(
                f"record {index} differs: {left_record.theorem_id} != "
                f"{right_record.theorem_id} or representation content/status changed"
            )
    return RepresentationReplayReport(len(left), len(right), tuple(errors))


def audit_representations(
    records: tuple[RepresentationRecord, ...],
    *,
    source_by_theorem: dict[str, str],
    failure_keys: set[tuple[str, str]] | None = None,
    expected_context_by_theorem: dict[str, str] | None = None,
    expected_raw_by_theorem: dict[str, str] | None = None,
    expected_headless_by_theorem: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, JSON-serializable Gate-3 audit report."""

    by_source: dict[str, list[RepresentationRecord]] = defaultdict(list)
    theorem_ids = [record.theorem_id for record in records]
    representation_ids = [record.representation_id for record in records]
    duplicate_theorem_ids = sorted(
        theorem_id for theorem_id, count in Counter(theorem_ids).items() if count > 1
    )
    duplicate_representation_ids = sorted(
        representation_id
        for representation_id, count in Counter(representation_ids).items()
        if count > 1
    )
    for record in records:
        source = source_by_theorem.get(record.theorem_id, "unexpected")
        by_source[source].append(record)
    expected_by_source = Counter(source_by_theorem.values())
    expected_ids = set(source_by_theorem)
    expected_records = [record for record in records if record.theorem_id in expected_ids]
    normalization_version_errors = [
        f"normalization version mismatch: {record.theorem_id}:"
        f"{record.normalization_version}!={NORMALIZATION_VERSION}"
        for record in records
        if record.normalization_version != NORMALIZATION_VERSION
    ]

    context_attachment_errors: list[str] = []
    source_view_errors: list[str] = []
    content_hash_errors: list[str] = []
    for record in expected_records:
        theorem_id = record.theorem_id
        if expected_context_by_theorem is not None:
            expected_context = expected_context_by_theorem.get(theorem_id)
            if expected_context is None:
                context_attachment_errors.append(f"missing expected context: {theorem_id}")
            elif record.context_id != expected_context:
                context_attachment_errors.append(
                    f"context mismatch: {theorem_id}:{record.context_id}!={expected_context}"
                )
        if expected_raw_by_theorem is not None:
            expected_raw = expected_raw_by_theorem.get(theorem_id)
            if expected_raw is None:
                source_view_errors.append(f"missing expected proof-stripped source: {theorem_id}")
            elif record.raw_proof_stripped != expected_raw:
                source_view_errors.append(f"raw proof-stripped mismatch: {theorem_id}")
        if (
            expected_headless_by_theorem is not None
            and theorem_id in expected_headless_by_theorem
            and record.headless != expected_headless_by_theorem[theorem_id]
        ):
            source_view_errors.append(f"headless source-signature mismatch: {theorem_id}")

        recomputed_content_hash = representation_content_hash(
            {
                "raw_proof_stripped": record.raw_proof_stripped,
                "headless": record.headless,
                "signature_pp": record.signature_pp,
                "signature_explicit": record.signature_explicit,
                "semantic_atoms": (
                    list(record.semantic_atoms) if record.semantic_atoms is not None else None
                ),
                "operator_tree": record.operator_tree,
                "alpha_identity_fingerprint": record.alpha_identity_fingerprint,
            }
        )
        if record.content_hash != recomputed_content_hash:
            content_hash_errors.append(f"representation content hash mismatch: {theorem_id}")

    coverage: dict[str, dict[str, dict[str, float | int]]] = {}
    threshold_failures: list[str] = []
    coverage_sources = sorted(set(expected_by_source) | set(by_source))
    for source in [*coverage_sources, "overall"]:
        source_records = expected_records if source == "overall" else by_source[source]
        denominator = (
            len(source_by_theorem)
            if source == "overall"
            else expected_by_source.get(source, len(source_records))
        )
        views: dict[str, dict[str, float | int]] = {}
        for view, threshold in _THRESHOLDS.items():
            successes = sum(record.view_status[view] == ViewStatus.OK for record in source_records)
            rate = successes / denominator if denominator else 0.0
            views[view] = {
                "successes": successes,
                "denominator": denominator,
                "rate": rate,
                "threshold": threshold,
            }
            if rate < threshold:
                threshold_failures.append(f"{source}:{view}:{rate:.6f}<{threshold:.6f}")
        coverage[source] = views

    identity_fingerprint_coverage: dict[str, dict[str, float | int]] = {}
    identity_coverage_failures: list[str] = []
    for source in [*coverage_sources, "overall"]:
        source_records = expected_records if source == "overall" else by_source[source]
        denominator = (
            len(source_by_theorem)
            if source == "overall"
            else expected_by_source.get(source, len(source_records))
        )
        successes = sum(
            record.alpha_identity_fingerprint is not None
            and _canonical_identity(record) is not None
            for record in source_records
        )
        rate = successes / denominator if denominator else 0.0
        identity_fingerprint_coverage[source] = {
            "successes": successes,
            "denominator": denominator,
            "rate": rate,
            "threshold": _IDENTITY_FINGERPRINT_THRESHOLD,
        }
        if rate < _IDENTITY_FINGERPRINT_THRESHOLD:
            identity_coverage_failures.append(
                f"{source}:alpha_identity_fingerprint:"
                f"{rate:.6f}<{_IDENTITY_FINGERPRINT_THRESHOLD:.6f}"
            )

    fingerprint_to_bytes: dict[str, bytes] = {}
    cryptographic_collisions: list[dict[str, str]] = []
    missing_identity: list[str] = []
    for record in records:
        canonical = _canonical_identity(record)
        fingerprint = record.alpha_identity_fingerprint
        if canonical is None or fingerprint is None:
            missing_identity.append(record.theorem_id)
            continue
        recomputed = sha256_hex(canonical)
        if recomputed != fingerprint:
            cryptographic_collisions.append(
                {
                    "theorem_id": record.theorem_id,
                    "stored": fingerprint,
                    "recomputed": recomputed,
                    "kind": "fingerprint_mismatch",
                }
            )
        previous = fingerprint_to_bytes.setdefault(fingerprint, canonical)
        if previous != canonical:
            cryptographic_collisions.append(
                {
                    "theorem_id": record.theorem_id,
                    "stored": fingerprint,
                    "recomputed": recomputed,
                    "kind": "same_hash_different_canonical_bytes",
                }
            )

    reasons = {
        "signature_pp": "pretty_print_erasure",
        "semantic_atoms": "semantic_atom_projection_loss",
        "operator_tree": "operator_tree_projection_loss",
    }
    clusters: list[LossyCollisionCluster] = []
    for view, reason in reasons.items():
        grouped: dict[str, list[RepresentationRecord]] = defaultdict(list)
        for record in records:
            value = _view_bytes(record, view)
            if value is not None:
                grouped[sha256_hex(value)].append(record)
        for view_hash, group in grouped.items():
            fingerprints = sorted(
                {
                    record.alpha_identity_fingerprint
                    for record in group
                    if record.alpha_identity_fingerprint is not None
                }
            )
            if len(fingerprints) > 1:
                clusters.append(
                    LossyCollisionCluster(
                        view=view,
                        view_hash=view_hash,
                        theorem_ids=tuple(sorted(record.theorem_id for record in group)),
                        alpha_fingerprints=tuple(fingerprints),
                        reason_code=reason,
                    )
                )
    clusters.sort(key=lambda cluster: (cluster.view, cluster.view_hash))
    manual_audit = clusters[: min(200, len(clusters))]
    expected_failure_keys = {
        (record.theorem_id, view)
        for record in records
        for view, status in record.view_status.items()
        if status == ViewStatus.FAILED
    }
    failure_record_errors: list[str] = []
    if failure_keys is not None:
        for missing in sorted(expected_failure_keys - failure_keys):
            failure_record_errors.append(f"missing explicit failure: {missing[0]}:{missing[1]}")
        for extra in sorted(failure_keys - expected_failure_keys):
            failure_record_errors.append(f"unexpected explicit failure: {extra[0]}:{extra[1]}")
    mechanical_pass = (
        not threshold_failures
        and not identity_coverage_failures
        and not cryptographic_collisions
        and not missing_identity
        and not duplicate_theorem_ids
        and not duplicate_representation_ids
        and not failure_record_errors
        and not context_attachment_errors
        and not source_view_errors
        and not content_hash_errors
        and not normalization_version_errors
    )
    manual_audit_status = "pending" if manual_audit else "not_required"
    return {
        "schema_version": 1,
        "record_count": len(records),
        "source_counts": {
            source: {
                "expected": expected_by_source.get(source, 0),
                "represented": len(by_source[source]),
            }
            for source in coverage_sources
        },
        "coverage": coverage,
        "coverage_threshold_failures": threshold_failures,
        "identity_fingerprint_coverage": identity_fingerprint_coverage,
        "identity_coverage_failures": identity_coverage_failures,
        "missing_identity_theorem_ids": sorted(missing_identity),
        "duplicate_theorem_ids": duplicate_theorem_ids,
        "duplicate_representation_ids": duplicate_representation_ids,
        "context_attachment_errors": context_attachment_errors,
        "source_view_errors": source_view_errors,
        "proof_leakage_check": {
            "method": "exact_proof_stripped_and_extracted_headless_source_match_v1",
            "errors": source_view_errors,
            "passed": not source_view_errors,
        },
        "content_hash_errors": content_hash_errors,
        "expected_normalization_version": NORMALIZATION_VERSION,
        "normalization_version_errors": normalization_version_errors,
        "expected_view_failure_count": len(expected_failure_keys),
        "failure_record_errors": failure_record_errors,
        "cryptographic_or_alpha_collisions": cryptographic_collisions,
        "lossy_collision_cluster_count": len(clusters),
        "lossy_collision_clusters": [
            {
                "view": cluster.view,
                "view_hash": cluster.view_hash,
                "theorem_ids": cluster.theorem_ids,
                "alpha_fingerprints": cluster.alpha_fingerprints,
                "reason_code": cluster.reason_code,
            }
            for cluster in clusters
        ],
        "manual_audit_required": [
            {"view": cluster.view, "view_hash": cluster.view_hash} for cluster in manual_audit
        ],
        "manual_audit_status": manual_audit_status,
        "mechanical_pass": mechanical_pass,
        "gate_pass": mechanical_pass and manual_audit_status == "not_required",
        "report_hash": sha256_hex(
            json.dumps(
                {
                    "records": sorted(record.representation_id for record in records),
                    "coverage": coverage,
                    "collisions": [cluster.view_hash for cluster in clusters],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
    }
