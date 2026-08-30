"""Cross-judge comparison and stable-ID post-audit SFT2A release materialization."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.models import CoreRow


class ReleaseMaterializationError(RuntimeError):
    """Source lineage, stable joins, or immutable outputs differ."""


@dataclass(frozen=True, slots=True)
class MaterializedResult:
    output_root: Path
    manifest: dict[str, object]
    replayed: bool


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseMaterializationError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseMaterializationError(f"JSON artifact is not an object: {path}")
    return value


def _rows(path: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseMaterializationError(f"cannot read JSONL artifact {path}: {exc}") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReleaseMaterializationError(f"invalid JSONL at {path}:{number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ReleaseMaterializationError(f"non-object JSONL row at {path}:{number}")
        result.append(value)
    return result


def _jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) + b"\n" for row in rows)


def _atomic(path: Path, payload: bytes) -> bool:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ReleaseMaterializationError(f"immutable output conflict: {path}")
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


def compare_fable_and_opus(loaded: LoadedSFT2AConfig) -> MaterializedResult:
    """Join the two immutable one-root runs by root/slot and record judge differences."""

    paths = run_paths(loaded)
    fable = paths.historical_fable_one_root
    opus = paths.one_root
    fable_manifest_path = fable / "manifest.json"
    opus_manifest_path = opus / "manifest.json"
    fable_manifest = _object(fable_manifest_path)
    opus_manifest = _object(opus_manifest_path)
    fable_rows = _rows(fable / "new_core/sidecar.jsonl")
    opus_rows = _rows(opus / "new_core/sidecar.jsonl")

    def keyed(rows: Sequence[Mapping[str, object]]) -> dict[tuple[str, str], Mapping[str, object]]:
        result: dict[tuple[str, str], Mapping[str, object]] = {}
        for row in rows:
            key = (str(row.get("root_id")), str(row.get("slot_id")))
            if key in result:
                raise ReleaseMaterializationError(f"duplicate comparison key: {key}")
            result[key] = row
        return result

    fable_by_key = keyed(fable_rows)
    opus_by_key = keyed(opus_rows)
    if fable_by_key.keys() != opus_by_key.keys():
        raise ReleaseMaterializationError("Fable and Opus accepted-slot sets differ")
    comparison_rows: list[dict[str, object]] = []
    for key in sorted(fable_by_key):
        left = fable_by_key[key]
        right = opus_by_key[key]
        left_judge = left.get("claude_judge")
        right_judge = right.get("claude_judge")
        if not isinstance(left_judge, dict) or not isinstance(right_judge, dict):
            raise ReleaseMaterializationError("comparison sidecar lacks Claude judgment")
        comparison_rows.append(
            {
                "root_id": key[0],
                "slot_id": key[1],
                "same_candidate": left.get("raw_candidate_signature")
                == right.get("raw_candidate_signature"),
                "same_row_id": left.get("row_id") == right.get("row_id"),
                "fable_verdict": left_judge.get("verdict"),
                "opus_verdict": right_judge.get("verdict"),
                "judge_agrees": left_judge.get("verdict") == right_judge.get("verdict"),
                "fable_call_key": left.get("claude_call_key"),
                "opus_call_key": right.get("claude_call_key"),
                "judge_prompt_hash": right.get("judge_prompt_hash"),
            }
        )
    rows_payload = _jsonl(comparison_rows)
    rows_path = paths.comparison / "rows.jsonl"
    rows_replayed = _atomic(rows_path, rows_payload)
    manifest = {
        "version": "leanfaith_sft2a_fable_opus_comparison_v1",
        "fable_source_run_sha256": hash_file(fable_manifest_path),
        "opus_source_run_sha256": hash_file(opus_manifest_path),
        "fable_provider": fable_rows[0].get("claude_provider") if fable_rows else None,
        "opus_provider": loaded.config.claude_judge.model_dump(mode="json"),
        "rows": len(comparison_rows),
        "same_candidates": sum(bool(row["same_candidate"]) for row in comparison_rows),
        "judge_agreements": sum(bool(row["judge_agrees"]) for row in comparison_rows),
        "fable_llm": fable_manifest.get("llm"),
        "opus_llm": opus_manifest.get("llm"),
        "artifact": {"path": "rows.jsonl", "sha256": hash_file(rows_path)},
        "comparison_hash": hash_canonical(comparison_rows),
    }
    manifest_path = paths.comparison / "manifest.json"
    manifest_replayed = _atomic(manifest_path, canonical_json_bytes(manifest) + b"\n")
    return MaterializedResult(paths.comparison, manifest, rows_replayed and manifest_replayed)


def materialize_post_audit_core(
    *,
    source_run: Path,
    audit_run: Path,
    output_root: Path,
) -> MaterializedResult:
    """Exclude every disagreed stable row ID before writing minimal training triples."""

    source_manifest_path = source_run / "manifest.json"
    audit_manifest_path = audit_run / "manifest.json"
    _object(source_manifest_path)
    audit_manifest = _object(audit_manifest_path)
    expected_source = audit_manifest.get(
        "source_run_manifest_sha256", audit_manifest.get("one_root_manifest_sha256")
    )
    if expected_source != hash_file(source_manifest_path):
        raise ReleaseMaterializationError(
            "audit source-run hash does not match the source manifest"
        )
    core = _rows(source_run / "new_core/core.jsonl")
    sidecars = _rows(source_run / "new_core/sidecar.jsonl")
    if len(core) != len(sidecars):
        raise ReleaseMaterializationError("core and sidecar row counts differ")
    by_id: dict[str, dict[str, object]] = {}
    for core_row, source_sidecar in zip(core, sidecars, strict=True):
        row_id = source_sidecar.get("row_id")
        if not isinstance(row_id, str) or not row_id or row_id in by_id:
            raise ReleaseMaterializationError("source run has missing or duplicate stable row IDs")
        by_id[row_id] = CoreRow.model_validate(core_row).model_dump(mode="json")
    excluded: set[str] = set()
    for audit_row in _rows(audit_run / "audit/rows.jsonl"):
        row_id = audit_row.get("row_id")
        if not isinstance(row_id, str) or row_id not in by_id:
            raise ReleaseMaterializationError("audit row ID does not join to the source run")
        if audit_row.get("agrees") is not True:
            excluded.add(row_id)
    released_ids = sorted(set(by_id) - excluded)
    released = [by_id[row_id] for row_id in released_ids]
    release_sidecar = [
        {"row_id": row_id, "source_run_sha256": hash_file(source_manifest_path)}
        for row_id in released_ids
    ]
    core_replayed = _atomic(output_root / "core.jsonl", _jsonl(released))
    sidecar_replayed = _atomic(output_root / "sidecar.jsonl", _jsonl(release_sidecar))
    manifest = {
        "version": "leanfaith_sft2a_post_audit_releasable_core_v1",
        "source_run_manifest_sha256": hash_file(source_manifest_path),
        "audit_manifest_sha256": hash_file(audit_manifest_path),
        "source_rows": len(core),
        "excluded_disagreement_row_ids": sorted(excluded),
        "released_rows": len(released),
        "schema": ["reference", "candidate", "label"],
        "artifacts": {
            "core.jsonl": hash_file(output_root / "core.jsonl"),
            "sidecar.jsonl": hash_file(output_root / "sidecar.jsonl"),
        },
    }
    manifest_replayed = _atomic(
        output_root / "manifest.json", canonical_json_bytes(manifest) + b"\n"
    )
    return MaterializedResult(
        output_root,
        manifest,
        core_replayed and sidecar_replayed and manifest_replayed,
    )


__all__ = [
    "MaterializedResult",
    "ReleaseMaterializationError",
    "compare_fable_and_opus",
    "materialize_post_audit_core",
]
