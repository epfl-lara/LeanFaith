"""Authorized, resumable, sharded SFT2A v5 rehearsal and blinded audit."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.host_resources import claim_resources, release_resources
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft2a.budget import BudgetedProvider, PersistentProviderBudget
from leanfaith.sft2a.census import loaded_with_root, prepare_rehearsal_sample
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.dedup import PersistentCandidateRegistry
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.lean_oracle import SignatureOracle
from leanfaith.sft2a.legacy import _atomic_exact, _blocklist
from leanfaith.sft2a.mechanisms import (
    MechanismAssignment,
    MechanismPolarity,
    shortcut_violation,
)
from leanfaith.sft2a.models import JudgeOutputV5, OneRootConfig, SFT2AV5Config
from leanfaith.sft2a.pipeline import StructuredProvider, run_one_root
from leanfaith.sft2a.prompts import prompt_hash, render_blinded_judge_prompt
from leanfaith.sft2a.providers import (
    claude_judge_provider,
    lemex_audit_provider,
    proposer_provider,
)


class RehearsalError(RuntimeError):
    """Authorization, execution, replay, compaction, audit, or detached launch failed."""


@dataclass(frozen=True, slots=True)
class LoadedRehearsalAuthorization:
    path: Path
    document: dict[str, object]
    sha256: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _v5(loaded: LoadedSFT2AConfig) -> SFT2AV5Config:
    if not isinstance(loaded.config, SFT2AV5Config):
        raise RehearsalError("v5 rehearsal requires the additive closure-aware config")
    return loaded.config


def _output(loaded: LoadedSFT2AConfig) -> Path:
    config = _v5(loaded)
    return Path(config.staging_root) / config.rehearsal.output_subdir


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RehearsalError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RehearsalError(f"JSON artifact is not an object: {path}")
    return value


def _jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RehearsalError(f"JSONL row is not an object: {path}")
        rows.append(value)
    return rows


def _jsonl_bytes(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


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


def load_rehearsal_authorization(
    loaded: LoadedSFT2AConfig, path: Path
) -> LoadedRehearsalAuthorization:
    """Load a hash-bound receipt; readiness receipts are valid but never authorize calls."""

    config = _v5(loaded)
    resolved = path if path.is_absolute() else loaded.repo_root / path
    if resolved.is_symlink() or not resolved.is_file():
        raise RehearsalError("rehearsal authorization receipt is missing or unsafe")
    document = _object(resolved)
    sample = prepare_rehearsal_sample(loaded)
    expected = {
        "version": "leanfaith_sft2a_rehearsal_authorization_v5",
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "sample_sha256": sample["sample_sha256"],
        "root_count": 100,
        "slot_count": 400,
        "ceilings": config.rehearsal.ceilings.model_dump(mode="json"),
        "legacy_rejudge_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "publication_authorized": False,
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise RehearsalError(f"rehearsal authorization receipt differs at {key}")
    authorized = document.get("authorized")
    status = document.get("status")
    if not isinstance(authorized, bool) or status not in {
        "ready_not_authorized",
        "authorized_rehearsal",
    }:
        raise RehearsalError("rehearsal authorization state is malformed")
    if authorized != (status == "authorized_rehearsal"):
        raise RehearsalError("rehearsal authorization flag/status contradict")
    implementation_commit = document.get("implementation_commit")
    implementation_tree = document.get("implementation_tree")
    if (
        not isinstance(implementation_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", implementation_commit) is None
    ):
        raise RehearsalError("rehearsal implementation commit is malformed")
    if (
        not isinstance(implementation_tree, str)
        or re.fullmatch(r"[0-9a-f]{40}", implementation_tree) is None
    ):
        raise RehearsalError("rehearsal implementation tree is malformed")
    observed_tree = subprocess.run(
        ("git", "rev-parse", f"{implementation_commit}^{{tree}}"),
        cwd=loaded.repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ancestor = subprocess.run(
        ("git", "merge-base", "--is-ancestor", implementation_commit, "HEAD"),
        cwd=loaded.repo_root,
        check=False,
        capture_output=True,
    )
    if observed_tree != implementation_tree or ancestor.returncode != 0:
        raise RehearsalError("rehearsal implementation identity is not an ancestor of HEAD")
    smoke_path_value = document.get("smoke_receipt_path")
    smoke_sha256 = document.get("smoke_receipt_sha256")
    if not isinstance(smoke_path_value, str) or not isinstance(smoke_sha256, str):
        raise RehearsalError("rehearsal authorization lacks its smoke receipt binding")
    smoke_path = loaded.repo_root / smoke_path_value
    if smoke_path.is_symlink() or not smoke_path.is_file() or hash_file(smoke_path) != smoke_sha256:
        raise RehearsalError("rehearsal smoke receipt binding differs")
    return LoadedRehearsalAuthorization(
        path=resolved,
        document=document,
        sha256=hash_file(resolved),
    )


def require_rehearsal_authorization(receipt: LoadedRehearsalAuthorization) -> None:
    if receipt.document.get("authorized") is not True:
        raise RehearsalError("100-root/400-slot rehearsal is not authorized")
    if receipt.document.get("authorization_scope") != "sft2a_v5_100_roots_400_slots_only":
        raise RehearsalError("rehearsal authorization scope differs")


def _sample_rows(loaded: LoadedSFT2AConfig) -> list[dict[str, object]]:
    output = _output(loaded)
    manifest = prepare_rehearsal_sample(loaded)
    rows = _jsonl(output / "sample.jsonl")
    if len(rows) != 100 or hash_file(output / "sample.jsonl") != manifest["sample_sha256"]:
        raise RehearsalError("v5 rehearsal sample population/hash differs")
    return rows


def _root_output(output: Path, row: Mapping[str, object]) -> Path:
    root = row.get("root")
    if not isinstance(root, Mapping):
        raise RehearsalError("sample row lacks root")
    context = root.get("compile_context")
    if not isinstance(context, Mapping):
        raise RehearsalError("sample root lacks compile context")
    return output / "roots" / str(context["project_id"]) / hash_canonical(str(root["root_id"]))[:20]


def _mechanism_plan(row: Mapping[str, object]) -> dict[str, MechanismAssignment]:
    raw = row.get("mechanism_plan")
    if not isinstance(raw, Mapping):
        raise RehearsalError("sample row lacks a frozen mechanism plan")
    result: dict[str, MechanismAssignment] = {}
    for slot_id, value in raw.items():
        if not isinstance(value, dict):
            raise RehearsalError("sample mechanism assignment is malformed")
        polarity = value.get("polarity")
        if polarity not in {"preserving", "breaking"}:
            raise RehearsalError("sample mechanism polarity is malformed")
        result[str(slot_id)] = MechanismAssignment(
            family=str(value.get("family")),
            polarity=cast(MechanismPolarity, polarity),
            instruction=str(value.get("instruction")),
            applicability=str(value.get("applicability")),
            shape_id=str(value.get("shape_id")),
        )
    return result


def _append_journal(path: Path, event: Mapping[str, object]) -> None:
    record = {"event_id": "sft2a-v5:" + hash_canonical(event), **event}
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


def _root_receipts(loaded: LoadedSFT2AConfig) -> list[dict[str, object]]:
    output = _output(loaded)
    receipts: list[dict[str, object]] = []
    for row in _sample_rows(loaded):
        root = row["root"]
        assert isinstance(root, dict)
        path = _root_output(output, row) / "manifest.json"
        if not path.is_file():
            raise RehearsalError(f"rehearsal root is incomplete: {root['root_id']}")
        manifest = _object(path)
        counts = manifest.get("counts")
        accepted = counts.get("accepted", 0) if isinstance(counts, Mapping) else 0
        context = root.get("compile_context")
        if not isinstance(context, Mapping):
            raise RehearsalError("sample root lacks compile context")
        receipts.append(
            {
                "root_id": root["root_id"],
                "project_id": context["project_id"],
                "manifest_path": str(path.relative_to(output)),
                "manifest_sha256": hash_file(path),
                "accepted": accepted,
            }
        )
    return receipts


def compact_rehearsal(loaded: LoadedSFT2AConfig) -> dict[str, object]:
    """Deterministically compact accepted roots and enforce global release screens."""

    output = _output(loaded)
    sample_rows = _sample_rows(loaded)
    core_by_id: dict[str, dict[str, object]] = {}
    sidecar_by_id: dict[str, dict[str, object]] = {}
    candidate_goals: set[str] = set()
    candidate_exprs: set[str] = set()
    mechanism_counts: Counter[tuple[str, str]] = Counter()
    cosmetic = 0
    _blocklist_path, blocked_hashes = _blocklist(loaded)
    for row in sample_rows:
        root_output = _root_output(output, row)
        core = _jsonl(root_output / "new_core/core.jsonl")
        sidecars = _jsonl(root_output / "new_core/sidecar.jsonl")
        if len(core) != len(sidecars):
            raise RehearsalError("root core/sidecar row counts differ")
        for core_row, sidecar in zip(core, sidecars, strict=True):
            row_id = sidecar.get("row_id")
            reference = core_row.get("reference")
            candidate = core_row.get("candidate")
            candidate_expr = sidecar.get("candidate_closed_expr_hash")
            reference_expr = sidecar.get("reference_closed_expr_hash")
            if not isinstance(row_id, str) or row_id in core_by_id:
                raise RehearsalError("duplicate stable row ID during compaction")
            if reference == candidate or candidate_expr == reference_expr:
                raise RehearsalError("self-pair reached v5 compaction")
            if not isinstance(candidate, str) or candidate in candidate_goals:
                raise RehearsalError("cross-root rendered candidate duplicate reached compaction")
            if not isinstance(candidate_expr, str) or candidate_expr in candidate_exprs:
                raise RehearsalError("cross-root closed Expr duplicate reached compaction")
            if signature_near_dup_hash(candidate) in blocked_hashes:
                raise RehearsalError("gold contamination reached v5 compaction")
            raw_reference = sidecar.get("raw_reference_signature")
            raw_candidate = sidecar.get("raw_candidate_signature")
            if isinstance(raw_reference, str) and isinstance(raw_candidate, str):
                cosmetic += shortcut_violation(raw_reference, raw_candidate) is not None
            planned = sidecar.get("planned_mechanism")
            if not isinstance(planned, dict):
                raise RehearsalError("accepted v5 row lacks its planned mechanism")
            mechanism_counts[(str(planned["polarity"]), str(planned["family"]))] += 1
            candidate_goals.add(candidate)
            candidate_exprs.add(candidate_expr)
            core_by_id[row_id] = core_row
            sidecar_by_id[row_id] = sidecar
    ordered = sorted(core_by_id)
    compacted_core = [core_by_id[row_id] for row_id in ordered]
    compacted_sidecars = [sidecar_by_id[row_id] for row_id in ordered]
    _atomic_exact(output / "compacted/new_core/core.jsonl", _jsonl_bytes(compacted_core))
    _atomic_exact(output / "compacted/new_core/sidecar.jsonl", _jsonl_bytes(compacted_sidecars))
    histogram = {
        polarity: {
            family: mechanism_counts[(polarity, family)]
            for family in sorted(
                family for seen_polarity, family in mechanism_counts if seen_polarity == polarity
            )
        }
        for polarity in ("preserving", "breaking")
    }
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_rehearsal_compaction_v5",
        "config_hash": loaded.config_hash,
        "sample_sha256": hash_file(output / "sample.jsonl"),
        "root_count": 100,
        "planned_slots": 400,
        "accepted_rows": len(compacted_core),
        "self_pairs": 0,
        "candidate_duplicates": 0,
        "contamination_hits": 0,
        "cosmetic_or_tautological_rows": cosmetic,
        "cosmetic_or_tautological_fraction": (
            0.0 if not compacted_core else cosmetic / len(compacted_core)
        ),
        "mechanism_histogram": histogram,
        "artifacts": {
            "compacted/new_core/core.jsonl": hash_file(output / "compacted/new_core/core.jsonl"),
            "compacted/new_core/sidecar.jsonl": hash_file(
                output / "compacted/new_core/sidecar.jsonl"
            ),
        },
    }
    _atomic_exact(output / "compacted/manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


def _number(mapping: Mapping[str, object], key: str) -> float:
    value = mapping.get(key, 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def project_50000_root_attempts(
    *, observed_retry_attempts: int, observed_slots: int
) -> dict[str, object]:
    """Project four base slots per root plus the separately measured retry rate."""

    if observed_retry_attempts < 0 or observed_slots <= 0:
        raise ValueError("invalid observed retry population")
    base_slots = 50_000 * 4
    retry_rate_per_slot = observed_retry_attempts / observed_slots
    projected_attempts = round(base_slots * (1.0 + retry_rate_per_slot))
    return {
        "base_candidate_slots": base_slots,
        "projected_candidate_retries": projected_attempts - base_slots,
        "projected_candidate_attempts": projected_attempts,
        "linear_projection_only": True,
        "authorized": False,
    }


def consolidate_rehearsal_quality(
    loaded: LoadedSFT2AConfig,
    *,
    audit: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Consolidate retry taxonomy, fresh project throughput, mechanisms, and gates."""

    output = _output(loaded)
    compacted = compact_rehearsal(loaded)
    config = _v5(loaded)
    sample_by_root: dict[str, dict[str, object]] = {}
    for row in _sample_rows(loaded):
        root = row.get("root")
        if not isinstance(root, Mapping):
            raise RehearsalError("sample row lacks root")
        sample_by_root[str(root["root_id"])] = row
    project_stats: dict[str, Counter[str]] = defaultdict(Counter)
    counts: Counter[str] = Counter()
    for receipt in _root_receipts(loaded):
        manifest = _object(output / str(receipt["manifest_path"]))
        root_counts = manifest.get("counts")
        lean = manifest.get("lean")
        if not isinstance(root_counts, dict) or not isinstance(lean, dict):
            raise RehearsalError("root manifest lacks count/Lean metrics")
        counts.update({key: int(_number(root_counts, key)) for key in root_counts})
        row = sample_by_root[str(receipt["root_id"])]
        root = row.get("root")
        if not isinstance(root, Mapping):
            raise RehearsalError("sample row lacks root")
        context = root.get("compile_context")
        if not isinstance(context, Mapping):
            raise RehearsalError("sample root lacks compile context")
        project = str(context["project_id"])
        project_stats[project]["candidate_requests"] += int(_number(lean, "candidate_requests"))
        project_stats[project]["candidate_executed"] += int(_number(lean, "candidate_executed"))
        project_stats[project]["elapsed_milliseconds"] += round(
            _number(lean, "candidate_elapsed_seconds") * 1000
        )
    observed_attempts = counts["candidate_attempts"] or counts["attempts"]
    retry_attempts = counts["candidate_retry_attempts"]
    projection = project_50000_root_attempts(
        observed_retry_attempts=retry_attempts, observed_slots=400
    )
    project_metrics = []
    for project, stats in sorted(project_stats.items()):
        seconds = stats["elapsed_milliseconds"] / 1000
        project_metrics.append(
            {
                "project_id": project,
                "candidate_requests": stats["candidate_requests"],
                "candidate_executed": stats["candidate_executed"],
                "elapsed_seconds": seconds,
                "fresh_rows_per_second": (
                    None if seconds == 0 else stats["candidate_executed"] / seconds
                ),
                "fresh_measurement": stats["candidate_executed"] > 0 and seconds > 0,
            }
        )
    histogram = compacted["mechanism_histogram"]
    assert isinstance(histogram, dict)
    families = {
        polarity: len(values) if isinstance(values, dict) else 0
        for polarity, values in histogram.items()
    }
    dominant = max(
        (
            count / max(1, sum(values.values()))
            for values in histogram.values()
            if isinstance(values, dict)
            for count in values.values()
            if isinstance(count, int)
        ),
        default=0.0,
    )
    criteria = config.rehearsal.pass_criteria
    checks: dict[str, bool] = {
        "zero_self_pairs": compacted["self_pairs"] == 0,
        "zero_duplicates": compacted["candidate_duplicates"] == 0,
        "zero_contamination": compacted["contamination_hits"] == 0,
        "mechanism_family_minimum": all(
            families.get(polarity, 0) >= criteria.minimum_mechanism_families_per_polarity
            for polarity in ("preserving", "breaking")
        ),
        "no_dominant_shortcut_family": dominant <= criteria.maximum_dominant_family_fraction,
        "cosmetic_fraction": _number(compacted, "cosmetic_or_tautological_fraction")
        < criteria.maximum_cosmetic_or_tautological_fraction,
        "fresh_per_project_throughput": all(
            bool(row["fresh_measurement"]) for row in project_metrics
        ),
        "interrupted_resume_verified": (
            (output / "interrupted_resume_receipt.json").is_file()
            and _object(output / "interrupted_resume_receipt.json").get("resume_verified") is True
        ),
        "zero_call_replay": (output / "reproducibility_receipt.json").is_file(),
    }
    if audit is not None:
        checks.update(
            {
                "minimum_kimi_audits": _number(audit, "selected_rows")
                >= config.rehearsal.minimum_kimi_audits,
                "audit_agreement": _number(audit, "agreement_rate")
                >= criteria.minimum_audit_agreement_after_malformed_retries,
                "zero_confirmed_label_errors": _number(audit, "confirmed_label_errors") == 0,
                "no_systematic_disagreement": audit.get("systematic_disagreement") is False,
            }
        )
    quality: dict[str, object] = {
        "version": "leanfaith_sft2a_rehearsal_quality_v5",
        "config_hash": loaded.config_hash,
        "sample_sha256": hash_file(output / "sample.jsonl"),
        "root_count": 100,
        "slot_count": 400,
        "accepted_rows": compacted["accepted_rows"],
        "counts": dict(counts),
        "retry_taxonomy": {
            "base_candidate_slots": 400,
            "candidate_retry_attempts": retry_attempts,
            "observed_candidate_attempts": observed_attempts,
            "judge_malformed_retry": "same candidate; no candidate slot attempt consumed",
            "provider_infrastructure_retry": "transport-only; no semantic attempt consumed",
            "lean_infrastructure_failure": "terminal shard failure; no semantic retry",
        },
        "fresh_project_throughput": project_metrics,
        "mechanism_histogram": histogram,
        "dominant_family_fraction": dominant,
        "projected_50000_roots": projection,
        "audit": None if audit is None else dict(audit),
        "pass_checks": checks,
        "rehearsal_passed": audit is not None and all(checks.values()),
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "published": False,
    }
    _atomic_replace(output / "quality_manifest.json", quality)
    return quality


def run_rehearsal(
    loaded: LoadedSFT2AConfig,
    authorization: LoadedRehearsalAuthorization,
    *,
    proposer: StructuredProvider | None = None,
    opus_judge: StructuredProvider | None = None,
    stop_after_roots: int | None = None,
) -> dict[str, object]:
    """Run project-grouped roots; completed root manifests make resume call-free."""

    require_rehearsal_authorization(authorization)
    config = _v5(loaded)
    output = _output(loaded)
    final_path = output / "manifest.json"
    if final_path.is_file():
        manifest = _object(final_path)
        raw_receipts = manifest.get("root_manifests")
        if not isinstance(raw_receipts, list):
            raise RehearsalError("rehearsal manifest lacks root receipts")
        for receipt in raw_receipts:
            if not isinstance(receipt, dict):
                raise RehearsalError("rehearsal root receipt is malformed")
            path = output / str(receipt["manifest_path"])
            if hash_file(path) != receipt.get("manifest_sha256"):
                raise RehearsalError("rehearsal replay root artifact differs")
        compact_rehearsal(loaded)
        return {
            **manifest,
            "replayed": True,
            "provider_calls_executed": 0,
            "lean_requests_executed": 0,
        }
    sample_rows = _sample_rows(loaded)
    budget = PersistentProviderBudget(
        output / "provider_budget_journal.jsonl", config.rehearsal.ceilings
    )
    proposer_client = BudgetedProvider(
        proposer or proposer_provider(loaded), kind="proposer", budget=budget
    )
    judge_client = BudgetedProvider(
        opus_judge or claude_judge_provider(loaded), kind="opus", budget=budget
    )
    registry = PersistentCandidateRegistry(output / "candidate_registry.jsonl")
    resume_path = output / "interrupted_resume_receipt.json"
    resume_document = _object(resume_path) if resume_path.is_file() else None
    resume_pending = (
        isinstance(resume_document, dict)
        and resume_document.get("resume_required") is True
        and resume_document.get("resume_verified") is not True
    )
    resume_budget_before = budget.snapshot()
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in sample_rows:
        root = row["root"]
        assert isinstance(root, dict)
        context = root["compile_context"]
        assert isinstance(context, dict)
        groups[str(context["project_id"])].append(row)
    completed_this_call = 0
    for project_id in sorted(groups):
        rows = groups[project_id]
        pending = [
            row for row in rows if not (_root_output(output, row) / "manifest.json").is_file()
        ]
        oracle: SignatureOracle | None = None
        try:
            for row in rows:
                root = OneRootConfig.model_validate(row["root"])
                root_loaded = loaded_with_root(loaded, root)
                root_output = _root_output(output, row)
                if not (root_output / "manifest.json").is_file():
                    if oracle is None:
                        oracle = SignatureOracle(root_loaded)
                    else:
                        oracle.rebind(root_loaded)
                result = run_one_root(
                    root_loaded,
                    proposer=proposer_client,
                    claude_judge=judge_client,
                    oracle=oracle,
                    output_root=root_output,
                    enforce_expected_reference_goal=False,
                    enforce_smoke_ceilings=False,
                    cross_root_registry=registry,
                    mechanism_plan=_mechanism_plan(row),
                )
                if result.replayed and resume_pending:
                    if budget.snapshot() != resume_budget_before:
                        raise RehearsalError("interrupted root replay consumed a provider call")
                    assert isinstance(resume_document, dict)
                    _atomic_replace(
                        resume_path,
                        {
                            **resume_document,
                            "resume_required": False,
                            "resume_verified": True,
                            "verified_root_id": root.root_id,
                            "verified_manifest_sha256": hash_file(root_output / "manifest.json"),
                            "provider_calls_replayed": 0,
                            "lean_requests_replayed": 0,
                            "verified_at": _now(),
                        },
                    )
                    resume_pending = False
                if not result.replayed:
                    completed_this_call += 1
                    _append_journal(
                        output / "journal.jsonl",
                        {
                            "event": "root_completed",
                            "at": _now(),
                            "root_id": root.root_id,
                            "project_id": project_id,
                            "manifest_sha256": hash_file(root_output / "manifest.json"),
                        },
                    )
                    if stop_after_roots is not None and completed_this_call >= stop_after_roots:
                        _atomic_replace(
                            output / "interrupted_resume_receipt.json",
                            {
                                "version": "leanfaith_sft2a_interrupted_resume_v5",
                                "interrupted_after_roots": completed_this_call,
                                "resume_required": True,
                                "resume_verified": False,
                                "provider_calls_replayed": 0,
                                "lean_requests_replayed": 0,
                            },
                        )
                        raise RehearsalError("intentional rehearsal interruption canary")
        finally:
            if oracle is not None:
                oracle.close()
        if pending:
            _append_journal(
                output / "journal.jsonl",
                {
                    "event": "project_group_completed",
                    "at": _now(),
                    "project_id": project_id,
                    "roots": len(rows),
                    "persistent_lean_environments": 1,
                },
            )
    receipts = _root_receipts(loaded)
    compacted = compact_rehearsal(loaded)
    quality = consolidate_rehearsal_quality(loaded)
    manifest = {
        "version": "leanfaith_sft2a_100_root_rehearsal_v5",
        "config_hash": loaded.config_hash,
        "authorization_receipt_sha256": authorization.sha256,
        "implementation_commit": authorization.document["implementation_commit"],
        "implementation_tree": authorization.document["implementation_tree"],
        "smoke_receipt_sha256": authorization.document["smoke_receipt_sha256"],
        "sample_sha256": hash_file(output / "sample.jsonl"),
        "root_count": len(receipts),
        "slot_count": 400,
        "root_manifests": receipts,
        "compaction_manifest_sha256": hash_file(output / "compacted/manifest.json"),
        "quality_manifest_sha256": hash_file(output / "quality_manifest.json"),
        "provider_budget": budget.snapshot(),
        "candidate_registry": registry.snapshot(),
        "accepted_rows": compacted["accepted_rows"],
        "projected_50000_roots": quality["projected_50000_roots"],
        "rehearsal_completed": True,
        "audit_completed": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "published": False,
    }
    _atomic_exact(final_path, canonical_json_bytes(manifest) + b"\n")
    return {**manifest, "replayed": False}


def _snapshot(root: Path, *, exclude: set[str]) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): hash_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and str(path.relative_to(root)) not in exclude
        and not str(path.relative_to(root)).startswith("audit_kimi_v5/")
    }


def verify_rehearsal_replay(
    loaded: LoadedSFT2AConfig, authorization: LoadedRehearsalAuthorization
) -> dict[str, object]:
    """Replay a completed rehearsal and prove zero calls/Lean plus byte stability."""

    output = _output(loaded)
    exclude = {"reproducibility_receipt.json"}
    before = _snapshot(output, exclude=exclude)
    provider_before = _snapshot(Path(loaded.config.staging_root) / "provider_calls", exclude=set())
    lean_before = _snapshot(Path(loaded.config.staging_root) / "lean_cache", exclude=set())
    replay = run_rehearsal(loaded, authorization)
    after = _snapshot(output, exclude=exclude)
    if (
        replay.get("replayed") is not True
        or before != after
        or provider_before
        != _snapshot(Path(loaded.config.staging_root) / "provider_calls", exclude=set())
        or lean_before != _snapshot(Path(loaded.config.staging_root) / "lean_cache", exclude=set())
    ):
        raise RehearsalError("v5 rehearsal replay changed durable artifacts or caches")
    receipt = {
        "version": "leanfaith_sft2a_rehearsal_replay_v5",
        "config_hash": loaded.config_hash,
        "authorization_receipt_sha256": authorization.sha256,
        "implementation_commit": authorization.document["implementation_commit"],
        "implementation_tree": authorization.document["implementation_tree"],
        "manifest_sha256": hash_file(output / "manifest.json"),
        "durable_artifact_set_sha256": hash_canonical(before),
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "durable_artifacts_changed": 0,
        "reproducible": True,
    }
    _atomic_exact(output / "reproducibility_receipt.json", canonical_json_bytes(receipt) + b"\n")
    return receipt


def _audit_selection(sidecars: Sequence[Mapping[str, object]], count: int) -> list[int]:
    strata: dict[tuple[str, str, str], list[tuple[str, int]]] = defaultdict(list)
    for index, row in enumerate(sidecars):
        root = str(row["root_id"])
        source = root.split(":", maxsplit=1)[0]
        planned = row.get("planned_mechanism")
        family = str(planned.get("family")) if isinstance(planned, dict) else "missing"
        key = (source, str(row["requested_polarity"]), family)
        strata[key].append(
            (hash_canonical({"audit": "sft2a-v5-kimi", "row_id": row["row_id"]}), index)
        )
    for ranked in strata.values():
        ranked.sort()
    selected: list[int] = []
    cursor = 0
    while len(selected) < count:
        progressed = False
        for key in sorted(strata):
            if cursor < len(strata[key]):
                selected.append(strata[key][cursor][1])
                progressed = True
                if len(selected) == count:
                    break
        if not progressed:
            raise RehearsalError("accepted rehearsal rows cannot fill the Kimi audit minimum")
        cursor += 1
    return selected


def exclude_audit_unknowns(
    core: Sequence[Mapping[str, object]],
    sidecars: Sequence[Mapping[str, object]],
    unknown_ids: set[str],
) -> list[dict[str, object]]:
    """Join by stable row ID and remove every audit unknown from the releasable core."""

    if len(core) != len(sidecars):
        raise RehearsalError("audit core/sidecar populations differ")
    observed_ids = {str(sidecar.get("row_id")) for sidecar in sidecars}
    if not unknown_ids <= observed_ids:
        raise RehearsalError("audit unknown row ID is absent from the source core")
    return [
        dict(core_row)
        for core_row, sidecar in zip(core, sidecars, strict=True)
        if str(sidecar["row_id"]) not in unknown_ids
    ]


def run_rehearsal_audit(
    loaded: LoadedSFT2AConfig,
    authorization: LoadedRehearsalAuthorization,
    *,
    auditor: StructuredProvider | None = None,
) -> dict[str, object]:
    """Audit at least 40 combined rows after replay, retrying malformed output once."""

    require_rehearsal_authorization(authorization)
    config = _v5(loaded)
    output = _output(loaded)
    replay_path = output / "reproducibility_receipt.json"
    if not replay_path.is_file() or _object(replay_path).get("reproducible") is not True:
        raise RehearsalError("v5 Kimi audit requires successful rehearsal replay")
    audit_root = output / "audit_kimi_v5"
    manifest_path = audit_root / "manifest.json"
    if manifest_path.is_file():
        return _object(manifest_path)
    sidecars = _jsonl(output / "compacted/new_core/sidecar.jsonl")
    core = _jsonl(output / "compacted/new_core/core.jsonl")
    selected = _audit_selection(sidecars, config.rehearsal.minimum_kimi_audits)
    budget = PersistentProviderBudget(
        output / "provider_budget_journal.jsonl", config.rehearsal.ceilings
    )
    client = BudgetedProvider(auditor or lemex_audit_provider(loaded), kind="lemex", budget=budget)
    audit_rows: list[dict[str, object]] = []
    unknown_ids: set[str] = set()
    stratum_counts: Counter[tuple[str, str]] = Counter()
    stratum_disagreements: Counter[tuple[str, str]] = Counter()
    for index in selected:
        sidecar = sidecars[index]
        reference = sidecar["reference_repr"]["record"]["goal_v1"]  # type: ignore[index]
        candidate = sidecar["candidate_repr"]["record"]["goal_v1"]  # type: ignore[index]
        prompt = render_blinded_judge_prompt(
            loaded, statement_a=str(reference), statement_b=str(candidate)
        )
        result = call_consistent_judge(
            client,
            prompt=prompt,
            input_ids=(str(sidecar["row_id"]), "rehearsal_kimi_audit_v5"),
            closure_aware=True,
            malformed_retries=1,
        )
        source_judge = JudgeOutputV5.model_validate(sidecar["claude_judge"])
        judgment = result.judgment
        agrees = judgment is not None and judgment.verdict == source_judge.verdict
        malformed_exhausted = judgment is None
        row_id = str(sidecar["row_id"])
        source = str(sidecar["root_id"]).split(":", maxsplit=1)[0]
        stratum = (source, str(sidecar["requested_polarity"]))
        stratum_counts[stratum] += 1
        stratum_disagreements[stratum] += not agrees and not malformed_exhausted
        if not agrees:
            unknown_ids.add(row_id)
        audit_rows.append(
            {
                "row_id": row_id,
                "source": source,
                "requested_polarity": sidecar["requested_polarity"],
                "opus_verdict": source_judge.verdict,
                "kimi_judgment": (None if judgment is None else judgment.model_dump(mode="json")),
                "agrees": agrees,
                "malformed_attempts": list(result.malformed_attempts),
                "malformed_retries": result.malformed_retries,
                "malformed_exhausted": malformed_exhausted,
                "call_keys": [call.call_key for call in result.calls],
                "prompt_hash": prompt_hash(prompt),
                "action": "retain" if agrees else "unknown_review_exclude_core",
            }
        )
    _atomic_exact(audit_root / "audit/rows.jsonl", _jsonl_bytes(audit_rows))
    _atomic_exact(
        audit_root / "unknown_review/rows.jsonl",
        _jsonl_bytes(
            [{"row_id": row_id, "training_eligible": False} for row_id in sorted(unknown_ids)]
        ),
    )
    released = exclude_audit_unknowns(core, sidecars, unknown_ids)
    _atomic_exact(audit_root / "releasable_core/core.jsonl", _jsonl_bytes(released))
    agreements = sum(bool(row["agrees"]) for row in audit_rows)
    genuine_disagreements = sum(
        not bool(row["agrees"]) and not bool(row["malformed_exhausted"]) for row in audit_rows
    )
    systematic = agreements / len(audit_rows) < 0.95 or any(
        stratum_counts[key] >= 5 and stratum_disagreements[key] / stratum_counts[key] >= 0.2
        for key in stratum_counts
    )
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_rehearsal_kimi_audit_v5",
        "config_hash": loaded.config_hash,
        "authorization_receipt_sha256": authorization.sha256,
        "source_manifest_sha256": hash_file(output / "manifest.json"),
        "replay_receipt_sha256": hash_file(replay_path),
        "selected_rows": len(selected),
        "selected_row_ids": [str(sidecars[index]["row_id"]) for index in selected],
        "agreements": agreements,
        "genuine_semantic_disagreements": genuine_disagreements,
        "malformed_attempts": sum(
            len(value) if isinstance(value, list) else 0
            for row in audit_rows
            for value in (row.get("malformed_attempts"),)
        ),
        "malformed_retries": sum(
            value if isinstance(value, int) and not isinstance(value, bool) else 0
            for row in audit_rows
            for value in (row.get("malformed_retries"),)
        ),
        "malformed_exhausted": sum(bool(row["malformed_exhausted"]) for row in audit_rows),
        "agreement_rate": agreements / len(audit_rows),
        "confirmed_label_errors": 0,
        "unknown_review_rows": len(unknown_ids),
        "released_rows": len(released),
        "systematic_disagreement": systematic,
        "provider": loaded.config.lemex_auditor.model_dump(mode="json"),
        "prompt": loaded.config.prompts.blinded_claude_judge.model_dump(mode="json"),
        "persistent_provider_budget": budget.snapshot(),
        "artifacts": {
            "audit/rows.jsonl": hash_file(audit_root / "audit/rows.jsonl"),
            "unknown_review/rows.jsonl": hash_file(audit_root / "unknown_review/rows.jsonl"),
            "releasable_core/core.jsonl": hash_file(audit_root / "releasable_core/core.jsonl"),
        },
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "published": False,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    consolidate_rehearsal_quality(loaded, audit=manifest)
    return manifest


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RehearsalError("v5 rehearsal lock is already held") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _session_exists(name: str) -> bool:
    return (
        subprocess.run(
            ("tmux", "has-session", "-t", name), check=False, capture_output=True
        ).returncode
        == 0
    )


def preflight_rehearsal_launch(
    loaded: LoadedSFT2AConfig, authorization: LoadedRehearsalAuthorization
) -> dict[str, object]:
    """Fail closed immediately before tmux without invoking a provider or Lean."""

    require_rehearsal_authorization(authorization)
    output = _output(loaded)
    config = _v5(loaded)
    policy = config.rehearsal.detached_launch
    if _session_exists(policy.session_name):
        raise RehearsalError("v5 rehearsal tmux session already exists")
    terminal = output / policy.terminal_status_relative_path
    if terminal.is_file() and _object(terminal).get("status") == "complete":
        raise RehearsalError("completed v5 rehearsal cannot restart")
    sample = prepare_rehearsal_sample(loaded)
    return {
        "version": "leanfaith_sft2a_rehearsal_preflight_v5",
        "boundary": "tmux_start_not_executed",
        "session_name": policy.session_name,
        "output_root": str(output),
        "sample_sha256": sample["sample_sha256"],
        "config_hash": loaded.config_hash,
        "authorization_receipt_sha256": authorization.sha256,
        "implementation_commit": authorization.document["implementation_commit"],
        "implementation_tree": authorization.document["implementation_tree"],
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "tmux_sessions_started": 0,
        "legacy_rejudge_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
    }


def launch_detached_rehearsal(
    loaded: LoadedSFT2AConfig, authorization: LoadedRehearsalAuthorization
) -> dict[str, object]:
    """Launch the one authorized v5 rehearsal session; never called during readiness."""

    preflight = preflight_rehearsal_launch(loaded, authorization)
    output = _output(loaded)
    policy = _v5(loaded).rehearsal.detached_launch
    command = (
        sys.executable,
        "-m",
        "leanfaith.sft2a",
        "--config",
        str(loaded.path),
        "--rehearsal-authorization",
        str(authorization.path),
        "detached-v5-rehearsal-worker",
    )
    log = output / policy.combined_log_relative_path
    log.parent.mkdir(parents=True, exist_ok=True)
    shell_command = f"exec {shlex.join(command)} </dev/null >>{shlex.quote(str(log))} 2>&1"
    with _exclusive_lock(output / "detached/launch.lock"):
        if _session_exists(policy.session_name):
            raise RehearsalError("duplicate v5 rehearsal launch refused")
        completed = subprocess.run(
            ("tmux", "new-session", "-d", "-s", policy.session_name, shell_command),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RehearsalError(f"tmux launch failed: {completed.stderr.strip()}")
    return {"preflight": preflight, "session_started": True}


def run_detached_rehearsal_worker(
    loaded: LoadedSFT2AConfig, authorization: LoadedRehearsalAuthorization
) -> dict[str, object]:
    """Hold the exclusive lock and one Lean claim across run, replay, and audit."""

    require_rehearsal_authorization(authorization)
    output = _output(loaded)
    config = _v5(loaded)
    policy = config.rehearsal.detached_launch
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
                {"event": "resource_claimed", "at": _now(), "pid": os.getpid()},
            )
            launch_receipt = {
                "version": "leanfaith_sft2a_rehearsal_detached_launch_v5",
                "session_name": policy.session_name,
                "pid": os.getpid(),
                "config_hash": loaded.config_hash,
                "config_file_sha256": hash_file(loaded.path),
                "authorization_receipt_sha256": authorization.sha256,
                "implementation_commit": authorization.document["implementation_commit"],
                "implementation_tree": authorization.document["implementation_tree"],
                "smoke_receipt_sha256": authorization.document["smoke_receipt_sha256"],
                "sample_sha256": hash_file(output / "sample.jsonl"),
                "ceilings": config.rehearsal.ceilings.model_dump(mode="json"),
                "output_root": str(output),
                "shared_cache_root": config.run_layout.shared_cache_root,
                "combined_log": str(output / policy.combined_log_relative_path),
                "journal": str(output / policy.journal_relative_path),
                "resume_command": (
                    f"uv run python -m leanfaith.sft2a --config {loaded.path} "
                    f"--rehearsal-authorization {authorization.path} launch-v5-rehearsal"
                ),
                "health_command": (
                    f"tmux has-session -t {policy.session_name} && tail -n 40 "
                    f"{output / policy.combined_log_relative_path}"
                ),
                "duplicate_restart_forbidden": True,
            }
            _atomic_exact(
                output / policy.launch_receipt_relative_path,
                canonical_json_bytes(launch_receipt) + b"\n",
            )
            if not (output / "interrupted_resume_receipt.json").is_file():
                try:
                    run_rehearsal(loaded, authorization, stop_after_roots=1)
                except RehearsalError as exc:
                    if str(exc) != "intentional rehearsal interruption canary":
                        raise
            run_rehearsal(loaded, authorization)
            verify_rehearsal_replay(loaded, authorization)
            audit = run_rehearsal_audit(loaded, authorization)
            terminal = {
                "version": "leanfaith_sft2a_rehearsal_terminal_v5",
                "status": "complete",
                "completed_at": _now(),
                "manifest_sha256": hash_file(output / "manifest.json"),
                "replay_sha256": hash_file(output / "reproducibility_receipt.json"),
                "audit_sha256": hash_file(output / "audit_kimi_v5/manifest.json"),
                "systematic_disagreement": audit["systematic_disagreement"],
                "scale_10k_authorized": False,
                "scale_50k_authorized": False,
            }
            _atomic_replace(terminal_path, terminal)
            return terminal
        except Exception as exc:
            terminal = {
                "version": "leanfaith_sft2a_rehearsal_terminal_v5",
                "status": "failed",
                "failed_at": _now(),
                "error_type": type(exc).__name__,
                "detail": str(exc),
            }
            _atomic_replace(terminal_path, terminal)
            raise
        finally:
            if reservation is not None:
                release_resources(task=policy.resource_task)


__all__ = [
    "LoadedRehearsalAuthorization",
    "RehearsalError",
    "compact_rehearsal",
    "consolidate_rehearsal_quality",
    "exclude_audit_unknowns",
    "launch_detached_rehearsal",
    "load_rehearsal_authorization",
    "preflight_rehearsal_launch",
    "project_50000_root_attempts",
    "require_rehearsal_authorization",
    "run_detached_rehearsal_worker",
    "run_rehearsal",
    "run_rehearsal_audit",
    "verify_rehearsal_replay",
]
