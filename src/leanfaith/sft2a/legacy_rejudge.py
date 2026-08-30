"""Deterministic REPR-ready legacy sampling and optional Opus rejudging."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.representations.goal_v1 import GoalV1Error, render_surface
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft2a.budget import BudgetedProvider, PersistentProviderBudget
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.legacy import _atomic_exact, _blocklist, _compile_context, _placeholder_reasons
from leanfaith.sft2a.models import CoreRow, JudgeOutput, SFT2AOpusConfig
from leanfaith.sft2a.pipeline import StructuredProvider
from leanfaith.sft2a.prompts import prompt_hash, render_blinded_judge_prompt
from leanfaith.sft2a.providers import claude_judge_provider
from leanfaith.sft2a.readiness import LoadedPilotReadiness, implementation_identity


class LegacyRejudgeError(RuntimeError):
    """Legacy sample lineage, authorization, ceiling, or judgment failed."""


def _rows(path: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LegacyRejudgeError(f"invalid JSONL at {path}:{number}: {exc}") from exc
        if not isinstance(value, dict):
            raise LegacyRejudgeError(f"non-object JSONL row at {path}:{number}")
        result.append(value)
    return result


def _jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _stratified_negative_indices(
    sidecars: Sequence[Mapping[str, object]], count: int, salt: str
) -> list[int]:
    strata: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for index, row in enumerate(sidecars):
        family = row.get("family")
        if not isinstance(family, str) or not family:
            raise LegacyRejudgeError("legacy sidecar lacks a family stratum")
        rank = hash_canonical({"salt": salt, "row_id": row.get("row_id")})
        strata[family].append((rank, index))
    if count > len(sidecars):
        raise LegacyRejudgeError("negative sample exceeds the eligible negative population")
    for rows in strata.values():
        rows.sort()
    total = len(sidecars)
    allocations: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for family, rows in strata.items():
        exact = count * len(rows) / total
        allocations[family] = int(exact)
        remainders.append((exact - int(exact), family))
    remaining = count - sum(allocations.values())
    for _remainder, family in sorted(remainders, key=lambda item: (-item[0], item[1]))[:remaining]:
        allocations[family] += 1
    selected: list[int] = []
    for family in sorted(strata):
        selected.extend(index for _rank, index in strata[family][: allocations[family]])
    if len(selected) != count:
        raise LegacyRejudgeError("stratified legacy negative sampler produced the wrong count")
    return sorted(selected)


def _output(loaded: LoadedSFT2AConfig, readiness: LoadedPilotReadiness) -> Path:
    pure = PurePosixPath(readiness.config.legacy_rejudge.output_subdir)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise LegacyRejudgeError("unsafe legacy rejudge output subdirectory")
    return Path(loaded.config.staging_root).joinpath(*pure.parts)


def prepare_legacy_opus_sample(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
    *,
    implementation: Mapping[str, str] | None = None,
    output_root: Path | None = None,
) -> dict[str, object]:
    """Materialize binary calls and unresolved auxiliary rows without provider calls."""

    if not isinstance(loaded.config, SFT2AOpusConfig):
        raise LegacyRejudgeError("legacy Opus sampling requires the additive Opus config")
    source = Path(loaded.config.staging_root) / "legacy_import_v1"
    source_manifest = source / "manifest.json"
    core = _rows(source / "legacy_single_judge/core.jsonl")
    sidecars = _rows(source / "legacy_single_judge/sidecar.jsonl")
    if len(core) != len(sidecars):
        raise LegacyRejudgeError("legacy core and sidecar counts differ")
    paired = list(zip(core, sidecars, strict=True))
    positives = [(row, sidecar) for row, sidecar in paired if row.get("label") is True]
    negatives = [(row, sidecar) for row, sidecar in paired if row.get("label") is False]
    policy = readiness.config.legacy_rejudge
    if len(positives) != policy.all_admitted_positives:
        raise LegacyRejudgeError("legacy admitted-positive population differs from the freeze")
    negative_sidecars = [sidecar for _row, sidecar in negatives]
    negative_indices = _stratified_negative_indices(
        negative_sidecars, policy.minimum_stratified_negatives, policy.salt
    )
    selected_pairs = positives + [negatives[index] for index in negative_indices]
    sample: list[dict[str, object]] = []
    for core_row, sidecar in selected_pairs:
        row_id = sidecar.get("row_id")
        if not isinstance(row_id, str):
            raise LegacyRejudgeError("selected legacy row lacks a stable row ID")
        sample.append(
            {
                "row_id": row_id,
                "selection_kind": (
                    "all_admitted_positive"
                    if core_row.get("label") is True
                    else "deterministic_stratified_admitted_negative"
                ),
                "reference": core_row.get("reference"),
                "candidate": core_row.get("candidate"),
                "legacy_label": core_row.get("label"),
                "family": sidecar.get("family"),
                "record_id": sidecar.get("record_id"),
            }
        )

    blocked_path, blocked_hashes = _blocklist(loaded)
    context = _compile_context(loaded)
    unresolved_auxiliary: list[dict[str, object]] = []
    for unknown in _rows(source / "legacy_unknown/rows.jsonl"):
        reference = unknown.get("reference_headless")
        candidate = unknown.get("candidate_headless")
        if not isinstance(reference, str) or not isinstance(candidate, str):
            raise LegacyRejudgeError("legacy unknown lacks source signatures")
        if _placeholder_reasons(reference, candidate):
            continue
        try:
            reference_goal = render_surface(
                raw_statement=reference,
                parsed_signature=reference,
                declaration_kind="theorem",
                compile_context=context,
            ).core_text()
            candidate_goal = render_surface(
                raw_statement=candidate,
                parsed_signature=candidate,
                declaration_kind="theorem",
                compile_context=context,
            ).core_text()
        except (GoalV1Error, ValueError):
            continue
        hashes = {
            signature_near_dup_hash(reference),
            signature_near_dup_hash(candidate),
            signature_near_dup_hash(reference_goal),
            signature_near_dup_hash(candidate_goal),
        }
        if hashes & blocked_hashes:
            continue
        row_id = "sft2a_legacy_unknown:" + hash_canonical(
            {
                "source_tree": loaded.config.legacy.immutable_tree_sha256,
                "record_id": unknown.get("record_id"),
                "reference": reference,
                "candidate": candidate,
            }
        )
        unresolved_auxiliary.append(
            {
                "row_id": row_id,
                "configuration": "legacy_single_judge_needs_second_judge",
                "selection_kind": "all_renderable_unresolved",
                "reference": reference_goal,
                "candidate": candidate_goal,
                "legacy_label": None,
                "family": "unresolved",
                "record_id": unknown.get("record_id"),
                "provider_call_allowed": False,
                "training_eligible": False,
                "reason": "no_prior_binary_label_do_not_pay_opus_only_to_discard",
            }
        )
    sample.sort(key=lambda row: (str(row["selection_kind"]), str(row["row_id"])))
    unresolved_auxiliary.sort(key=lambda row: str(row["row_id"]))
    output = output_root or _output(loaded, readiness)
    sample_path = output / "sample.jsonl"
    auxiliary_path = output / "needs_second_judge/rows.jsonl"
    _atomic_exact(sample_path, _jsonl(sample))
    _atomic_exact(auxiliary_path, _jsonl(unresolved_auxiliary))
    identity = dict(implementation or implementation_identity(loaded.repo_root))
    manifest = {
        "version": "leanfaith_sft2a_legacy_opus_sample_v2",
        "readiness_config_hash": readiness.config_hash,
        "source_configuration": "legacy_single_judge",
        "provider_call_configuration": "legacy_double_judge",
        "unresolved_auxiliary_configuration": "legacy_single_judge_needs_second_judge",
        "source_manifest_sha256": hash_file(source_manifest),
        "gold_blocklist_sha256": hash_file(blocked_path),
        "selection_salt": policy.salt,
        "counts": {
            "all_admitted_positives": len(positives),
            "stratified_admitted_negatives": len(negative_indices),
            "provider_call_rows": len(sample),
            "renderable_unresolved_auxiliary_no_call": len(unresolved_auxiliary),
            "total_tracked_rows": len(sample) + len(unresolved_auxiliary),
        },
        "excluded_populations": ["legacy_placeholder_audit", "invalid", "contamination"],
        "sample_sha256": hash_file(sample_path),
        "unresolved_auxiliary_sha256": hash_file(auxiliary_path),
        "provider_calls_executed": 0,
        "legacy_rejudge_authorized": policy.authorized,
        "implementation": identity,
    }
    _atomic_exact(output / "sample_manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


def _require_legacy_authorization(readiness: LoadedPilotReadiness) -> None:
    if readiness.config.legacy_rejudge.authorized is not True:
        raise LegacyRejudgeError("legacy Opus bulk rejudging is not authorized")
    if readiness.authorization.get("legacy_rejudge_authorized") is not True:
        raise LegacyRejudgeError("hash-bound receipt does not authorize legacy rejudging")


def run_legacy_opus_rejudge(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
    *,
    judge: StructuredProvider | None = None,
) -> dict[str, object]:
    """Execute binary legacy rows only after additive hash-bound authorization."""

    if not isinstance(loaded.config, SFT2AOpusConfig):
        raise LegacyRejudgeError("legacy Opus rejudge requires the additive Opus config")
    _require_legacy_authorization(readiness)
    identity = implementation_identity(loaded.repo_root)
    prepare_legacy_opus_sample(loaded, readiness, implementation=identity)
    output = _output(loaded, readiness)
    sample = _rows(output / "sample.jsonl")
    ceiling = readiness.config.legacy_rejudge.ceilings
    if len(sample) > ceiling.maximum_opus_calls or len(sample) > ceiling.maximum_provider_calls:
        raise LegacyRejudgeError("legacy sample exceeds a frozen provider-call ceiling")
    budget = PersistentProviderBudget(output / "provider_budget_journal.jsonl", ceiling)
    client = BudgetedProvider(judge or claude_judge_provider(loaded), kind="opus", budget=budget)
    core: list[dict[str, object]] = []
    sidecar: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    for row in sample:
        legacy_label = row.get("legacy_label")
        if type(legacy_label) is not bool:
            raise LegacyRejudgeError("legacy rejudge sample contains a non-binary source label")
        prompt = render_blinded_judge_prompt(
            loaded,
            statement_a=str(row["reference"]),
            statement_b=str(row["candidate"]),
        )
        call = client.call(prompt=prompt, input_ids=(str(row["row_id"]), "legacy_opus_rejudge_v2"))
        judgment = JudgeOutput.model_validate(call.structured)
        opus_label = (
            True
            if judgment.verdict == "equivalent"
            else False
            if judgment.verdict == "non_equivalent"
            else None
        )
        agrees = opus_label == legacy_label
        trace = {
            **row,
            "opus_judgment": judgment.model_dump(mode="json"),
            "opus_provider": loaded.config.claude_judge.model_dump(mode="json"),
            "call_key": call.call_key,
            "prompt_hash": prompt_hash(prompt),
            "usage": call.usage,
            "cost_usd": call.cost_usd,
            "agrees_with_legacy_label": agrees,
        }
        if agrees:
            core.append(
                CoreRow(
                    reference=str(row["reference"]),
                    candidate=str(row["candidate"]),
                    label=legacy_label,
                ).model_dump(mode="json")
            )
            sidecar.append({**trace, "configuration": "legacy_double_judge"})
        else:
            excluded.append({**trace, "training_eligible": False})
    _atomic_exact(output / "legacy_double_judge/core.jsonl", _jsonl(core))
    _atomic_exact(output / "legacy_double_judge/sidecar.jsonl", _jsonl(sidecar))
    _atomic_exact(output / "excluded/rows.jsonl", _jsonl(excluded))
    budget_snapshot = budget.snapshot()
    manifest = {
        "version": "leanfaith_sft2a_legacy_double_judge_v2",
        "readiness_config_hash": readiness.config_hash,
        "source_sample_sha256": hash_file(output / "sample.jsonl"),
        "source_sample_manifest_sha256": hash_file(output / "sample_manifest.json"),
        "provider": loaded.config.claude_judge.model_dump(mode="json"),
        "prompt_artifact": loaded.config.prompts.blinded_claude_judge.model_dump(mode="json"),
        "persistent_provider_budget": budget_snapshot,
        "ceilings": ceiling.model_dump(mode="json"),
        "accepted_agreements": len(core),
        "excluded": len(excluded),
        "unresolved_auxiliary_provider_calls": 0,
        "implementation": identity,
        "publication_allowed": False,
    }
    _atomic_exact(output / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


__all__ = [
    "LegacyRejudgeError",
    "prepare_legacy_opus_sample",
    "run_legacy_opus_rejudge",
]
