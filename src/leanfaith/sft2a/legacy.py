"""Deterministic, Lean-free adapter for the accepted legacy SFT2A tranche."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.representations.goal_v1 import CompileContext, GoalV1Error, render_surface
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.models import CoreRow


class LegacyAdapterError(RuntimeError):
    """Legacy inputs, counts, or deterministic outputs differ from the freeze."""


@dataclass(frozen=True, slots=True)
class LegacyAdapterResult:
    output_root: Path
    manifest: dict[str, object]
    replayed: bool


def _json_line(line: str, *, path: Path, line_number: int) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(line, object_pairs_hook=pairs)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LegacyAdapterError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
    if not isinstance(value, dict):
        raise LegacyAdapterError(f"JSONL row is not an object at {path}:{line_number}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    try:
        with path.open(encoding="utf-8") as handle:
            return [
                _json_line(line, path=path, line_number=index)
                for index, line in enumerate(handle, start=1)
                if line.strip()
            ]
    except OSError as exc:
        raise LegacyAdapterError(f"cannot read legacy JSONL {path}: {exc}") from exc


def _canonical_jsonl(rows: Iterable[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _atomic_exact(path: Path, payload: bytes) -> bool:
    """Write atomically, accept an identical replay, and reject a conflict."""

    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise LegacyAdapterError(f"output path is unsafe: {path}")
        if path.read_bytes() != payload:
            raise LegacyAdapterError(f"immutable output conflict: {path}")
        return True
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
    return False


def _require_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise LegacyAdapterError(f"legacy row has missing/non-text {key}")
    return value


def _placeholder_reasons(reference: str, candidate: str) -> tuple[str, ...]:
    combined = reference + "\n" + candidate
    reasons: list[str] = []
    if "[anonymous]" in combined.casefold():
        reasons.append("forbidden_anonymous_placeholder")
    if "⋯" in combined:
        reasons.append("forbidden_unicode_ellipsis_placeholder")
    return tuple(reasons)


def _compile_context(loaded: LoadedSFT2AConfig) -> CompileContext:
    source = loaded.config.root.compile_context
    return CompileContext(
        project_id=source.project_id,
        project_revision=source.project_revision,
        lean_version=source.lean_version,
        import_header=source.import_header,
        command_preamble=source.command_preamble,
        namespace_context=source.namespace_context,
        open_context=source.open_context,
        scoped_context=source.scoped_context,
        options=source.options,
    )


def _blocklist(loaded: LoadedSFT2AConfig) -> tuple[Path, set[str]]:
    policy = loaded.config.gold_screen
    path = loaded.repo_root / policy.path
    if hash_file(path) != policy.sha256:
        raise LegacyAdapterError("golden blocklist hash differs from the frozen SFT2A pin")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyAdapterError(f"cannot load golden blocklist: {exc}") from exc
    hashes = document.get("near_dup_hashes") if isinstance(document, dict) else None
    groups = document.get("group_keys") if isinstance(document, dict) else None
    if (
        not isinstance(hashes, list)
        or any(not isinstance(value, str) for value in hashes)
        or len(hashes) != policy.near_dup_hash_count
        or not isinstance(groups, list)
        or any(not isinstance(value, str) for value in groups)
        or len(groups) != policy.group_key_count
    ):
        raise LegacyAdapterError("golden blocklist near_dup_hashes is malformed")
    return path, set(hashes)


def adapt_legacy(loaded: LoadedSFT2AConfig) -> LegacyAdapterResult:
    """Screen, deduplicate, surface-render, and separate the legacy configurations."""

    recipe = loaded.config.legacy
    trainer_path = Path(recipe.trainer_records_path)
    judgments_path = Path(recipe.judgments_path)
    pair_plan_path = Path(recipe.pair_plan_path)
    source_hashes_before = {
        str(trainer_path): hash_file(trainer_path),
        str(judgments_path): hash_file(judgments_path),
        str(pair_plan_path): hash_file(pair_plan_path),
    }
    expected_hashes = {
        str(trainer_path): recipe.trainer_records_sha256,
        str(judgments_path): recipe.judgments_sha256,
        str(pair_plan_path): recipe.pair_plan_sha256,
    }
    if source_hashes_before != expected_hashes:
        raise LegacyAdapterError("legacy source hashes differ from the accepted recipe")

    output_root = Path(loaded.config.staging_root) / "legacy_import_v1"
    existing_manifest_path = output_root / "manifest.json"
    if existing_manifest_path.is_file():
        try:
            existing = json.loads(existing_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LegacyAdapterError(f"invalid legacy replay manifest: {exc}") from exc
        if not isinstance(existing, dict) or not isinstance(existing.get("artifacts"), dict):
            raise LegacyAdapterError("legacy replay manifest is malformed")
        _blocklist(loaded)
        for relative, receipt in existing["artifacts"].items():
            if not isinstance(relative, str) or not isinstance(receipt, dict):
                raise LegacyAdapterError("legacy replay artifact receipt is malformed")
            path = output_root / relative
            if not path.is_file() or hash_file(path) != receipt.get("sha256"):
                raise LegacyAdapterError(f"legacy replay artifact differs: {relative}")
        return LegacyAdapterResult(output_root=output_root, manifest=existing, replayed=True)

    trainer = _read_jsonl(trainer_path)
    judgments = _read_jsonl(judgments_path)
    pair_plan = _read_jsonl(pair_plan_path)
    if len(trainer) != recipe.resolved_rows_before_dedup:
        raise LegacyAdapterError("legacy resolved-row count differs")
    if len(judgments) != recipe.gross_rows or len(pair_plan) != recipe.gross_rows:
        raise LegacyAdapterError("legacy gross judgment/pair-plan counts differ")

    pair_counts: Counter[tuple[str, str]] = Counter()
    chosen: dict[tuple[str, str], dict[str, object]] = {}
    for row in trainer:
        reference = _require_text(row, "reference_headless")
        candidate = _require_text(row, "candidate_headless")
        record_id = _require_text(row, "record_id")
        label = row.get("label")
        if type(label) is not bool:
            raise LegacyAdapterError(f"legacy record {record_id} has a non-bool label")
        pair = (reference, candidate)
        pair_counts[pair] += 1
        previous = chosen.get(pair)
        if previous is None or record_id < _require_text(previous, "record_id"):
            chosen[pair] = row
    duplicate_excess = sum(count - 1 for count in pair_counts.values())
    if duplicate_excess != recipe.directed_duplicate_excess_rows:
        raise LegacyAdapterError("legacy directed duplicate count differs")
    kept = sorted(chosen.values(), key=lambda row: _require_text(row, "record_id"))

    placeholder_rows: list[dict[str, object]] = []
    eligible: list[dict[str, object]] = []
    for row in kept:
        reference = _require_text(row, "reference_headless")
        candidate = _require_text(row, "candidate_headless")
        reasons = _placeholder_reasons(reference, candidate)
        if reasons:
            placeholder_rows.append(
                {
                    "record_id": _require_text(row, "record_id"),
                    "reference_headless": reference,
                    "candidate_headless": candidate,
                    "legacy_label": row["label"],
                    "reasons": list(reasons),
                    "training_eligible": False,
                }
            )
        else:
            eligible.append(row)
    reason_sets: list[set[str]] = []
    for row in placeholder_rows:
        reason_values = row.get("reasons")
        if not isinstance(reason_values, list) or any(
            not isinstance(reason, str) for reason in reason_values
        ):
            raise LegacyAdapterError("placeholder audit row has malformed reasons")
        reason_sets.append(set(reason_values))
    anonymous_count = sum("forbidden_anonymous_placeholder" in reasons for reasons in reason_sets)
    ellipsis_count = sum(
        "forbidden_unicode_ellipsis_placeholder" in reasons for reasons in reason_sets
    )
    if anonymous_count != recipe.rejected_anonymous_rows:
        raise LegacyAdapterError("legacy [anonymous] count differs")
    if ellipsis_count != recipe.rejected_ellipsis_rows_after_dedup:
        raise LegacyAdapterError("legacy ellipsis count differs")
    if len(eligible) != recipe.admitted_rows_after_dedup_and_placeholder_screen:
        raise LegacyAdapterError("legacy post-screen eligible count differs")

    context = _compile_context(loaded)
    blocklist_path, blocked_hashes = _blocklist(loaded)
    core: list[dict[str, object]] = []
    sidecars: list[dict[str, object]] = []
    repr_failures: list[dict[str, object]] = []
    contamination: list[dict[str, object]] = []
    for row in eligible:
        record_id = _require_text(row, "record_id")
        reference = _require_text(row, "reference_headless")
        candidate = _require_text(row, "candidate_headless")
        try:
            reference_sidecar = render_surface(
                raw_statement=reference,
                parsed_signature=reference,
                declaration_kind="theorem",
                compile_context=context,
            )
            candidate_sidecar = render_surface(
                raw_statement=candidate,
                parsed_signature=candidate,
                declaration_kind="theorem",
                compile_context=context,
            )
        except (GoalV1Error, ValueError) as exc:
            repr_failures.append(
                {
                    "record_id": record_id,
                    "reference_headless": reference,
                    "candidate_headless": candidate,
                    "legacy_label": row["label"],
                    "reason": f"goal_v1_surface_fail_closed:{type(exc).__name__}:{exc}",
                    "training_eligible": False,
                }
            )
            continue
        reference_goal = reference_sidecar.core_text()
        candidate_goal = candidate_sidecar.core_text()
        screen_hashes = {
            "raw_reference": signature_near_dup_hash(reference),
            "raw_candidate": signature_near_dup_hash(candidate),
            "goal_reference": signature_near_dup_hash(reference_goal),
            "goal_candidate": signature_near_dup_hash(candidate_goal),
        }
        hits = sorted(name for name, digest in screen_hashes.items() if digest in blocked_hashes)
        if hits:
            contamination.append(
                {
                    "record_id": record_id,
                    "screen_hashes": screen_hashes,
                    "hit_fields": hits,
                    "training_eligible": False,
                }
            )
            continue
        stable_id = "sft2a_legacy:" + hash_canonical(
            {
                "source_tree_sha256": recipe.immutable_tree_sha256,
                "record_id": record_id,
                "reference_headless": reference,
                "candidate_headless": candidate,
            }
        )
        core_row = CoreRow(
            reference=reference_goal,
            candidate=candidate_goal,
            label=bool(row["label"]),
        ).model_dump(mode="json")
        core.append(core_row)
        sidecars.append(
            {
                "row_id": stable_id,
                "record_id": record_id,
                "group_key": _require_text(row, "group_key"),
                "family": _require_text(row, "family"),
                "label_provenance": recipe.label_basis,
                "configuration": recipe.output_configuration,
                "reference": reference_sidecar.to_dict(),
                "candidate": candidate_sidecar.to_dict(),
                "gold_screen_hashes": screen_hashes,
            }
        )

    plan_by_row_id = {_require_text(row, "plan_row_id"): row for row in pair_plan}
    unknowns: list[dict[str, object]] = []
    for judgment in judgments:
        if judgment.get("status") != "unresolved_reverse":
            continue
        plan_row_id = _require_text(judgment, "plan_row_id")
        plan = plan_by_row_id.get(plan_row_id)
        if plan is None:
            raise LegacyAdapterError(f"unknown judgment lacks pair-plan row {plan_row_id}")
        unknowns.append(
            {
                "record_id": _require_text(judgment, "record_id"),
                "pair_id": _require_text(judgment, "pair_id"),
                "reference_headless": _require_text(plan, "reference_headless"),
                "candidate_headless": _require_text(plan, "candidate_headless"),
                "judgment": judgment,
                "training_eligible": False,
            }
        )
    unknowns.sort(key=lambda row: str(row["record_id"]))
    if len(unknowns) != recipe.unknown_sidecar_rows:
        raise LegacyAdapterError("legacy unknown sidecar count differs")

    artifacts: dict[str, bytes] = {
        "legacy_single_judge/core.jsonl": _canonical_jsonl(core),
        "legacy_single_judge/sidecar.jsonl": _canonical_jsonl(sidecars),
        "legacy_placeholder_audit/rows.jsonl": _canonical_jsonl(placeholder_rows),
        "legacy_unknown/rows.jsonl": _canonical_jsonl(unknowns),
        "invalid/legacy_goal_v1_failures.jsonl": _canonical_jsonl(repr_failures),
        "contamination/legacy_gold_hits.jsonl": _canonical_jsonl(contamination),
    }
    artifact_manifest: dict[str, object] = {}
    replayed = True
    for relative, payload in artifacts.items():
        replayed = _atomic_exact(output_root / relative, payload) and replayed
        artifact_manifest[relative] = {
            "rows": payload.count(b"\n"),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    source_hashes_after = {path: hash_file(Path(path)) for path in source_hashes_before}
    if source_hashes_after != source_hashes_before:
        raise LegacyAdapterError("legacy sources changed during adaptation")
    manifest: dict[str, object] = {
        "version": "leanfaith_sft2a_legacy_import_v1",
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "adapter_path": "src/leanfaith/sft2a/legacy.py",
        "adapter_sha256": hash_file(Path(__file__)),
        "source_tree_sha256": recipe.immutable_tree_sha256,
        "source_hashes": source_hashes_before,
        "dedup": {
            "input_rows": len(trainer),
            "duplicate_groups": sum(count > 1 for count in pair_counts.values()),
            "excess_rows_removed": duplicate_excess,
            "keep_rule": recipe.keep_rule,
        },
        "placeholder_screen": {
            "anonymous_rejected": anonymous_count,
            "ellipsis_rejected": ellipsis_count,
        },
        "counts": {
            "post_dedup_placeholder_clean": len(eligible),
            "goal_v1_ready": len(core),
            "goal_v1_fail_closed": len(repr_failures),
            "gold_contamination_hits": len(contamination),
            "unknown_sidecar_only": len(unknowns),
        },
        "gold_blocklist": {
            "path": str(blocklist_path.relative_to(loaded.repo_root)),
            "sha256": hash_file(blocklist_path),
        },
        "repr": loaded.config.repr.model_dump(mode="json"),
        "artifacts": artifact_manifest,
        "publication_allowed": False,
    }
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    manifest_replay = _atomic_exact(output_root / "manifest.json", manifest_bytes)
    return LegacyAdapterResult(
        output_root=output_root,
        manifest=manifest,
        replayed=replayed and manifest_replay,
    )


__all__ = ["LegacyAdapterError", "LegacyAdapterResult", "adapt_legacy"]
