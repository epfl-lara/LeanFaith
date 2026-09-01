"""Blinded, budget-shared Lemex audit over a completed SFT2A pilot."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a.budget import BudgetedProvider, PersistentProviderBudget
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.dedup import PersistentCandidateRegistry
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.models import (
    CoreRow,
    JudgeOutput,
    JudgeOutputV5,
    SFT2AProductionConfig,
    SFT2AV5Config,
)
from leanfaith.sft2a.pilot import (
    _pilot_output,
    cast_mapping,
    consolidate_pilot_quality,
)
from leanfaith.sft2a.pipeline import StructuredProvider
from leanfaith.sft2a.prompts import prompt_hash, render_blinded_judge_prompt
from leanfaith.sft2a.providers import lemex_audit_provider
from leanfaith.sft2a.readiness import LoadedPilotReadiness, implementation_identity

_AUDIT_VERSION = "sft2a_pilot_lemex_stratified_10pct_cap8_v1"
_AUDIT_VERSION_V5 = "sft2a_rehearsal_lemex_stratified_min40_v5"


class PilotAuditError(RuntimeError):
    """Pilot replay, audit selection, budget, join, or release materialization failed."""


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotAuditError(f"invalid pilot audit JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PilotAuditError(f"pilot audit JSON root is not an object: {path}")
    return value


def _rows(path: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PilotAuditError(f"cannot read pilot audit JSONL {path}: {exc}") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PilotAuditError(f"invalid JSONL at {path}:{number}: {exc}") from exc
        if not isinstance(value, dict):
            raise PilotAuditError(f"non-object JSONL row at {path}:{number}")
        result.append(value)
    return result


def _jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _usage_summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    totals: dict[str, float] = {}
    reported_cost = 0.0
    reported_cost_rows = 0
    unavailable_cost_rows = 0
    for row in rows:
        usage = row.get("usage")
        if isinstance(usage, dict):
            for key, value in usage.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, int | float)
                    and not isinstance(value, bool)
                ):
                    totals[key] = totals.get(key, 0.0) + float(value)
        cost = row.get("cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            reported_cost += float(cost)
            reported_cost_rows += 1
        else:
            unavailable_cost_rows += 1
    return {
        "usage_totals": dict(sorted(totals.items())),
        "reported_cost_usd": reported_cost,
        "reported_cost_rows": reported_cost_rows,
        "unavailable_cost_rows": unavailable_cost_rows,
        "cost_limitation": "lemex_cost_unavailable",
    }


def pilot_audit_indices(
    sidecars: Sequence[Mapping[str, object]],
    *,
    fraction: float = 0.1,
    cap: int = 8,
    selection_version: str = _AUDIT_VERSION,
) -> tuple[list[int], dict[str, int]]:
    """Freeze a stratified 10% selection with deterministic cap-aware round robin."""

    strata: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for index, row in enumerate(sidecars):
        row_id = row.get("row_id")
        polarity = row.get("requested_polarity")
        claude = row.get("claude_judge")
        if (
            not isinstance(row_id, str)
            or not isinstance(polarity, str)
            or not isinstance(claude, dict)
            or not isinstance(claude.get("verdict"), str)
        ):
            raise PilotAuditError("pilot sidecar lacks audit strata or stable row ID")
        key = (polarity, str(claude["verdict"]))
        rank = hash_canonical({"audit": selection_version, "row_id": row_id})
        strata.setdefault(key, []).append((rank, index))
    targets: dict[tuple[str, str], int] = {}
    for key, ranked in strata.items():
        ranked.sort()
        targets[key] = max(1, math.ceil(len(ranked) * fraction))
    selected: list[int] = []
    allocations = dict.fromkeys(strata, 0)
    while len(selected) < cap:
        advanced = False
        for key in sorted(strata):
            offset = allocations[key]
            if offset >= targets[key]:
                continue
            selected.append(strata[key][offset][1])
            allocations[key] += 1
            advanced = True
            if len(selected) == cap:
                break
        if not advanced:
            break
    named_allocations = {f"{key[0]}+{key[1]}": allocations[key] for key in sorted(allocations)}
    return sorted(selected), named_allocations


def _combined_rows(
    output: Path,
    pilot_manifest: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[Path]]:
    references = pilot_manifest.get("root_manifests")
    if not isinstance(references, list):
        raise PilotAuditError("pilot manifest lacks root receipts")
    core: list[dict[str, object]] = []
    sidecars: list[dict[str, object]] = []
    root_manifest_paths: list[Path] = []
    seen_ids: set[str] = set()
    for reference in sorted(
        references,
        key=lambda item: str(item.get("root_id")) if isinstance(item, dict) else "",
    ):
        if not isinstance(reference, dict):
            raise PilotAuditError("pilot root receipt is malformed")
        relative = reference.get("manifest_path")
        if not isinstance(relative, str):
            raise PilotAuditError("pilot root receipt lacks a manifest path")
        manifest_path = output / relative
        if hash_file(manifest_path) != reference.get("manifest_sha256"):
            raise PilotAuditError("pilot root manifest hash differs")
        root_manifest_paths.append(manifest_path)
        root = manifest_path.parent
        root_core = _rows(root / "new_core/core.jsonl")
        root_sidecars = _rows(root / "new_core/sidecar.jsonl")
        if len(root_core) != len(root_sidecars):
            raise PilotAuditError("pilot root core and sidecar counts differ")
        for core_row, sidecar in zip(root_core, root_sidecars, strict=True):
            row_id = sidecar.get("row_id")
            if not isinstance(row_id, str) or row_id in seen_ids:
                raise PilotAuditError("pilot accepted rows lack unique stable IDs")
            seen_ids.add(row_id)
            core.append(CoreRow.model_validate(core_row).model_dump(mode="json"))
            sidecars.append(sidecar)
    return core, sidecars, root_manifest_paths


def _validate_existing_audit(output: Path, manifest: Mapping[str, object]) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise PilotAuditError("pilot audit manifest lacks artifacts")
    for relative, receipt in artifacts.items():
        if not isinstance(relative, str) or not isinstance(receipt, dict):
            raise PilotAuditError("pilot audit artifact receipt is malformed")
        audit_subdir = str(manifest.get("audit_output_subdir", "audit_lemex_v1"))
        if hash_file(output / audit_subdir / relative) != receipt.get("sha256"):
            raise PilotAuditError(f"pilot audit artifact differs: {relative}")
    quality = _object(output / "pilot_quality_manifest.json")
    audit = quality.get("audit")
    if not isinstance(audit, dict) or audit.get("manifest_sha256") != hash_file(
        output / str(manifest.get("audit_output_subdir", "audit_lemex_v1")) / "manifest.json"
    ):
        raise PilotAuditError("consolidated pilot quality audit hash differs")


def run_pilot_lemex_audit(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
    *,
    auditor: StructuredProvider | None = None,
) -> dict[str, object]:
    """Audit combined accepted pilot rows only after zero-execution pilot replay."""

    config = loaded.config
    if not isinstance(config, SFT2AProductionConfig):
        raise PilotAuditError("pilot audit requires the production-default config")
    output = _pilot_output(loaded, readiness)
    pilot_manifest_path = output / "manifest.json"
    replay_path = output / "pilot_reproducibility_receipt.json"
    pilot_manifest = _object(pilot_manifest_path)
    replay = _object(replay_path)
    if (
        replay.get("reproducible") is not True
        or replay.get("provider_calls_executed") != 0
        or replay.get("lean_requests_executed") != 0
        or replay.get("pilot_manifest_sha256") != hash_file(pilot_manifest_path)
    ):
        raise PilotAuditError("pilot Lemex audit requires the successful replay receipt")
    v5_config = config if isinstance(config, SFT2AV5Config) else None
    closure_aware = v5_config is not None
    audit_output_subdir = "audit_lemex_v5" if closure_aware else "audit_lemex_v1"
    audit_root = output / audit_output_subdir
    manifest_path = audit_root / "manifest.json"
    if manifest_path.exists():
        existing_manifest = _object(manifest_path)
        _validate_existing_audit(output, existing_manifest)
        return existing_manifest
    core, sidecars, root_manifest_paths = _combined_rows(output, pilot_manifest)
    maximum_calls = (
        v5_config.rehearsal.maximum_kimi_audits
        if v5_config is not None
        else readiness.config.ceilings.maximum_lemex_calls
    )
    selected, allocations = pilot_audit_indices(
        sidecars,
        fraction=loaded.config.audit.fraction,
        cap=maximum_calls,
        selection_version=_AUDIT_VERSION_V5 if closure_aware else _AUDIT_VERSION,
    )
    if not closure_aware and len(selected) > 8:
        raise PilotAuditError("pilot audit exceeds the frozen eight-call cap")
    if v5_config is not None and len(selected) < v5_config.rehearsal.minimum_kimi_audits:
        raise PilotAuditError("v5 rehearsal audit selected fewer than forty rows")
    budget = PersistentProviderBudget(
        output / "provider_budget_journal.jsonl", readiness.config.ceilings
    )
    client = BudgetedProvider(auditor or lemex_audit_provider(loaded), kind="lemex", budget=budget)
    audit_rows: list[dict[str, object]] = []
    unknown_review: list[dict[str, object]] = []
    checkpoint_receipts: dict[str, str] = {}
    for index in selected:
        row = sidecars[index]
        reference_repr = cast_mapping(row.get("reference_repr"))
        candidate_repr = cast_mapping(row.get("candidate_repr"))
        reference_record = cast_mapping(reference_repr.get("record"))
        candidate_record = cast_mapping(candidate_repr.get("record"))
        statement_a = reference_record.get("goal_v1")
        statement_b = candidate_record.get("goal_v1")
        if not isinstance(statement_a, str) or not isinstance(statement_b, str):
            raise PilotAuditError("pilot audit REPR goal is not text")
        prompt = render_blinded_judge_prompt(
            loaded,
            statement_a=statement_a,
            statement_b=statement_b,
        )
        claude = cast_mapping(row.get("claude_judge"))
        rendered_prompt_hash = prompt_hash(prompt)
        checkpoint_path = audit_root / "audit/checkpoints" / f"{hash_canonical(row['row_id'])}.json"
        if checkpoint_path.is_file():
            audit_row = _object(checkpoint_path)
            expected_checkpoint = {
                "row_id": row["row_id"],
                "requested_polarity": row["requested_polarity"],
                "claude_verdict": claude.get("verdict"),
                "prompt_hash": rendered_prompt_hash,
                "provider_id": loaded.config.lemex_auditor.provider_id,
            }
            if any(audit_row.get(key) != value for key, value in expected_checkpoint.items()):
                raise PilotAuditError("pilot audit checkpoint lineage differs")
            malformed_final = audit_row.get("malformed_exhausted") is True
            if malformed_final:
                judgment = None
                agrees = False
            else:
                judgment = (
                    JudgeOutputV5.model_validate(audit_row.get("lemex_judgment"))
                    if closure_aware
                    else JudgeOutput.model_validate(audit_row.get("lemex_judgment"))
                )
                agrees = judgment.verdict == claude.get("verdict")
                if audit_row.get("agrees") is not agrees:
                    raise PilotAuditError("pilot audit checkpoint verdict differs")
        else:
            if closure_aware:
                assert v5_config is not None
                consistent = call_consistent_judge(
                    client,
                    prompt=prompt,
                    input_ids=(str(row["row_id"]), "blinded_rehearsal_lemex_audit_v5"),
                    closure_aware=True,
                    malformed_retries=v5_config.rehearsal.malformed_audit_retries,
                )
                call = consistent.calls[-1]
                judgment = consistent.judgment
            else:
                call = client.call(
                    prompt=prompt,
                    input_ids=(str(row["row_id"]), "blinded_pilot_lemex_audit_v1"),
                )
                judgment = JudgeOutput.model_validate(call.structured)
                consistent = None
            agrees = judgment is not None and judgment.verdict == claude.get("verdict")
            exhausted = judgment is None
            audit_row = {
                "row_id": row["row_id"],
                "requested_polarity": row["requested_polarity"],
                "claude_verdict": claude.get("verdict"),
                "lemex_judgment": (None if judgment is None else judgment.model_dump(mode="json")),
                "agrees": agrees,
                "action": (
                    "retain"
                    if agrees
                    else "malformed_unknown_review_exclude_core"
                    if exhausted
                    else "unknown_review_exclude_core"
                ),
                "call_key": call.call_key,
                "provider_id": call.provider_id,
                "prompt_hash": rendered_prompt_hash,
                "usage": call.usage,
                "cost_usd": call.cost_usd,
                "elapsed_seconds": call.elapsed_seconds,
                "malformed_attempts": (
                    [] if consistent is None else list(consistent.malformed_attempts)
                ),
                "malformed_retries": 0 if consistent is None else consistent.malformed_retries,
                "malformed_exhausted": exhausted,
            }
            _atomic_exact(checkpoint_path, canonical_json_bytes(audit_row) + b"\n")
        checkpoint_receipts[str(row["row_id"])] = hash_file(checkpoint_path)
        audit_rows.append(audit_row)
        if not agrees:
            unknown_review.append({**audit_row, "training_eligible": False})
    malformed_attempts = sum(
        len(value) if isinstance(value, list) else 0
        for row in audit_rows
        for value in (row.get("malformed_attempts"),)
    )
    malformed_retries = sum(
        int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
        for row in audit_rows
        for value in (row.get("malformed_retries"),)
    )
    malformed_exhausted = sum(row.get("malformed_exhausted") is True for row in audit_rows)
    genuine_disagreements = sum(
        row.get("agrees") is False and row.get("malformed_exhausted") is not True
        for row in audit_rows
    )
    checkpoints_manifest = {
        "version": (
            "leanfaith_sft2a_rehearsal_audit_checkpoints_v5"
            if closure_aware
            else "leanfaith_sft2a_pilot_audit_checkpoints_v1"
        ),
        "selected_row_ids": [str(sidecars[index]["row_id"]) for index in selected],
        "receipts": checkpoint_receipts,
    }
    _atomic_exact(
        audit_root / "audit/checkpoints_manifest.json",
        canonical_json_bytes(checkpoints_manifest) + b"\n",
    )
    _atomic_exact(audit_root / "audit/rows.jsonl", _jsonl(audit_rows))
    _atomic_exact(audit_root / "unknown_review/rows.jsonl", _jsonl(unknown_review))
    excluded = {str(row["row_id"]) for row in unknown_review}
    released_core: list[dict[str, object]] = []
    released_sidecars: list[dict[str, object]] = []
    for core_row, sidecar in zip(core, sidecars, strict=True):
        row_id = str(sidecar["row_id"])
        if row_id in excluded:
            continue
        released_core.append(core_row)
        released_sidecars.append(
            {
                "row_id": row_id,
                "source_pilot_manifest_sha256": hash_file(pilot_manifest_path),
            }
        )
    _atomic_exact(audit_root / "releasable_core/core.jsonl", _jsonl(released_core))
    _atomic_exact(audit_root / "releasable_core/sidecar.jsonl", _jsonl(released_sidecars))
    release_manifest = {
        "version": (
            "leanfaith_sft2a_rehearsal_post_audit_core_v5"
            if closure_aware
            else "leanfaith_sft2a_pilot_post_audit_core_v1"
        ),
        "source_pilot_manifest_sha256": hash_file(pilot_manifest_path),
        "pilot_replay_receipt_sha256": hash_file(replay_path),
        "audit_rows_sha256": hash_file(audit_root / "audit/rows.jsonl"),
        "source_rows": len(core),
        "excluded_disagreement_row_ids": sorted(excluded),
        "released_rows": len(released_core),
        "schema": ["reference", "candidate", "label"],
        "artifacts": {
            "core.jsonl": hash_file(audit_root / "releasable_core/core.jsonl"),
            "sidecar.jsonl": hash_file(audit_root / "releasable_core/sidecar.jsonl"),
        },
    }
    _atomic_exact(
        audit_root / "releasable_core/manifest.json",
        canonical_json_bytes(release_manifest) + b"\n",
    )
    disagreements = genuine_disagreements
    budget_snapshot = budget.snapshot()
    rendered_prompt_hashes = [str(row["prompt_hash"]) for row in audit_rows]
    manifest: dict[str, object] = {
        "version": (
            "leanfaith_sft2a_combined_rehearsal_lemex_audit_v5"
            if closure_aware
            else "leanfaith_sft2a_combined_pilot_lemex_audit_v1"
        ),
        "config_hash": loaded.config_hash,
        "readiness_config_hash": readiness.config_hash,
        "source_pilot_manifest_sha256": hash_file(pilot_manifest_path),
        "pilot_replay_receipt_sha256": hash_file(replay_path),
        "selection_version": _AUDIT_VERSION_V5 if closure_aware else _AUDIT_VERSION,
        "target_fraction": loaded.config.audit.fraction,
        "maximum_calls": maximum_calls,
        "population_rows": len(sidecars),
        "selected_rows": len(selected),
        "selected_row_ids": [str(sidecars[index]["row_id"]) for index in selected],
        "stratum_allocations": allocations,
        "selection_sha256": hash_canonical([str(sidecars[index]["row_id"]) for index in selected]),
        "agreements": sum(bool(row.get("agrees")) for row in audit_rows),
        "disagreements": disagreements,
        "malformed_attempts": malformed_attempts,
        "malformed_retries": malformed_retries,
        "malformed_exhausted": malformed_exhausted,
        "agreement_rate_after_malformed_retries": (
            0.0
            if not audit_rows
            else sum(bool(row.get("agrees")) for row in audit_rows) / len(audit_rows)
        ),
        "providers": {
            "opus_source_judge": config.claude_judge.model_dump(mode="json"),
            "lemex_auditor": config.lemex_auditor.model_dump(mode="json"),
        },
        "labeling_defaults_policy": config.labeling_defaults_policy.model_dump(mode="json"),
        "prompt": {
            "template": config.prompts.blinded_claude_judge.model_dump(mode="json"),
            "rendered_prompt_hashes": rendered_prompt_hashes,
            "rendered_prompt_set_sha256": hash_canonical(rendered_prompt_hashes),
        },
        "usage_and_cost": _usage_summary(audit_rows),
        "source_run": {
            "pilot_manifest_sha256": hash_file(pilot_manifest_path),
            "pilot_replay_receipt_sha256": hash_file(replay_path),
            "config_hash": loaded.config_hash,
            "readiness_config_hash": readiness.config_hash,
        },
        "persistent_provider_budget": budget_snapshot,
        "systematic_disagreement_blocks_scale": disagreements > 0,
        "malformed_output_blocks_scale": malformed_exhausted > 0,
        "scale_gate": (
            "blocked_disagreement"
            if disagreements
            else "blocked_malformed_audit"
            if malformed_exhausted
            else "audit_passed"
        ),
        "audit_output_subdir": audit_output_subdir,
        "artifacts": {
            "audit/rows.jsonl": {
                "sha256": hash_file(audit_root / "audit/rows.jsonl"),
                "rows": len(audit_rows),
            },
            "audit/checkpoints_manifest.json": {
                "sha256": hash_file(audit_root / "audit/checkpoints_manifest.json"),
                "rows": len(checkpoint_receipts),
            },
            "unknown_review/rows.jsonl": {
                "sha256": hash_file(audit_root / "unknown_review/rows.jsonl"),
                "rows": len(unknown_review),
            },
            "releasable_core/manifest.json": {
                "sha256": hash_file(audit_root / "releasable_core/manifest.json"),
                "rows": len(released_core),
            },
        },
        "scale_50k_started": False,
        "published": False,
    }
    _atomic_exact(manifest_path, canonical_json_bytes(manifest) + b"\n")
    sample_manifest = _object(output / "sample_manifest.json")
    identity = implementation_identity(loaded.repo_root)
    consolidate_pilot_quality(
        output=output,
        sample_manifest=sample_manifest,
        root_manifest_paths=root_manifest_paths,
        implementation=identity,
        budget_snapshot=budget_snapshot,
        dedup_snapshot=PersistentCandidateRegistry(output / "candidate_registry.jsonl").snapshot(),
        audit_manifest=manifest,
        artifact_stem="pilot_quality",
    )
    return manifest


__all__ = ["PilotAuditError", "pilot_audit_indices", "run_pilot_lemex_audit"]
