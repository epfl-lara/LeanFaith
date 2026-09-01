"""Hash-bound closure-aware semantic regression canaries for SFT2A v5."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.models import SFT2AV5Config
from leanfaith.sft2a.pipeline import StructuredProvider
from leanfaith.sft2a.prompts import prompt_hash, render_blinded_judge_prompt
from leanfaith.sft2a.providers import ProviderCallResult, claude_judge_provider


class ClosureCanaryError(RuntimeError):
    """The v5 canary artifact, provider response, or immutable output differs."""


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClosureCanaryError(f"invalid closure canary JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClosureCanaryError("closure canary JSON root is not an object")
    return value


def load_closure_canaries(loaded: LoadedSFT2AConfig) -> tuple[dict[str, object], ...]:
    if not isinstance(loaded.config, SFT2AV5Config):
        raise ClosureCanaryError("closure canaries require the additive v5 config")
    binding = loaded.config.closure_canaries
    path = loaded.repo_root / binding.path
    if path.is_symlink() or not path.is_file() or hash_file(path) != binding.sha256:
        raise ClosureCanaryError("closure canary artifact differs from its v5 binding")
    document = _object(path)
    rows = document.get("canaries")
    if (
        document.get("schema_version") != 5
        or document.get("canary_set_id") != "sft2a_universal_closure_equivalence_canaries_v5"
        or not isinstance(rows, list)
        or len(rows) != 3
    ):
        raise ClosureCanaryError("closure canary population/version differs")
    expected_ids = {
        "nat_add_comm/break_1",
        "set_union_comm/break_1",
        "nat_gcd_comm/break_0",
    }
    result: list[dict[str, object]] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("canary_id") not in expected_ids
            or row.get("required_verdict") != "equivalent"
            or not isinstance(row.get("statement_a"), str)
            or not isinstance(row.get("statement_b"), str)
        ):
            raise ClosureCanaryError("closure canary row differs from the frozen contract")
        result.append(row)
    if {str(row["canary_id"]) for row in result} != expected_ids:
        raise ClosureCanaryError("closure canary IDs are missing or duplicated")
    return tuple(result)


def _validate_existing(root: Path, manifest: Mapping[str, object]) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ClosureCanaryError("closure canary manifest lacks artifacts")
    for relative, digest in artifacts.items():
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or hash_file(root / relative) != digest
        ):
            raise ClosureCanaryError("closure canary replay artifact differs")


def run_closure_canaries(
    loaded: LoadedSFT2AConfig,
    *,
    judge: StructuredProvider | None = None,
) -> dict[str, object]:
    """Require Opus to pass all three closure regressions before the v5 smoke."""

    if not isinstance(loaded.config, SFT2AV5Config):
        raise ClosureCanaryError("closure canaries require the additive v5 config")
    root = run_paths(loaded).one_root / "closure_canaries_v5"
    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        existing = _object(manifest_path)
        _validate_existing(root, existing)
        return existing
    client = judge or claude_judge_provider(loaded)
    audit_rows: list[dict[str, object]] = []
    all_calls: list[ProviderCallResult] = []
    all_passed = True
    for canary in load_closure_canaries(loaded):
        prompt = render_blinded_judge_prompt(
            loaded,
            statement_a=str(canary["statement_a"]),
            statement_b=str(canary["statement_b"]),
        )
        result = call_consistent_judge(
            client,
            prompt=prompt,
            input_ids=(str(canary["canary_id"]), "closure_canary_v5"),
            closure_aware=True,
            malformed_retries=1,
        )
        all_calls.extend(result.calls)
        passed = result.judgment is not None and result.judgment.verdict == "equivalent"
        all_passed = all_passed and passed
        audit_rows.append(
            {
                "canary_id": canary["canary_id"],
                "expected_verdict": "equivalent",
                "passed": passed,
                "judgment": (
                    None if result.judgment is None else result.judgment.model_dump(mode="json")
                ),
                "call_keys": [call.call_key for call in result.calls],
                "malformed_attempts": list(result.malformed_attempts),
                "prompt_hash": prompt_hash(prompt),
            }
        )
    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in audit_rows)
    _atomic_exact(root / "rows.jsonl", payload)
    canary_document: dict[str, object] = {
        "version": "leanfaith_sft2a_closure_canary_run_v5",
        "config_hash": loaded.config_hash,
        "canary_artifact": loaded.config.closure_canaries.model_dump(mode="json"),
        "total": len(audit_rows),
        "passed": sum(bool(row["passed"]) for row in audit_rows),
        "malformed_attempts": sum(
            len(value) if isinstance(value, list) else 0
            for row in audit_rows
            for value in (row.get("malformed_attempts"),)
        ),
        "all_passed": all_passed,
        "artifacts": {"rows.jsonl": hash_file(root / "rows.jsonl")},
        "provider": loaded.config.claude_judge.model_dump(mode="json"),
        "provider_calls": sum(
            len(value) if isinstance(value, list) else 0
            for row in audit_rows
            for value in (row.get("call_keys"),)
        ),
        "provider_usage": [
            {
                "call_key": call.call_key,
                "provider_id": call.provider_id,
                "cache_hit": call.cache_hit,
                "usage": call.usage,
                "cost_usd": call.cost_usd,
                "elapsed_seconds": call.elapsed_seconds,
            }
            for call in all_calls
        ],
        "reported_opus_spend_usd": sum(
            call.cost_usd or 0.0 for call in all_calls if not call.cache_hit
        ),
        "provider_latency_seconds": sum(call.elapsed_seconds for call in all_calls),
        "lean_requests": 0,
        "scale_50k_started": False,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(canary_document) + b"\n")
    if not all_passed:
        raise ClosureCanaryError("one or more closure-aware canaries failed")
    return canary_document


__all__ = ["ClosureCanaryError", "load_closure_canaries", "run_closure_canaries"]
