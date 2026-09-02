"""Authorized provider-backed runner for the corrected certified SFT2A v5.2 sample."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.host_resources import claim_resources, list_reservations, release_resources
from leanfaith.sft2a.certified_sample_v52 import (
    verify_certified_reference_row,
    verify_corrected_global_preflight,
)
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.dedup import PersistentCandidateRegistry
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.lean_oracle import (
    AuthoringViewResult,
    CacheVersion,
    EffectiveContextV3,
    SignatureOracle,
    SignatureOracleResult,
    elaborator_sha256,
    oracle_method_version,
)
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.mechanisms import MechanismAssignment
from leanfaith.sft2a.models import (
    ExecutionCeilings,
    JudgeOutputV5,
    OneRootConfig,
    SFT2AV52Config,
)
from leanfaith.sft2a.parallel_rehearsal import (
    READ_ONLY_MAXIMUM_WORKERS,
    AtomicBudgetedProvider,
    AtomicProviderBudget,
    ParallelRehearsalError,
    ParallelRootStateMachine,
    parallel_launch_lock,
)
from leanfaith.sft2a.pipeline import run_one_root
from leanfaith.sft2a.prompts import prompt_hash, render_blinded_judge_prompt
from leanfaith.sft2a.providers import (
    StructuredProviderError,
    claude_judge_provider,
    lemex_audit_provider,
    proposer_provider,
)
from leanfaith.sft2a.rehearsal import _audit_selection, exclude_audit_unknowns


class ProviderRehearsalV52Error(RuntimeError):
    """A corrected config, authorization, reference, worker, or launch invariant failed."""


@dataclass(frozen=True, slots=True)
class LoadedProviderRehearsalV52:
    path: Path
    document: dict[str, object]
    sha256: str
    base: LoadedSFT2AConfig
    sample_path: Path
    output_root: Path
    ceilings: ExecutionCeilings
    recovery_source: dict[str, object] | None
    kind: Literal["corrected", "recovery", "sprint"]


@dataclass(frozen=True, slots=True)
class LoadedProviderAuthorizationV52:
    path: Path
    document: dict[str, object]
    sha256: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderRehearsalV52Error(f"invalid JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise ProviderRehearsalV52Error(f"JSON artifact is not an object: {path}")
    return value


def _sample_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ProviderRehearsalV52Error(f"certified sample line {number} is not an object")
        rows.append(value)
    return rows


def _jsonl_bytes(rows: list[dict[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def _atomic_replace_json(path: Path, document: Mapping[str, object]) -> None:
    """Write a replaceable (non-immutable) JSON status document atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(document) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _completed_manifest_seal(root: Path) -> tuple[int, str]:
    manifests = sorted(root.glob("roots/*/manifest.json"))
    payload = b"".join(
        f"{hash_file(path)}  {path.relative_to(root).as_posix()}\n".encode() for path in manifests
    )
    return len(manifests), sha256_hex(payload)


def _repo_path(repo_root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ProviderRehearsalV52Error("provider config repository path is malformed")
    path = (repo_root / value).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ProviderRehearsalV52Error("provider config path escapes repository") from exc
    return path


def load_provider_rehearsal_v52(path: Path) -> LoadedProviderRehearsalV52:
    resolved = path.resolve()
    document = _object(resolved)
    version = document.get("version")
    is_sprint_pilot = version == "leanfaith_sft2a_provider_rehearsal_v5_2_sprint_pilot_v1"
    if is_sprint_pilot:
        if document.get("authorized") is not True:
            raise ProviderRehearsalV52Error("sprint pilot config must be authorized")
        if document.get("status") != "sprint_authorized":
            raise ProviderRehearsalV52Error("sprint pilot config status must be sprint_authorized")
    elif (
        version
        not in {
            "leanfaith_sft2a_provider_rehearsal_v5_2_corrected_v1",
            "leanfaith_sft2a_provider_rehearsal_v5_2_recovery_v1",
        }
        or document.get("authorized") is not False
        or document.get("status") != "ready_not_authorized"
    ):
        raise ProviderRehearsalV52Error("corrected provider config is not readiness-only")
    repo_root = Path(__file__).resolve().parents[3]
    base_path = _repo_path(repo_root, document.get("base_config_path"))
    if hash_file(base_path) != document.get("base_config_sha256"):
        raise ProviderRehearsalV52Error("base v5.2 config hash differs")
    policy = _repo_path(repo_root, document.get("labeling_defaults_policy_path"))
    if hash_file(policy) != document.get("labeling_defaults_policy_sha256"):
        raise ProviderRehearsalV52Error("active global defaults policy hash differs")
    base = load_sft2a_config(base_path, verify_binaries=False)
    if not isinstance(base.config, SFT2AV52Config):
        raise ProviderRehearsalV52Error("corrected runner base is not v5.2")
    if is_sprint_pilot:
        sample_path = Path(str(document.get("sample_path")))
        if not sample_path.is_file():
            raise ProviderRehearsalV52Error("sprint pilot sample path does not exist")
        declared_sample_sha = document.get("sample_sha256")
        if (
            not isinstance(declared_sample_sha, str)
            or hash_file(sample_path) != declared_sample_sha
        ):
            raise ProviderRehearsalV52Error("sprint pilot sample SHA-256 differs from its pin")
        rows = _sample_rows(sample_path)
        if len(rows) < 1:
            raise ProviderRehearsalV52Error("sprint pilot sample is empty")
        completed_paths = document.get("completed_root_sample_paths")
        if (
            not isinstance(completed_paths, list)
            or not completed_paths
            or any(
                not isinstance(item, str) or not Path(item).is_file() for item in completed_paths
            )
        ):
            raise ProviderRehearsalV52Error(
                "sprint pilot config must list existing completed-root sample paths"
            )
        ceilings = ExecutionCeilings.model_validate(document.get("ceilings"))
        if ceilings.maximum_roots != len(rows):
            raise ProviderRehearsalV52Error(
                "sprint pilot ceiling maximum_roots must match sample size"
            )
        role = document.get("sprint_role", "pilot")
        if role not in {"pilot", "shard", "canary"}:
            raise ProviderRehearsalV52Error("sprint_role must be pilot, shard, or canary")
        oracle_version = document.get("oracle_cache_version", "v2")
        if oracle_version not in {"v2", "v3"}:
            raise ProviderRehearsalV52Error("oracle_cache_version must be v2 or v3")
        if oracle_version == "v3":
            gate_path = document.get("oracle_v3_gate_receipt_path")
            if not isinstance(gate_path, str) or not gate_path:
                raise ProviderRehearsalV52Error(
                    "v3 sprint configs must name oracle_v3_gate_receipt_path"
                )
        projection_blocking = document.get("projection_blocking", True)
        if not isinstance(projection_blocking, bool):
            raise ProviderRehearsalV52Error("projection_blocking must be a boolean")
        workers = document.get("maximum_total_lean_workers")
        if role == "canary":
            # The v3 canary runs on the measured shard allocation (one cooperative worker at
            # 16 GiB) so its throughput and attribution telemetry are the shard path's.
            if (
                document.get("maximum_root_workers") != 1
                or workers != 1
                or document.get("maximum_measured_rss_gib") != 16.0
            ):
                raise ProviderRehearsalV52Error(
                    "sprint canary requires exactly one persistent Lean worker at 16 GiB"
                )
            next_shard = document.get("next_shard_config_path")
            if next_shard is not None and (
                not isinstance(next_shard, str) or not Path(next_shard).is_file()
            ):
                raise ProviderRehearsalV52Error("next_shard_config_path must name an existing file")
        elif role == "pilot":
            if (
                document.get("maximum_root_workers") != 2
                or workers != 2
                or document.get("maximum_measured_rss_gib") != 40.0
            ):
                raise ProviderRehearsalV52Error(
                    "sprint pilot requires exactly two persistent Lean workers and 40 GiB RSS"
                )
        elif (
            isinstance(workers, bool)
            or workers not in {1, 2}
            or document.get("maximum_root_workers") != workers
            or document.get("maximum_measured_rss_gib") != 16.0 * int(cast(int, workers))
        ):
            raise ProviderRehearsalV52Error(
                "sprint shard requires one or two persistent Lean workers at 16 GiB each"
            )
        registry_path = document.get("shared_candidate_registry_path")
        if registry_path is not None and (not isinstance(registry_path, str) or not registry_path):
            raise ProviderRehearsalV52Error("shared_candidate_registry_path must be a path")
        deadline = document.get("sprint_deadline_utc")
        if deadline is not None:
            try:
                datetime.fromisoformat(str(deadline))
            except ValueError as exc:
                raise ProviderRehearsalV52Error("sprint_deadline_utc is not ISO-8601") from exc
        concurrency = document.get("provider_concurrency")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not (1 <= concurrency <= 64)
        ):
            raise ProviderRehearsalV52Error("sprint pilot provider_concurrency must be 1..64")
        if role in {"pilot", "canary"}:
            kimi_rows = document.get("kimi_audit_rows")
            if (
                isinstance(kimi_rows, bool)
                or not isinstance(kimi_rows, int)
                or not 0 <= kimi_rows <= 8
            ):
                raise ProviderRehearsalV52Error("sprint pilot Kimi telemetry is at most 8 rows")
            kimi_maximum = kimi_rows
        else:
            fraction = document.get("kimi_audit_fraction")
            kimi_maximum_value = document.get("kimi_audit_rows_maximum")
            fallback = document.get("fallback_provider_concurrency")
            throughput = document.get("minimum_accepted_rows_per_minute")
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, int | float)
                or not 0.0 < float(fraction) <= 0.2
                or isinstance(kimi_maximum_value, bool)
                or not isinstance(kimi_maximum_value, int)
                or not 1 <= kimi_maximum_value <= 400
                or isinstance(fallback, bool)
                or not isinstance(fallback, int)
                or not 1 <= fallback <= concurrency
                or isinstance(throughput, bool)
                or not isinstance(throughput, int | float)
                or float(throughput) <= 0.0
            ):
                raise ProviderRehearsalV52Error(
                    "sprint shard requires kimi_audit_fraction (<=0.2), kimi_audit_rows_maximum "
                    "(<=400), fallback_provider_concurrency, and minimum_accepted_rows_per_minute"
                )
            kimi_maximum = kimi_maximum_value
            next_shard = document.get("next_shard_config_path")
            if next_shard is not None and (
                not isinstance(next_shard, str) or not Path(next_shard).is_file()
            ):
                raise ProviderRehearsalV52Error("next_shard_config_path must name an existing file")
        if 2 * kimi_maximum > ceilings.maximum_lemex_calls:
            raise ProviderRehearsalV52Error(
                "sprint Kimi ceiling must allow one malformed retry per telemetry row"
            )
        stop_after = document.get(
            "controlled_stop_after_completed_roots", 1 if role in {"pilot", "canary"} else 0
        )
        if isinstance(stop_after, bool) or not isinstance(stop_after, int) or stop_after < 0:
            raise ProviderRehearsalV52Error("controlled stop count must be a non-negative integer")
        mix = document.get("expected_source_mix")
        if mix is not None and (
            not isinstance(mix, dict)
            or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0
                for count in mix.values()
            )
            or sum(cast(dict[str, int], mix).values()) != len(rows)
        ):
            raise ProviderRehearsalV52Error("expected_source_mix must sum to the sample size")
        next_stage = document.get("next_stage_config_path")
        if next_stage is not None and (
            not isinstance(next_stage, str) or not _repo_path(repo_root, next_stage).is_file()
        ):
            raise ProviderRehearsalV52Error("next_stage_config_path must name a repository file")
        if any(
            document.get(flag) is not False
            for flag in (
                "legacy_rejudge_authorized",
                "publication_authorized",
                "scale_10k_authorized",
                "scale_50k_authorized",
                "training_authorized",
            )
        ):
            raise ProviderRehearsalV52Error("an out-of-scope sprint pilot action is authorized")
    else:
        corrected = Path(str(document.get("corrected_certification_root")))
        sample_path = corrected / "certified_sample.jsonl"
        checks = {
            sample_path: document.get("corrected_sample_sha256"),
            corrected / "corrected_sample_manifest.json": document.get(
                "corrected_sample_manifest_sha256"
            ),
            corrected / "corrected_replay_receipt.json": document.get(
                "corrected_reference_replay_sha256"
            ),
            corrected / "global_100_preflight_receipt.json": document.get(
                "corrected_global_preflight_sha256"
            ),
            corrected / "structured_goal_regressions.json": document.get(
                "structured_goal_regressions_sha256"
            ),
        }
        for artifact, expected in checks.items():
            if hash_file(artifact) != expected:
                raise ProviderRehearsalV52Error(
                    f"corrected provider input hash differs: {artifact}"
                )
        rows = _sample_rows(sample_path)
        if len(rows) != 100:
            raise ProviderRehearsalV52Error("corrected provider sample is not exactly 100 roots")
        ceilings = ExecutionCeilings.model_validate(document.get("ceilings"))
        if (
            ceilings.maximum_roots != 100
            or ceilings.maximum_provider_calls != 2480
            or ceilings.maximum_proposer_calls != 1200
            or ceilings.maximum_opus_calls != 1200
            or ceilings.maximum_lemex_calls != 80
            or ceilings.maximum_reported_opus_spend_usd != 160.0
        ):
            raise ProviderRehearsalV52Error(
                "corrected provider ceilings differ from the approved shape"
            )
        if (
            document.get("maximum_root_workers") != 2
            or document.get("maximum_total_lean_workers") != 2
            or document.get("maximum_measured_rss_gib") != 40.0
        ):
            raise ProviderRehearsalV52Error("corrected provider resource contract differs")
        if any(
            document.get(flag) is not False
            for flag in (
                "legacy_rejudge_authorized",
                "publication_authorized",
                "scale_10k_authorized",
                "scale_50k_authorized",
                "training_authorized",
            )
        ):
            raise ProviderRehearsalV52Error(
                "an out-of-scope corrected provider action is authorized"
            )
    recovery_source: dict[str, object] | None = None
    if version == "leanfaith_sft2a_provider_rehearsal_v5_2_recovery_v1":
        raw_recovery = document.get("recovery_source")
        if not isinstance(raw_recovery, dict):
            raise ProviderRehearsalV52Error("recovery provider config lacks source identity")
        recovery_source = dict(raw_recovery)
        source_root = Path(str(recovery_source.get("output_root")))
        output_root = Path(str(document["provider_output_root"]))
        if source_root == output_root or not source_root.is_dir():
            raise ProviderRehearsalV52Error("recovery source/output roots are not disjoint")
        source_checks = {
            source_root / "provider_budget.jsonl": recovery_source.get("provider_budget_sha256"),
            source_root / "root_state.jsonl": recovery_source.get("root_state_sha256"),
            source_root / "authorization/authorization_receipt.json": recovery_source.get(
                "authorization_receipt_sha256"
            ),
            source_root / "detached/launch_receipt.json": recovery_source.get(
                "launch_receipt_sha256"
            ),
        }
        for artifact, expected in source_checks.items():
            if hash_file(artifact) != expected:
                raise ProviderRehearsalV52Error(
                    f"recovery source artifact hash differs: {artifact}"
                )
        completed, manifest_seal = _completed_manifest_seal(source_root)
        if completed != recovery_source.get(
            "completed_roots"
        ) or manifest_seal != recovery_source.get("completed_manifest_seal_sha256"):
            raise ProviderRehearsalV52Error("recovery completed-root seal differs")
        identity = {
            "algorithm": "sha256_canonical_recovery_source_identity_v1",
            "provider_budget_sha256": recovery_source["provider_budget_sha256"],
            "root_state_sha256": recovery_source["root_state_sha256"],
            "authorization_receipt_sha256": recovery_source["authorization_receipt_sha256"],
            "launch_receipt_sha256": recovery_source["launch_receipt_sha256"],
            "completed_manifest_seal_sha256": manifest_seal,
            "completed_roots": completed,
        }
        if hash_canonical(identity) != recovery_source.get("source_run_identity_hash"):
            raise ProviderRehearsalV52Error("recovery source identity hash differs")
        source_budget = AtomicProviderBudget(
            source_root / "provider_budget.jsonl", ceilings
        ).snapshot()
        expected_budget = recovery_source.get("provider_budget_snapshot")
        if source_budget != expected_budget or source_budget["outstanding_calls"] != 0:
            raise ProviderRehearsalV52Error("recovery source budget snapshot differs")
    kind: Literal["corrected", "recovery", "sprint"] = (
        "sprint" if is_sprint_pilot else "recovery" if recovery_source is not None else "corrected"
    )
    return LoadedProviderRehearsalV52(
        path=resolved,
        document=document,
        sha256=hash_file(resolved),
        base=base,
        sample_path=sample_path,
        output_root=Path(str(document["provider_output_root"])),
        ceilings=ceilings,
        recovery_source=recovery_source,
        kind=kind,
    )


def preflight_sample_v52(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Zero-Lean sample preflight: the sprint verifier or the historical 100-root certificate."""

    if loaded.kind == "sprint":
        from leanfaith.sft2a.sprint_pilot_v52 import verify_sprint_pilot_sample_v52

        return verify_sprint_pilot_sample_v52(loaded)
    return verify_corrected_global_preflight(loaded.sample_path.parent)


def prepare_provider_recovery_seed_v52(
    loaded: LoadedProviderRehearsalV52,
) -> dict[str, object] | None:
    """Copy a failed run's terminal ledger exactly into an additive recovery root."""

    if loaded.recovery_source is None:
        return None
    source_root = Path(str(loaded.recovery_source["output_root"]))
    source_ledger = source_root / "provider_budget.jsonl"
    target_ledger = loaded.output_root / "provider_budget.jsonl"
    _atomic_exact(target_ledger, source_ledger.read_bytes())
    snapshot = AtomicProviderBudget(target_ledger, loaded.ceilings).snapshot()
    if snapshot != loaded.recovery_source["provider_budget_snapshot"]:
        raise ProviderRehearsalV52Error("recovery cumulative budget seed differs")
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_provider_recovery_seed_v1",
        "source_run_identity_hash": loaded.recovery_source["source_run_identity_hash"],
        "source_output_root": str(source_root),
        "source_provider_budget_sha256": hash_file(source_ledger),
        "cumulative_budget_seed_sha256": hash_file(target_ledger),
        "cumulative_budget_snapshot": snapshot,
        "completed_source_roots": loaded.recovery_source["completed_roots"],
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
    }
    _atomic_exact(
        loaded.output_root / "recovery/recovery_seed_receipt.json",
        canonical_json_bytes(receipt) + b"\n",
    )
    return receipt


def _git_identity(repo_root: Path) -> tuple[str, str]:
    status = subprocess.run(
        ("git", "status", "--porcelain"), cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout
    if status:
        raise ProviderRehearsalV52Error("readiness requires a clean committed worktree")
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout.strip()
    tree = subprocess.run(
        ("git", "rev-parse", "HEAD^{tree}"),
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return commit, tree


def provider_readiness_path_v52(loaded: LoadedProviderRehearsalV52) -> Path:
    """Return the clean-implementation-scoped additive readiness path."""

    commit, _tree = _git_identity(loaded.base.repo_root)
    return loaded.output_root / "readiness" / f"provider_readiness_{commit[:12]}.json"


def prepare_provider_readiness_v52(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Create the clean-commit readiness receipt; this never authorizes execution."""

    preflight = verify_corrected_global_preflight(loaded.sample_path.parent)
    commit, tree = _git_identity(loaded.base.repo_root)
    recovery_seed = prepare_provider_recovery_seed_v52(loaded)
    closure_receipt = _repo_path(
        loaded.base.repo_root, loaded.document.get("closure_canary_receipt_path")
    )
    closure = _object(closure_receipt)
    closure_summary = closure.get("closure_canaries")
    if (
        closure.get("status") != "passed"
        or not isinstance(closure_summary, dict)
        or closure_summary.get("passed") != closure_summary.get("total")
    ):
        raise ProviderRehearsalV52Error("closure canary receipt is not passing")
    receipt: dict[str, object] = {
        "version": (
            "leanfaith_sft2a_provider_readiness_v5_2_recovery_v1"
            if recovery_seed is not None
            else "leanfaith_sft2a_provider_readiness_v5_2_corrected_v1"
        ),
        "status": "ready_not_authorized",
        "authorized": False,
        "implementation_commit": commit,
        "implementation_tree": tree,
        "provider_config_sha256": loaded.sha256,
        "sample_sha256": preflight["sample_sha256"],
        "corrected_sample_manifest_sha256": preflight["corrected_sample_manifest_sha256"],
        "reference_preflight_sha256": hash_file(
            loaded.sample_path.parent / "global_100_preflight_receipt.json"
        ),
        "reference_replay_sha256": hash_file(
            loaded.sample_path.parent / "corrected_replay_receipt.json"
        ),
        "structured_regressions_sha256": preflight["structured_regressions_sha256"],
        "closure_canary_receipt_sha256": hash_file(closure_receipt),
        "labeling_defaults_policy_sha256": loaded.document["labeling_defaults_policy_sha256"],
        "ceilings": loaded.ceilings.model_dump(mode="json"),
        "maximum_root_workers": 2,
        "maximum_total_lean_workers": 2,
        "maximum_measured_rss_gib": 40.0,
        "output_root": str(loaded.output_root),
        "tmux_session": loaded.document["tmux_session"],
        "resource_task": loaded.document["resource_task"],
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "training_authorized": False,
    }
    if recovery_seed is not None:
        receipt["recovery_source_run_identity_hash"] = recovery_seed["source_run_identity_hash"]
        receipt["cumulative_budget_seed_sha256"] = recovery_seed["cumulative_budget_seed_sha256"]
        receipt["cumulative_budget_snapshot"] = recovery_seed["cumulative_budget_snapshot"]
        receipt["recovery_seed_receipt_sha256"] = hash_file(
            loaded.output_root / "recovery/recovery_seed_receipt.json"
        )
    path = provider_readiness_path_v52(loaded)
    _atomic_exact(path, canonical_json_bytes(receipt) + b"\n")
    return receipt


def authorization_sentence_v52(loaded: LoadedProviderRehearsalV52, readiness_sha256: str) -> str:
    readiness = _object(provider_readiness_path_v52(loaded))
    recovery_clause = ""
    action = "launch only"
    descriptor = "provider rehearsal"
    if loaded.recovery_source is not None:
        action = "resume only"
        descriptor = "provider recovery rehearsal"
        recovery_clause = (
            f", cumulative failed-run budget seed {readiness['cumulative_budget_seed_sha256']} "
            f"from source run {readiness['recovery_source_run_identity_hash']}"
        )
    return (
        f"I authorize SFT2A to {action} the corrected 100-root/400-slot closure-aware v5.2 "
        f"{descriptor} bound to sample {loaded.document['corrected_sample_sha256']}, config "
        f"{loaded.sha256}, readiness {readiness_sha256}, implementation "
        f"commit {readiness['implementation_commit']} and tree {readiness['implementation_tree']}"
        f"{recovery_clause} "
        "under ceilings 2,480 total provider calls, 1,200 Terra calls, 1,200 Opus calls, 80 Kimi "
        "calls, three candidate attempts per slot, and $160 reported Opus spend with at most two "
        "Lean/root workers and 40 GiB measured RSS; the approximately-10K gate, 50K run, legacy "
        "rejudge, publication, training, and all other runs remain unauthorized."
    )


def materialize_provider_authorization_v52(
    loaded: LoadedProviderRehearsalV52, *, authorization_sentence: str
) -> dict[str, object]:
    """Materialize only an exact future user authorization; never launch from this function."""

    readiness_path = provider_readiness_path_v52(loaded)
    readiness = _object(readiness_path)
    readiness_sha = hash_file(readiness_path)
    expected = authorization_sentence_v52(loaded, readiness_sha)
    if authorization_sentence != expected:
        raise ProviderRehearsalV52Error(
            "provider authorization sentence differs from exact request"
        )
    commit, tree = _git_identity(loaded.base.repo_root)
    if (
        readiness.get("implementation_commit") != commit
        or readiness.get("implementation_tree") != tree
    ):
        raise ProviderRehearsalV52Error("provider authorization implementation differs")
    receipt: dict[str, object] = {
        "version": (
            "leanfaith_sft2a_provider_authorization_v5_2_recovery_v1"
            if loaded.recovery_source is not None
            else "leanfaith_sft2a_provider_authorization_v5_2_corrected_v1"
        ),
        "authorized": True,
        "status": "authorized_rehearsal_only",
        "authorization_sentence": authorization_sentence,
        "authorization_sentence_sha256": sha256_hex(authorization_sentence.encode()),
        "readiness_sha256": readiness_sha,
        "implementation_commit": commit,
        "implementation_tree": tree,
        "provider_config_sha256": loaded.sha256,
        "sample_sha256": loaded.document["corrected_sample_sha256"],
        "reference_preflight_sha256": readiness["reference_preflight_sha256"],
        "closure_canary_receipt_sha256": readiness["closure_canary_receipt_sha256"],
        "ceilings": loaded.ceilings.model_dump(mode="json"),
        "maximum_root_workers": 2,
        "maximum_total_lean_workers": 2,
        "maximum_measured_rss_gib": 40.0,
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "training_authorized": False,
        "launch_started": False,
    }
    if loaded.recovery_source is not None:
        receipt["recovery_source_run_identity_hash"] = readiness[
            "recovery_source_run_identity_hash"
        ]
        receipt["cumulative_budget_seed_sha256"] = readiness["cumulative_budget_seed_sha256"]
        receipt["recovery_seed_receipt_sha256"] = readiness["recovery_seed_receipt_sha256"]
    _atomic_exact(
        loaded.output_root / "authorization/authorization_receipt.json",
        canonical_json_bytes(receipt) + b"\n",
    )
    return receipt


def load_provider_authorization_v52(
    loaded: LoadedProviderRehearsalV52, path: Path
) -> LoadedProviderAuthorizationV52:
    document = _object(path)
    readiness_path = provider_readiness_path_v52(loaded)
    readiness = _object(readiness_path)
    if (
        document.get("authorized") is not True
        or document.get("status") != "authorized_rehearsal_only"
        or document.get("provider_config_sha256") != loaded.sha256
        or document.get("sample_sha256") != loaded.document["corrected_sample_sha256"]
        or document.get("readiness_sha256") != hash_file(readiness_path)
        or document.get("authorization_sentence")
        != authorization_sentence_v52(loaded, hash_file(readiness_path))
    ):
        raise ProviderRehearsalV52Error("provider authorization receipt differs")
    if loaded.recovery_source is not None and (
        document.get("recovery_source_run_identity_hash")
        != readiness.get("recovery_source_run_identity_hash")
        or document.get("cumulative_budget_seed_sha256")
        != readiness.get("cumulative_budget_seed_sha256")
        or document.get("recovery_seed_receipt_sha256")
        != readiness.get("recovery_seed_receipt_sha256")
    ):
        raise ProviderRehearsalV52Error("provider recovery authorization differs")
    commit, tree = _git_identity(loaded.base.repo_root)
    if (
        document.get("implementation_commit") != commit
        or document.get("implementation_tree") != tree
    ):
        raise ProviderRehearsalV52Error("provider authorization commit/tree differs")
    return LoadedProviderAuthorizationV52(path.resolve(), document, hash_file(path))


def preflight_provider_launch_v52(
    loaded: LoadedProviderRehearsalV52,
    authorization: LoadedProviderAuthorizationV52 | None,
) -> dict[str, object]:
    """Reach the detached boundary without constructing a provider or Lean backend."""

    preflight = verify_corrected_global_preflight(loaded.sample_path.parent)
    recovery_seed: dict[str, object] | None = None
    if loaded.recovery_source is not None:
        seed_path = loaded.output_root / "recovery/recovery_seed_receipt.json"
        recovery_seed = _object(seed_path)
        target_ledger = loaded.output_root / "provider_budget.jsonl"
        source_ledger = Path(str(loaded.recovery_source["output_root"])) / ("provider_budget.jsonl")
        if (
            not target_ledger.is_file()
            or not target_ledger.read_bytes().startswith(source_ledger.read_bytes())
            or recovery_seed.get("cumulative_budget_seed_sha256") != hash_file(source_ledger)
            or recovery_seed.get("source_run_identity_hash")
            != loaded.recovery_source["source_run_identity_hash"]
        ):
            raise ProviderRehearsalV52Error("recovery cumulative budget seed is absent or differs")
    session = str(loaded.document["tmux_session"])
    exists = (
        subprocess.run(
            ("tmux", "has-session", "-t", session), check=False, capture_output=True
        ).returncode
        == 0
    )
    if exists:
        raise ProviderRehearsalV52Error("corrected provider tmux session already exists")
    task = str(loaded.document["resource_task"])
    if any(reservation.task == task for reservation in list_reservations()):
        raise ProviderRehearsalV52Error("corrected provider resource task is already claimed")
    run_lock = loaded.output_root / "detached/run.lock"
    try:
        with parallel_launch_lock(run_lock):
            pass
    except ParallelRehearsalError as exc:
        raise ProviderRehearsalV52Error("corrected provider run lock is held") from exc
    terminal = loaded.output_root / "detached/terminal_status.json"
    if terminal.is_file() and _object(terminal).get("status") == "complete":
        raise ProviderRehearsalV52Error("completed corrected provider rehearsal cannot restart")
    if authorization is not None and authorization.document.get("authorized") is not True:
        raise ProviderRehearsalV52Error("corrected provider launch is not authorized")
    result: dict[str, object] = {
        "version": "leanfaith_sft2a_provider_launch_preflight_v5_2_corrected_v1",
        "boundary": "tmux_start_not_executed",
        "authorization_present": authorization is not None,
        "provider_config_sha256": loaded.sha256,
        "sample_sha256": preflight["sample_sha256"],
        "reference_preflight_sha256": hash_file(
            loaded.sample_path.parent / "global_100_preflight_receipt.json"
        ),
        "output_root": str(loaded.output_root),
        "tmux_session": session,
        "resource_task": loaded.document["resource_task"],
        "resource_claim_absent": True,
        "run_lock_free": True,
        "maximum_root_workers": 2,
        "maximum_total_lean_workers": 2,
        "maximum_measured_rss_gib": 40.0,
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "tmux_sessions_started": 0,
    }
    if recovery_seed is not None:
        result["recovery_source_run_identity_hash"] = recovery_seed["source_run_identity_hash"]
        result["cumulative_budget_seed_sha256"] = recovery_seed["cumulative_budget_seed_sha256"]
        result["cumulative_budget_snapshot"] = recovery_seed["cumulative_budget_snapshot"]
    return result


def certified_reference_result_v52(row: dict[str, object]) -> SignatureOracleResult:
    verified = verify_certified_reference_row(row)
    root = OneRootConfig.model_validate(row["root"])
    certified = cast(dict[str, object], row["certified_reference"])
    cache = _object(Path(str(certified["certification_cache_path"])))
    sidecar = cache.get("sidecar")
    if not isinstance(sidecar, dict):
        raise ProviderRehearsalV52Error("certified reference cache lacks REPR sidecar")
    if verified["root_id"] != root.root_id:
        raise ProviderRehearsalV52Error("certified reference root differs")
    return SignatureOracleResult(
        status="valid",
        cache_key=str(certified["certification_cache_key"]),
        cache_hit=True,
        signature_sha256=sha256_hex(root.reference_signature.encode()),
        goal_v1=str(certified["goal_v1"]),
        sidecar=sidecar,
        lean_status="valid",
        request_hash=(str(cache["request_hash"]) if cache.get("request_hash") else None),
        elapsed_ms=0,
        raw_response_path=str(cache["raw_response_path"]),
        detail="authoritative certified reference cache hit; no reference elaboration",
    )


def _root_loaded(loaded: LoadedProviderRehearsalV52, row: dict[str, object]) -> LoadedSFT2AConfig:
    root = OneRootConfig.model_validate(row["root"])
    config = loaded.base.config.model_copy(update={"root": root})
    return replace(loaded.base, config=config, config_hash=loaded.sha256)


def _mechanism_plan(row: dict[str, object]) -> dict[str, MechanismAssignment]:
    raw = row.get("mechanism_plan")
    if not isinstance(raw, dict):
        raise ProviderRehearsalV52Error("certified row lacks structured mechanism plan")
    result: dict[str, MechanismAssignment] = {}
    for slot, value in raw.items():
        if not isinstance(value, dict):
            raise ProviderRehearsalV52Error("certified mechanism assignment is malformed")
        family = value.get("family")
        polarity = value.get("polarity")
        instruction = value.get("instruction")
        applicability = value.get("applicability")
        shape_id = value.get("shape_id")
        if not all(
            isinstance(item, str) and item
            for item in (family, instruction, applicability, shape_id)
        ) or polarity not in {"preserving", "breaking"}:
            raise ProviderRehearsalV52Error("certified mechanism fields are malformed")
        assert isinstance(family, str)
        assert polarity in {"preserving", "breaking"}
        assert isinstance(instruction, str)
        assert isinstance(applicability, str)
        assert isinstance(shape_id, str)
        result[str(slot)] = MechanismAssignment(
            family=family,
            polarity=polarity,
            instruction=instruction,
            applicability=applicability,
            shape_id=shape_id,
        )
    return result


def _root_output(loaded: LoadedProviderRehearsalV52, root_id: str) -> Path:
    return loaded.output_root / "roots" / hash_canonical(root_id)


class OraclePool:
    """Exactly ``workers`` persistent project-affine SignatureOracle slots reused with rebind.

    Slot selection prefers a free slot already bound to the requested project, then an empty
    slot, and replaces a differently bound free slot only when no slot holds the project. When a
    matching slot exists but is busy, the caller waits for it instead of closing and recreating
    another backend. Active backends therefore never exceed the claimed worker count.
    """

    def __init__(
        self,
        *,
        cache_version: CacheVersion = "v2",
        workers: int = 2,
        oracle_factory: Callable[[LoadedSFT2AConfig], SignatureOracle] | None = None,
    ) -> None:
        if workers < 1:
            raise ProviderRehearsalV52Error("oracle pool requires at least one worker slot")
        self._cache_version = cache_version
        self._workers = workers
        self._factory: Callable[[LoadedSFT2AConfig], SignatureOracle] = oracle_factory or (
            lambda root_loaded: SignatureOracle(root_loaded, cache_version=cache_version)
        )
        self._oracles: list[SignatureOracle | None] = [None] * workers
        self._projects: list[str | None] = [None] * workers
        self._busy: list[bool] = [False] * workers
        self._last_used: list[int] = [0] * workers
        self._tick = 0
        self._condition = threading.Condition()
        self._closed = False
        self.stats: dict[str, int] = {
            "created_backends": 0,
            "closed_backends": 0,
            "reuses": 0,
            "waits": 0,
            "max_active_backends": 0,
            "max_concurrent_busy": 0,
        }

    @property
    def workers(self) -> int:
        return self._workers

    def _select_slot_locked(self, project_id: str) -> int | None:
        for index in range(self._workers):
            if not self._busy[index] and self._projects[index] == project_id:
                return index
        for index in range(self._workers):
            if not self._busy[index] and self._oracles[index] is None:
                return index
        if project_id not in self._projects:
            free = [index for index in range(self._workers) if not self._busy[index]]
            if free:
                return min(free, key=lambda index: self._last_used[index])
        return None

    @contextmanager
    def acquire(self, root_loaded: LoadedSFT2AConfig) -> Iterator[SignatureOracle]:
        project_id = root_loaded.config.root.compile_context.project_id
        with self._condition:
            if self._closed:
                raise ProviderRehearsalV52Error("oracle pool is closed")
            slot = self._select_slot_locked(project_id)
            while slot is None:
                self.stats["waits"] += 1
                self._condition.wait()
                if self._closed:
                    raise ProviderRehearsalV52Error("oracle pool is closed")
                slot = self._select_slot_locked(project_id)
            self._busy[slot] = True
            self._tick += 1
            self._last_used[slot] = self._tick
            self.stats["max_concurrent_busy"] = max(
                self.stats["max_concurrent_busy"], sum(self._busy)
            )
        try:
            oracle = self._oracles[slot]
            if oracle is None or self._projects[slot] != project_id:
                if oracle is not None:
                    oracle.close()
                    with self._condition:
                        self._oracles[slot] = None
                        self._projects[slot] = None
                        self.stats["closed_backends"] += 1
                oracle = self._factory(root_loaded)
                with self._condition:
                    self._oracles[slot] = oracle
                    self._projects[slot] = project_id
                    self.stats["created_backends"] += 1
                    self.stats["max_active_backends"] = max(
                        self.stats["max_active_backends"], self.active_backend_count()
                    )
            else:
                oracle.rebind(root_loaded)
                with self._condition:
                    self.stats["reuses"] += 1
            yield oracle
        finally:
            with self._condition:
                self._busy[slot] = False
                self._condition.notify_all()

    def active_backend_count(self) -> int:
        return sum(1 for oracle in self._oracles if oracle is not None)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            for index in range(self._workers):
                oracle = self._oracles[index]
                if oracle is not None:
                    oracle.close()
                    self.stats["closed_backends"] += 1
                    self._oracles[index] = None
                    self._projects[index] = None
            self._condition.notify_all()


def _template_version(cache_version: CacheVersion) -> str | None:
    from leanfaith.sft2a.lean_oracle import command_template_version

    return command_template_version(cache_version)


class PooledOracle:
    """PropositionOracle adapter that delegates to an OraclePool with per-call locking."""

    def __init__(self, pool: OraclePool, root_loaded: LoadedSFT2AConfig) -> None:
        self._pool = pool
        self._root_loaded = root_loaded
        self.method_version = oracle_method_version(pool._cache_version)
        self.cache_version = pool._cache_version
        self.elaborator_sha256 = elaborator_sha256(pool._cache_version)
        self.command_template_version = (
            None if pool._cache_version == "v1" else _template_version(pool._cache_version)
        )

    def elaborate(
        self, signature: str, *, endpoint_role: Literal["reference", "candidate", "authoring"]
    ) -> SignatureOracleResult:
        with self._pool.acquire(self._root_loaded) as oracle:
            return oracle.elaborate(signature, endpoint_role=endpoint_role)

    def effective_context(self) -> EffectiveContextV3:
        with self._pool.acquire(self._root_loaded) as oracle:
            return oracle.effective_context()

    def authoring_view(
        self,
        declaration_name: str,
        *,
        expected_closed_expr_hash: str,
        expected_level_params: Sequence[str],
    ) -> AuthoringViewResult:
        with self._pool.acquire(self._root_loaded) as oracle:
            return oracle.authoring_view(
                declaration_name,
                expected_closed_expr_hash=expected_closed_expr_hash,
                expected_level_params=expected_level_params,
            )

    def close(self) -> None:
        pass


def run_provider_worker_v52(
    loaded: LoadedProviderRehearsalV52,
    authorization: LoadedProviderAuthorizationV52,
    *,
    worker_index: int,
) -> dict[str, object]:
    """Run one of the two deterministic project-grouped workers."""

    if worker_index not in {0, 1}:
        raise ProviderRehearsalV52Error("provider worker index must be zero or one")
    preflight_sample_v52(loaded)
    rows = _sample_rows(loaded.sample_path)
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        root = OneRootConfig.model_validate(row["root"])
        groups[root.compile_context.project_id].append(row)
    assigned: list[dict[str, object]] = []
    for project_id in sorted(groups):
        ordered = sorted(groups[project_id], key=lambda row: str(row["root"]["root_id"]))  # type: ignore[index]
        assigned.extend(row for index, row in enumerate(ordered) if index % 2 == worker_index)
    worker_id = f"worker-{worker_index}"
    ledger = AtomicProviderBudget(loaded.output_root / "provider_budget.jsonl", loaded.ceilings)
    proposer = AtomicBudgetedProvider(
        proposer_provider(loaded.base), ledger=ledger, kind="proposer", worker_id=worker_id
    )
    opus = AtomicBudgetedProvider(
        claude_judge_provider(loaded.base),
        ledger=ledger,
        kind="opus",
        worker_id=worker_id,
        maximum_charge_usd=(
            loaded.ceilings.maximum_reported_opus_spend_usd / loaded.ceilings.maximum_opus_calls
        ),
    )
    # Constructed here so the post-run audit necessarily shares the same ledger; it is not called
    # until the separate deterministic audit phase selects rows.
    _kimi = AtomicBudgetedProvider(
        lemex_audit_provider(loaded.base), ledger=ledger, kind="lemex", worker_id=worker_id
    )
    registry = PersistentCandidateRegistry(loaded.output_root / "candidate_registry.jsonl")
    states = ParallelRootStateMachine(loaded.output_root / "root_state.jsonl")
    completed = 0
    for row in assigned:
        root = OneRootConfig.model_validate(row["root"])
        snapshot = states.snapshot()["roots"]
        assert isinstance(snapshot, dict)
        current = snapshot.get(root.root_id)
        if isinstance(current, dict) and current.get("status") == "crashed":
            states.reclaim(
                root_id=root.root_id,
                prior_worker_id=str(current["owner"]),
                worker_id=worker_id,
            )
        outcome = states.claim(root_id=root.root_id, worker_id=worker_id)
        if outcome == "replay_complete":
            continue
        root_loaded = _root_loaded(loaded, row)
        reference = certified_reference_result_v52(row)
        oracle = SignatureOracle(root_loaded)
        try:
            result = run_one_root(
                root_loaded,
                proposer=proposer,
                claude_judge=opus,
                oracle=oracle,
                output_root=_root_output(loaded, root.root_id),
                enforce_expected_reference_goal=True,
                enforce_smoke_ceilings=False,
                cross_root_registry=registry,
                mechanism_plan=_mechanism_plan(row),
                certified_reference=reference,
            )
            manifest_path = result.output_root / "manifest.json"
            states.complete(
                root_id=root.root_id,
                worker_id=worker_id,
                manifest_hash=hash_file(manifest_path),
            )
            completed += not result.replayed
        except Exception as exc:
            states.crash(root_id=root.root_id, worker_id=worker_id, reason=type(exc).__name__)
            raise
        finally:
            oracle.close()
    return {
        "worker_id": worker_id,
        "assigned_roots": len(assigned),
        "completed_in_invocation": completed,
        "provider_budget": ledger.snapshot(),
        "candidate_registry": registry.snapshot(),
        "authorization_receipt_sha256": authorization.sha256,
    }


def run_two_provider_workers_v52(
    loaded: LoadedProviderRehearsalV52,
    authorization: LoadedProviderAuthorizationV52,
    *,
    provider_concurrency: int = 8,
    lean_workers: int | None = None,
    stop_after_completed_roots: int | None = None,
    stop_request_path: Path | None = None,
    enforce_closure_canaries: bool = True,
    registry_path: Path | None = None,
    oracle_cache_version: CacheVersion | None = None,
) -> list[dict[str, object]]:
    """Dynamic as_completed root queue over exactly ``lean_workers`` persistent oracles.

    Provider concurrency is decoupled from the Lean cap: ``provider_concurrency`` threads pull
    roots from one queue, while ``OraclePool`` keeps exactly the claimed number of locked,
    persistent, project-affine SignatureOracle slots reused with rebind. A controlled stop
    (``stop_after_completed_roots`` or a stop-request file) lets in-flight roots finish, starts
    no new root, and returns a durable ``stopped`` summary; a crashed root records its crash,
    stops new work, and re-raises after in-flight roots finish.
    """

    import queue as queue_module

    preflight_sample_v52(loaded)
    rows = _sample_rows(loaded.sample_path)
    claimed_workers = (
        lean_workers
        if lean_workers is not None
        else int(cast(int, loaded.document.get("maximum_total_lean_workers", 2)))
    )
    worker_tag = "dynamic-queue"
    ledger = AtomicProviderBudget(loaded.output_root / "provider_budget.jsonl", loaded.ceilings)
    proposer = AtomicBudgetedProvider(
        proposer_provider(loaded.base),
        ledger=ledger,
        kind="proposer",
        worker_id=worker_tag,
        reclaim_from_worker=worker_tag,
    )
    opus = AtomicBudgetedProvider(
        claude_judge_provider(loaded.base),
        ledger=ledger,
        kind="opus",
        worker_id=worker_tag,
        maximum_charge_usd=(
            loaded.ceilings.maximum_reported_opus_spend_usd / loaded.ceilings.maximum_opus_calls
        ),
        reclaim_from_worker=worker_tag,
    )
    configured_registry = loaded.document.get("shared_candidate_registry_path")
    registry = PersistentCandidateRegistry(
        registry_path
        if registry_path is not None
        else Path(str(configured_registry))
        if isinstance(configured_registry, str) and configured_registry
        else loaded.output_root / "candidate_registry.jsonl"
    )
    states = ParallelRootStateMachine(
        loaded.output_root / "root_state.jsonl", maximum_workers=provider_concurrency
    )
    configured_version = str(loaded.document.get("oracle_cache_version", "v2"))
    if configured_version not in {"v2", "v3"}:
        raise ProviderRehearsalV52Error("oracle_cache_version must be v2 or v3")
    pool_version: CacheVersion = (
        oracle_cache_version
        if oracle_cache_version is not None
        else cast(CacheVersion, configured_version)
    )
    oracle_pool = OraclePool(cache_version=pool_version, workers=claimed_workers)
    completed_count = 0
    completed_lock = threading.Lock()
    stop_event = threading.Event()
    stop_reasons: list[str] = []
    errors: list[BaseException] = []

    pending_queue: queue_module.Queue[dict[str, object]] = queue_module.Queue()
    initial_snapshot = states.snapshot()["roots"]
    assert isinstance(initial_snapshot, dict)
    for row in rows:
        root = OneRootConfig.model_validate(row["root"])
        current = initial_snapshot.get(root.root_id)
        if isinstance(current, dict) and current.get("status") == "complete":
            continue
        pending_queue.put(row)
    pending_count = pending_queue.qsize()

    def request_stop(reason: str) -> None:
        with completed_lock:
            if reason not in stop_reasons:
                stop_reasons.append(reason)
        stop_event.set()

    def process_roots() -> None:
        nonlocal completed_count
        while not stop_event.is_set():
            if stop_request_path is not None and stop_request_path.exists():
                request_stop("stop_request_file")
                break
            try:
                row = pending_queue.get_nowait()
            except queue_module.Empty:
                break
            root = OneRootConfig.model_validate(row["root"])
            root_worker = f"dyn-{root.root_id}"
            try:
                snapshot = states.snapshot()["roots"]
                assert isinstance(snapshot, dict)
                current = snapshot.get(root.root_id)
                if isinstance(current, dict) and current.get("status") == "crashed":
                    states.reclaim(
                        root_id=root.root_id,
                        prior_worker_id=str(current["owner"]),
                        worker_id=root_worker,
                    )
                outcome = states.claim(root_id=root.root_id, worker_id=root_worker)
                if outcome == "replay_complete":
                    continue
                try:
                    root_loaded = _root_loaded(loaded, row)
                    reference = certified_reference_result_v52(row)
                    oracle = PooledOracle(oracle_pool, root_loaded)
                    result = run_one_root(
                        root_loaded,
                        proposer=proposer,
                        claude_judge=opus,
                        oracle=oracle,
                        output_root=_root_output(loaded, root.root_id),
                        enforce_expected_reference_goal=True,
                        enforce_smoke_ceilings=False,
                        cross_root_registry=registry,
                        mechanism_plan=_mechanism_plan(row),
                        certified_reference=reference,
                        enforce_closure_canaries=enforce_closure_canaries,
                    )
                except Exception as exc:
                    # Every failure after the claim is a durable crash so resume can reclaim it.
                    states.crash(
                        root_id=root.root_id, worker_id=root_worker, reason=type(exc).__name__
                    )
                    raise
                manifest_path = result.output_root / "manifest.json"
                states.complete(
                    root_id=root.root_id,
                    worker_id=root_worker,
                    manifest_hash=hash_file(manifest_path),
                )
                with completed_lock:
                    if not result.replayed:
                        completed_count += 1
                    reached = (
                        stop_after_completed_roots is not None
                        and completed_count >= stop_after_completed_roots
                    )
                if reached:
                    request_stop("controlled_stop_after_completed_roots")
            except Exception as exc:
                with completed_lock:
                    errors.append(exc)
                request_stop(f"crash:{type(exc).__name__}")
                return

    if pending_count > 0 and not (
        stop_after_completed_roots is not None and stop_after_completed_roots == 0
    ):
        worker_count = min(provider_concurrency, pending_count)
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="sft2a-dyn") as pool:
            futures = [pool.submit(process_roots) for _ in range(worker_count)]
            for future in as_completed(futures):
                future.result()
    oracle_pool.close()
    if errors:
        raise errors[0]
    final_snapshot = states.snapshot()["roots"]
    assert isinstance(final_snapshot, dict)
    roots_complete = sum(
        isinstance(state, dict) and state.get("status") == "complete"
        for state in final_snapshot.values()
    )
    return [
        {
            "worker_id": worker_tag,
            "assigned_roots": pending_count,
            "replayed_at_start": len(rows) - pending_count,
            "completed_in_invocation": completed_count,
            "roots_complete": roots_complete,
            "roots_total": len(rows),
            "stopped": stop_event.is_set(),
            "stop_reasons": list(stop_reasons),
            "provider_concurrency": provider_concurrency,
            "lean_workers": claimed_workers,
            "oracle_cache_version": pool_version,
            "oracle_pool": dict(oracle_pool.stats),
            "provider_budget": ledger.snapshot(),
            "candidate_registry": registry.snapshot(),
            "candidate_registry_path": str(registry.path),
            "authorization_receipt_sha256": authorization.sha256,
        }
    ]


def sprint_audit_selection(
    sidecars: Sequence[Mapping[str, object]],
    count: int,
    *,
    salt: str = "sft2a-sprint-kimi-source-polarity-cells-v1",
) -> list[int]:
    """Deterministic source-by-polarity cell sampler for sprint Kimi telemetry.

    Rows are grouped into the eight cells Mathlib/Physlib/CSLib/compiler-data by preserving/
    breaking. The sampler walks the cells round-robin in that fixed order, so an eight-row audit
    takes exactly one row from every non-empty cell; larger audits keep the per-cell balance and
    diversify by planned mechanism family inside each cell (families and rows are ordered by a
    salted hash of the row ID). Returns positions into ``sidecars``.
    """

    if count < 0:
        raise ProviderRehearsalV52Error("Kimi audit count must be non-negative")
    families_by_cell: dict[tuple[str, str], dict[str, list[tuple[str, int]]]] = {}
    for index, row in enumerate(sidecars):
        source = str(row["root_id"]).split(":", maxsplit=1)[0]
        polarity = str(row["requested_polarity"])
        planned = row.get("planned_mechanism")
        family = str(planned.get("family")) if isinstance(planned, dict) else "missing"
        rank = hash_canonical({"salt": salt, "row_id": row["row_id"]})
        families_by_cell.setdefault((source, polarity), {}).setdefault(family, []).append(
            (rank, index)
        )
    sequences: dict[tuple[str, str], list[int]] = {}
    for cell, families in families_by_cell.items():
        for ranked in families.values():
            ranked.sort()
        family_order = sorted(families, key=lambda name: (families[name][0][0], name))
        sequence: list[int] = []
        cursor = 0
        while True:
            progressed = False
            for name in family_order:
                if cursor < len(families[name]):
                    sequence.append(families[name][cursor][1])
                    progressed = True
            if not progressed:
                break
            cursor += 1
        sequences[cell] = sequence
    cell_order = [
        (source, polarity)
        for source in ("mathlib", "physlib", "cslib", "compiler_data")
        for polarity in ("preserving", "breaking")
    ]
    cell_order.extend(sorted(cell for cell in sequences if cell not in cell_order))
    selected: list[int] = []
    cursor = 0
    while len(selected) < count:
        progressed = False
        for cell in cell_order:
            sequence = sequences.get(cell, [])
            if cursor < len(sequence):
                selected.append(sequence[cursor])
                progressed = True
                if len(selected) == count:
                    break
        if not progressed:
            raise ProviderRehearsalV52Error("accepted rows cannot fill the Kimi audit count")
        cursor += 1
    return selected


def compact_provider_rehearsal_v52(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Deterministically compact all completed root outputs by stable row ID."""

    rows = _sample_rows(loaded.sample_path)
    expected_count = len(rows)
    states = ParallelRootStateMachine(
        loaded.output_root / "root_state.jsonl", maximum_workers=READ_ONLY_MAXIMUM_WORKERS
    ).snapshot()["roots"]
    if (
        not isinstance(states, dict)
        or len(states) != expected_count
        or any(
            not isinstance(state, dict) or state.get("status") != "complete"
            for state in states.values()
        )
    ):
        raise ProviderRehearsalV52Error(
            f"provider compaction requires {expected_count} completed roots"
        )
    core_by_id: dict[str, dict[str, object]] = {}
    sidecar_by_id: dict[str, dict[str, object]] = {}
    goals: set[str] = set()
    exprs: set[str] = set()
    for row in rows:
        root = OneRootConfig.model_validate(row["root"])
        root_output = _root_output(loaded, root.root_id)
        core = _sample_rows(root_output / "new_core/core.jsonl")
        sidecars = _sample_rows(root_output / "new_core/sidecar.jsonl")
        if len(core) != len(sidecars):
            raise ProviderRehearsalV52Error("provider root core/sidecar counts differ")
        for core_row, sidecar in zip(core, sidecars, strict=True):
            row_id = sidecar.get("row_id")
            candidate = core_row.get("candidate")
            reference = core_row.get("reference")
            expr_hash = sidecar.get("candidate_closed_expr_hash")
            reference_expr = sidecar.get("reference_closed_expr_hash")
            if not isinstance(row_id, str) or row_id in core_by_id:
                raise ProviderRehearsalV52Error("provider compaction found duplicate row ID")
            if (
                not isinstance(candidate, str)
                or candidate == reference
                or candidate in goals
                or not isinstance(expr_hash, str)
                or expr_hash == reference_expr
                or expr_hash in exprs
            ):
                raise ProviderRehearsalV52Error(
                    "provider compaction found self-pair or cross-root duplicate"
                )
            core_by_id[row_id] = core_row
            sidecar_by_id[row_id] = sidecar
            goals.add(candidate)
            exprs.add(expr_hash)
    ordered = sorted(core_by_id)
    core_rows = [core_by_id[row_id] for row_id in ordered]
    sidecar_rows = [sidecar_by_id[row_id] for row_id in ordered]
    output = loaded.output_root / "compacted/new_core"
    _atomic_exact(output / "core.jsonl", _jsonl_bytes(core_rows))
    _atomic_exact(output / "sidecar.jsonl", _jsonl_bytes(sidecar_rows))
    sample_rows = _sample_rows(loaded.sample_path)
    planned_slots = len(sample_rows) * 4
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_provider_compaction_v5_2_corrected_v1",
        "provider_config_sha256": loaded.sha256,
        "sample_sha256": hash_file(loaded.sample_path),
        "roots": len(sample_rows),
        "planned_slots": planned_slots,
        "accepted_rows": len(core_rows),
        "self_pairs": 0,
        "candidate_duplicates": 0,
        "core_sha256": hash_file(output / "core.jsonl"),
        "sidecar_sha256": hash_file(output / "sidecar.jsonl"),
    }
    _atomic_exact(
        loaded.output_root / "compacted/manifest.json",
        canonical_json_bytes(manifest) + b"\n",
    )
    return manifest


def verify_provider_replay_v52(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Replay all completed roots with zero provider calls and zero Lean requests."""

    preflight_sample_v52(loaded)
    compact_provider_rehearsal_v52(loaded)
    excluded = {"replay/reproducibility_receipt.json"}
    before = {
        str(path.relative_to(loaded.output_root)): hash_file(path)
        for path in sorted(loaded.output_root.rglob("*"))
        if path.is_file()
        and str(path.relative_to(loaded.output_root)) not in excluded
        and not str(path.relative_to(loaded.output_root)).startswith("audit_kimi/")
    }
    for row in _sample_rows(loaded.sample_path):
        root = OneRootConfig.model_validate(row["root"])
        result = run_one_root(
            _root_loaded(loaded, row), output_root=_root_output(loaded, root.root_id)
        )
        if not result.replayed:
            raise ProviderRehearsalV52Error("provider replay unexpectedly executed a root")
    compact_provider_rehearsal_v52(loaded)
    after = {
        str(path.relative_to(loaded.output_root)): hash_file(path)
        for path in sorted(loaded.output_root.rglob("*"))
        if path.is_file()
        and str(path.relative_to(loaded.output_root)) not in excluded
        and not str(path.relative_to(loaded.output_root)).startswith("audit_kimi/")
    }
    if before != after:
        raise ProviderRehearsalV52Error("provider zero-call replay changed durable artifacts")
    receipt: dict[str, object] = {
        "version": "leanfaith_sft2a_provider_replay_v5_2_corrected_v1",
        "provider_config_sha256": loaded.sha256,
        "sample_sha256": hash_file(loaded.sample_path),
        "roots_replayed": len(_sample_rows(loaded.sample_path)),
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "durable_artifact_hashes_preserved": True,
        "durable_tree_hash": hash_canonical(before),
        "reproducible": True,
    }
    _atomic_exact(
        loaded.output_root / "replay/reproducibility_receipt.json",
        canonical_json_bytes(receipt) + b"\n",
    )
    return receipt


def run_provider_kimi_audit_v52(
    loaded: LoadedProviderRehearsalV52,
    authorization: LoadedProviderAuthorizationV52,
    *,
    kimi_count: int = 40,
    concurrency: int = 8,
    audit_subdir: str = "audit_kimi",
    sampler: Literal["stratified_v5", "source_polarity_cells"] = "stratified_v5",
    quarantine_row_ids: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Run the Kimi audit with one durable checkpoint per row.

    ``kimi_count`` is 40 for the historical audit and at most 8 for pilot telemetry. Futures
    map to result positions, never to sidecar indices, so a non-contiguous stratified selection
    assembles correctly. Kimi is asynchronous telemetry: this function constructs only the Lemex
    provider, never Terra, Opus, or Lean, and holds no Lean reservation. A row whose judge
    output stays malformed after the single retry is routed to unknown/excluded; a row whose
    provider or ledger fails is left uncheckpointed and reported after the other rows finish.
    """

    if kimi_count < 0 or concurrency < 1:
        raise ProviderRehearsalV52Error("Kimi audit count/concurrency are malformed")
    replay_path = loaded.output_root / "replay/reproducibility_receipt.json"
    if not replay_path.is_file() or _object(replay_path).get("reproducible") is not True:
        raise ProviderRehearsalV52Error("Kimi audit requires the successful zero-call replay")
    audit_root = loaded.output_root / audit_subdir
    manifest_path = audit_root / "manifest.json"
    if manifest_path.is_file():
        return _object(manifest_path)
    compacted = loaded.output_root / "compacted/new_core"
    core = _sample_rows(compacted / "core.jsonl")
    sidecars = _sample_rows(compacted / "sidecar.jsonl")
    if not kimi_count:
        selected: list[int] = []
    elif sampler == "source_polarity_cells":
        selected = sprint_audit_selection(sidecars, kimi_count)
    else:
        selected = _audit_selection(sidecars, kimi_count)
    ledger = AtomicProviderBudget(loaded.output_root / "provider_budget.jsonl", loaded.ceilings)
    client = AtomicBudgetedProvider(
        lemex_audit_provider(loaded.base),
        ledger=ledger,
        kind="lemex",
        worker_id="audit-kimi",
        reclaim_from_worker="audit-kimi",
    )
    checkpoint_root = audit_root / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)

    def _checkpoint_path(row_id: str) -> Path:
        safe = row_id.replace("/", "_").replace(":", "_")
        return checkpoint_root / f"{safe}.json"

    def _process_audit_row(index: int) -> dict[str, object]:
        sidecar = sidecars[index]
        row_id = str(sidecar["row_id"])
        checkpoint = _checkpoint_path(row_id)
        if checkpoint.is_file():
            return {**_object(checkpoint), "checkpoint_hit": True}
        reference_record = sidecar.get("reference_repr")
        candidate_record = sidecar.get("candidate_repr")
        reference = reference_record.get("record") if isinstance(reference_record, dict) else None
        candidate = candidate_record.get("record") if isinstance(candidate_record, dict) else None
        if not isinstance(reference, dict) or not isinstance(candidate, dict):
            raise ProviderRehearsalV52Error("audit sidecar lacks frozen REPR records")
        prompt = render_blinded_judge_prompt(
            loaded.base,
            statement_a=str(reference["goal_v1"]),
            statement_b=str(candidate["goal_v1"]),
        )
        source_judge = JudgeOutputV5.model_validate(sidecar["claude_judge"])
        source = str(sidecar["root_id"]).split(":", maxsplit=1)[0]
        try:
            result = call_consistent_judge(
                client,
                prompt=prompt,
                input_ids=(row_id, "provider_rehearsal_kimi_v5_2"),
                closure_aware=True,
                malformed_retries=1,
            )
        except (StructuredProviderError, ParallelRehearsalError) as exc:
            return {
                "row_id": row_id,
                "source": source,
                "requested_polarity": sidecar["requested_polarity"],
                "opus_verdict": source_judge.verdict,
                "kimi_judgment": None,
                "agrees": False,
                "malformed_attempts": [],
                "malformed_retries": 0,
                "malformed_exhausted": False,
                "infrastructure_failed": True,
                "infrastructure_error": f"{type(exc).__name__}: {exc}"[:1000],
                "call_keys": [],
                "prompt_hash": prompt_hash(prompt),
                "action": "retry_on_resume_not_checkpointed",
                "checkpoint_hit": False,
            }
        judgment = result.judgment
        agrees = judgment is not None and judgment.verdict == source_judge.verdict
        malformed_exhausted = judgment is None
        row: dict[str, object] = {
            "row_id": row_id,
            "source": source,
            "requested_polarity": sidecar["requested_polarity"],
            "opus_verdict": source_judge.verdict,
            "kimi_judgment": (None if judgment is None else judgment.model_dump(mode="json")),
            "agrees": agrees,
            "malformed_attempts": list(result.malformed_attempts),
            "malformed_retries": result.malformed_retries,
            "malformed_exhausted": malformed_exhausted,
            "infrastructure_failed": False,
            "call_keys": [call.call_key for call in result.calls],
            "cache_hits": sum(call.cache_hit for call in result.calls),
            "prompt_hash": prompt_hash(prompt),
            "action": (
                "unknown_review_exclude_core_malformed_exhausted"
                if malformed_exhausted
                else "retain"
                if agrees
                else "unknown_review_exclude_core"
            ),
        }
        _atomic_exact(checkpoint, canonical_json_bytes(row) + b"\n")
        return {**row, "checkpoint_hit": False}

    audit_rows: list[dict[str, object]] = [{} for _ in selected]
    if selected:
        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(selected)), thread_name_prefix="sft2a-kimi"
        ) as kimi_pool:
            future_to_position = {
                kimi_pool.submit(_process_audit_row, source_index): position
                for position, source_index in enumerate(selected)
            }
            for future in as_completed(future_to_position):
                position = future_to_position[future]
                audit_rows[position] = future.result()
    if any(not row for row in audit_rows):
        raise ProviderRehearsalV52Error("Kimi audit assembled an empty result position")
    infrastructure_failed = [row for row in audit_rows if row.get("infrastructure_failed")]
    if infrastructure_failed:
        partial = {
            "version": "leanfaith_sft2a_provider_kimi_audit_partial_v5_2",
            "selected_rows": len(selected),
            "checkpointed_rows": sum(
                _checkpoint_path(str(sidecars[index]["row_id"])).is_file() for index in selected
            ),
            "infrastructure_failed_rows": [
                {"row_id": row["row_id"], "error": row.get("infrastructure_error")}
                for row in infrastructure_failed
            ],
            "resumable": True,
        }
        _atomic_replace_json(audit_root / "partial_status.json", partial)
        raise ProviderRehearsalV52Error(
            f"Kimi audit left {len(infrastructure_failed)} uncheckpointed rows after provider "
            "or ledger failures; rerun resumes from per-row checkpoints"
        )
    checkpoint_hits = sum(bool(row.get("checkpoint_hit")) for row in audit_rows)
    persisted_rows = [
        {key: value for key, value in row.items() if key != "checkpoint_hit"} for row in audit_rows
    ]
    _atomic_exact(audit_root / "audit_rows.jsonl", _jsonl_bytes(persisted_rows))
    unknown_ids = {str(row["row_id"]) for row in audit_rows if not bool(row["agrees"])}
    observed_ids = {str(sidecar.get("row_id")) for sidecar in sidecars}
    quarantined = {row_id for row_id in quarantine_row_ids if row_id in observed_ids}
    released = exclude_audit_unknowns(core, sidecars, unknown_ids | quarantined)
    _atomic_exact(audit_root / "releasable_core.jsonl", _jsonl_bytes(released))
    agreements = sum(bool(row["agrees"]) for row in audit_rows)
    strata: Counter[tuple[str, str]] = Counter()
    disagreements: Counter[tuple[str, str]] = Counter()
    for row in audit_rows:
        stratum = (str(row["source"]), str(row["requested_polarity"]))
        strata[stratum] += 1
        disagreements[stratum] += not bool(row["agrees"]) and not bool(row["malformed_exhausted"])
    agreement_rate = agreements / len(audit_rows) if audit_rows else None
    systematic = bool(audit_rows) and (
        agreements / len(audit_rows) < 0.95
        or any(strata[key] >= 5 and disagreements[key] / strata[key] >= 0.2 for key in strata)
    )
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_provider_kimi_audit_v5_2_corrected_v1",
        "authorization_receipt_sha256": authorization.sha256,
        "provider_config_sha256": loaded.sha256,
        "source_replay_sha256": hash_file(replay_path),
        "kimi_count_requested": kimi_count,
        "kimi_concurrency": concurrency,
        "worker_id": "audit-kimi",
        "audit_subdir": audit_subdir,
        "sampler": sampler,
        "selected_rows": len(selected),
        "selected_sidecar_indices": list(selected),
        "selected_cells": {
            f"{source}/{polarity}": count
            for (source, polarity), count in sorted(
                Counter(
                    (str(row["source"]), str(row["requested_polarity"])) for row in audit_rows
                ).items()
            )
        },
        "quarantined_rows": sorted(quarantined),
        "checkpoint_hits": checkpoint_hits,
        "agreements": agreements,
        "agreement_rate": agreement_rate,
        "genuine_semantic_disagreements": sum(
            not bool(row["agrees"]) and not bool(row["malformed_exhausted"]) for row in audit_rows
        ),
        "malformed_exhausted": sum(bool(row["malformed_exhausted"]) for row in audit_rows),
        "malformed_retries": sum(int(cast(int, row["malformed_retries"])) for row in audit_rows),
        "unknown_review_rows": len(unknown_ids),
        "released_rows": len(released),
        "systematic_disagreement": systematic,
        "scale_blocked": systematic,
        "persistent_provider_budget": ledger.snapshot(),
        "audit_rows_sha256": hash_file(audit_root / "audit_rows.jsonl"),
        "releasable_core_sha256": hash_file(audit_root / "releasable_core.jsonl"),
        "checkpointed_per_row": True,
        "terra_calls_executed": 0,
        "opus_calls_executed": 0,
        "lean_requests_executed": 0,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "publication_authorized": False,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return manifest


def launch_provider_rehearsal_v52(
    loaded: LoadedProviderRehearsalV52, authorization: LoadedProviderAuthorizationV52
) -> dict[str, object]:
    preflight = preflight_provider_launch_v52(loaded, authorization)
    session = str(loaded.document["tmux_session"])
    command = (
        sys.executable,
        "-m",
        "leanfaith.sft2a",
        "--provider-rehearsal-config",
        str(loaded.path),
        "--provider-rehearsal-authorization",
        str(authorization.path),
        "detached-provider-rehearsal-v5-2-worker",
    )
    with parallel_launch_lock(loaded.output_root / "detached/launch.lock"):
        completed = subprocess.run(
            ("tmux", "new-session", "-d", "-s", session, shlex.join(command)),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ProviderRehearsalV52Error(f"corrected tmux launch failed: {completed.stderr}")
    return {"preflight": preflight, "session_started": True}


def run_detached_provider_rehearsal_v52(
    loaded: LoadedProviderRehearsalV52, authorization: LoadedProviderAuthorizationV52
) -> dict[str, object]:
    loaded.output_root.mkdir(parents=True, exist_ok=True)
    log_path = loaded.output_root / "detached/combined.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps({"event": "worker_start", "at": _now(), "pid": os.getpid()}) + "\n")
        log.flush()
    with parallel_launch_lock(loaded.output_root / "detached/run.lock"):
        reservation = claim_resources(
            task=str(loaded.document["resource_task"]),
            lean_workers=2,
            lean_rss_gib=40.0,
            gpu=False,
            pid=os.getpid(),
            owner_session=str(loaded.document["tmux_session"]),
            worktree=loaded.base.repo_root,
        )
        try:
            launch_receipt = {
                "version": "leanfaith_sft2a_detached_provider_launch_v5_2_corrected_v1",
                "session_name": loaded.document["tmux_session"],
                "pid": os.getpid(),
                "implementation_commit": authorization.document["implementation_commit"],
                "implementation_tree": authorization.document["implementation_tree"],
                "provider_config_sha256": loaded.sha256,
                "authorization_receipt_sha256": authorization.sha256,
                "sample_sha256": loaded.document["corrected_sample_sha256"],
                "output_root": str(loaded.output_root),
                "provider_budget_ledger": str(loaded.output_root / "provider_budget.jsonl"),
                "root_journal": str(loaded.output_root / "root_state.jsonl"),
                "combined_log": str(log_path),
                "maximum_root_workers": 2,
                "maximum_total_lean_workers": 2,
                "maximum_measured_rss_gib": 40.0,
                "resume_command": (
                    f"uv run python -m leanfaith.sft2a --provider-rehearsal-config {loaded.path} "
                    f"--provider-rehearsal-authorization {authorization.path} "
                    "launch-provider-rehearsal-v5-2"
                ),
                "health_command": (
                    f"tmux has-session -t {loaded.document['tmux_session']} && "
                    f"tail -n 40 {log_path}"
                ),
                "duplicate_restart_forbidden": True,
            }
            if loaded.recovery_source is not None:
                readiness = _object(provider_readiness_path_v52(loaded))
                launch_receipt["recovery_source_run_identity_hash"] = readiness[
                    "recovery_source_run_identity_hash"
                ]
                launch_receipt["cumulative_budget_seed_sha256"] = readiness[
                    "cumulative_budget_seed_sha256"
                ]
            _atomic_exact(
                loaded.output_root / "detached/launch_receipt.json",
                canonical_json_bytes(launch_receipt) + b"\n",
            )
            try:
                workers = run_two_provider_workers_v52(loaded, authorization)
                compacted = compact_provider_rehearsal_v52(loaded)
                replay = verify_provider_replay_v52(loaded)
                audit = run_provider_kimi_audit_v52(loaded, authorization)
                terminal = {
                    "version": "leanfaith_sft2a_provider_terminal_v5_2_corrected_v1",
                    "status": "complete",
                    "workers": workers,
                    "compaction_sha256": hash_file(loaded.output_root / "compacted/manifest.json"),
                    "replay_sha256": hash_file(
                        loaded.output_root / "replay/reproducibility_receipt.json"
                    ),
                    "audit_sha256": hash_file(loaded.output_root / "audit_kimi/manifest.json"),
                    "accepted_rows": compacted["accepted_rows"],
                    "replay_zero_calls": replay["provider_calls_executed"] == 0,
                    "audit_agreement_rate": audit["agreement_rate"],
                    "systematic_disagreement": audit["systematic_disagreement"],
                    "completed_at": _now(),
                    "scale_10k_authorized": False,
                    "scale_50k_authorized": False,
                }
                _atomic_exact(
                    loaded.output_root / "detached/terminal_status.json",
                    canonical_json_bytes(terminal) + b"\n",
                )
                return terminal
            except Exception as exc:
                failure = {
                    "version": "leanfaith_sft2a_provider_terminal_v5_2_corrected_v1",
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:1000],
                    "provider_budget": AtomicProviderBudget(
                        loaded.output_root / "provider_budget.jsonl", loaded.ceilings
                    ).snapshot(),
                    "root_state": ParallelRootStateMachine(
                        loaded.output_root / "root_state.jsonl",
                        maximum_workers=READ_ONLY_MAXIMUM_WORKERS,
                    ).snapshot(),
                    "failed_at": _now(),
                    "scale_10k_authorized": False,
                    "scale_50k_authorized": False,
                }
                _atomic_exact(
                    loaded.output_root / "detached/terminal_status.json",
                    canonical_json_bytes(failure) + b"\n",
                )
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(
                        json.dumps(
                            {
                                "event": "worker_failed",
                                "at": failure["failed_at"],
                                "error_type": failure["error_type"],
                            }
                        )
                        + "\n"
                    )
                    log.flush()
                raise
        finally:
            if reservation is not None:
                release_resources(task=str(loaded.document["resource_task"]))


def provider_rehearsal_health_v52(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    session = str(loaded.document["tmux_session"])
    alive = (
        subprocess.run(
            ("tmux", "has-session", "-t", session), check=False, capture_output=True
        ).returncode
        == 0
    )
    pane_pid: int | None = None
    process_tree = ""
    if alive:
        pane = subprocess.run(
            ("tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"),
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
    resource_task = str(loaded.document["resource_task"])
    reservation = next((item for item in list_reservations() if item.task == resource_task), None)
    state_path = loaded.output_root / "root_state.jsonl"
    budget_path = loaded.output_root / "provider_budget.jsonl"
    return {
        "session_name": session,
        "tmux_alive": alive,
        "pane_pid": pane_pid,
        "process_tree": process_tree,
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
        "root_state": (
            ParallelRootStateMachine(
                state_path, maximum_workers=READ_ONLY_MAXIMUM_WORKERS
            ).snapshot()
            if state_path.is_file()
            else {}
        ),
        "provider_budget": (
            AtomicProviderBudget(budget_path, loaded.ceilings).snapshot()
            if budget_path.is_file()
            else {}
        ),
        "launch_receipt_present": (loaded.output_root / "detached/launch_receipt.json").is_file(),
        "combined_log_present": (loaded.output_root / "detached/combined.log").is_file(),
        "terminal_status_present": (loaded.output_root / "detached/terminal_status.json").is_file(),
    }


__all__ = [
    "LoadedProviderAuthorizationV52",
    "LoadedProviderRehearsalV52",
    "OraclePool",
    "PooledOracle",
    "ProviderRehearsalV52Error",
    "authorization_sentence_v52",
    "certified_reference_result_v52",
    "compact_provider_rehearsal_v52",
    "launch_provider_rehearsal_v52",
    "load_provider_authorization_v52",
    "load_provider_rehearsal_v52",
    "materialize_provider_authorization_v52",
    "preflight_provider_launch_v52",
    "preflight_sample_v52",
    "prepare_provider_readiness_v52",
    "prepare_provider_recovery_seed_v52",
    "provider_readiness_path_v52",
    "provider_rehearsal_health_v52",
    "run_detached_provider_rehearsal_v52",
    "run_provider_kimi_audit_v52",
    "run_provider_worker_v52",
    "run_two_provider_workers_v52",
    "sprint_audit_selection",
    "verify_provider_replay_v52",
]
