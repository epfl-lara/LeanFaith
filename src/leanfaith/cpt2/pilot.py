"""One-example and bounded 10K/oracle gates for CPT2."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanfaith.cpt2.oracle import OracleObservation
from leanfaith.cpt2.source import SourceRow, SourceSnapshot, snapshot_to_dict
from leanfaith.cpt2.splitters import (
    DECLARATION_AWARE_METHOD,
    RAW_REVERSE_METHOD,
    SPLITTERS,
    SplitResult,
    source_features,
    split_source,
)

SCHEMA_VERSION = "cpt2_theorem_body_label_v1"
PILOT_VERSION = "cpt2_splitter_pilot_v1"
MIN_EXACT_AGREEMENT = 0.99
MIN_COVERAGE = 0.98


@dataclass(frozen=True, slots=True)
class MethodAudit:
    method: str
    eligible: int
    total: int
    elapsed_seconds: float
    rows_per_second: float
    coverage: float
    exact_matches: int = 0
    oracle_boundaries: int = 0
    exact_rate: float = 0.0


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def serialize_row(split: SplitResult, label: bool) -> dict[str, str | bool]:
    """Return the entire core CPT2 schema, preserving the source bool exactly."""

    if type(label) is not bool:
        raise TypeError("CPT2 label must be the source isValid bool")
    return {"theorem": split.theorem, "body": split.body, "label": label}


def _canonical_line(row: Mapping[str, str | bool]) -> bytes:
    if tuple(row) != ("theorem", "body", "label"):
        raise ValueError("CPT2 row schema must be exactly theorem/body/label")
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_artifact(
    output_dir: Path,
    *,
    rows: Sequence[Mapping[str, str | bool]],
    manifest: Mapping[str, Any],
    build_id: str,
) -> tuple[Path, Path, bool]:
    """Write data then its completion manifest; verify and resume completed builds."""

    data_path = output_dir / "data.jsonl"
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("build_id") != build_id:
            raise ValueError("completed CPT2 artifact belongs to a different deterministic build")
        expected_hash = str(existing.get("data_sha256") or "")
        if not data_path.is_file() or _sha256_file(data_path) != expected_hash:
            raise ValueError("completed CPT2 artifact data hash does not match its manifest")
        return data_path, manifest_path, True

    data = b"".join(_canonical_line(row) for row in rows)
    _atomic_write(data_path, data)
    payload = dict(manifest)
    payload.update(
        {
            "build_id": build_id,
            "schema_version": SCHEMA_VERSION,
            "data_file": data_path.name,
            "data_rows": len(rows),
            "data_sha256": _sha256_bytes(data),
        }
    )
    _atomic_write(
        manifest_path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return data_path, manifest_path, False


def _quantiles(values: Sequence[int]) -> dict[str, int]:
    if not values:
        return {"min": 0, "p25": 0, "p50": 0, "p75": 0, "p95": 0, "max": 0}
    ordered = sorted(values)

    def at(fraction: float) -> int:
        return ordered[round((len(ordered) - 1) * fraction)]

    return {
        "min": ordered[0],
        "p25": at(0.25),
        "p50": at(0.50),
        "p75": at(0.75),
        "p95": at(0.95),
        "max": ordered[-1],
    }


def benchmark_methods(rows: Sequence[SourceRow]) -> tuple[MethodAudit, ...]:
    audits: list[MethodAudit] = []
    for method, splitter in SPLITTERS.items():
        started = time.perf_counter()
        eligible = sum(splitter(row.source_code) is not None for row in rows)
        elapsed = time.perf_counter() - started
        audits.append(
            MethodAudit(
                method=method,
                eligible=eligible,
                total=len(rows),
                elapsed_seconds=elapsed,
                rows_per_second=(len(rows) / elapsed if elapsed else float("inf")),
                coverage=(eligible / len(rows) if rows else 0.0),
            )
        )
    return tuple(audits)


def _length_boundaries(rows: Sequence[SourceRow]) -> tuple[int, int, int]:
    lengths = sorted(len(row.source_code) for row in rows)
    if not lengths:
        return (0, 0, 0)
    return (
        lengths[round((len(lengths) - 1) * 0.25)],
        lengths[round((len(lengths) - 1) * 0.50)],
        lengths[round((len(lengths) - 1) * 0.75)],
    )


def _length_bucket(length: int, boundaries: tuple[int, int, int]) -> str:
    if length <= boundaries[0]:
        return "q1"
    if length <= boundaries[1]:
        return "q2"
    if length <= boundaries[2]:
        return "q3"
    return "q4"


def _stratum(row: SourceRow, boundaries: tuple[int, int, int]) -> str:
    split = split_source(row.source_code, DECLARATION_AWARE_METHOD)
    features = source_features(row.source_code, split)
    return "|".join(
        (
            f"label={str(row.is_valid).lower()}",
            f"length={_length_bucket(len(row.source_code), boundaries)}",
            f"multi={int(bool(features['multiple_declarations']))}",
            f"nested_by={int(bool(features['nested_by']))}",
            f"masked={int(bool(features['comments_strings_or_chars']))}",
            f"source_shard={row.row_group}",
        )
    )


def select_oracle_rows(rows: Sequence[SourceRow], *, count: int = 500) -> tuple[SourceRow, ...]:
    """Round-robin deterministic strata so rare lexical cases cannot disappear."""

    if count <= 0 or count > 500:
        raise ValueError("CPT2 oracle selection count must be in 1..500")
    eligible_count = sum(
        any(split_source(row.source_code, method) is not None for method in SPLITTERS)
        for row in rows
    )
    if eligible_count < count:
        raise ValueError("not enough cheap-sample rows for the oracle")
    # Build the strata over the full frozen cheap sample, then skip unmatched
    # rows in place. This preserves the deterministic order if eligibility
    # filtering is tightened after an interrupted, cached oracle run.
    boundaries = _length_boundaries(rows)
    buckets: dict[str, deque[SourceRow]] = {}
    grouped: dict[str, list[SourceRow]] = defaultdict(list)
    for row in rows:
        grouped[_stratum(row, boundaries)].append(row)
    for key, values in grouped.items():
        values.sort(
            key=lambda row: hashlib.sha256(f"cpt2-oracle-v1\0{row.source_id}".encode()).hexdigest()
        )
        buckets[key] = deque(values)
    selected: list[SourceRow] = []
    while len(selected) < count:
        progressed = False
        for key in sorted(buckets):
            if buckets[key]:
                row = buckets[key].popleft()
                progressed = True
                eligible = any(
                    split_source(row.source_code, method) is not None for method in SPLITTERS
                )
                if eligible:
                    selected.append(row)
                if len(selected) == count:
                    break
        if not progressed:
            raise AssertionError("oracle strata exhausted before requested count")
    return tuple(selected)


def add_oracle_agreement(
    rows: Sequence[SourceRow],
    observations: Sequence[OracleObservation],
    cheap_audits: Sequence[MethodAudit],
) -> tuple[MethodAudit, ...]:
    if len(rows) != len(observations):
        raise ValueError("oracle rows and observations differ in length")
    row_by_id = {row.source_id: row for row in rows}
    if len(row_by_id) != len(rows):
        raise ValueError("duplicate source IDs in oracle sample")
    exact = Counter[str]()
    for observation in observations:
        row = row_by_id[observation.source_id]
        for method in SPLITTERS:
            split = split_source(row.source_code, method)
            if split is not None and split.by_offset == observation.boundary:
                exact[method] += 1
    denominator = sum(observation.boundary is not None for observation in observations)
    return tuple(
        MethodAudit(
            method=audit.method,
            eligible=audit.eligible,
            total=audit.total,
            elapsed_seconds=audit.elapsed_seconds,
            rows_per_second=audit.rows_per_second,
            coverage=audit.coverage,
            exact_matches=exact[audit.method],
            oracle_boundaries=denominator,
            exact_rate=(exact[audit.method] / denominator if denominator else 0.0),
        )
        for audit in cheap_audits
    )


def choose_method(audits: Sequence[MethodAudit]) -> str:
    qualified = [
        audit
        for audit in audits
        if audit.coverage >= MIN_COVERAGE and audit.exact_rate >= MIN_EXACT_AGREEMENT
    ]
    if not qualified:
        raise ValueError("no CPT2 splitter meets the frozen coverage/agreement thresholds")
    if any(audit.method == RAW_REVERSE_METHOD for audit in qualified):
        return RAW_REVERSE_METHOD
    return max(qualified, key=lambda audit: audit.rows_per_second).method


def _audit_dict(audit: MethodAudit) -> dict[str, int | float | str]:
    return {
        "method": audit.method,
        "eligible": audit.eligible,
        "total": audit.total,
        "coverage": audit.coverage,
        "elapsed_seconds": audit.elapsed_seconds,
        "rows_per_second": audit.rows_per_second,
        "exact_matches": audit.exact_matches,
        "oracle_boundaries": audit.oracle_boundaries,
        "exact_rate": audit.exact_rate,
    }


def _build_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def run_one_example(
    output_dir: Path,
    *,
    method: str = DECLARATION_AWARE_METHOD,
) -> dict[str, Any]:
    fixtures = (
        ("theorem only_one : True := by\n  trivial\n", True),
        (
            "import Mathlib\n\nlemma helper : True := by trivial\n\n"
            "theorem final_decl : True := by\n  trivial\n",
            False,
        ),
        (
            "theorem inner_by (h : True) : True := by\n"
            "  have h2 : True := by\n    exact h\n  exact h2\n",
            True,
        ),
    )
    rows: list[dict[str, str | bool]] = []
    cases: list[dict[str, Any]] = []
    for index, (source, label) in enumerate(fixtures):
        offsets: dict[str, int] = {}
        for candidate in SPLITTERS:
            result = split_source(source, candidate)
            if result is None or result.reconstruct() != source:
                raise AssertionError(f"one-example splitter failed: {candidate}")
            offsets[candidate] = result.by_offset
        split = split_source(source, method)
        if split is None:
            raise AssertionError(f"one-example selected splitter failed: {method}")
        rows.append(serialize_row(split, label))
        cases.append(
            {
                "case": index,
                "source_sha256": _sha256_bytes(source.encode()),
                "source_label": label,
                "serialized_label": rows[-1]["label"],
                "round_trip": split.reconstruct() == source,
                "candidate_offsets": offsets,
            }
        )
    splitter_path = Path(__file__).with_name("splitters.py")
    identity = {
        "pilot_version": PILOT_VERSION,
        "gate": "one_example",
        "method": method,
        "splitter_source_sha256": _sha256_file(splitter_path),
    }
    data_path, manifest_path, resumed = write_artifact(
        output_dir,
        rows=rows,
        manifest={
            **identity,
            "cases": cases,
            "resume_contract": "manifest-last exact-hash duplicate suppression",
        },
        build_id=_build_id(identity),
    )
    return {
        "data_path": str(data_path),
        "manifest_path": str(manifest_path),
        "resumed": resumed,
        "rows": len(rows),
    }


def finalize_pilot(
    output_dir: Path,
    *,
    snapshot: SourceSnapshot,
    sample_rows: Sequence[SourceRow],
    oracle_rows: Sequence[SourceRow],
    observations: Sequence[OracleObservation],
    cheap_audits: Sequence[MethodAudit],
    blocklist_path: Path,
    code_revision: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    audits = add_oracle_agreement(oracle_rows, observations, cheap_audits)
    selected = choose_method(audits)
    serialized: list[dict[str, str | bool]] = []
    skips = Counter[str]()
    prefix_lengths: list[int] = []
    body_lengths: list[int] = []
    gold_payload = json.loads(blocklist_path.read_text(encoding="utf-8"))
    exact_hashes = frozenset(str(item) for item in gold_payload.get("near_dup_hashes", ()))
    gold_hits: list[str] = []
    for row in sample_rows:
        split = split_source(row.source_code, selected)
        if split is None:
            skips["unmatched_selected_splitter"] += 1
            continue
        if split.reconstruct() != row.source_code:
            skips["round_trip_failure"] += 1
            continue
        theorem_hash = _sha256_bytes(split.theorem.encode())
        if theorem_hash in exact_hashes:
            gold_hits.append(theorem_hash)
            skips["gold_exact_hash_hit"] += 1
            continue
        serialized.append(serialize_row(split, row.is_valid))
        prefix_lengths.append(len(split.theorem))
        body_lengths.append(len(split.body))

    failures = Counter(
        observation.failure or "oracle_boundary_established" for observation in observations
    )
    boundaries = _length_boundaries(oracle_rows)
    per_stratum: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    oracle_by_id = {item.source_id: item for item in observations}
    for row in oracle_rows:
        stratum = _stratum(row, boundaries)
        observation = oracle_by_id[row.source_id]
        per_stratum[stratum]["rows"] += 1
        if observation.boundary is not None:
            per_stratum[stratum]["oracle_boundaries"] += 1
        for method in SPLITTERS:
            split = split_source(row.source_code, method)
            if split is not None and split.by_offset == observation.boundary:
                per_stratum[stratum][method] += 1
    stratum_payload: dict[str, dict[str, int | float]] = {}
    for stratum, counts in sorted(per_stratum.items()):
        payload: dict[str, int | float] = dict(counts)
        oracle_boundaries = counts.get("oracle_boundaries", 0)
        for method in SPLITTERS:
            payload[f"{method}_exact_rate"] = (
                counts.get(method, 0) / oracle_boundaries if oracle_boundaries else 0.0
            )
        stratum_payload[stratum] = payload
    mismatch_examples: list[dict[str, Any]] = []
    for row in oracle_rows:
        observation = oracle_by_id[row.source_id]
        offsets = {
            method: (split.by_offset if (split := split_source(row.source_code, method)) else None)
            for method in SPLITTERS
        }
        if any(offset != observation.boundary for offset in offsets.values()):
            mismatch_examples.append(
                {
                    "source_id": row.source_id,
                    "source_sha256": _sha256_bytes(row.source_code.encode()),
                    "row_group": row.row_group,
                    "row_offset": row.row_offset,
                    "oracle_boundary": observation.boundary,
                    "candidate_boundaries": offsets,
                }
            )
        if len(mismatch_examples) == 20:
            break

    selected_audit = next(audit for audit in audits if audit.method == selected)
    projected_seconds = snapshot.row_count / selected_audit.rows_per_second
    identity = {
        "pilot_version": PILOT_VERSION,
        "source_revision": snapshot.resolved_revision,
        "source_ids_sha256": _sha256_bytes(
            "\n".join(row.source_id for row in sample_rows).encode()
        ),
        "selected_method": selected,
        "code_revision": code_revision,
        "task_code_sha256": str(context.get("task_code_sha256") or ""),
    }
    manifest: dict[str, Any] = {
        **identity,
        "source": snapshot_to_dict(snapshot),
        "cheap_sample_row_groups": sorted({row.row_group for row in sample_rows}),
        "input_pin_matches": (
            str(context.get("contract_source_revision") or snapshot.requested_revision)
            == snapshot.resolved_revision
        ),
        "selection_rules": {
            "cheap_sample": "label-balanced quotas across eight evenly spaced row groups",
            "oracle": "deterministic round-robin label/length/lexical/source-shard strata",
            "thresholds": {
                "minimum_exact_boundary_agreement": MIN_EXACT_AGREEMENT,
                "minimum_eligible_coverage": MIN_COVERAGE,
                "raw_method_priority_if_qualified": True,
            },
        },
        "splitter_audits": [_audit_dict(audit) for audit in audits],
        "oracle": {
            "version": str(context.get("oracle_version") or "unknown"),
            "rows": len(observations),
            "unique_source_rows": int(context.get("oracle_unique_source_rows") or 0),
            "base_attempts": int(context.get("oracle_base_attempts") or 0),
            "targeted_correction_rows": int(context.get("oracle_targeted_correction_rows") or 0),
            "lean_requests_current_run": sum(not item.cache_hit for item in observations),
            "cache_hits": sum(item.cache_hit for item in observations),
            "status_counts": dict(Counter(item.status for item in observations)),
            "failure_counts": dict(failures),
            "stored_elapsed_ms": sum(item.elapsed_ms for item in observations),
            "elapsed_ms_current_run": sum(
                item.elapsed_ms for item in observations if not item.cache_hit
            ),
            "by_stratum": stratum_payload,
            "mismatch_examples": mismatch_examples,
        },
        "output_counts": {
            "input": len(sample_rows),
            "emitted": len(serialized),
            "labels": dict(Counter(str(row["label"]).lower() for row in serialized)),
            "skips": dict(skips),
        },
        "lengths": {
            "theorem": _quantiles(prefix_lengths),
            "body": _quantiles(body_lengths),
        },
        "gold_screen": {
            "blocklist_path": str(blocklist_path),
            "blocklist_sha256": _sha256_file(blocklist_path),
            "comparison": "sha256(exact theorem prefix) against frozen hash entries",
            "hit_count": len(gold_hits),
            "hit_hashes": sorted(set(gold_hits)),
            "action": "excluded",
        },
        "throughput_projection": {
            "full_source_rows": snapshot.row_count,
            "string_split_rows_per_second": selected_audit.rows_per_second,
            "projected_full_run_seconds": projected_seconds,
            "projected_full_run_hours": projected_seconds / 3600,
            "lean_rows_at_scale": 0,
        },
        "context": dict(context),
        "source_label_contract": "label is source compiler_data isValid unchanged",
        "training_started": False,
    }
    data_path, manifest_path, resumed = write_artifact(
        output_dir,
        rows=serialized,
        manifest=manifest,
        build_id=_build_id(identity),
    )
    return {
        "data_path": str(data_path),
        "manifest_path": str(manifest_path),
        "resumed": resumed,
        "selected_method": selected,
        "rows": len(serialized),
        "audits": [_audit_dict(audit) for audit in audits],
    }


def audits_to_json(audits: Iterable[MethodAudit]) -> list[dict[str, int | float | str]]:
    return [_audit_dict(audit) for audit in audits]
