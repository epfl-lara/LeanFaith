"""Authorized provider-backed runner for the corrected certified SFT2A v5.2 sample."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
from collections import Counter, defaultdict
from collections.abc import Iterator
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
from leanfaith.sft2a.lean_oracle import SignatureOracle, SignatureOracleResult
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.mechanisms import MechanismAssignment
from leanfaith.sft2a.models import (
    ExecutionCeilings,
    JudgeOutputV5,
    OneRootConfig,
    SFT2AV52Config,
)
from leanfaith.sft2a.parallel_rehearsal import (
    AtomicBudgetedProvider,
    AtomicProviderBudget,
    ParallelRehearsalError,
    ParallelRootStateMachine,
    parallel_launch_lock,
)
from leanfaith.sft2a.pipeline import run_one_root
from leanfaith.sft2a.prompts import prompt_hash, render_blinded_judge_prompt
from leanfaith.sft2a.providers import (
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
    if (
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
            raise ProviderRehearsalV52Error(f"corrected provider input hash differs: {artifact}")
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
        raise ProviderRehearsalV52Error("an out-of-scope corrected provider action is authorized")
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
    return LoadedProviderRehearsalV52(
        path=resolved,
        document=document,
        sha256=hash_file(resolved),
        base=base,
        sample_path=sample_path,
        output_root=Path(str(document["provider_output_root"])),
        ceilings=ceilings,
        recovery_source=recovery_source,
    )


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
    """Two locked persistent project-grouped SignatureOracles reused with rebind."""

    def __init__(self, *, cache_version: Literal["v1", "v2"] = "v2") -> None:
        self._cache_version = cache_version
        self._oracles: list[SignatureOracle | None] = [None, None]
        self._projects: list[str | None] = [None, None]
        self._use_locks = [threading.Lock(), threading.Lock()]

    @contextmanager
    def acquire(self, root_loaded: LoadedSFT2AConfig) -> Iterator[SignatureOracle]:
        project_id = root_loaded.config.root.compile_context.project_id
        slot = -1
        for i in range(2):
            if self._use_locks[i].acquire(blocking=False):
                slot = i
                break
        if slot < 0:
            self._use_locks[0].acquire()
            slot = 0
        try:
            current_oracle = self._oracles[slot]
            if current_oracle is None or self._projects[slot] != project_id:
                if current_oracle is not None:
                    current_oracle.close()
                current_oracle = SignatureOracle(root_loaded, cache_version=self._cache_version)
                self._oracles[slot] = current_oracle
                self._projects[slot] = project_id
            else:
                current_oracle.rebind(root_loaded)
            yield current_oracle
        finally:
            self._use_locks[slot].release()

    def close(self) -> None:
        for i in range(2):
            with self._use_locks[i]:
                oracle = self._oracles[i]
                if oracle is not None:
                    oracle.close()
                    self._oracles[i] = None
                    self._projects[i] = None


class PooledOracle:
    """PropositionOracle adapter that delegates to an OraclePool with per-call locking."""

    def __init__(self, pool: OraclePool, root_loaded: LoadedSFT2AConfig) -> None:
        self._pool = pool
        self._root_loaded = root_loaded

    def elaborate(
        self, signature: str, *, endpoint_role: Literal["reference", "candidate"]
    ) -> SignatureOracleResult:
        with self._pool.acquire(self._root_loaded) as oracle:
            return oracle.elaborate(signature, endpoint_role=endpoint_role)

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
    verify_corrected_global_preflight(loaded.sample_path.parent)
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
) -> list[dict[str, object]]:
    """Dynamic as_completed root queue with two persistent project-grouped oracles.

    Provider concurrency is decoupled from the two-worker Lean cap: the
    ThreadPoolExecutor runs at *provider_concurrency*, while OraclePool keeps
    exactly two locked persistent SignatureOracle slots reused with rebind.
    """

    import queue as queue_module

    verify_corrected_global_preflight(loaded.sample_path.parent)
    rows = _sample_rows(loaded.sample_path)
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
    registry = PersistentCandidateRegistry(loaded.output_root / "candidate_registry.jsonl")
    states = ParallelRootStateMachine(
        loaded.output_root / "root_state.jsonl", maximum_workers=provider_concurrency
    )
    oracle_pool = OraclePool(cache_version="v2")
    completed_count = 0
    completed_lock = threading.Lock()

    pending_queue: queue_module.Queue[dict[str, object]] = queue_module.Queue()
    for row in rows:
        root = OneRootConfig.model_validate(row["root"])
        snapshot = states.snapshot()["roots"]
        assert isinstance(snapshot, dict)
        current = snapshot.get(root.root_id)
        if isinstance(current, dict) and current.get("status") == "complete":
            continue
        pending_queue.put(row)

    def process_roots() -> None:
        nonlocal completed_count
        while True:
            try:
                row = pending_queue.get_nowait()
            except queue_module.Empty:
                break
            root = OneRootConfig.model_validate(row["root"])
            root_worker = f"dyn-{root.root_id}"
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
            root_loaded = _root_loaded(loaded, row)
            reference = certified_reference_result_v52(row)
            oracle = PooledOracle(oracle_pool, root_loaded)
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
                    worker_id=root_worker,
                    manifest_hash=hash_file(manifest_path),
                )
                if not result.replayed:
                    with completed_lock:
                        completed_count += 1
            except Exception as exc:
                states.crash(root_id=root.root_id, worker_id=root_worker, reason=type(exc).__name__)
                raise

    pending_count = pending_queue.qsize()
    if pending_count > 0:
        with ThreadPoolExecutor(
            max_workers=min(provider_concurrency, pending_count),
            thread_name_prefix="sft2a-dyn",
        ) as pool:
            worker_count = min(provider_concurrency, pending_count)
            futures = [pool.submit(process_roots) for _ in range(worker_count)]
            for future in as_completed(futures):
                future.result()

    oracle_pool.close()
    return [
        {
            "worker_id": worker_tag,
            "assigned_roots": pending_count,
            "completed_in_invocation": completed_count,
            "provider_budget": ledger.snapshot(),
            "candidate_registry": registry.snapshot(),
            "authorization_receipt_sha256": authorization.sha256,
        }
    ]


def compact_provider_rehearsal_v52(loaded: LoadedProviderRehearsalV52) -> dict[str, object]:
    """Deterministically compact all completed root outputs by stable row ID."""

    rows = _sample_rows(loaded.sample_path)
    states = ParallelRootStateMachine(loaded.output_root / "root_state.jsonl").snapshot()["roots"]
    if (
        not isinstance(states, dict)
        or len(states) != 100
        or any(
            not isinstance(state, dict) or state.get("status") != "complete"
            for state in states.values()
        )
    ):
        raise ProviderRehearsalV52Error("provider compaction requires 100 completed roots")
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
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_provider_compaction_v5_2_corrected_v1",
        "provider_config_sha256": loaded.sha256,
        "sample_sha256": loaded.document["corrected_sample_sha256"],
        "roots": 100,
        "planned_slots": 400,
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

    verify_corrected_global_preflight(loaded.sample_path.parent)
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
        "sample_sha256": loaded.document["corrected_sample_sha256"],
        "roots_replayed": 100,
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
) -> dict[str, object]:
    """Run the 40-row stratified Kimi audit with per-row checkpoints at concurrency 8."""

    replay_path = loaded.output_root / "replay/reproducibility_receipt.json"
    if not replay_path.is_file() or _object(replay_path).get("reproducible") is not True:
        raise ProviderRehearsalV52Error("Kimi audit requires the successful zero-call replay")
    manifest_path = loaded.output_root / "audit_kimi/manifest.json"
    if manifest_path.is_file():
        return _object(manifest_path)
    compacted = loaded.output_root / "compacted/new_core"
    core = _sample_rows(compacted / "core.jsonl")
    sidecars = _sample_rows(compacted / "sidecar.jsonl")
    selected = _audit_selection(sidecars, 40)
    ledger = AtomicProviderBudget(loaded.output_root / "provider_budget.jsonl", loaded.ceilings)
    client = AtomicBudgetedProvider(
        lemex_audit_provider(loaded.base),
        ledger=ledger,
        kind="lemex",
        worker_id="audit-kimi",
        reclaim_from_worker="audit-kimi",
    )
    audit_root = loaded.output_root / "audit_kimi"
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
            return _object(checkpoint)
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
        result = call_consistent_judge(
            client,
            prompt=prompt,
            input_ids=(row_id, "provider_rehearsal_kimi_v5_2"),
            closure_aware=True,
            malformed_retries=1,
        )
        source_judge = JudgeOutputV5.model_validate(sidecar["claude_judge"])
        judgment = result.judgment
        agrees = judgment is not None and judgment.verdict == source_judge.verdict
        malformed_exhausted = judgment is None
        source = str(sidecar["root_id"]).split(":", maxsplit=1)[0]
        row = {
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
        _atomic_exact(checkpoint, canonical_json_bytes(row) + b"\n")
        return row

    audit_rows: list[dict[str, object]] = [{} for _ in selected]
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="sft2a-kimi") as kimi_pool:
        future_to_index = {kimi_pool.submit(_process_audit_row, index): index for index in selected}
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            audit_rows[index] = future.result()

    _atomic_exact(audit_root / "audit_rows.jsonl", _jsonl_bytes(audit_rows))
    unknown_ids = {str(row["row_id"]) for row in audit_rows if not bool(row["agrees"])}
    released = exclude_audit_unknowns(core, sidecars, unknown_ids)
    _atomic_exact(audit_root / "releasable_core.jsonl", _jsonl_bytes(released))
    agreements = sum(bool(row["agrees"]) for row in audit_rows)
    strata: Counter[tuple[str, str]] = Counter()
    disagreements: Counter[tuple[str, str]] = Counter()
    for row in audit_rows:
        stratum = (str(row["source"]), str(row["requested_polarity"]))
        strata[stratum] += 1
        disagreements[stratum] += not bool(row["agrees"]) and not bool(row["malformed_exhausted"])
    systematic = agreements / len(audit_rows) < 0.95 or any(
        strata[key] >= 5 and disagreements[key] / strata[key] >= 0.2 for key in strata
    )
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_provider_kimi_audit_v5_2_corrected_v1",
        "authorization_receipt_sha256": authorization.sha256,
        "provider_config_sha256": loaded.sha256,
        "source_replay_sha256": hash_file(replay_path),
        "selected_rows": len(selected),
        "agreements": agreements,
        "agreement_rate": agreements / len(audit_rows),
        "genuine_semantic_disagreements": sum(
            not bool(row["agrees"]) and not bool(row["malformed_exhausted"]) for row in audit_rows
        ),
        "malformed_exhausted": sum(bool(row["malformed_exhausted"]) for row in audit_rows),
        "unknown_review_rows": len(unknown_ids),
        "released_rows": len(released),
        "systematic_disagreement": systematic,
        "scale_blocked": systematic,
        "persistent_provider_budget": ledger.snapshot(),
        "audit_rows_sha256": hash_file(audit_root / "audit_rows.jsonl"),
        "releasable_core_sha256": hash_file(audit_root / "releasable_core.jsonl"),
        "checkpointed_per_row": True,
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
                        loaded.output_root / "root_state.jsonl"
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
            ParallelRootStateMachine(state_path).snapshot() if state_path.is_file() else {}
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
    "ProviderRehearsalV52Error",
    "authorization_sentence_v52",
    "certified_reference_result_v52",
    "compact_provider_rehearsal_v52",
    "launch_provider_rehearsal_v52",
    "load_provider_authorization_v52",
    "load_provider_rehearsal_v52",
    "materialize_provider_authorization_v52",
    "preflight_provider_launch_v52",
    "prepare_provider_readiness_v52",
    "prepare_provider_recovery_seed_v52",
    "provider_readiness_path_v52",
    "provider_rehearsal_health_v52",
    "run_detached_provider_rehearsal_v52",
    "run_provider_kimi_audit_v52",
    "run_provider_worker_v52",
    "run_two_provider_workers_v52",
    "verify_provider_replay_v52",
]
