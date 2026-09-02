"""Post-pilot sprint scale path: the ~12K reference pool, its persistent-Lean certification,
and the ten resumable 1K-root shard samples/configs that chain automatically.

Every stage here reuses the frozen v5.2 certifier, census inventory, structured mechanism
planner, and sprint pilot runner. The pool is prepared with zero Lean; certification uses at most
the claimed two persistent project-grouped workers; shard freezing is zero Lean and zero provider.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.host_resources import (
    ReservationError,
    claim_resources,
    list_reservations,
    release_resources,
)
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft2a.census import _has_slot_mechanism_coverage, _stratified_select
from leanfaith.sft2a.certified_sample_v52 import (
    FORBIDDEN_MODEL_GOAL_MARKERS,
    CorrectedSampleError,
    _replacement_row,
    certified_shape,
    verify_certified_reference_row,
)
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.legacy import _atomic_exact, _blocklist
from leanfaith.sft2a.mechanisms import (
    SignatureShape,
    applicable_mechanisms,
    plan_structured_mechanism_rotation,
    planning_signature_from_goal_v1,
)
from leanfaith.sft2a.models import SFT2AV52Config
from leanfaith.sft2a.parallel_rehearsal import ParallelRehearsalError, parallel_launch_lock
from leanfaith.sft2a.provider_rehearsal_v52 import (
    _atomic_replace_json,
    _object,
    _repo_path,
    load_provider_rehearsal_v52,
)
from leanfaith.sft2a.reference_certification import (
    _result_document,
    _root,
)
from leanfaith.sft2a.reference_certifier import (
    AuthoritativeReferenceCertifier,
    ReferenceCertifierError,
)
from leanfaith.sft2a.sprint_pilot_v52 import (
    SPRINT_PILOT_VERSION,
    _append_stage,
    _process_tree,
    _redirect_stdio,
    _stage_events,
    _start_tmux,
    _tmux_pane_pid,
    _tmux_session_exists,
    launch_sprint_pilot_v52,
    sprint_capacity_check,
)

POOL_CONFIG_VERSION = "leanfaith_sft2a_sprint_reference_pool_v1"
_SOURCES = ("mathlib", "physlib", "cslib", "compiler_data")


class SprintScaleError(RuntimeError):
    """A pool, certification, shard-freeze, or chain invariant failed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise SprintScaleError(f"non-object JSONL row {path}:{number}")
            rows.append(value)
    return rows


def _jsonl_bytes(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


@dataclass(frozen=True, slots=True)
class LoadedSprintPoolConfig:
    path: Path
    document: dict[str, object]
    sha256: str
    base: LoadedSFT2AConfig
    census_root: Path
    output_root: Path
    shard_root: Path
    allocations: dict[str, int]
    exclusion_sample_paths: tuple[Path, ...]


def load_sprint_pool_config(path: Path) -> LoadedSprintPoolConfig:
    """Load the additive 12K pool/certification/shard config with its pins."""

    resolved = path.resolve()
    document = _object(resolved)
    if document.get("version") != POOL_CONFIG_VERSION:
        raise SprintScaleError("sprint pool config version differs")
    repo_root = Path(__file__).resolve().parents[3]
    base_path = _repo_path(repo_root, document.get("base_config_path"))
    if hash_file(base_path) != document.get("base_config_sha256"):
        raise SprintScaleError("sprint pool base config hash differs")
    base = load_sft2a_config(base_path, verify_binaries=False)
    if not isinstance(base.config, SFT2AV52Config):
        raise SprintScaleError("sprint pool base config is not v5.2")
    census_root = Path(str(document.get("census_root")))
    inventory = census_root / "eligible_roots.jsonl"
    if not inventory.is_file() or hash_file(inventory) != document.get("census_eligible_sha256"):
        raise SprintScaleError("sprint pool census inventory is absent or its hash differs")
    allocations = document.get("allocations")
    if not isinstance(allocations, dict) or set(allocations) != set(_SOURCES):
        raise SprintScaleError("sprint pool allocations must cover the four sources")
    for value in allocations.values():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SprintScaleError("sprint pool allocations must be positive integers")
    exclusions = document.get("exclusion_sample_paths")
    if not isinstance(exclusions, list) or any(
        not isinstance(item, str) or not Path(item).is_file() for item in exclusions
    ):
        raise SprintScaleError("sprint pool exclusion sample paths must exist")
    shards = document.get("shards")
    if not isinstance(shards, dict):
        raise SprintScaleError("sprint pool config lacks the shard contract")
    for key in ("count", "roots_per_shard", "first_shard_provider_concurrency"):
        value = shards.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SprintScaleError(f"sprint shard contract field {key} is malformed")
    gate_receipt = document.get("oracle_v2_gate_receipt_path")
    if not isinstance(gate_receipt, str) or not gate_receipt:
        raise SprintScaleError("sprint pool config must name the oracle-v2 gate receipt path")
    if (
        document.get("lean_workers") != 2
        or document.get("lean_rss_gib") != 40.0
        or document.get("provider_calls_allowed") != 0
    ):
        raise SprintScaleError(
            "sprint certification requires two workers/40 GiB and zero provider calls"
        )
    deadline = shards.get("sprint_deadline_utc")
    if deadline is not None:
        try:
            datetime.fromisoformat(str(deadline))
        except ValueError as exc:
            raise SprintScaleError("shards.sprint_deadline_utc is not ISO-8601") from exc
    return LoadedSprintPoolConfig(
        path=resolved,
        document=document,
        sha256=hash_file(resolved),
        base=base,
        census_root=census_root,
        output_root=Path(str(document["output_root"])),
        shard_root=Path(str(shards["output_root"])),
        allocations={source: int(cast(int, allocations[source])) for source in _SOURCES},
        exclusion_sample_paths=tuple(Path(item) for item in exclusions),
    )


# --------------------------------------------------------------------------------------------
# Pool preparation (zero Lean, zero provider)
# --------------------------------------------------------------------------------------------


def prepare_sprint_reference_pool(loaded: LoadedSprintPoolConfig) -> dict[str, object]:
    """Freeze the stratified pool from the census inventory minus every used root."""

    output = loaded.output_root
    manifest_path = output / "pool_manifest.json"
    if manifest_path.is_file():
        existing = _object(manifest_path)
        if hash_file(output / "pool.jsonl") != existing.get("pool_sha256"):
            raise SprintScaleError("immutable sprint pool replay differs")
        return existing
    used_ids: set[str] = set()
    used_exprs: set[str] = set()
    for sample in loaded.exclusion_sample_paths:
        for row in _jsonl(sample):
            root = cast(dict[str, object], row["root"])
            used_ids.add(str(root["root_id"]))
            certified = row.get("certified_reference")
            if isinstance(certified, dict) and isinstance(certified.get("closed_expr_hash"), str):
                used_exprs.add(str(certified["closed_expr_hash"]))
    _blocklist_path, blocked = _blocklist(loaded.base)
    by_source: dict[str, list[dict[str, object]]] = defaultdict(list)
    rejected: Counter[str] = Counter()
    for row in _jsonl(loaded.census_root / "eligible_roots.jsonl"):
        root_id = str(row["root_id"])
        if root_id in used_ids:
            rejected["already_used_root"] += 1
            continue
        if not isinstance(row.get("source_header"), str):
            rejected["missing_source_header"] += 1
            continue
        signature = str(row["reference_signature"])
        if signature_near_dup_hash(signature) in blocked:
            rejected["gold_contamination"] += 1
            continue
        if not _has_slot_mechanism_coverage(signature):
            rejected["insufficient_mechanism_coverage"] += 1
            continue
        by_source[str(row["source"])].append(row)
    salt = str(loaded.document["pool_salt"])
    selected: list[dict[str, object]] = []
    available: dict[str, int] = {}
    for source in _SOURCES:
        candidates = by_source[source]
        available[source] = len(candidates)
        quota = min(loaded.allocations[source], len(candidates))
        if quota == 0:
            raise SprintScaleError(f"sprint pool has no eligible {source} roots")
        chosen = (
            list(candidates)
            if quota == len(candidates)
            else _stratified_select(candidates, count=quota, salt=f"{salt}:{source}")
        )
        selected.extend({**row, "pool_phase": "sprint_12k"} for row in chosen)
    selected.sort(key=lambda row: (str(row["source"]), str(row["root_id"])))
    if len({str(row["root_id"]) for row in selected}) != len(selected):
        raise SprintScaleError("sprint pool contains duplicate root IDs")
    _atomic_exact(output / "pool.jsonl", _jsonl_bytes(selected))
    counts = Counter(str(row["source"]) for row in selected)
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_sprint_reference_pool_manifest_v1",
        "pool_config_sha256": loaded.sha256,
        "census_eligible_sha256": hash_file(loaded.census_root / "eligible_roots.jsonl"),
        "exclusion_sample_paths": [str(path) for path in loaded.exclusion_sample_paths],
        "excluded_used_roots": len(used_ids),
        "excluded_used_closed_exprs": len(used_exprs),
        "available_after_screens": available,
        "allocations": dict(loaded.allocations),
        "source_counts": dict(sorted(counts.items())),
        "root_count": len(selected),
        "rejected": dict(sorted(rejected.items())),
        "pool_sha256": hash_file(output / "pool.jsonl"),
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest


# --------------------------------------------------------------------------------------------
# Certification through at most two persistent project-grouped workers
# --------------------------------------------------------------------------------------------


def _result_path(output: Path, row: Mapping[str, object]) -> Path:
    return output / "results" / str(row["source"]) / f"{hash_canonical(row['root_id'])}.json"


def _certify_group(
    loaded: LoadedSprintPoolConfig,
    rows: Sequence[dict[str, object]],
    *,
    journal: Path,
    stop_event: threading.Event,
) -> dict[str, int]:
    output = loaded.output_root
    certifier: AuthoritativeReferenceCertifier | None = None
    counts = {"attempts": 0, "cache_hits": 0, "lean_requests": 0, "skipped_existing": 0}
    try:
        for row in sorted(rows, key=lambda item: str(item["root_id"])):
            if stop_event.is_set():
                break
            path = _result_path(output, row)
            if path.is_file():
                counts["skipped_existing"] += 1
                continue
            root = _root(row)
            if certifier is None:
                certifier = AuthoritativeReferenceCertifier(loaded.base, root)
            else:
                certifier.rebind(root)
            result = certifier.certify(
                source_header=str(row["source_header"]),
                compiler_data_theorem_sha256=(
                    str(row["compiler_data_theorem_sha256"])
                    if row.get("compiler_data_theorem_sha256") is not None
                    else None
                ),
            )
            counts["attempts"] += 1
            counts["cache_hits"] += int(result.cache_hit)
            counts["lean_requests"] += int(not result.cache_hit)
            if result.status == "infrastructure":
                raise SprintScaleError(
                    f"infrastructure failure certifying {row['root_id']}: {result.taxonomy}"
                )
            document = _result_document(row, result)
            _atomic_exact(path, canonical_json_bytes(document) + b"\n")
            _append_stage(
                journal,
                {
                    "event": "reference_certified",
                    "root_id": row["root_id"],
                    "source": row["source"],
                    "status": result.status,
                    "taxonomy": result.taxonomy,
                    "cache_hit": result.cache_hit,
                    "elapsed_ms": result.elapsed_ms,
                },
            )
    except ReferenceCertifierError as exc:
        raise SprintScaleError(str(exc)) from exc
    finally:
        if certifier is not None:
            certifier.close()
    return counts


def certify_sprint_pool(
    loaded: LoadedSprintPoolConfig, *, lean_workers: int = 2
) -> dict[str, object]:
    """Certify every pool row through persistent project-grouped certifiers (resumable)."""

    output = loaded.output_root
    manifest_path = output / "certification_manifest.json"
    if manifest_path.is_file():
        return _object(manifest_path)
    rows = _jsonl(output / "pool.jsonl")
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        context = cast(dict[str, object], row["compile_context"])
        groups[str(context["project_id"])].append(row)
    ordered = sorted(groups, key=lambda project: -len(groups[project]))
    journal = output / "certification_journal.jsonl"
    stop_event = threading.Event()
    totals: Counter[str] = Counter()
    errors: list[BaseException] = []
    lock = threading.Lock()
    queue = list(ordered)

    def worker() -> None:
        while not stop_event.is_set():
            with lock:
                if not queue:
                    return
                project = queue.pop(0)
            try:
                counts = _certify_group(
                    loaded, groups[project], journal=journal, stop_event=stop_event
                )
            except Exception as exc:
                with lock:
                    errors.append(exc)
                stop_event.set()
                return
            with lock:
                totals.update(counts)

    started = time.monotonic()
    threads = [threading.Thread(target=worker, name=f"sft2a-cert-{i}") for i in range(lean_workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    elapsed = time.monotonic() - started
    statuses: Counter[str] = Counter()
    taxonomies: Counter[str] = Counter()
    for row in rows:
        result = _object(_result_path(output, row))
        cert = cast(dict[str, object], result["certification"])
        statuses[str(cert["status"])] += 1
        taxonomies[str(cert["taxonomy"])] += 1
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_sprint_reference_certification_manifest_v1",
        "pool_config_sha256": loaded.sha256,
        "pool_sha256": hash_file(output / "pool.jsonl"),
        "root_count": len(rows),
        "project_groups": {project: len(groups[project]) for project in ordered},
        "lean_workers": lean_workers,
        "executed_in_this_invocation": dict(totals),
        "elapsed_seconds_this_invocation": elapsed,
        "status_counts": dict(sorted(statuses.items())),
        "taxonomy_counts": dict(sorted(taxonomies.items())),
        "provider_calls_executed": 0,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest


# --------------------------------------------------------------------------------------------
# Shard freezing (zero Lean, zero provider)
# --------------------------------------------------------------------------------------------


def _screen_certified(
    loaded: LoadedSprintPoolConfig, rows: Sequence[dict[str, object]]
) -> tuple[dict[str, list[dict[str, object]]], Counter[str]]:
    _path, blocked = _blocklist(loaded.base)
    used_exprs: set[str] = set()
    used_goals: set[str] = set()
    for sample in loaded.exclusion_sample_paths:
        for row in _jsonl(sample):
            certified = row.get("certified_reference")
            if isinstance(certified, dict):
                used_exprs.add(str(certified.get("closed_expr_hash")))
                used_goals.add(str(certified.get("rendered_goal_hash")))
    accepted: dict[str, list[dict[str, object]]] = defaultdict(list)
    rejected: Counter[str] = Counter()
    verification_failures: list[dict[str, object]] = []
    for row in rows:
        result_path = _result_path(loaded.output_root, row)
        if not result_path.is_file():
            rejected["missing_result"] += 1
            continue
        cert = _object(result_path).get("certification")
        if not isinstance(cert, dict) or cert.get("status") != "valid":
            rejected[str(cert.get("taxonomy")) if isinstance(cert, dict) else "malformed"] += 1
            continue
        goal = cert.get("goal_v1")
        expr_hash = cert.get("closed_expr_hash")
        rendered = cert.get("rendered_goal_hash")
        if not all(isinstance(value, str) and value for value in (goal, expr_hash, rendered)):
            rejected["malformed_valid_result"] += 1
            continue
        goal_text, expr_text, rendered_text = str(goal), str(expr_hash), str(rendered)
        if any(marker in goal_text for marker in FORBIDDEN_MODEL_GOAL_MARKERS):
            rejected["placeholder_marker"] += 1
            continue
        if signature_near_dup_hash(goal_text) in blocked:
            rejected["gold_contamination"] += 1
            continue
        if expr_text in used_exprs or rendered_text in used_goals:
            rejected["duplicate_of_used_root"] += 1
            continue
        planning = planning_signature_from_goal_v1(goal_text)
        if (
            len(applicable_mechanisms(planning, "preserving")) < 2
            or len(applicable_mechanisms(planning, "breaking")) < 2
        ):
            rejected["insufficient_mechanism_coverage"] += 1
            continue
        # The frozen certificate verifier must accept the row exactly as a shard sample row
        # would carry it; rows it refuses (for example long goals whose raw payload text is
        # line-wrapped by the pretty-printer) are screened out rather than relaxing the verifier.
        try:
            verify_certified_reference_row(_replacement_row(row, result_path))
        except CorrectedSampleError as exc:
            rejected["certificate_verification_failed"] += 1
            verification_failures.append({"root_id": str(row["root_id"]), "reason": str(exc)})
            continue
        used_exprs.add(expr_text)
        used_goals.add(rendered_text)
        accepted[str(row["source"])].append(row)
    if verification_failures:
        _atomic_replace_json_bytes(
            loaded.shard_root / "certificate_verification_failures.jsonl",
            _jsonl_bytes(verification_failures),
        )
    return accepted, rejected


def _shard_quotas(available: Mapping[str, int], *, count: int, per_shard: int) -> dict[str, int]:
    """Proportional per-shard source quotas (largest remainder) capped by availability."""

    total = sum(available.values())
    if total < count * per_shard:
        raise SprintScaleError("certified pool is smaller than the requested shards")
    caps = {source: available[source] // count for source in _SOURCES}
    ideal = {source: per_shard * available[source] / total for source in _SOURCES}
    quotas = {source: min(caps[source], int(ideal[source])) for source in _SOURCES}
    remaining = per_shard - sum(quotas.values())
    while remaining > 0:
        open_sources = [source for source in _SOURCES if quotas[source] < caps[source]]
        if not open_sources:
            raise SprintScaleError("per-source shard quotas exceed the certified availability")
        chosen = max(open_sources, key=lambda source: (ideal[source] - quotas[source], source))
        quotas[chosen] += 1
        remaining -= 1
    return quotas


def freeze_sprint_shards(loaded: LoadedSprintPoolConfig) -> dict[str, object]:
    """Freeze ten disjoint 1K-root shard samples plus chained provider configs."""

    shard_root = loaded.shard_root
    manifest_path = shard_root / "shards_manifest.json"
    if manifest_path.is_file():
        return _object(manifest_path)
    shards = cast(dict[str, object], loaded.document["shards"])
    count = int(cast(int, shards["count"]))
    per_shard = int(cast(int, shards["roots_per_shard"]))
    salt = str(shards["salt"])
    rows = _jsonl(loaded.output_root / "pool.jsonl")
    accepted, rejected = _screen_certified(loaded, rows)
    total_accepted = sum(len(items) for items in accepted.values())
    if total_accepted < count * per_shard:
        raise SprintScaleError(
            f"certified pool has {total_accepted} usable roots; {count * per_shard} are required"
        )
    quotas = _shard_quotas(
        {source: len(accepted[source]) for source in _SOURCES}, count=count, per_shard=per_shard
    )
    ordered_by_source = {
        source: _stratified_select(
            accepted[source], count=quotas[source] * count, salt=f"{salt}:{source}"
        )
        for source in _SOURCES
    }
    base_config = loaded.base.config
    if not isinstance(base_config, SFT2AV52Config):
        raise SprintScaleError("sprint shard freezing requires the v5.2 base config")
    fraction = base_config.mechanism_rotation.maximum_family_fraction_per_polarity
    pilot_defaults = cast(dict[str, object], shards["provider_config"])
    receipts: list[dict[str, object]] = []
    config_paths: list[Path] = []
    for shard_index in range(count):
        chosen = [
            row
            for source in _SOURCES
            for row in ordered_by_source[source][
                shard_index * quotas[source] : (shard_index + 1) * quotas[source]
            ]
        ]
        sample_rows: list[dict[str, object]] = []
        shapes: dict[str, tuple[SignatureShape, str]] = {}
        for pool_row in chosen:
            row = _replacement_row(pool_row, _result_path(loaded.output_root, pool_row))
            root_id = str(cast(dict[str, object], row["root"])["root_id"])
            shapes[root_id] = certified_shape(cast(dict[str, object], row["certified_reference"]))
            sample_rows.append(row)
        rotation = plan_structured_mechanism_rotation(
            [(root_id, shape) for root_id, (shape, _hash) in shapes.items()],
            salt=f"{salt}:structured:{shard_index + 1:02d}",
            maximum_family_fraction_per_polarity=fraction,
        )
        for row in sample_rows:
            root_id = str(cast(dict[str, object], row["root"])["root_id"])
            shape, structure_hash = shapes[root_id]
            row["shape_id"] = shape.shape_id
            row["structured_goal"] = {
                "version": "sft2a_structured_certified_goal_v5_2_1",
                "shape": asdict(shape),
                "structure_hash": structure_hash,
            }
            row["mechanism_plan"] = {
                slot: assignment.to_dict() for slot, assignment in sorted(rotation[root_id].items())
            }
        sample_rows.sort(
            key=lambda row: (
                str(
                    cast(
                        dict[str, object], cast(dict[str, object], row["root"])["compile_context"]
                    )["project_id"]
                ),
                str(cast(dict[str, object], row["root"])["root_id"]),
            )
        )
        shard_dir = shard_root / f"shard_{shard_index + 1:02d}"
        sample_path = shard_dir / "certified_sample.jsonl"
        _atomic_exact(sample_path, _jsonl_bytes(sample_rows))
        config_paths.append(shard_dir / "provider_config.json")
        receipts.append(
            {
                "shard": shard_index + 1,
                "sample_path": str(sample_path),
                "sample_sha256": hash_file(sample_path),
                "roots": len(sample_rows),
                "source_mix": dict(
                    sorted(
                        Counter(
                            str(cast(dict[str, object], row["root"])["source"])
                            for row in sample_rows
                        ).items()
                    )
                ),
            }
        )
    for shard_index, receipt in enumerate(receipts):
        config_path = config_paths[shard_index]
        next_path = config_paths[shard_index + 1] if shard_index + 1 < count else None
        document: dict[str, object] = {
            **pilot_defaults,
            "version": SPRINT_PILOT_VERSION,
            "status": "sprint_authorized",
            "authorized": True,
            "sprint_role": "shard",
            "shard_index": shard_index + 1,
            "shard_count": count,
            "sprint_authority": "plans/72h_sft_data_sprint_2026-09-01.md",
            "sample_path": receipt["sample_path"],
            "sample_sha256": receipt["sample_sha256"],
            "expected_source_mix": receipt["source_mix"],
            "completed_root_sample_paths": [str(path) for path in loaded.exclusion_sample_paths],
            "provider_output_root": str(config_path.parent / "run"),
            "tmux_session": f"leanfaith-sft2a-sprint-shard-{shard_index + 1:02d}",
            "resource_task": f"SFT2A-SPRINT-SHARD-{shard_index + 1:02d}",
            "maximum_root_workers": 1,
            "maximum_total_lean_workers": 1,
            "maximum_measured_rss_gib": 20.0,
            "lean_worker_policy": "single_cooperative_worker_leaves_one_for_sft1_sft2b",
            "controlled_stop_after_completed_roots": 0,
            "oracle_v2_gate_receipt_path": str(loaded.document["oracle_v2_gate_receipt_path"]),
            "shared_candidate_registry_path": str(shard_root / "candidate_registry_shared.jsonl"),
            "sprint_deadline_utc": shards.get("sprint_deadline_utc"),
            "next_shard_config_path": None if next_path is None else str(next_path),
            "legacy_rejudge_authorized": False,
            "publication_authorized": False,
            "scale_10k_authorized": False,
            "scale_50k_authorized": False,
            "training_authorized": False,
        }
        ceilings = cast(dict[str, object], document["ceilings"])
        ceilings["maximum_roots"] = int(cast(int, receipt["roots"]))
        _atomic_replace_json(config_path, document)
        receipt["provider_config_path"] = str(config_path)
        receipt["provider_config_sha256"] = hash_file(config_path)
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_sprint_shards_manifest_v1",
        "pool_config_sha256": loaded.sha256,
        "certified_usable_roots": total_accepted,
        "accepted_by_source": {source: len(accepted[source]) for source in _SOURCES},
        "screen_rejections": dict(sorted(rejected.items())),
        "shard_count": count,
        "roots_per_shard": per_shard,
        "per_shard_quotas": quotas,
        "shards": receipts,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    for config_path in config_paths:
        load_provider_rehearsal_v52(config_path)
    return manifest


# --------------------------------------------------------------------------------------------
# Deterministic combined compaction with cross-shard deduplication
# --------------------------------------------------------------------------------------------


def _dedup_keys(sidecar: Mapping[str, object]) -> tuple[str, str, str]:
    raw = " ".join(str(sidecar.get("raw_candidate_signature", "")).split())
    candidate_record = cast(Mapping[str, object], sidecar.get("candidate_repr") or {}).get("record")
    rendered = (
        str(cast(Mapping[str, object], candidate_record).get("goal_v1", ""))
        if isinstance(candidate_record, Mapping)
        else ""
    )
    return (
        "raw:" + hash_canonical(raw),
        "rendered:" + signature_near_dup_hash(rendered),
        "closed_expr:" + str(sidecar.get("candidate_closed_expr_hash")),
    )


def compact_sprint_shards(
    loaded: LoadedSprintPoolConfig, *, quarantine_row_ids: frozenset[str] = frozenset()
) -> dict[str, object]:
    """Merge every completed shard into one deduplicated, deterministic release view.

    Rows are taken shard by shard in shard order; a row whose raw signature, rendered goal
    near-duplicate key, or closed Expr hash was already seen in an earlier shard (or earlier in
    the same shard) is dropped as a cross-shard duplicate. Rows quarantined by
    ``configs/sft2a/sprint_quarantine_v1.json`` and rows excluded by each shard's Kimi
    telemetry are removed from the releasable view. Zero Lean and zero provider calls.
    """

    shard_root = loaded.shard_root
    shards_manifest = _object(shard_root / "shards_manifest.json")
    core_rows: list[dict[str, object]] = []
    sidecar_rows: list[dict[str, object]] = []
    seen: dict[str, str] = {}
    per_shard: list[dict[str, object]] = []
    duplicates: list[dict[str, object]] = []
    excluded_quarantine = 0
    excluded_audit = 0
    for receipt in cast(list[dict[str, object]], shards_manifest["shards"]):
        run_root = Path(str(receipt["provider_config_path"])).parent / "run"
        compacted = run_root / "compacted"
        if not (compacted / "manifest.json").is_file():
            per_shard.append({"shard": receipt["shard"], "status": "incomplete", "rows": 0})
            continue
        audit_excluded: set[str] = set()
        audit_rows_path = run_root / "audit_kimi/audit_rows.jsonl"
        if audit_rows_path.is_file():
            for row in _jsonl(audit_rows_path):
                if row.get("action") != "retain":
                    audit_excluded.add(str(row["row_id"]))
        core = _jsonl(compacted / "new_core/core.jsonl")
        sidecars = _jsonl(compacted / "new_core/sidecar.jsonl")
        kept = 0
        for core_row, sidecar in zip(core, sidecars, strict=True):
            row_id = str(sidecar["row_id"])
            if row_id in quarantine_row_ids:
                excluded_quarantine += 1
                continue
            if row_id in audit_excluded:
                excluded_audit += 1
                continue
            keys = _dedup_keys(sidecar)
            prior = next((seen[key] for key in keys if key in seen), None)
            if prior is not None:
                duplicates.append(
                    {"row_id": row_id, "duplicate_of": prior, "shard": receipt["shard"]}
                )
                continue
            for key in keys:
                seen[key] = row_id
            core_rows.append(dict(core_row))
            sidecar_rows.append({**sidecar, "sprint_shard": receipt["shard"]})
            kept += 1
        per_shard.append(
            {
                "shard": receipt["shard"],
                "status": "complete",
                "rows": len(core),
                "kept": kept,
                "audit_excluded": len(audit_excluded),
            }
        )
    order = sorted(range(len(sidecar_rows)), key=lambda index: str(sidecar_rows[index]["row_id"]))
    core_rows = [core_rows[index] for index in order]
    sidecar_rows = [sidecar_rows[index] for index in order]
    output = shard_root / "combined"
    output.mkdir(parents=True, exist_ok=True)
    _atomic_replace_json_bytes(output / "core.jsonl", _jsonl_bytes(core_rows))
    _atomic_replace_json_bytes(output / "sidecar.jsonl", _jsonl_bytes(sidecar_rows))
    _atomic_replace_json_bytes(output / "cross_shard_duplicates.jsonl", _jsonl_bytes(duplicates))
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_sprint_combined_compaction_v1",
        "pool_config_sha256": loaded.sha256,
        "shards": per_shard,
        "completed_shards": sum(item["status"] == "complete" for item in per_shard),
        "rows_before_dedup": sum(int(cast(int, item.get("rows", 0))) for item in per_shard),
        "cross_shard_duplicates": len(duplicates),
        "excluded_quarantined": excluded_quarantine,
        "excluded_by_kimi_telemetry": excluded_audit,
        "rows": len(core_rows),
        "positive_rows": sum(bool(row["label"]) for row in core_rows),
        "negative_rows": sum(not bool(row["label"]) for row in core_rows),
        "core_sha256": hash_file(output / "core.jsonl"),
        "sidecar_sha256": hash_file(output / "sidecar.jsonl"),
        "deterministic": True,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
        "published": False,
    }
    _atomic_replace_json(output / "manifest.json", manifest)
    return manifest


def _atomic_replace_json_bytes(path: Path, payload: bytes) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# --------------------------------------------------------------------------------------------
# Detached certification worker, launch, health, and chaining to shard 1
# --------------------------------------------------------------------------------------------


def run_detached_sprint_pool_certification_worker(
    loaded: LoadedSprintPoolConfig,
    *,
    wait_for_capacity_seconds: float = 12 * 3600.0,
    capacity_poll_seconds: float = 60.0,
    redirect_stdio: bool = True,
    launch_first_shard: bool = True,
) -> dict[str, object]:
    """Prepare the pool, certify it under a two-worker claim, freeze shards, launch shard 1."""

    output = loaded.output_root
    detached = output / "detached"
    detached.mkdir(parents=True, exist_ok=True)
    stage_path = detached / "stage_journal.jsonl"
    terminal_path = detached / "terminal_status.json"
    keepalive = _redirect_stdio(detached / "combined.log") if redirect_stdio else None
    print(json.dumps({"event": "worker_stdio_ready", "pid": os.getpid()}), flush=True)
    _append_stage(stage_path, {"event": "worker_started"})
    resource_task = str(loaded.document["resource_task"])
    claimed = False
    try:
        with parallel_launch_lock(detached / "run.lock"):
            prior = _object(terminal_path) if terminal_path.is_file() else {}
            if prior.get("status") == "complete":
                raise SprintScaleError("completed sprint certification cannot be restarted")
            pool = prepare_sprint_reference_pool(loaded)
            _append_stage(stage_path, {"event": "pool_prepared", "roots": pool["root_count"]})
            waits = 0
            deadline = time.monotonic() + wait_for_capacity_seconds
            claimed_workers = 0
            while True:
                # Cooperative claim: take both workers only when both are free right now;
                # otherwise take one so SFT1/SFT2B Lean work is never starved by certification.
                claimed_workers = 0
                for workers in (2, 1):
                    try:
                        claim_resources(
                            task=resource_task,
                            lean_workers=workers,
                            lean_rss_gib=20.0 * workers,
                            gpu=False,
                            pid=os.getpid(),
                            owner_session=str(loaded.document["tmux_session"]),
                            worktree=loaded.base.repo_root,
                        )
                    except ReservationError as exc:
                        if "cap exceeded" not in str(exc):
                            raise
                        continue
                    claimed_workers = workers
                    break
                if claimed_workers:
                    break
                if time.monotonic() >= deadline:
                    raise SprintScaleError("Lean capacity unavailable for the certification")
                if waits % 10 == 0:
                    _append_stage(
                        stage_path,
                        {
                            "event": "waiting_for_lean_capacity",
                            "capacity": sprint_capacity_check(lean_workers=1, lean_rss_gib=20.0),
                        },
                    )
                waits += 1
                time.sleep(capacity_poll_seconds)
            claimed = True
            _append_stage(
                stage_path,
                {"event": "resource_claimed", "waits": waits, "lean_workers": claimed_workers},
            )
            try:
                certification = certify_sprint_pool(loaded, lean_workers=claimed_workers)
            finally:
                release_resources(task=resource_task)
                claimed = False
                _append_stage(stage_path, {"event": "resource_released"})
            _append_stage(
                stage_path,
                {
                    "event": "certification_complete",
                    "status_counts": certification["status_counts"],
                },
            )
            shards = freeze_sprint_shards(loaded)
            _append_stage(stage_path, {"event": "shards_frozen", "count": shards["shard_count"]})
            terminal: dict[str, object] = {
                "version": "leanfaith_sft2a_sprint_pool_terminal_v1",
                "status": "complete",
                "pool_manifest_sha256": hash_file(output / "pool_manifest.json"),
                "certification_manifest_sha256": hash_file(output / "certification_manifest.json"),
                "shards_manifest_sha256": hash_file(loaded.shard_root / "shards_manifest.json"),
                "certified_usable_roots": shards["certified_usable_roots"],
                "completed_at": _now(),
            }
            _atomic_replace_json(terminal_path, terminal)
            chain: dict[str, object] = {"launched": False, "reason": "launch_first_shard_disabled"}
            if launch_first_shard:
                first = cast(list[dict[str, object]], shards["shards"])[0]
                try:
                    launch = launch_sprint_pilot_v52(
                        load_provider_rehearsal_v52(Path(str(first["provider_config_path"])))
                    )
                    chain = {
                        "launched": True,
                        "target": first["provider_config_path"],
                        "launch": launch,
                    }
                except Exception as exc:
                    chain = {
                        "launched": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:1000],
                    }
            _atomic_replace_json(detached / "chain_receipt.json", chain)
            _append_stage(stage_path, {"event": "chain_first_shard", **chain})
    except Exception as exc:
        if claimed:
            with contextlib.suppress(ReservationError):
                release_resources(task=resource_task)
        failure = {
            "version": "leanfaith_sft2a_sprint_pool_terminal_v1",
            "status": "failed_resumable",
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:2000],
            "failed_at": _now(),
        }
        _atomic_replace_json(terminal_path, failure)
        _append_stage(stage_path, {"event": "worker_failed", "error_type": type(exc).__name__})
        print(json.dumps(failure, sort_keys=True), flush=True)
        if keepalive is not None:
            os.close(keepalive)
        raise
    print(json.dumps(terminal, sort_keys=True), flush=True)
    if keepalive is not None:
        os.close(keepalive)
    return terminal


def launch_sprint_pool_certification(
    loaded: LoadedSprintPoolConfig, *, startup_timeout: float = 90.0
) -> dict[str, object]:
    """Start the detached pool certification worker in its named tmux session."""

    session = str(loaded.document["tmux_session"])
    if _tmux_session_exists(session):
        raise SprintScaleError(f"sprint certification tmux session already exists: {session}")
    if any(item.task == str(loaded.document["resource_task"]) for item in list_reservations()):
        raise SprintScaleError("sprint certification resource task is already claimed")
    detached = loaded.output_root / "detached"
    try:
        with parallel_launch_lock(detached / "run.lock"):
            pass
    except ParallelRehearsalError as exc:
        raise SprintScaleError("sprint certification run lock is held") from exc
    terminal_path = detached / "terminal_status.json"
    if terminal_path.is_file() and _object(terminal_path).get("status") == "complete":
        raise SprintScaleError("sprint certification already completed")
    command = (
        sys.executable,
        "-m",
        "leanfaith.sft2a",
        "--sprint-pool-config",
        str(loaded.path),
        "detached-sprint-pool-certification-worker",
    )
    _start_tmux(session, command, loaded.base.repo_root)
    deadline = time.monotonic() + startup_timeout
    health = sprint_pool_certification_health(loaded)
    while time.monotonic() < deadline and not bool(health["worker_started"]):
        time.sleep(1.0)
        health = sprint_pool_certification_health(loaded)
    if not bool(health["worker_started"]):
        raise SprintScaleError(f"sprint certification worker did not start: {health}")
    return {
        "version": "leanfaith_sft2a_sprint_pool_launch_v1",
        "session_started": True,
        "sanitized_command": shlex.join(command),
        "capacity_recheck": sprint_capacity_check(lean_workers=1, lean_rss_gib=20.0),
        "health": health,
    }


def sprint_pool_certification_health(loaded: LoadedSprintPoolConfig) -> dict[str, object]:
    """Read-only health for the pool certification job."""

    session = str(loaded.document["tmux_session"])
    detached = loaded.output_root / "detached"
    alive = _tmux_session_exists(session)
    pane_pid = _tmux_pane_pid(session) if alive else None
    events = _stage_events(detached / "stage_journal.jsonl")
    results_root = loaded.output_root / "results"
    result_files = sum(1 for _ in results_root.rglob("*.json")) if results_root.is_dir() else 0
    terminal_path = detached / "terminal_status.json"
    reservation = next(
        (item for item in list_reservations() if item.task == loaded.document["resource_task"]),
        None,
    )
    return {
        "version": "leanfaith_sft2a_sprint_pool_health_v1",
        "checked_at": _now(),
        "tmux_session": session,
        "tmux_alive": alive,
        "pane_pid": pane_pid,
        "process_tree": _process_tree(pane_pid) if pane_pid is not None else "",
        "worker_started": any(event.get("event") == "worker_started" for event in events),
        "last_stage_event": events[-1] if events else None,
        "certified_result_files": result_files,
        "pool_present": (loaded.output_root / "pool.jsonl").is_file(),
        "resource_claim": None
        if reservation is None
        else {"task": reservation.task, "pid": reservation.pid},
        "terminal_status": _object(terminal_path) if terminal_path.is_file() else {},
        "shards_manifest_present": (loaded.shard_root / "shards_manifest.json").is_file(),
    }


__all__ = [
    "POOL_CONFIG_VERSION",
    "LoadedSprintPoolConfig",
    "SprintScaleError",
    "certify_sprint_pool",
    "compact_sprint_shards",
    "freeze_sprint_shards",
    "launch_sprint_pool_certification",
    "load_sprint_pool_config",
    "prepare_sprint_reference_pool",
    "run_detached_sprint_pool_certification_worker",
    "sprint_pool_certification_health",
]
