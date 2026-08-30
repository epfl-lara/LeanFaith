"""Deterministic four-source SFT2A pilot sampler and grouped runner."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft2a.budget import BudgetedProvider, PersistentProviderBudget
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.dedup import PersistentCandidateRegistry
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.lean_oracle import SignatureOracle
from leanfaith.sft2a.legacy import _atomic_exact, _blocklist
from leanfaith.sft2a.models import CompileContextConfig, OneRootConfig, SFT2AOpusConfig
from leanfaith.sft2a.pipeline import StructuredProvider, run_one_root
from leanfaith.sft2a.providers import claude_judge_provider, proposer_provider
from leanfaith.sft2a.readiness import (
    LoadedPilotReadiness,
    implementation_identity,
    require_pilot_authorization,
)

_SAMPLER_VERSION = "sft2a_hash_bound_catalog_sampler_v2"
_SAMPLER_SALT = "leanfaith-sft2a-diverse-root-opus5-pilot-v2"


class PilotError(RuntimeError):
    """Pilot catalog, sampling, authorization, ceiling, or grouped execution failed."""


def _safe_output(staging_root: str, subdir: str) -> Path:
    pure = PurePosixPath(subdir)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise PilotError(f"unsafe pilot output subdirectory: {subdir!r}")
    return Path(staging_root).joinpath(*pure.parts)


def _pilot_output(loaded: LoadedSFT2AConfig, readiness: LoadedPilotReadiness | None) -> Path:
    if readiness is None:
        return run_paths(loaded).pilot
    return _safe_output(loaded.config.staging_root, readiness.config.sample_output_subdir)


def _catalog(
    loaded: LoadedSFT2AConfig, readiness: LoadedPilotReadiness | None
) -> dict[str, object]:
    if not isinstance(loaded.config, SFT2AOpusConfig):
        raise PilotError("diverse-root pilot requires the additive Opus config")
    if readiness is None:
        relative = loaded.config.pilot.catalog_path
        expected = loaded.config.pilot.catalog_sha256
    else:
        relative = readiness.config.catalog.path
        expected = readiness.config.catalog.sha256
    path = loaded.repo_root / relative
    if hash_file(path) != expected:
        raise PilotError("pilot root catalog differs from its frozen hash")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError(f"invalid pilot catalog: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise PilotError("pilot catalog has an unsupported schema")
    return value


def _expanded_roots(
    loaded: LoadedSFT2AConfig, readiness: LoadedPilotReadiness | None
) -> list[dict[str, object]]:
    catalog = _catalog(loaded, readiness)
    contexts = catalog.get("contexts")
    roots = catalog.get("roots")
    if not isinstance(contexts, dict) or not isinstance(roots, list):
        raise PilotError("pilot catalog lacks contexts or roots")
    expanded: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in roots:
        if not isinstance(raw, dict):
            raise PilotError("pilot catalog root is not an object")
        root_id = raw.get("root_id")
        context_id = raw.get("context_id")
        if not isinstance(root_id, str) or root_id in seen or not isinstance(context_id, str):
            raise PilotError("pilot catalog root ID/context is missing or duplicated")
        context_raw = contexts.get(context_id)
        if not isinstance(context_raw, dict):
            raise PilotError(f"pilot root references unknown context {context_id!r}")
        context = CompileContextConfig.model_validate(context_raw)
        source = raw.get("source")
        revision = raw.get("source_revision")
        if source != "compiler_data" and revision != context.project_revision:
            raise PilotError("imported-project root revision differs from its compile context")
        root_fields = {
            key: value for key, value in raw.items() if key not in {"context_id", "source_locator"}
        }
        root = OneRootConfig.model_validate(
            {
                **root_fields,
                "external_transmission": True,
                "policy_version": "source_use_v2",
                "expected_reference_goal_v1": "UNVERIFIED_UNTIL_AUTHORIZED_PILOT",
                "compile_context": context.model_dump(mode="json"),
            }
        )
        expanded.append(
            {
                "root": root.model_dump(mode="json"),
                "context_id": context_id,
                "source_locator": raw.get("source_locator"),
            }
        )
        seen.add(root_id)
    return expanded


def cast_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PilotError("pilot row has a malformed nested mapping")
    return value


def _project_id(row: Mapping[str, object]) -> str:
    root = cast_mapping(row.get("root"))
    context = cast_mapping(root.get("compile_context"))
    return str(context.get("project_id"))


def prepare_pilot_sample(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness | None = None,
    *,
    implementation: Mapping[str, str] | None = None,
    output_root: Path | None = None,
) -> dict[str, object]:
    """Freeze exact roots and grouped order without invoking Lean or a provider."""

    if not isinstance(loaded.config, SFT2AOpusConfig):
        raise PilotError("diverse-root pilot requires the additive Opus config")
    roots = _expanded_roots(loaded, readiness)
    allocations = (
        loaded.config.pilot.allocations if readiness is None else readiness.config.allocations
    )
    catalog_hash = (
        loaded.config.pilot.catalog_sha256 if readiness is None else readiness.config.catalog.sha256
    )
    sampler_version = loaded.config.pilot.sampler_version if readiness is None else _SAMPLER_VERSION
    salt = loaded.config.pilot.salt if readiness is None else _SAMPLER_SALT
    ceilings = loaded.config.pilot.ceilings if readiness is None else readiness.config.ceilings
    by_source: dict[str, list[tuple[str, dict[str, object]]]] = defaultdict(list)
    _blocklist_path, blocked_hashes = _blocklist(loaded)
    for row in roots:
        root = cast_mapping(row["root"])
        signature = root.get("reference_signature")
        if not isinstance(signature, str):
            raise PilotError("catalog root lacks a reference signature")
        if "[anonymous]" in signature.casefold() or "⋯" in signature:
            raise PilotError("catalog root contains a forbidden placeholder")
        if signature_near_dup_hash(signature) in blocked_hashes:
            raise PilotError("catalog root matches the frozen gold blocklist")
        source = str(root.get("source"))
        rank = hash_canonical(
            {"sampler": sampler_version, "salt": salt, "root_id": root.get("root_id")}
        )
        by_source[source].append((rank, row))
    selected: list[dict[str, object]] = []
    for allocation in allocations:
        ranked = sorted(by_source.get(allocation.source, []), key=lambda item: item[0])
        if len(ranked) < allocation.roots:
            raise PilotError(f"pilot catalog has too few {allocation.source} roots")
        selected.extend(row for _rank, row in ranked[: allocation.roots])
    selected.sort(
        key=lambda row: (
            _project_id(row),
            str(row["context_id"]),
            str(cast_mapping(row["root"])["root_id"]),
        )
    )
    output = output_root or _pilot_output(loaded, readiness)
    sample_path = output / "sample.jsonl"
    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in selected)
    _atomic_exact(sample_path, payload)
    observed_sample_hash = hash_file(sample_path)
    if readiness is not None and observed_sample_hash != readiness.config.expected_sample_sha256:
        raise PilotError("pilot sample differs from its authorization-bound hash")
    source_counts = Counter(str(cast_mapping(row["root"])["source"]) for row in selected)
    group_counts = Counter((_project_id(row), str(row["context_id"])) for row in selected)
    identity = dict(implementation or implementation_identity(loaded.repo_root))
    manifest: dict[str, object] = {
        "version": (
            "leanfaith_sft2a_diverse_root_sample_v1"
            if readiness is None
            else "leanfaith_sft2a_diverse_root_sample_v2"
        ),
        "catalog_sha256": catalog_hash,
        "sampler_version": sampler_version,
        "salt": salt,
        "root_count": len(selected),
        "source_mix": dict(sorted(source_counts.items())),
        "grouped_execution_order": [
            {"project_id": key[0], "context_id": key[1], "roots": count}
            for key, count in sorted(group_counts.items())
        ],
        "selected_roots": [str(cast_mapping(row["root"])["root_id"]) for row in selected],
        "sample_sha256": observed_sample_hash,
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "pilot_authorized": (
            loaded.config.pilot.authorized
            if readiness is None
            else readiness.authorization.get("authorized") is True
        ),
        "ceilings": ceilings.model_dump(mode="json"),
        "implementation": identity,
    }
    if readiness is not None:
        manifest["readiness"] = {
            "config_id": readiness.config.config_id,
            "config_hash": readiness.config_hash,
            "authorization_receipt_sha256": readiness.config.authorization_receipt.sha256,
            "authorization_scope": readiness.authorization.get("authorization_scope"),
            "historical_fable_seal_sha256": readiness.config.historical_fable_seal.sha256,
        }
    _atomic_exact(output / "sample_manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


def _number(mapping: Mapping[str, object], key: str) -> float:
    value = mapping.get(key, 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def consolidate_pilot_quality(
    *,
    output: Path,
    sample_manifest: Mapping[str, object],
    root_manifest_paths: Sequence[Path],
    implementation: Mapping[str, str],
    budget_snapshot: Mapping[str, object],
    dedup_snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Write one quality/cost/throughput view across all completed pilot roots."""

    manifests: list[dict[str, object]] = []
    for path in root_manifest_paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PilotError(f"cannot consolidate pilot root manifest {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise PilotError(f"pilot root manifest is not an object: {path}")
        manifests.append(value)
    count_keys = (
        "accepted",
        "accepted_positive",
        "accepted_negative",
        "invalid_attempts",
        "unknown_rows",
        "judge_disagreements",
        "gold_contamination",
        "cross_root_duplicates",
        "retry_slots",
        "attempts",
    )
    counts = {
        key: int(sum(_number(cast_mapping(manifest.get("counts")), key) for manifest in manifests))
        for key in count_keys
    }
    lean = {
        key: sum(_number(cast_mapping(manifest.get("lean")), key) for manifest in manifests)
        for key in (
            "candidate_requests",
            "candidate_cache_hits",
            "candidate_executed",
            "candidate_elapsed_seconds",
        )
    }
    llm = {
        key: sum(_number(cast_mapping(manifest.get("llm")), key) for manifest in manifests)
        for key in (
            "proposer_calls",
            "proposer_cache_hits",
            "claude_calls",
            "claude_cache_hits",
            "nominal_cost_usd",
            "executed_cost_usd",
            "latency_seconds",
        )
    }
    roots = len(manifests)
    slots = roots * 4
    projected_scale = 50_000
    projected_factor = 0.0 if roots == 0 else projected_scale / roots
    quality: dict[str, object] = {
        "version": "leanfaith_sft2a_pilot_quality_manifest_v1",
        "sample_sha256": sample_manifest.get("sample_sha256"),
        "sample_manifest_sha256": hash_file(output / "sample_manifest.json"),
        "root_count": roots,
        "slot_count": slots,
        "source_mix": sample_manifest.get("source_mix"),
        "selected_roots": sample_manifest.get("selected_roots"),
        "counts": counts,
        "rates": {
            "accepted_per_slot": None if slots == 0 else counts["accepted"] / slots,
            "invalid_per_attempt": (
                None if counts["attempts"] == 0 else counts["invalid_attempts"] / counts["attempts"]
            ),
            "unknown_per_attempt": (
                None if counts["attempts"] == 0 else counts["unknown_rows"] / counts["attempts"]
            ),
            "judge_disagreement_per_attempt": (
                None
                if counts["attempts"] == 0
                else counts["judge_disagreements"] / counts["attempts"]
            ),
        },
        "lean": lean,
        "llm": llm,
        "persistent_provider_budget": dict(budget_snapshot),
        "cross_root_candidate_registry": dict(dedup_snapshot),
        "projected_50000_roots_from_bounded_pilot": {
            "is_linear_projection_not_authorization": True,
            "reported_opus_spend_usd": _number(budget_snapshot, "reported_opus_spend_usd")
            * projected_factor,
            "candidate_executed": lean["candidate_executed"] * projected_factor,
            "lean_elapsed_seconds": lean["candidate_elapsed_seconds"] * projected_factor,
        },
        "implementation": dict(implementation),
        "publication_allowed": False,
        "scale_50k_started": False,
    }
    report_lines = [
        "# SFT2A bounded pilot quality report",
        "",
        f"- Roots: {roots}; slots: {slots}; accepted: {counts['accepted']}.",
        (
            f"- Invalid attempts: {counts['invalid_attempts']}; unknown rows: "
            f"{counts['unknown_rows']}; judge disagreements: {counts['judge_disagreements']}."
        ),
        (
            f"- Lean candidate requests: {int(lean['candidate_requests'])}; executed: "
            f"{int(lean['candidate_executed'])}; elapsed: "
            f"{lean['candidate_elapsed_seconds']:.6f} seconds."
        ),
        (
            "- Persistent provider calls: "
            f"{budget_snapshot.get('unique_provider_calls')}; reported Opus spend: "
            f"${_number(budget_snapshot, 'reported_opus_spend_usd'):.6f}."
        ),
        "- The 50K figures are linear projections only; they do not authorize scaling.",
        "",
    ]
    report_path = output / "pilot_quality_report.md"
    _atomic_exact(report_path, "\n".join(report_lines).encode())
    quality["report_sha256"] = hash_file(report_path)
    _atomic_exact(output / "pilot_quality_manifest.json", canonical_json_bytes(quality) + b"\n")
    return quality


def run_multi_root_pilot(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
    *,
    proposer: StructuredProvider | None = None,
    opus_judge: StructuredProvider | None = None,
) -> dict[str, object]:
    """Run grouped roots with one persistent Lean environment per compile-context group."""

    if not isinstance(loaded.config, SFT2AOpusConfig):
        raise PilotError("diverse-root pilot requires the additive Opus config")
    require_pilot_authorization(readiness)
    identity = implementation_identity(loaded.repo_root)
    sample_manifest = prepare_pilot_sample(loaded, readiness, implementation=identity)
    output = _pilot_output(loaded, readiness)
    sample_rows = [json.loads(line) for line in (output / "sample.jsonl").read_text().splitlines()]
    if len(sample_rows) > readiness.config.ceilings.maximum_roots:
        raise PilotError("pilot root ceiling exceeded")
    budget = PersistentProviderBudget(
        output / "provider_budget_journal.jsonl", readiness.config.ceilings
    )
    proposer_client = BudgetedProvider(
        proposer or proposer_provider(loaded), kind="proposer", budget=budget
    )
    judge_client = BudgetedProvider(
        opus_judge or claude_judge_provider(loaded), kind="opus", budget=budget
    )
    candidate_registry = PersistentCandidateRegistry(output / "candidate_registry.jsonl")
    groups: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in sample_rows:
        root = cast_mapping(row.get("root"))
        context = cast_mapping(root.get("compile_context"))
        key = (
            str(context.get("project_id")),
            str(context.get("project_revision")),
            str(row.get("context_id")),
        )
        groups[key].append(row)
    root_manifests: list[dict[str, object]] = []
    root_manifest_paths: list[Path] = []
    for key in sorted(groups):
        group_rows = sorted(groups[key], key=lambda row: str(cast_mapping(row["root"])["root_id"]))
        first_root = OneRootConfig.model_validate(group_rows[0]["root"])
        first_config = loaded.config.model_copy(update={"root": first_root})
        first_loaded = replace(
            loaded,
            config=first_config,
            config_hash=hash_canonical(first_config.model_dump(mode="json")),
        )
        oracle = SignatureOracle(first_loaded)
        try:
            for row in group_rows:
                root_model = OneRootConfig.model_validate(row["root"])
                root_config = loaded.config.model_copy(update={"root": root_model})
                root_loaded = replace(
                    loaded,
                    config=root_config,
                    config_hash=hash_canonical(root_config.model_dump(mode="json")),
                )
                root_output = output / "roots" / key[0] / hash_canonical(root_model.root_id)[:16]
                result = run_one_root(
                    root_loaded,
                    proposer=proposer_client,
                    claude_judge=judge_client,
                    oracle=oracle,
                    output_root=root_output,
                    enforce_expected_reference_goal=False,
                    enforce_smoke_ceilings=False,
                    cross_root_registry=candidate_registry,
                )
                root_manifest_path = root_output / "manifest.json"
                root_manifest_paths.append(root_manifest_path)
                root_manifests.append(
                    {
                        "root_id": root_model.root_id,
                        "manifest_sha256": hash_file(root_manifest_path),
                        "replayed": result.replayed,
                    }
                )
        finally:
            oracle.close()
    budget_snapshot = budget.snapshot()
    dedup_snapshot = candidate_registry.snapshot()
    quality = consolidate_pilot_quality(
        output=output,
        sample_manifest=sample_manifest,
        root_manifest_paths=root_manifest_paths,
        implementation=identity,
        budget_snapshot=budget_snapshot,
        dedup_snapshot=dedup_snapshot,
    )
    manifest = {
        "version": "leanfaith_sft2a_diverse_root_pilot_v2",
        "readiness_config_hash": readiness.config_hash,
        "authorization_receipt_sha256": readiness.config.authorization_receipt.sha256,
        "sample_manifest_sha256": hash_file(output / "sample_manifest.json"),
        "sample_sha256": sample_manifest["sample_sha256"],
        "root_count": len(root_manifests),
        "persistent_provider_budget": budget_snapshot,
        "cross_root_candidate_registry": dedup_snapshot,
        "ceilings": readiness.config.ceilings.model_dump(mode="json"),
        "root_manifests": root_manifests,
        "pilot_quality_manifest_sha256": hash_file(output / "pilot_quality_manifest.json"),
        "pilot_quality_report_sha256": quality["report_sha256"],
        "implementation": identity,
        "published": False,
        "scale_50k_started": False,
    }
    _atomic_exact(output / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


__all__ = [
    "PilotError",
    "consolidate_pilot_quality",
    "prepare_pilot_sample",
    "run_multi_root_pilot",
]
