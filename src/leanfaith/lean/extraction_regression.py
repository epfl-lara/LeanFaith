"""Immutable Gate-2 extraction-regression comparison.

The expected artifact contains no private source text.  It records only row
indices, route/failure outcomes, declaration names, and statement hashes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.schemas.source import make_hf_source_record_id


@dataclass(frozen=True, slots=True)
class ExtractionRegressionReport:
    expected_rows: int
    observed_rows: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class ExtractionReplayReport:
    left_theorems: int
    right_theorems: int
    left_failures: int
    right_failures: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class Gate2ScaleAuditReport:
    sample_rows: int
    accepted_rows: int
    failed_rows: int
    theorem_records: int
    failure_records: int
    errors: tuple[str, ...]
    report_hash: str

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _canonical_projection(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _theorem_projection(row: dict[str, Any]) -> str:
    theorem = row.get("theorem", row)
    representation = row.get("representation", {})
    theorem_fields = (
        "source_record_id",
        "theorem_id",
        "ancestry_id",
        "statement_content_hash",
        "raw_row_hash",
        "question_hash",
        "lean_code_hash",
        "extraction_route",
        "question_lean_code_agreement",
        "declaration_name",
        "declaration_full_name",
        "declaration_ordinal",
        "elaboration_status",
        "nl_pair_eligibility",
        "nl_source_link",
        "nl_trust",
    )
    representation_fields = (
        "representation_id",
        "normalization_version",
        "content_hash",
        "headless",
        "signature_pp",
        "view_status",
    )
    return _canonical_projection(
        {
            "theorem": {field: theorem.get(field) for field in theorem_fields},
            "inline_elaboration_source_hash": (
                hash_canonical(theorem.get("inline_elaboration_source"))
                if theorem.get("inline_elaboration_source") is not None
                else None
            ),
            "route_metadata": {
                field: theorem.get("metadata", {}).get(field)
                for field in (
                    "question_parse_status",
                    "fallback_strip_status",
                    "question_route_status",
                    "lean_code_route_status",
                )
            },
            "representation": {field: representation.get(field) for field in representation_fields},
        }
    )


def _failure_projection(row: dict[str, Any]) -> str:
    fields = (
        "source_record",
        "declaration_name",
        "code",
        "outcome_level",
        "extraction_route",
    )
    return _canonical_projection({field: row.get(field) for field in fields})


def compare_extraction_replays(
    *,
    left_theorem_path: Path,
    left_failure_path: Path,
    right_theorem_path: Path,
    right_failure_path: Path,
) -> ExtractionReplayReport:
    """Compare deterministic normalized outcomes while excluding timestamps/raw paths."""

    left_theorems = _read_jsonl(left_theorem_path)
    right_theorems = _read_jsonl(right_theorem_path)
    left_failures = _read_jsonl(left_failure_path)
    right_failures = _read_jsonl(right_failure_path)
    errors: list[str] = []
    comparisons = (
        (
            "theorem",
            [_theorem_projection(row) for row in left_theorems],
            [_theorem_projection(row) for row in right_theorems],
        ),
        (
            "failure",
            [_failure_projection(row) for row in left_failures],
            [_failure_projection(row) for row in right_failures],
        ),
    )
    for kind, left, right in comparisons:
        if len(left) != len(right):
            errors.append(
                f"{kind}: ordered replay length mismatch: left={len(left)}, right={len(right)}"
            )
        mismatch_indices = [
            index
            for index, (left_item, right_item) in enumerate(zip(left, right, strict=False))
            if left_item != right_item
        ]
        if mismatch_indices:
            errors.append(
                f"{kind}: {len(mismatch_indices)} ordered normalized outcomes differ; "
                f"first index={mismatch_indices[0]}"
            )
    return ExtractionReplayReport(
        left_theorems=len(left_theorems),
        right_theorems=len(right_theorems),
        left_failures=len(left_failures),
        right_failures=len(right_failures),
        errors=tuple(errors),
    )


def audit_gate2_scale(
    *,
    sample_path: Path,
    sample_manifest_path: Path,
    extraction_manifest_path: Path,
    theorem_path: Path,
    failure_path: Path,
) -> Gate2ScaleAuditReport:
    """Mechanically reconcile a frozen Gate-2 scale run without denominator filtering."""

    sample_manifest = json.loads(sample_manifest_path.read_text(encoding="utf-8"))
    extraction_manifest = json.loads(extraction_manifest_path.read_text(encoding="utf-8"))
    sample = _read_jsonl(sample_path)
    theorems = _read_jsonl(theorem_path)
    failures = _read_jsonl(failure_path)
    errors: list[str] = []

    expected_sample_hash = sample_manifest.get("output_sha256")
    if hash_file(sample_path) != expected_sample_hash:
        errors.append("frozen sample hash does not match its sampling manifest")
    if sample_manifest.get("sample_rows") != len(sample):
        errors.append("sampling manifest row count does not match frozen sample")
    partitions = sample_manifest.get("input_partitions")
    if not isinstance(partitions, list) or not partitions:
        errors.append("sampling manifest has no input partitions")
    elif any(not item.get("sha256") or not item.get("rows") for item in partitions):
        errors.append("sampling manifest contains an unhashed or empty input partition")

    dataset_id = str(sample_manifest.get("dataset_id", ""))
    revision = str(sample_manifest.get("revision", ""))
    split = str(sample_manifest.get("split", ""))
    expected_ids: set[str] = set()
    observed_indices: set[int] = set()
    for item in sample:
        row_index = item.get("source_row_index")
        if not isinstance(row_index, int):
            errors.append("frozen sample contains a non-integer source_row_index")
            continue
        if row_index in observed_indices:
            errors.append(f"duplicate source_row_index in frozen sample: {row_index}")
        observed_indices.add(row_index)
        expected_ids.add(make_hf_source_record_id(dataset_id, revision, split, row_index))
    if len(expected_ids) != len(sample):
        errors.append("frozen sample source locator IDs are not unique")

    accepted_ids: set[str] = set()
    theorem_ids: set[str] = set()
    for row in theorems:
        theorem = row.get("theorem", row)
        theorem_id = theorem.get("theorem_id")
        if not isinstance(theorem_id, str) or theorem_id in theorem_ids:
            errors.append(f"missing or duplicate theorem_id: {theorem_id!r}")
        else:
            theorem_ids.add(theorem_id)
        source_id = theorem.get("source_record_id")
        if not isinstance(source_id, str) or source_id not in expected_ids:
            errors.append(f"unexpected theorem source_record_id: {source_id!r}")
            continue
        accepted_ids.add(source_id)
        required = (
            "raw_row_hash",
            "question_hash",
            "lean_code_hash",
            "extraction_route",
            "inline_elaboration_source",
            "nl_source_link",
            "nl_trust",
        )
        missing = [field for field in required if theorem.get(field) in (None, "")]
        if missing:
            errors.append(f"accepted source {source_id} lacks provenance fields {missing}")

    failed_ids: set[str] = set()
    for row in failures:
        if row.get("outcome_level") != "row":
            continue
        source_id = row.get("source_record")
        if not isinstance(source_id, str) or source_id not in expected_ids:
            errors.append(f"unexpected row-level failure source_record: {source_id!r}")
            continue
        if source_id in failed_ids:
            errors.append(f"duplicate row-level terminal failure for {source_id}")
        failed_ids.add(source_id)

    overlap = accepted_ids & failed_ids
    if overlap:
        errors.append(f"{len(overlap)} rows have both accepted and failed terminal outcomes")
    missing_ids = expected_ids - accepted_ids - failed_ids
    if missing_ids:
        errors.append(f"{len(missing_ids)} frozen rows have no terminal outcome")
    terminal_ids = accepted_ids | failed_ids
    if len(terminal_ids) != len(sample):
        errors.append(f"terminal outcome coverage is {len(terminal_ids)}/{len(sample)} frozen rows")

    if extraction_manifest.get("attempted_row_count") != len(sample):
        errors.append("extraction manifest attempted_row_count does not equal frozen denominator")
    if extraction_manifest.get("row_count") != len(theorems):
        errors.append("extraction manifest row_count does not match theorem partition")
    declaration_failures = sum(
        1 for failure in failures if failure.get("outcome_level") == "declaration"
    )
    if extraction_manifest.get("declaration_count") != len(theorems) + declaration_failures:
        errors.append("extraction manifest declaration_count does not reconcile")
    terminal_counts = extraction_manifest.get("terminal_outcome_counts", {})
    if not isinstance(terminal_counts, dict) or sum(terminal_counts.values()) != len(sample):
        errors.append("extraction manifest terminal counts do not reconcile")
    if not extraction_manifest.get("config_hash"):
        errors.append("extraction manifest config hash is empty")
    for key in ("environment_hash", "context_hash", "code_tree_hash"):
        if not extraction_manifest.get(key):
            errors.append(f"extraction manifest {key} is empty")

    sample_hash = hash_file(sample_path)
    if sample_hash not in extraction_manifest.get("input_partition_checksums", {}).values():
        errors.append("extraction manifest does not bind the frozen sample hash")
    theorem_hash = hash_file(theorem_path)
    if theorem_hash not in extraction_manifest.get("output_partition_checksums", {}).values():
        errors.append("extraction manifest theorem partition hash mismatch")
    failure_hash = hash_file(failure_path)
    if failure_hash not in extraction_manifest.get("failure_partition_checksums", {}).values():
        errors.append("extraction manifest failure partition hash mismatch")

    payload = {
        "sample_rows": len(sample),
        "accepted_rows": len(accepted_ids),
        "failed_rows": len(failed_ids),
        "theorem_records": len(theorems),
        "failure_records": len(failures),
        "errors": errors,
    }
    return Gate2ScaleAuditReport(
        sample_rows=len(sample),
        accepted_rows=len(accepted_ids),
        failed_rows=len(failed_ids),
        theorem_records=len(theorems),
        failure_records=len(failures),
        errors=tuple(errors),
        report_hash=hash_canonical(payload),
    )


def validate_sft_classic_regression(
    *,
    input_path: Path,
    theorem_path: Path,
    failure_path: Path,
    expected_path: Path,
) -> ExtractionRegressionReport:
    """Compare one extraction run to the versioned per-row expectation."""

    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    input_hash = hash_file(input_path)
    if input_hash != expected["input_sha256"]:
        errors.append(
            f"input hash mismatch: expected {expected['input_sha256']}, observed {input_hash}"
        )

    expected_rows = {int(row["row_index"]): row for row in expected["rows"]}
    source_ids = {
        make_hf_source_record_id(
            expected["dataset_id"], expected["revision"], expected["split"], row_index
        ): row_index
        for row_index in expected_rows
    }
    observed: dict[int, dict[str, Any]] = {}
    for row in _read_jsonl(theorem_path):
        theorem = row.get("theorem", row)
        source_record_id = theorem.get("source_record_id")
        row_index = source_ids.get(source_record_id)
        if row_index is None:
            errors.append(f"unexpected theorem source_record_id {source_record_id!r}")
            continue
        if row_index in observed:
            errors.append(f"row {row_index}: multiple terminal outcomes")
            continue
        observed[row_index] = {
            "outcome": "accepted",
            "extraction_route": theorem.get("extraction_route"),
            "declaration_name": theorem.get("declaration_name"),
            "statement_content_hash": theorem.get("statement_content_hash"),
        }

    for failure in _read_jsonl(failure_path):
        if failure.get("outcome_level") != "row":
            continue
        source_record = failure.get("source_record")
        row_index = source_ids.get(source_record) if isinstance(source_record, str) else None
        if row_index is None:
            errors.append(f"unexpected failure source_record {failure.get('source_record')!r}")
            continue
        if row_index in observed:
            errors.append(f"row {row_index}: multiple terminal outcomes")
            continue
        observed[row_index] = {
            "outcome": "failure",
            "failure_code": failure.get("code"),
        }

    for row_index, expected_row in expected_rows.items():
        actual = observed.get(row_index)
        if actual is None:
            errors.append(f"row {row_index}: missing terminal outcome")
            continue
        expected_fields = {key: value for key, value in expected_row.items() if key != "row_index"}
        if actual != expected_fields:
            errors.append(f"row {row_index}: expected {expected_fields!r}, observed {actual!r}")
    for row_index in sorted(set(observed) - set(expected_rows)):
        errors.append(f"row {row_index}: not present in expected artifact")

    return ExtractionRegressionReport(
        expected_rows=len(expected_rows),
        observed_rows=len(observed),
        errors=tuple(errors),
    )
