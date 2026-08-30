"""Command-line gates for CPT2 without changing the repository's shared CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from leanfaith.cpt2.oracle import (
    ORACLE_VERSION,
    OracleObservation,
    load_oracle_observations,
    run_oracle,
)
from leanfaith.cpt2.pilot import (
    benchmark_methods,
    finalize_pilot,
    run_one_example,
    select_oracle_rows,
)
from leanfaith.cpt2.scale import ScaleSettings, download_pinned_source, run_scale
from leanfaith.cpt2.source import SourceRow, inspect_snapshot, read_balanced_sample
from leanfaith.cpt2.splitters import DECLARATION_AWARE_METHOD, split_source
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_config(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("CPT2 config must be a mapping")
    return payload


def _git_revision(path: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _task_code_sha256(root: Path) -> str:
    paths = sorted((root / "src/leanfaith/cpt2").glob("*.py"))
    paths.append(root / "configs/data/cpt2/cpt2_v1.yaml")
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _context(
    *,
    project_dir: Path,
    contract_source_revision: str,
    pilot_source_revision: str,
) -> tuple[str, dict[str, Any]]:
    project_revision = _git_revision(project_dir)
    toolchain = (project_dir / "lean-toolchain").read_text(encoding="utf-8").strip()
    payload: dict[str, Any] = {
        "oracle_version": ORACLE_VERSION,
        "project_dir": str(project_dir),
        "project_revision": project_revision,
        "lean_toolchain": toolchain,
        "lean_interact_version": "0.11.4",
        "environment_schema_version": 1,
        "imports": ["Mathlib", "Aesop"],
        "options": {"Elab.async": False},
        "contract_source_revision": contract_source_revision,
        "pilot_source_revision": pilot_source_revision,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = hashlib.sha256(encoded).hexdigest()
    payload["context_fingerprint"] = fingerprint
    payload["context_id"] = f"ctx:{fingerprint}"
    return fingerprint, payload


def _resume_frozen_oracle_rows(
    sample_rows: tuple[SourceRow, ...],
    *,
    cache_path: Path,
    count: int,
) -> tuple[SourceRow, ...] | None:
    """Recover the already-compiled frozen oracle membership from its journal."""

    if not cache_path.exists():
        return None
    source_ids: list[str] = []
    seen: set[str] = set()
    for line in cache_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        source_id = str(payload["source_id"])
        if source_id not in seen:
            seen.add(source_id)
            source_ids.append(source_id)
    if len(source_ids) != count:
        return None
    by_id = {str(row.source_id): row for row in sample_rows}
    missing = [source_id for source_id in source_ids if source_id not in by_id]
    if missing:
        raise ValueError("frozen CPT2 oracle cache is not a subset of the deterministic sample")
    return tuple(by_id[source_id] for source_id in source_ids)


def _one_example(args: argparse.Namespace) -> None:
    result = run_one_example(Path(args.output_dir))
    print(json.dumps(result, indent=2, sort_keys=True))


def _pilot(args: argparse.Namespace) -> None:
    config = _load_config(Path(args.config))
    source_config = config["source"]
    if not isinstance(source_config, dict):
        raise ValueError("CPT2 source config must be a mapping")
    contract_revision = str(source_config["contract_revision"])
    pilot_revision = str(args.revision or source_config["pilot_revision_override"])
    snapshot = inspect_snapshot(
        repo_id=str(source_config["repo_id"]),
        revision=pilot_revision,
        parquet_path=str(source_config["parquet_path"]),
    )
    expected_rows = int(source_config["expected_rows"])
    if snapshot.row_count != expected_rows:
        raise ValueError(
            f"compiler_data row count drift: expected {expected_rows}, "
            f"observed {snapshot.row_count}"
        )
    sample_rows = read_balanced_sample(
        snapshot,
        sample_size=int(args.sample_size),
        source_shards=int(args.source_shards),
    )
    cheap_audits = benchmark_methods(sample_rows)
    state_root = Path(args.output_dir).parent / "state"
    oracle_cache_path = state_root / "oracle_cache.jsonl"
    selected_oracle_rows = select_oracle_rows(sample_rows, count=int(args.oracle_count))
    resumed_oracle_rows = _resume_frozen_oracle_rows(
        sample_rows,
        cache_path=oracle_cache_path,
        count=int(args.oracle_count),
    )
    oracle_rows = resumed_oracle_rows or selected_oracle_rows
    base_observations = load_oracle_observations(oracle_cache_path)
    base_by_id = {observation.source_id: observation for observation in base_observations}
    if set(base_by_id) != {row.source_id for row in oracle_rows}:
        raise ValueError("base oracle journal does not exactly match frozen oracle membership")
    correction_rows = tuple(
        row
        for row in oracle_rows
        if (
            base_by_id[row.source_id].boundary is None
            or (candidate := split_source(row.source_code, DECLARATION_AWARE_METHOD)) is None
            or candidate.by_offset != base_by_id[row.source_id].boundary
        )
    )

    project_dir = Path(args.project_dir).resolve()
    fingerprint, context = _context(
        project_dir=project_dir,
        contract_source_revision=contract_revision,
        pilot_source_revision=snapshot.resolved_revision,
    )
    output_root = Path(args.output_dir)
    settings = BackendSettings(
        project_dir=project_dir,
        context_fingerprint=fingerprint,
        environment_schema_version=1,
        raw_response_dir=state_root / "oracle_raw",
        memory_hard_limit_mb=24576,
        method_version="cpt2_declaration_range_oracle_v1+leaninteract_backend_v3",
        enable_parallel_elaboration=False,
        isolate_incremental_commands=True,
    )
    backend = LeanInteractBackend(settings)
    correction_cache_path = state_root / "oracle_corrections_v2.jsonl"
    observations: tuple[OracleObservation, ...]
    milestones: list[dict[str, Any]] = []
    corrections: tuple[OracleObservation, ...]
    if args.allow_oracle_corrections:
        started = time.perf_counter()
        try:
            corrections = run_oracle(
                backend,
                correction_rows,
                context_id=f"ctx:{fingerprint}",
                context_fingerprint=fingerprint,
                cache_path=correction_cache_path,
                timeout_seconds=float(args.timeout_seconds),
                batch_size=int(args.batch_size),
            )
        finally:
            backend.close()
        milestones.append(
            {
                "stage": "v2_targeted_correction",
                "rows": len(correction_rows),
                "wall_seconds": time.perf_counter() - started,
                "lean_requests": sum(not item.cache_hit for item in corrections),
                "cache_hits": sum(item.cache_hit for item in corrections),
                "boundary_established": sum(item.boundary is not None for item in corrections),
            }
        )
    else:
        corrections = load_oracle_observations(correction_cache_path)
        if {item.source_id for item in corrections} != {row.source_id for row in correction_rows}:
            raise ValueError(
                "oracle corrections are incomplete; rerun with --allow-oracle-corrections "
                "under a CPT2 host reservation"
            )
    correction_by_id = {observation.source_id: observation for observation in corrections}
    observations = tuple(
        correction_by_id.get(row.source_id, base_by_id[row.source_id]) for row in oracle_rows
    )
    context["oracle_milestones"] = milestones
    context["oracle_base_cache_path"] = str(oracle_cache_path)
    context["oracle_correction_cache_path"] = str(correction_cache_path)
    context["oracle_membership_resumed_from_cache"] = resumed_oracle_rows is not None
    context["oracle_unique_source_rows"] = len(oracle_rows)
    context["oracle_base_attempts"] = len(base_observations)
    context["oracle_targeted_correction_rows"] = len(correction_rows)
    context["task_code_sha256"] = _task_code_sha256(_repo_root())
    result = finalize_pilot(
        output_root,
        snapshot=snapshot,
        sample_rows=sample_rows,
        oracle_rows=oracle_rows,
        observations=observations,
        cheap_audits=cheap_audits,
        blocklist_path=Path(args.blocklist),
        code_revision=_git_revision(_repo_root()),
        context=context,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


def _scale(args: argparse.Namespace) -> None:
    config = _load_config(Path(args.config))
    source_config = config["source"]
    scale_config = config["scale"]
    if not isinstance(source_config, dict) or not isinstance(scale_config, dict):
        raise ValueError("CPT2 source/scale config must be mappings")
    revision = str(source_config["contract_revision"])
    snapshot = inspect_snapshot(
        repo_id=str(source_config["repo_id"]),
        revision=revision,
        parquet_path=str(source_config["parquet_path"]),
    )
    if snapshot.resolved_revision != revision:
        raise ValueError("compiler_data source did not resolve to the exact pinned revision")
    if snapshot.row_count != int(source_config["expected_rows"]):
        raise ValueError("compiler_data row count differs from the CPT2 contract")
    if snapshot.parquet_sha256 != str(source_config["parquet_sha256"]):
        raise ValueError("compiler_data Parquet hash differs from the CPT2 contract")
    output_root = Path(args.output_root or scale_config["output_root"])
    source_path = (
        Path(args.source_path)
        if args.source_path
        else download_pinned_source(snapshot, output_root / "_source")
    )
    settings = ScaleSettings(
        output_root=output_root,
        compression=str(scale_config["compression"]),
        validation_rows=int(scale_config["validation_rows"]),
        validation_true=int(scale_config["validation_true"]),
        validation_false=int(scale_config["validation_false"]),
        validation_salt=str(scale_config["validation_salt"]),
        row_groups_per_release_shard=int(scale_config["row_groups_per_release_shard"]),
        workers=int(args.workers or scale_config["workers"]),
        row_group_limit=args.row_group_limit,
    )
    result = run_scale(
        snapshot=snapshot,
        source_path=source_path,
        settings=settings,
        blocklist_path=Path(args.blocklist),
        task_code_sha256=_task_code_sha256(_repo_root()),
        code_revision=_git_revision(_repo_root()),
    )
    if args.row_group_limit is None:
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        expected_labels = {
            "true": int(source_config["expected_valid_true"]),
            "false": int(source_config["expected_valid_false"]),
        }
        if manifest["source_labels"] != expected_labels:
            raise ValueError("full-data source label counts differ from the CPT2 contract")
    payload = asdict(result)
    for key in ("output_root", "release_root", "manifest_path"):
        payload[key] = str(payload[key])
    print(json.dumps(payload, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    root = _repo_root()
    parser = argparse.ArgumentParser(description="CPT2 bounded dataset gates")
    subparsers = parser.add_subparsers(dest="command", required=True)

    one = subparsers.add_parser("one-example")
    one.add_argument(
        "--output-dir",
        default="/storage/milikic/leanfaith/value_first/cpt2_v1/gates/one_example_v3",
    )
    one.set_defaults(handler=_one_example)

    pilot = subparsers.add_parser("pilot")
    pilot.add_argument(
        "--config",
        default=str(root / "configs/data/cpt2/cpt2_v1.yaml"),
    )
    pilot.add_argument("--revision")
    pilot.add_argument("--sample-size", type=int, default=10_000)
    pilot.add_argument("--source-shards", type=int, default=8)
    pilot.add_argument("--oracle-count", type=int, default=500)
    pilot.add_argument("--batch-size", type=int, default=16)
    pilot.add_argument("--timeout-seconds", type=float, default=60.0)
    pilot.add_argument("--allow-oracle-corrections", action="store_true")
    pilot.add_argument("--project-dir", default="/storage/milikic/leanfaith/mathlib4")
    pilot.add_argument(
        "--blocklist",
        default=str(root / "data/benchmarks/golden_blocklist_v1.json"),
    )
    pilot.add_argument(
        "--output-dir",
        default="/storage/milikic/leanfaith/value_first/cpt2_v1/gates/pilot_10k_v3_final",
    )
    pilot.set_defaults(handler=_pilot)

    scale = subparsers.add_parser("scale")
    scale.add_argument(
        "--config",
        default=str(root / "configs/data/cpt2/cpt2_v1.yaml"),
    )
    scale.add_argument("--output-root")
    scale.add_argument("--source-path")
    scale.add_argument("--workers", type=int)
    scale.add_argument("--row-group-limit", type=int)
    scale.add_argument(
        "--blocklist",
        default=str(root / "data/benchmarks/golden_blocklist_v1.json"),
    )
    scale.set_defaults(handler=_scale)
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
