"""Deterministic orchestration and immutable evidence for SFT2A v5.2 references."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.host_resources import claim_resources, list_reservations, release_resources
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft2a.census import _stratified_select, run_zero_lean_census
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.legacy import _atomic_exact, _blocklist
from leanfaith.sft2a.mechanisms import (
    applicable_mechanisms,
    mechanism_histogram,
    plan_mechanism_rotation,
    planning_signature_from_goal_v1,
    signature_shape,
)
from leanfaith.sft2a.models import OneRootConfig, PilotSourceAllocation, SFT2AV52Config
from leanfaith.sft2a.reference_certifier import (
    AuthoritativeReferenceCertifier,
    ReferenceCertificationResult,
    ReferenceCertifierError,
)

POOL_VERSION = "leanfaith_sft2a_reference_pool_v5_2"
RUN_VERSION = "leanfaith_sft2a_reference_certification_run_v5_2"
SAMPLE_VERSION = "leanfaith_sft2a_certified_sample_v5_2"
AUTHORIZATION_TEXT = (
    "I authorize additive SFT2A v5.2 implementation and execution of the bounded local "
    "reference-certification phase. Do not launch Terra, Opus, or Kimi yet."
)


class ReferenceCertificationPhaseError(RuntimeError):
    """The local-only reference certification or its immutable evidence failed."""


@dataclass(frozen=True, slots=True)
class LoadedReferenceCertificationAuthorization:
    path: Path
    document: dict[str, object]
    sha256: str


def _v5_2(loaded: LoadedSFT2AConfig) -> SFT2AV52Config:
    if not isinstance(loaded.config, SFT2AV52Config):
        raise ReferenceCertificationPhaseError("reference certification requires v5.2")
    return loaded.config


def _output(loaded: LoadedSFT2AConfig) -> Path:
    config = _v5_2(loaded)
    return Path(config.staging_root) / config.reference_certification.output_subdir


def _jsonl_bytes(rows: Iterable[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceCertificationPhaseError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReferenceCertificationPhaseError(f"JSON artifact is not an object: {path}")
    return value


def _jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ReferenceCertificationPhaseError(f"non-object JSONL row {path}:{number}")
        rows.append(value)
    return rows


def _append_journal(path: Path, event: Mapping[str, object]) -> None:
    record = {"event_id": "sft2a-refcert:" + hash_canonical(event), **event}
    payload = canonical_json_bytes(record) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            if payload.rstrip() in handle.read().splitlines():
                return
            handle.seek(0, os.SEEK_END)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_replace(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(dict(document)) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _allocation_map(items: Sequence[PilotSourceAllocation]) -> dict[str, int]:
    return {item.source: item.roots for item in items}


def _force_canary(
    selected: list[dict[str, object]],
    candidates: Sequence[dict[str, object]],
    declaration_name: str,
) -> list[dict[str, object]]:
    if any(row.get("declaration_name") == declaration_name for row in selected):
        return selected
    canary = next(
        (row for row in candidates if row.get("declaration_name") == declaration_name), None
    )
    if canary is None:
        raise ReferenceCertificationPhaseError("positive CSLib constant-lookup canary is absent")
    if not selected:
        raise ReferenceCertificationPhaseError("cannot force canary into an empty source sample")
    return [*selected[:-1], canary]


def prepare_reference_pool(loaded: LoadedSFT2AConfig) -> dict[str, object]:
    """Freeze the 300 initial and 300 source-specific extension candidates without Lean."""

    config = _v5_2(loaded)
    output = _output(loaded)
    manifest_path = output / "pool_manifest.json"
    if manifest_path.is_file():
        existing_manifest = _json_object(manifest_path)
        for name, key in (
            ("initial_pool.jsonl", "initial_pool_sha256"),
            ("extension_pool.jsonl", "extension_pool_sha256"),
            ("pool.jsonl", "pool_sha256"),
        ):
            if hash_file(output / name) != existing_manifest.get(key):
                raise ReferenceCertificationPhaseError("immutable reference pool replay differs")
        return existing_manifest

    census_root = output / "census"
    census = run_zero_lean_census(loaded, output_root=census_root)
    rows = _jsonl(census_root / "eligible_roots.jsonl")
    by_source: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_source[str(row["source"])].append(row)
    initial_counts = _allocation_map(config.reference_certification.initial_allocations)
    extension_counts = _allocation_map(config.reference_certification.extension_allocations)
    initial: list[dict[str, object]] = []
    extension: list[dict[str, object]] = []
    for source in ("mathlib", "physlib", "cslib", "compiler_data"):
        candidates = by_source[source]
        required = initial_counts[source] + extension_counts[source]
        ranked = _stratified_select(
            candidates,
            count=required,
            salt=f"{config.reference_certification.pool_salt}:{source}",
        )
        first = ranked[: initial_counts[source]]
        if source == "cslib":
            first = _force_canary(
                first,
                candidates,
                config.reference_certification.positive_canary_declaration,
            )
        first_ids = {str(row["root_id"]) for row in first}
        remaining = [row for row in ranked if str(row["root_id"]) not in first_ids]
        if len(remaining) < extension_counts[source]:
            extras = [row for row in candidates if str(row["root_id"]) not in first_ids]
            remaining = _stratified_select(
                extras,
                count=extension_counts[source],
                salt=f"{config.reference_certification.pool_salt}:{source}:extension",
            )
        second = remaining[: extension_counts[source]]
        initial.extend({**row, "pool_phase": "initial"} for row in first)
        extension.extend({**row, "pool_phase": "extension"} for row in second)
    initial.sort(key=lambda row: (str(row["source"]), str(row["root_id"])))
    extension.sort(key=lambda row: (str(row["source"]), str(row["root_id"])))
    pool = [*initial, *extension]
    if len(initial) != 300 or len(extension) != 300 or len(pool) != 600:
        raise ReferenceCertificationPhaseError("frozen pool size differs from 300+300")
    if len({str(row["root_id"]) for row in pool}) != 600:
        raise ReferenceCertificationPhaseError("reference pool contains duplicate root IDs")
    _atomic_exact(output / "initial_pool.jsonl", _jsonl_bytes(initial))
    _atomic_exact(output / "extension_pool.jsonl", _jsonl_bytes(extension))
    _atomic_exact(output / "pool.jsonl", _jsonl_bytes(pool))
    manifest: dict[str, object] = {
        "version": POOL_VERSION,
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "census_manifest_sha256": hash_file(census_root / "manifest.json"),
        "census_inventory_sha256": census["eligible_roots_sha256"],
        "pool_salt": config.reference_certification.pool_salt,
        "initial_source_counts": dict(sorted(Counter(str(r["source"]) for r in initial).items())),
        "extension_source_counts": dict(
            sorted(Counter(str(r["source"]) for r in extension).items())
        ),
        "initial_pool_sha256": hash_file(output / "initial_pool.jsonl"),
        "extension_pool_sha256": hash_file(output / "extension_pool.jsonl"),
        "pool_sha256": hash_file(output / "pool.jsonl"),
        "extension_rule": config.reference_certification.extension_rule,
        "maximum_certification_attempts": 600,
        "canary_declaration": config.reference_certification.positive_canary_declaration,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest


def materialize_reference_authorization(
    loaded: LoadedSFT2AConfig, *, path: Path
) -> dict[str, object]:
    """Materialize the user's local-only authorization against a clean committed revision."""

    pool = prepare_reference_pool(loaded)
    status = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=loaded.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ReferenceCertificationPhaseError("authorization requires a clean committed worktree")
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=loaded.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ("git", "rev-parse", "HEAD^{tree}"),
        cwd=loaded.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    document: dict[str, object] = {
        "version": "leanfaith_sft2a_reference_certification_authorization_v5_2",
        "authorized": True,
        "authorization_scope": "bounded_local_reference_certification_only",
        "authorization_text": AUTHORIZATION_TEXT,
        "authorization_text_sha256": sha256_hex(AUTHORIZATION_TEXT.encode()),
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "pool_sha256": pool["pool_sha256"],
        "initial_pool_sha256": pool["initial_pool_sha256"],
        "extension_pool_sha256": pool["extension_pool_sha256"],
        "initial_source_counts": pool["initial_source_counts"],
        "extension_source_counts": pool["extension_source_counts"],
        "extension_rule": pool["extension_rule"],
        "maximum_certification_attempts": 600,
        "implementation_commit": commit,
        "implementation_tree": tree,
        "output_root": str(_output(loaded)),
        "tmux_session": _v5_2(loaded).reference_certification.detached_launch.session_name,
        "provider_calls_allowed": 0,
        "rehearsal_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "training_authorized": False,
    }
    _atomic_exact(path, canonical_json_bytes(document) + b"\n")
    return document


def load_reference_authorization(
    loaded: LoadedSFT2AConfig, path: Path
) -> LoadedReferenceCertificationAuthorization:
    resolved = path if path.is_absolute() else loaded.repo_root / path
    if resolved.is_symlink() or not resolved.is_file():
        raise ReferenceCertificationPhaseError("reference authorization is missing or unsafe")
    document = _json_object(resolved)
    pool = prepare_reference_pool(loaded)
    expected = {
        "version": "leanfaith_sft2a_reference_certification_authorization_v5_2",
        "authorized": True,
        "authorization_scope": "bounded_local_reference_certification_only",
        "authorization_text": AUTHORIZATION_TEXT,
        "authorization_text_sha256": sha256_hex(AUTHORIZATION_TEXT.encode()),
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "pool_sha256": pool["pool_sha256"],
        "initial_pool_sha256": pool["initial_pool_sha256"],
        "extension_pool_sha256": pool["extension_pool_sha256"],
        "extension_rule": pool["extension_rule"],
        "maximum_certification_attempts": 600,
        "provider_calls_allowed": 0,
        "rehearsal_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "training_authorized": False,
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise ReferenceCertificationPhaseError(f"reference authorization differs at {key}")
    commit = document.get("implementation_commit")
    tree = document.get("implementation_tree")
    if not isinstance(commit, str) or not isinstance(tree, str):
        raise ReferenceCertificationPhaseError("reference authorization implementation is absent")
    observed = subprocess.run(
        ("git", "rev-parse", f"{commit}^{{tree}}"),
        cwd=loaded.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ancestor = subprocess.run(
        ("git", "merge-base", "--is-ancestor", commit, "HEAD"),
        cwd=loaded.repo_root,
        check=False,
    )
    if observed != tree or ancestor.returncode != 0:
        raise ReferenceCertificationPhaseError("reference authorization implementation differs")
    return LoadedReferenceCertificationAuthorization(resolved, document, hash_file(resolved))


def _root(row: Mapping[str, object]) -> OneRootConfig:
    expected = str(row["reference_signature"])
    return OneRootConfig.model_validate(
        {
            **{
                key: row[key]
                for key in (
                    "root_id",
                    "source",
                    "source_revision",
                    "source_license",
                    "declaration_name",
                    "reference_signature",
                    "compile_context",
                )
            },
            "external_transmission": True,
            "policy_version": "source_use_v2",
            "expected_reference_goal_v1": expected,
        }
    )


def _result_document(
    row: Mapping[str, object], result: ReferenceCertificationResult
) -> dict[str, object]:
    value = asdict(result)
    value["cache_path"] = str(result.cache_path)
    return {
        "version": "leanfaith_sft2a_reference_certification_result_v5_2",
        "root_id": row["root_id"],
        "source": row["source"],
        "declaration_name": row["declaration_name"],
        "pool_phase": row["pool_phase"],
        "source_header": row["source_header"],
        "source_header_sha256": row["source_header_sha256"],
        "source_locator": row["source_locator"],
        "domain": row["domain"],
        "certification": value,
    }


def _result_path(output: Path, row: Mapping[str, object]) -> Path:
    return output / "results" / str(row["source"]) / f"{hash_canonical(row['root_id'])}.json"


def _screened_valid(
    loaded: LoadedSFT2AConfig,
    rows: Sequence[dict[str, object]],
) -> tuple[dict[str, list[dict[str, object]]], Counter[str]]:
    _path, blocked = _blocklist(loaded)
    accepted: dict[str, list[dict[str, object]]] = defaultdict(list)
    rejected: Counter[str] = Counter()
    exprs: set[str] = set()
    goals: set[str] = set()
    for row in rows:
        result = _json_object(_result_path(_output(loaded), row))
        cert = result.get("certification")
        if not isinstance(cert, dict) or cert.get("status") != "valid":
            rejected[
                str(cert.get("taxonomy", "malformed_result"))
                if isinstance(cert, dict)
                else "malformed_result"
            ] += 1
            continue
        goal = cert.get("goal_v1")
        expr_hash = cert.get("closed_expr_hash")
        rendered_hash = cert.get("rendered_goal_hash")
        if not all(isinstance(value, str) and value for value in (goal, expr_hash, rendered_hash)):
            rejected["malformed_valid_result"] += 1
            continue
        assert (
            isinstance(goal, str) and isinstance(expr_hash, str) and isinstance(rendered_hash, str)
        )
        if signature_near_dup_hash(goal) in blocked:
            rejected["gold_contamination"] += 1
            continue
        if expr_hash in exprs:
            rejected["duplicate_closed_expr"] += 1
            continue
        if rendered_hash in goals:
            rejected["duplicate_rendered_goal"] += 1
            continue
        planning = planning_signature_from_goal_v1(goal)
        if (
            len(applicable_mechanisms(planning, "preserving")) < 2
            or len(applicable_mechanisms(planning, "breaking")) < 2
        ):
            rejected["insufficient_mechanism_coverage"] += 1
            continue
        exprs.add(expr_hash)
        goals.add(rendered_hash)
        accepted[str(row["source"])].append(
            {
                **row,
                "certification_result_path": str(_result_path(_output(loaded), row)),
                "certification_result_sha256": hash_file(_result_path(_output(loaded), row)),
                "certification": cert,
                "certified_planning_signature": planning,
                "certified_shape_id": signature_shape(planning).shape_id,
            }
        )
    return accepted, rejected


def _certify_rows(
    loaded: LoadedSFT2AConfig,
    rows: Sequence[dict[str, object]],
    *,
    journal: Path,
) -> tuple[int, int, int]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        context = row.get("compile_context")
        if not isinstance(context, dict):
            raise ReferenceCertificationPhaseError("pool row lacks compile context")
        grouped[str(context["project_id"])].append(row)
    attempts = 0
    cache_hits = 0
    lean_requests = 0
    for project in sorted(grouped):
        certifier: AuthoritativeReferenceCertifier | None = None
        try:
            for row in sorted(grouped[project], key=lambda item: str(item["root_id"])):
                path = _result_path(_output(loaded), row)
                if path.is_file():
                    continue
                root = _root(row)
                if certifier is None:
                    certifier = AuthoritativeReferenceCertifier(loaded, root)
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
                attempts += 1
                cache_hits += int(result.cache_hit)
                lean_requests += int(not result.cache_hit)
                document = _result_document(row, result)
                _atomic_exact(path, canonical_json_bytes(document) + b"\n")
                _append_journal(
                    journal,
                    {
                        "event": "reference_certified",
                        "root_id": row["root_id"],
                        "source": row["source"],
                        "status": result.status,
                        "taxonomy": result.taxonomy,
                        "cache_hit": result.cache_hit,
                        "elapsed_ms": result.elapsed_ms,
                        "measured_rss_peak_bytes": result.measured_rss_peak_bytes,
                        "result_sha256": hash_file(path),
                        "at": datetime.now(UTC).isoformat(),
                    },
                )
                if result.status == "infrastructure":
                    raise ReferenceCertificationPhaseError(
                        f"infrastructure failure certifying {row['root_id']}: {result.taxonomy}"
                    )
        except ReferenceCertifierError as exc:
            raise ReferenceCertificationPhaseError(str(exc)) from exc
        finally:
            if certifier is not None:
                certifier.close()
    return attempts, cache_hits, lean_requests


def run_reference_certification(
    loaded: LoadedSFT2AConfig,
    authorization: LoadedReferenceCertificationAuthorization,
) -> dict[str, object]:
    """Certify the bounded pool, extend only underfilled sources, and freeze exactly 100 roots."""

    config = _v5_2(loaded)
    if authorization.document.get("authorized") is not True:
        raise ReferenceCertificationPhaseError("local reference certification is not authorized")
    output = _output(loaded)
    complete = output / "certification_manifest.json"
    if complete.is_file():
        return _json_object(complete)
    initial = _jsonl(output / "initial_pool.jsonl")
    extension = _jsonl(output / "extension_pool.jsonl")
    journal = output / "certification_journal.jsonl"
    attempts, hits, lean_requests = _certify_rows(loaded, initial, journal=journal)
    accepted, rejected = _screened_valid(loaded, initial)
    final_counts = _allocation_map(config.reference_certification.final_allocations)
    underfilled = [
        source for source, count in final_counts.items() if len(accepted[source]) < count
    ]
    extension_used: list[dict[str, object]] = []
    if underfilled:
        extension_used = [row for row in extension if str(row["source"]) in underfilled]
        extra_attempts, extra_hits, extra_requests = _certify_rows(
            loaded, extension_used, journal=journal
        )
        attempts += extra_attempts
        hits += extra_hits
        lean_requests += extra_requests
        accepted, rejected = _screened_valid(loaded, [*initial, *extension_used])
    attempted_rows = [
        row for row in [*initial, *extension_used] if _result_path(output, row).is_file()
    ]
    if len(attempted_rows) > config.reference_certification.maximum_certification_attempts:
        raise ReferenceCertificationPhaseError("reference certification exceeded 600 attempts")
    selected: list[dict[str, object]] = []
    for source in ("mathlib", "physlib", "cslib", "compiler_data"):
        if len(accepted[source]) < final_counts[source]:
            raise ReferenceCertificationPhaseError(
                f"certified source quota remains underfilled: {source}"
            )
        chosen = _stratified_select(
            accepted[source],
            count=final_counts[source],
            salt=f"{config.reference_certification.pool_salt}:final:{source}",
        )
        if source == "cslib":
            chosen = _force_canary(
                chosen,
                accepted[source],
                config.reference_certification.positive_canary_declaration,
            )
        selected.extend(chosen)
    planning_roots = [
        {"root_id": row["root_id"], "reference_signature": row["certified_planning_signature"]}
        for row in selected
    ]
    rotation = plan_mechanism_rotation(
        planning_roots,
        salt=config.mechanism_rotation.salt + ":certified-v5-2",
        maximum_family_fraction_per_polarity=(
            config.mechanism_rotation.maximum_family_fraction_per_polarity
        ),
    )
    sample: list[dict[str, object]] = []
    for row in selected:
        cert = row["certification"]
        assert isinstance(cert, dict)
        goal = str(cert["goal_v1"])
        root = _root(row).model_copy(update={"expected_reference_goal_v1": goal})
        cache_path = Path(str(cert["cache_path"]))
        sample.append(
            {
                "root": root.model_dump(mode="json"),
                "source_locator": row["source_locator"],
                "source_header": row["source_header"],
                "source_header_sha256": row["source_header_sha256"],
                "compiler_data_theorem_sha256": row.get("compiler_data_theorem_sha256"),
                "domain": row["domain"],
                "shape_id": row["certified_shape_id"],
                "certified_reference": {
                    "goal_v1": goal,
                    "closed_expr_hash": cert["closed_expr_hash"],
                    "rendered_goal_hash": cert["rendered_goal_hash"],
                    "certification_cache_key": cert["cache_key"],
                    "certification_cache_path": str(cache_path),
                    "certification_cache_sha256": hash_file(cache_path),
                    "sidecar_hash": cert["sidecar_hash"],
                    "compile_context_id": cert["compile_context_id"],
                    "route": cert["route"],
                    "result_path": row["certification_result_path"],
                    "result_sha256": row["certification_result_sha256"],
                },
                "mechanism_plan": {
                    slot: assignment.to_dict()
                    for slot, assignment in sorted(rotation[str(row["root_id"])].items())
                },
            }
        )
    sample.sort(
        key=lambda row: (
            str(row["root"]["compile_context"]["project_id"]),  # type: ignore[index]
            str(row["root"]["root_id"]),  # type: ignore[index]
        )
    )
    _atomic_exact(output / "certified_sample.jsonl", _jsonl_bytes(sample))
    shards: list[dict[str, object]] = []
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in sample:
        grouped[str(row["root"]["compile_context"]["project_id"])].append(row)  # type: ignore[index]
    for project in sorted(grouped):
        for start in range(0, len(grouped[project]), config.rehearsal.roots_per_shard):
            rows = grouped[project][start : start + config.rehearsal.roots_per_shard]
            shard = output / "certified_shards" / project / f"{project}-{start // 10:03d}.jsonl"
            _atomic_exact(shard, _jsonl_bytes(rows))
            shards.append(
                {
                    "project_id": project,
                    "path": str(shard.relative_to(output)),
                    "sha256": hash_file(shard),
                    "roots": len(rows),
                }
            )
    canary = next(
        row
        for row in sample
        if row["root"]["declaration_name"]  # type: ignore[index]
        == config.reference_certification.positive_canary_declaration
    )
    source_mix = Counter(str(row["root"]["source"]) for row in sample)  # type: ignore[index]
    elapsed_values: list[int] = []
    rss_values: list[int] = []
    for row in attempted_rows:
        result_document = _json_object(_result_path(output, row))
        certification = result_document.get("certification")
        if not isinstance(certification, dict):
            raise ReferenceCertificationPhaseError("certification result is malformed")
        elapsed = certification.get("elapsed_ms")
        rss = certification.get("measured_rss_peak_bytes")
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, int)
            or isinstance(rss, bool)
            or not isinstance(rss, int)
        ):
            raise ReferenceCertificationPhaseError("certification timing/RSS is malformed")
        elapsed_values.append(elapsed)
        rss_values.append(rss)
    manifest: dict[str, object] = {
        "version": RUN_VERSION,
        "sample_version": SAMPLE_VERSION,
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "authorization_sha256": authorization.sha256,
        "pool_manifest_sha256": hash_file(output / "pool_manifest.json"),
        "pool_sha256": hash_file(output / "pool.jsonl"),
        "certification_attempts": len(attempted_rows),
        "executed_in_this_invocation": attempts,
        "cache_hits_in_this_invocation": hits,
        "lean_requests_in_this_invocation": lean_requests,
        "extension_sources": underfilled,
        "extension_rule": config.reference_certification.extension_rule,
        "failure_taxonomy": dict(sorted(rejected.items())),
        "root_count": len(sample),
        "source_mix": dict(sorted(source_mix.items())),
        "sample_sha256": hash_file(output / "certified_sample.jsonl"),
        "shards": shards,
        "mechanism_plan_histogram": mechanism_histogram(rotation),
        "accepted_mechanism_evidence_histogram": {"preserving": {}, "breaking": {}},
        "canary": {
            "declaration_name": config.reference_certification.positive_canary_declaration,
            "goal_v1": canary["certified_reference"]["goal_v1"],  # type: ignore[index]
            "route": canary["certified_reference"]["route"],  # type: ignore[index]
            "closed_expr_hash": canary["certified_reference"]["closed_expr_hash"],  # type: ignore[index]
        },
        "maximum_measured_rss_bytes": max(rss_values),
        "total_elapsed_ms": sum(elapsed_values),
        "provider_calls_executed": 0,
        "rehearsal_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "training_authorized": False,
    }
    if len(sample) != 100 or dict(source_mix) != final_counts:
        raise ReferenceCertificationPhaseError("final certified sample quota differs")
    if "UNVERIFIED_UNTIL_AUTHORIZED_REHEARSAL" in (output / "certified_sample.jsonl").read_text():
        raise ReferenceCertificationPhaseError("uncertified placeholder reached final sample")
    _atomic_exact(complete, canonical_json_bytes(manifest) + b"\n")
    return manifest


def verify_reference_replay(loaded: LoadedSFT2AConfig) -> dict[str, object]:
    """Re-read all selected terminal cache records with zero backend/provider construction."""

    output = _output(loaded)
    manifest = _json_object(output / "certification_manifest.json")
    rows = _jsonl(output / "certified_sample.jsonl")
    derived_receipts = {
        "reference_replay_receipt.json",
        "global_100_preflight_receipt.json",
    }
    durable_before = {
        str(path.relative_to(output)): hash_file(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
        and "detached/" not in str(path.relative_to(output))
        and str(path.relative_to(output)) not in derived_receipts
    }
    hits = 0
    for row in rows:
        root_doc = row.get("root")
        certified = row.get("certified_reference")
        if not isinstance(root_doc, dict) or not isinstance(certified, dict):
            raise ReferenceCertificationPhaseError("certified sample row is malformed")
        root = OneRootConfig.model_validate(root_doc)
        certifier = AuthoritativeReferenceCertifier(loaded, root)
        try:
            result = certifier.certify(
                source_header=str(row["source_header"]),
                compiler_data_theorem_sha256=(
                    str(row["compiler_data_theorem_sha256"])
                    if row.get("compiler_data_theorem_sha256") is not None
                    else None
                ),
            )
        finally:
            certifier.close()
        if not result.cache_hit or result.cache_key != certified.get("certification_cache_key"):
            raise ReferenceCertificationPhaseError("reference replay missed or changed its cache")
        if result.goal_v1 != certified.get("goal_v1") or result.closed_expr_hash != certified.get(
            "closed_expr_hash"
        ):
            raise ReferenceCertificationPhaseError("reference replay content differs")
        hits += 1
    durable_after = {
        str(path.relative_to(output)): hash_file(path)
        for path in sorted(output.rglob("*"))
        if path.is_file()
        and "detached/" not in str(path.relative_to(output))
        and str(path.relative_to(output)) not in derived_receipts
    }
    if durable_before != durable_after:
        raise ReferenceCertificationPhaseError("reference replay changed durable artifact hashes")
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_reference_cache_replay_v5_2",
        "config_hash": loaded.config_hash,
        "certification_manifest_sha256": hash_file(output / "certification_manifest.json"),
        "sample_sha256": manifest["sample_sha256"],
        "roots_verified": hits,
        "cache_hits": hits,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
        "durable_artifact_hashes_preserved": True,
        "durable_tree_hash": hash_canonical(durable_before),
    }
    _atomic_exact(output / "reference_replay_receipt.json", canonical_json_bytes(receipt) + b"\n")
    return receipt


def verify_global_reference_preflight(loaded: LoadedSFT2AConfig) -> dict[str, object]:
    """Require a complete 100/100 cache-hit certificate before future provider construction."""

    output = _output(loaded)
    replay = verify_reference_replay(loaded)
    rows = _jsonl(output / "certified_sample.jsonl")
    if len(rows) != 100 or replay.get("cache_hits") != 100:
        raise ReferenceCertificationPhaseError("global reference certificate is not 100/100")
    for row in rows:
        root = row.get("root")
        certified = row.get("certified_reference")
        if not isinstance(root, dict) or not isinstance(certified, dict):
            raise ReferenceCertificationPhaseError("global reference row is malformed")
        if root.get("expected_reference_goal_v1") != certified.get("goal_v1"):
            raise ReferenceCertificationPhaseError(
                "expected reference goal differs from certificate"
            )
        for key in (
            "closed_expr_hash",
            "certification_cache_key",
            "sidecar_hash",
            "compile_context_id",
            "goal_v1",
        ):
            if not isinstance(certified.get(key), str) or not certified[key]:
                raise ReferenceCertificationPhaseError(f"certified reference lacks {key}")
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_global_reference_preflight_v5_2",
        "config_hash": loaded.config_hash,
        "sample_sha256": hash_file(output / "certified_sample.jsonl"),
        "certification_manifest_sha256": hash_file(output / "certification_manifest.json"),
        "reference_replay_receipt_sha256": hash_file(output / "reference_replay_receipt.json"),
        "certified_roots": 100,
        "cache_hits": 100,
        "global_certificate": "100/100",
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
        "provider_construction_allowed_by_this_receipt": False,
    }
    _atomic_exact(
        output / "global_100_preflight_receipt.json", canonical_json_bytes(receipt) + b"\n"
    )
    return receipt


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ReferenceCertificationPhaseError("reference run lock is unsafe")
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReferenceCertificationPhaseError(
                "reference run lock is held; duplicate launch refused"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_is_free(path: Path) -> bool:
    try:
        with _exclusive_lock(path):
            return True
    except ReferenceCertificationPhaseError:
        return False


def _session_exists(name: str) -> bool:
    return (
        subprocess.run(
            ("tmux", "has-session", "-t", name),
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def preflight_reference_launch(
    loaded: LoadedSFT2AConfig,
    authorization: LoadedReferenceCertificationAuthorization,
) -> dict[str, object]:
    """Check the committed local-only launch boundary without Lean or providers."""

    config = _v5_2(loaded)
    pool = prepare_reference_pool(loaded)
    policy = config.reference_certification.detached_launch
    output = _output(loaded)
    lock = output / policy.run_lock_relative_path
    if authorization.document.get("authorized") is not True:
        raise ReferenceCertificationPhaseError("reference certification is unauthorized")
    if _session_exists(policy.session_name):
        raise ReferenceCertificationPhaseError("reference certification tmux session exists")
    if not _lock_is_free(lock):
        raise ReferenceCertificationPhaseError("reference certification run lock is held")
    if (output / policy.terminal_status_relative_path).is_file():
        raise ReferenceCertificationPhaseError("reference certification has terminal state")
    return {
        "version": "leanfaith_sft2a_reference_launch_preflight_v5_2",
        "config_hash": loaded.config_hash,
        "authorization_sha256": authorization.sha256,
        "pool_sha256": pool["pool_sha256"],
        "initial_pool_sha256": pool["initial_pool_sha256"],
        "session_name": policy.session_name,
        "output_root": str(output),
        "run_lock_free": True,
        "initial_lean_workers": config.reference_certification.lean_workers_initial,
        "maximum_lean_workers": config.reference_certification.lean_workers_maximum,
        "maximum_measured_rss_gib": config.reference_certification.measured_rss_gib_maximum,
        "extension_rule": config.reference_certification.extension_rule,
        "lean_requests_executed": 0,
        "provider_calls_executed": 0,
        "tmux_start_not_executed": True,
    }


def reference_certification_health(loaded: LoadedSFT2AConfig) -> dict[str, object]:
    """Read tmux, resource, journal, log, and terminal status without restarting work."""

    config = _v5_2(loaded)
    policy = config.reference_certification.detached_launch
    output = _output(loaded)
    session_live = _session_exists(policy.session_name)
    pane_pid: int | None = None
    process_tree = ""
    if session_live:
        pane = subprocess.run(
            ("tmux", "list-panes", "-t", policy.session_name, "-F", "#{pane_pid}"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if pane.isdigit():
            pane_pid = int(pane)
            process_tree = subprocess.run(
                (
                    "ps",
                    "-o",
                    "pid=,ppid=,stat=,etime=,cmd=",
                    "--forest",
                    "-p",
                    pane,
                    "--ppid",
                    pane,
                ),
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
    reservation = next(
        (item for item in list_reservations() if item.task == policy.resource_task), None
    )
    journal = output / policy.journal_relative_path
    journal_rows = (
        sum(1 for line in journal.read_text(encoding="utf-8").splitlines() if line.strip())
        if journal.is_file()
        else 0
    )
    results = list((output / "results").rglob("*.json")) if (output / "results").is_dir() else []
    terminal_path = output / policy.terminal_status_relative_path
    return {
        "session_name": policy.session_name,
        "session_live": session_live,
        "pane_pid": pane_pid,
        "process_tree": process_tree,
        "run_lock_held": not _lock_is_free(output / policy.run_lock_relative_path),
        "resource_claim": (
            None
            if reservation is None
            else {
                "task": reservation.task,
                "pid": reservation.pid,
                "lean_workers": reservation.lean_workers,
                "lean_rss_gib": reservation.lean_rss_gib,
                "owner_session": reservation.owner_session,
            }
        ),
        "journal_rows": journal_rows,
        "durable_result_files": len(results),
        "combined_log": str(output / policy.combined_log_relative_path),
        "combined_log_bytes": (
            (output / policy.combined_log_relative_path).stat().st_size
            if (output / policy.combined_log_relative_path).is_file()
            else 0
        ),
        "terminal_status": _json_object(terminal_path) if terminal_path.is_file() else None,
    }


def launch_detached_reference_certification(
    loaded: LoadedSFT2AConfig,
    authorization: LoadedReferenceCertificationAuthorization,
) -> dict[str, object]:
    """Launch only the committed local certification worker in one named tmux session."""

    preflight = preflight_reference_launch(loaded, authorization)
    policy = _v5_2(loaded).reference_certification.detached_launch
    output = _output(loaded)
    command = (
        sys.executable,
        "-m",
        "leanfaith.sft2a",
        "--config",
        str(loaded.path),
        "--reference-certification-authorization",
        str(authorization.path),
        "detached-reference-certification-worker",
    )
    with _exclusive_lock(output / "detached/launch.lock"):
        if _session_exists(policy.session_name):
            raise ReferenceCertificationPhaseError("duplicate reference launch refused")
        _append_journal(
            output / policy.journal_relative_path,
            {
                "event": "launch_requested",
                "at": datetime.now(UTC).isoformat(),
                "session_name": policy.session_name,
                "config_hash": loaded.config_hash,
                "authorization_sha256": authorization.sha256,
                "command": shlex.join(command),
            },
        )
        completed = subprocess.run(
            ("tmux", "new-session", "-d", "-s", policy.session_name, shlex.join(command)),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ReferenceCertificationPhaseError(
                f"reference tmux launch failed: {completed.stderr.strip()}"
            )
    return {"preflight": preflight, "session_started": True}


def run_detached_reference_certification_worker(
    loaded: LoadedSFT2AConfig,
    authorization: LoadedReferenceCertificationAuthorization,
) -> dict[str, object]:
    """Hold the run lock and one host claim through certification and zero-call replay."""

    config = _v5_2(loaded)
    policy = config.reference_certification.detached_launch
    output = _output(loaded)
    log = output / policy.combined_log_relative_path
    log.parent.mkdir(parents=True, exist_ok=True)
    log_descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null_descriptor = os.open(os.devnull, os.O_RDONLY)
    tty_keepalive = os.dup(sys.stdout.fileno())
    os.set_inheritable(tty_keepalive, False)
    try:
        os.dup2(log_descriptor, sys.stdout.fileno())
        os.dup2(log_descriptor, sys.stderr.fileno())
        os.dup2(null_descriptor, sys.stdin.fileno())
    finally:
        os.close(log_descriptor)
        os.close(null_descriptor)
    print(json.dumps({"event": "worker_stdio_ready", "pid": os.getpid()}), flush=True)
    terminal_path = output / policy.terminal_status_relative_path
    with _exclusive_lock(output / policy.run_lock_relative_path):
        reservation = None
        try:
            reservation = claim_resources(
                task=policy.resource_task,
                lean_workers=policy.lean_workers,
                lean_rss_gib=policy.lean_rss_gib,
                gpu=False,
                pid=os.getpid(),
                owner_session=policy.session_name,
                worktree=loaded.repo_root,
            )
            _append_journal(
                output / policy.journal_relative_path,
                {
                    "event": "resource_claimed",
                    "at": datetime.now(UTC).isoformat(),
                    "pid": os.getpid(),
                },
            )
            launch_receipt = {
                "version": "leanfaith_sft2a_reference_detached_launch_v5_2",
                "session_name": policy.session_name,
                "pid": os.getpid(),
                "config_hash": loaded.config_hash,
                "config_file_sha256": hash_file(loaded.path),
                "authorization_sha256": authorization.sha256,
                "implementation_commit": authorization.document["implementation_commit"],
                "implementation_tree": authorization.document["implementation_tree"],
                "pool_sha256": hash_file(output / "pool.jsonl"),
                "output_root": str(output),
                "cache_root": str(
                    Path(config.staging_root) / config.reference_certification.cache_subdir
                ),
                "combined_log": str(log),
                "journal": str(output / policy.journal_relative_path),
                "resume_command": (
                    f"uv run python -m leanfaith.sft2a --config {loaded.path} "
                    f"--reference-certification-authorization {authorization.path} "
                    "launch-reference-certification"
                ),
                "health_command": (
                    f"uv run python -m leanfaith.sft2a --config {loaded.path} "
                    "reference-certification-health"
                ),
                "provider_calls_allowed": 0,
                "duplicate_restart_forbidden": True,
            }
            _atomic_exact(
                output / policy.launch_receipt_relative_path,
                canonical_json_bytes(launch_receipt) + b"\n",
            )
            manifest = run_reference_certification(loaded, authorization)
            replay = verify_reference_replay(loaded)
            preflight = verify_global_reference_preflight(loaded)
            terminal = {
                "version": "leanfaith_sft2a_reference_terminal_v5_2",
                "status": "complete",
                "completed_at": datetime.now(UTC).isoformat(),
                "manifest_sha256": hash_file(output / "certification_manifest.json"),
                "sample_sha256": manifest["sample_sha256"],
                "replay_sha256": hash_file(output / "reference_replay_receipt.json"),
                "preflight_sha256": hash_file(output / "global_100_preflight_receipt.json"),
                "global_certificate": preflight["global_certificate"],
                "replay_cache_hits": replay["cache_hits"],
                "provider_calls_executed": 0,
            }
            _atomic_replace(terminal_path, terminal)
            return terminal
        except Exception as exc:
            _atomic_replace(
                terminal_path,
                {
                    "version": "leanfaith_sft2a_reference_terminal_v5_2",
                    "status": "failed",
                    "failed_at": datetime.now(UTC).isoformat(),
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                    "provider_calls_executed": 0,
                },
            )
            raise
        finally:
            if reservation is not None:
                release_resources(task=policy.resource_task)
            os.close(tty_keepalive)


__all__ = [
    "AUTHORIZATION_TEXT",
    "LoadedReferenceCertificationAuthorization",
    "ReferenceCertificationPhaseError",
    "launch_detached_reference_certification",
    "load_reference_authorization",
    "materialize_reference_authorization",
    "preflight_reference_launch",
    "prepare_reference_pool",
    "reference_certification_health",
    "run_detached_reference_certification_worker",
    "run_reference_certification",
    "verify_global_reference_preflight",
    "verify_reference_replay",
]
