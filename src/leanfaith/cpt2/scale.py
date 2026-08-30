"""Resumable, string-only full-data materialization for CPT2."""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import sqlite3
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from huggingface_hub import hf_hub_download

from leanfaith.cpt2.pilot import SCHEMA_VERSION
from leanfaith.cpt2.source import SourceSnapshot, snapshot_to_dict
from leanfaith.cpt2.splitters import DECLARATION_AWARE_METHOD, split_source

SCALE_VERSION = "cpt2_full_scale_v1"
SELECTION_VERSION = "cpt2_validation_balanced_group_greedy_v1"
PREPARED_SCHEMA = pa.schema(
    [
        pa.field("theorem", pa.large_string(), nullable=False),
        pa.field("body", pa.large_string(), nullable=False),
        pa.field("label", pa.bool_(), nullable=False),
        pa.field("theorem_hash", pa.binary(32), nullable=False),
        pa.field("source_row_offset", pa.int32(), nullable=False),
    ]
)
RELEASE_SCHEMA = pa.schema(
    [
        pa.field("theorem", pa.large_string(), nullable=False),
        pa.field("body", pa.large_string(), nullable=False),
        pa.field("label", pa.bool_(), nullable=False),
    ]
)


class Cpt2ScaleError(RuntimeError):
    """The full-data build cannot safely proceed or resume."""


@dataclass(frozen=True, slots=True)
class ScaleSettings:
    output_root: Path
    compression: str = "zstd"
    validation_rows: int = 10_000
    validation_true: int = 5_000
    validation_false: int = 5_000
    validation_salt: str = "cpt2-validation-v1"
    row_groups_per_release_shard: int = 8
    workers: int = 2
    row_group_limit: int | None = None

    def __post_init__(self) -> None:
        if self.validation_rows != self.validation_true + self.validation_false:
            raise ValueError("validation label targets must sum to validation_rows")
        if self.validation_rows <= 0 or self.row_groups_per_release_shard <= 0:
            raise ValueError("CPT2 scale sizes must be positive")
        if self.workers <= 0:
            raise ValueError("CPT2 scale workers must be positive")
        if self.row_group_limit is not None and self.row_group_limit <= 0:
            raise ValueError("row_group_limit must be positive")


@dataclass(frozen=True, slots=True)
class ScaleResult:
    output_root: Path
    release_root: Path
    manifest_path: Path
    input_rows: int
    output_rows: int
    train_rows: int
    validation_rows: int
    written_source_shards: int
    resumed_source_shards: int
    written_release_shards: int
    resumed_release_shards: int
    elapsed_seconds: float


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_parquet(path: Path, table: Any, *, compression: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        pq.write_table(table, temporary, compression=compression, write_statistics=True)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_journal(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical_json(payload) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _require_mapping(value: object, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Cpt2ScaleError(f"{context} must be a mapping")
    return cast(dict[str, Any], value)


def _read_json(path: Path) -> dict[str, Any]:
    return _require_mapping(json.loads(path.read_text(encoding="utf-8")), context=str(path))


def load_gold_blocklist(path: Path) -> tuple[frozenset[str], str]:
    payload = _read_json(path)
    versions = payload.get("version")
    values = payload.get("near_dup_hashes")
    if (
        not isinstance(versions, list)
        or "golden_blocklist_v1" not in versions
        or not isinstance(values, list)
        or not all(isinstance(value, str) and len(value) == 64 for value in values)
    ):
        raise Cpt2ScaleError("gold blocklist does not match golden_blocklist_v1")
    return frozenset(cast(list[str], values)), _sha256_file(path)


def download_pinned_source(snapshot: SourceSnapshot, source_root: Path) -> Path:
    """Download once to resumable local storage and verify the pinned LFS object."""

    destination = source_root / snapshot.parquet_path
    if destination.is_file() and _sha256_file(destination) == snapshot.parquet_sha256:
        return destination
    downloaded = Path(
        hf_hub_download(
            repo_id=snapshot.repo_id,
            filename=snapshot.parquet_path,
            repo_type="dataset",
            revision=snapshot.resolved_revision,
            local_dir=source_root,
        )
    )
    if _sha256_file(downloaded) != snapshot.parquet_sha256:
        raise Cpt2ScaleError("downloaded compiler_data Parquet hash differs from pinned LFS hash")
    return downloaded


def _run_identity(
    *,
    snapshot: SourceSnapshot,
    settings: ScaleSettings,
    blocklist_sha256: str,
    task_code_sha256: str,
    selected_row_groups: Sequence[int],
) -> dict[str, object]:
    return {
        "scale_version": SCALE_VERSION,
        "source": snapshot_to_dict(snapshot),
        "selected_method": DECLARATION_AWARE_METHOD,
        "selected_row_groups": list(selected_row_groups),
        "gold_blocklist_sha256": blocklist_sha256,
        "task_code_sha256": task_code_sha256,
        "compression": settings.compression,
        "validation": {
            "version": SELECTION_VERSION,
            "rows": settings.validation_rows,
            "true": settings.validation_true,
            "false": settings.validation_false,
            "salt": settings.validation_salt,
        },
        "row_groups_per_release_shard": settings.row_groups_per_release_shard,
        "scale_lean_rows": 0,
    }


def _ensure_run_spec(path: Path, identity: Mapping[str, object]) -> tuple[str, bool]:
    run_id = _sha256_bytes(_canonical_json(identity).encode("utf-8"))
    payload = {"run_id": run_id, **identity}
    if path.exists():
        if _read_json(path) != payload:
            raise Cpt2ScaleError("existing scale run spec differs from the requested immutable run")
        return run_id, True
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return run_id, False


def _prepared_paths(output_root: Path, row_group: int) -> tuple[Path, Path]:
    stem = f"row-group-{row_group:05d}"
    return (
        output_root / "_state" / "prepared" / f"{stem}.parquet",
        output_root / "_state" / "prepared" / f"{stem}.manifest.json",
    )


def _verify_prepared_receipt(
    *, output_root: Path, row_group: int, run_id: str
) -> dict[str, Any] | None:
    data_path, manifest_path = _prepared_paths(output_root, row_group)
    if not manifest_path.exists():
        return None
    receipt = _read_json(manifest_path)
    if receipt.get("run_id") != run_id or receipt.get("row_group") != row_group:
        raise Cpt2ScaleError(f"prepared shard {row_group} belongs to another run")
    if not data_path.is_file() or _sha256_file(data_path) != receipt.get("data_sha256"):
        raise Cpt2ScaleError(f"prepared shard {row_group} differs from its completion receipt")
    parquet = pq.ParquetFile(data_path)
    if parquet.schema_arrow != PREPARED_SCHEMA:
        raise Cpt2ScaleError(f"prepared shard {row_group} has the wrong schema")
    if parquet.metadata.num_rows != receipt.get("emitted_rows"):
        raise Cpt2ScaleError(f"prepared shard {row_group} row count differs from its receipt")
    return receipt


def _extract_row_group_worker(
    source_path_text: str,
    output_root_text: str,
    row_group: int,
    run_id: str,
    blocklist_hashes: frozenset[str],
    compression: str,
) -> dict[str, Any]:
    """Process one source row group in an isolated, deterministic worker."""

    source_path = Path(source_path_text)
    output_root = Path(output_root_text)
    data_path, manifest_path = _prepared_paths(output_root, row_group)
    resumed = _verify_prepared_receipt(output_root=output_root, row_group=row_group, run_id=run_id)
    if resumed is not None:
        return {**resumed, "resumed": True}

    started = time.perf_counter()
    parquet = pq.ParquetFile(source_path)
    table = parquet.read_row_group(row_group, columns=["source_code", "isValid"])
    sources = table.column("source_code").to_pylist()
    labels = table.column("isValid").to_pylist()
    if len(sources) != len(labels):
        raise Cpt2ScaleError(f"source row group {row_group} has misaligned columns")

    theorems: list[str] = []
    bodies: list[str] = []
    output_labels: list[bool] = []
    theorem_hashes: list[bytes] = []
    row_offsets: list[int] = []
    source_labels: Counter[str] = Counter()
    output_label_counts: Counter[str] = Counter()
    skips: Counter[str] = Counter()
    gold_hashes: set[str] = set()
    theorem_characters = 0
    body_characters = 0
    for row_offset, (source, label) in enumerate(zip(sources, labels, strict=True)):
        if not isinstance(source, str) or type(label) is not bool:
            raise Cpt2ScaleError("compiler_data source_code/isValid schema drift")
        source_labels[str(label).lower()] += 1
        split = split_source(source, DECLARATION_AWARE_METHOD)
        if split is None:
            skips["unmatched_selected_splitter"] += 1
            continue
        if split.reconstruct() != source:
            raise Cpt2ScaleError(f"splitter round-trip failed at {row_group}:{row_offset}")
        theorem_hash = hashlib.sha256(split.theorem.encode("utf-8")).digest()
        theorem_hash_hex = theorem_hash.hex()
        if theorem_hash_hex in blocklist_hashes:
            skips["gold_exact_hash_hit"] += 1
            gold_hashes.add(theorem_hash_hex)
            continue
        theorems.append(split.theorem)
        bodies.append(split.body)
        output_labels.append(label)
        theorem_hashes.append(theorem_hash)
        row_offsets.append(row_offset)
        output_label_counts[str(label).lower()] += 1
        theorem_characters += len(split.theorem)
        body_characters += len(split.body)

    prepared = pa.Table.from_arrays(
        [
            pa.array(theorems, type=pa.large_string()),
            pa.array(bodies, type=pa.large_string()),
            pa.array(output_labels, type=pa.bool_()),
            pa.array(theorem_hashes, type=pa.binary(32)),
            pa.array(row_offsets, type=pa.int32()),
        ],
        schema=PREPARED_SCHEMA,
    )
    _atomic_parquet(data_path, prepared, compression=compression)
    receipt: dict[str, Any] = {
        "artifact_kind": "cpt2_prepared_source_row_group",
        "run_id": run_id,
        "row_group": row_group,
        "input_rows": len(sources),
        "source_labels": dict(source_labels),
        "emitted_rows": len(theorems),
        "output_labels": dict(output_label_counts),
        "skips": dict(skips),
        "gold_hit_hashes": sorted(gold_hashes),
        "theorem_characters": theorem_characters,
        "body_characters": body_characters,
        "data_file": data_path.name,
        "data_sha256": _sha256_file(data_path),
        "data_bytes": data_path.stat().st_size,
        "elapsed_seconds": time.perf_counter() - started,
    }
    _atomic_text(manifest_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return {**receipt, "resumed": False}


def extract_source_shards(
    *,
    source_path: Path,
    output_root: Path,
    selected_row_groups: Sequence[int],
    run_id: str,
    blocklist_hashes: frozenset[str],
    compression: str,
    workers: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Materialize manifest-last prepared shards, resuming verified completions."""

    receipts: dict[int, dict[str, Any]] = {}
    missing: list[int] = []
    for row_group in selected_row_groups:
        receipt = _verify_prepared_receipt(
            output_root=output_root, row_group=row_group, run_id=run_id
        )
        if receipt is None:
            missing.append(row_group)
        else:
            receipts[row_group] = receipt
    journal_path = output_root / "_state" / "journal.jsonl"
    if missing and workers == 1:
        for row_group in missing:
            receipt = _extract_row_group_worker(
                str(source_path),
                str(output_root),
                row_group,
                run_id,
                blocklist_hashes,
                compression,
            )
            receipts[row_group] = receipt
            _append_journal(
                journal_path,
                {
                    "event": "source_shard_complete",
                    "run_id": run_id,
                    "row_group": row_group,
                    "input_rows": receipt["input_rows"],
                    "emitted_rows": receipt["emitted_rows"],
                    "data_sha256": receipt["data_sha256"],
                },
            )
    elif missing:
        with ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn")
        ) as executor:
            futures = {
                executor.submit(
                    _extract_row_group_worker,
                    str(source_path),
                    str(output_root),
                    row_group,
                    run_id,
                    blocklist_hashes,
                    compression,
                ): row_group
                for row_group in missing
            }
            for future in as_completed(futures):
                row_group = futures[future]
                receipt = future.result()
                receipts[row_group] = receipt
                _append_journal(
                    journal_path,
                    {
                        "event": "source_shard_complete",
                        "run_id": run_id,
                        "row_group": row_group,
                        "input_rows": receipt["input_rows"],
                        "emitted_rows": receipt["emitted_rows"],
                        "data_sha256": receipt["data_sha256"],
                    },
                )
    ordered = [receipts[row_group] for row_group in selected_row_groups]
    return ordered, len(missing), len(selected_row_groups) - len(missing)


def _prepared_tree_hash(
    output_root: Path, selected_row_groups: Sequence[int]
) -> tuple[str, list[dict[str, object]]]:
    entries: list[dict[str, object]] = []
    for row_group in selected_row_groups:
        data_path, manifest_path = _prepared_paths(output_root, row_group)
        entries.append(
            {
                "row_group": row_group,
                "data_sha256": _sha256_file(data_path),
                "manifest_sha256": _sha256_file(manifest_path),
            }
        )
    return _sha256_bytes(_canonical_json(entries).encode("utf-8")), entries


def _connect_group_index(path: Path, *, run_id: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS processed_shards(
            row_group INTEGER PRIMARY KEY,
            manifest_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups(
            theorem_hash BLOB PRIMARY KEY,
            selection_key BLOB NOT NULL,
            true_count INTEGER NOT NULL,
            false_count INTEGER NOT NULL,
            row_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS selected_validation(
            theorem_hash BLOB PRIMARY KEY
        );
        """
    )
    existing = connection.execute("SELECT value FROM meta WHERE key='run_id'").fetchone()
    if existing is None:
        connection.execute("INSERT INTO meta(key, value) VALUES('run_id', ?)", (run_id,))
        connection.commit()
    elif existing[0] != run_id:
        connection.close()
        raise Cpt2ScaleError("theorem-group index belongs to another immutable scale run")
    return connection


def build_group_index(
    *,
    output_root: Path,
    selected_row_groups: Sequence[int],
    run_id: str,
    selection_salt: str,
) -> tuple[Path, int, int, str]:
    """Index exact theorem hashes transactionally, one prepared shard at a time."""

    database_path = output_root / "_state" / "theorem_groups.sqlite3"
    connection = _connect_group_index(database_path, run_id=run_id)
    indexed = 0
    resumed = 0
    try:
        for row_group in selected_row_groups:
            data_path, manifest_path = _prepared_paths(output_root, row_group)
            manifest_sha = _sha256_file(manifest_path)
            prior = connection.execute(
                "SELECT manifest_sha256 FROM processed_shards WHERE row_group=?", (row_group,)
            ).fetchone()
            if prior is not None:
                if prior[0] != manifest_sha:
                    raise Cpt2ScaleError("prepared shard changed after theorem grouping")
                resumed += 1
                continue
            table = pq.read_table(data_path, columns=["theorem_hash", "label"])
            hashes = table.column("theorem_hash").to_pylist()
            labels = table.column("label").to_pylist()
            per_group: Counter[tuple[bytes, bool]] = Counter()
            for theorem_hash, label in zip(hashes, labels, strict=True):
                if not isinstance(theorem_hash, bytes) or type(label) is not bool:
                    raise Cpt2ScaleError("prepared theorem hash/label schema drift")
                per_group[(theorem_hash, label)] += 1
            combined: dict[bytes, list[int]] = {}
            for (theorem_hash, label), count in per_group.items():
                counts = combined.setdefault(theorem_hash, [0, 0])
                counts[int(label)] += count
            rows = [
                (
                    theorem_hash,
                    hashlib.sha256(selection_salt.encode("utf-8") + b"\0" + theorem_hash).digest(),
                    counts[1],
                    counts[0],
                    counts[0] + counts[1],
                )
                for theorem_hash, counts in combined.items()
            ]
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """
                    INSERT INTO groups(
                        theorem_hash, selection_key, true_count, false_count, row_count
                    ) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(theorem_hash) DO UPDATE SET
                        true_count=true_count+excluded.true_count,
                        false_count=false_count+excluded.false_count,
                        row_count=row_count+excluded.row_count
                    """,
                    rows,
                )
                connection.execute(
                    "INSERT INTO processed_shards(row_group, manifest_sha256) VALUES(?, ?)",
                    (row_group, manifest_sha),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            indexed += 1
        connection.execute(
            "CREATE INDEX IF NOT EXISTS groups_selection_key ON groups(selection_key)"
        )
        connection.commit()
        group_count = cast(int, connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0])
        row_count = cast(int, connection.execute("SELECT SUM(row_count) FROM groups").fetchone()[0])
    finally:
        connection.close()
    prepared_hash, _ = _prepared_tree_hash(output_root, selected_row_groups)
    return database_path, group_count, row_count, prepared_hash


def _selection_digest(hashes: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for theorem_hash in sorted(hashes):
        digest.update(theorem_hash)
    return digest.hexdigest()


def select_validation_groups(
    *,
    database_path: Path,
    output_root: Path,
    run_id: str,
    prepared_tree_hash: str,
    target_true: int,
    target_false: int,
) -> dict[str, Any]:
    """Select whole theorem groups with deterministic greedy label balancing."""

    manifest_path = output_root / "_state" / "validation_selection.json"
    connection = _connect_group_index(database_path, run_id=run_id)
    try:
        if manifest_path.exists():
            existing_payload = _read_json(manifest_path)
            if (
                existing_payload.get("run_id") != run_id
                or existing_payload.get("prepared_tree_sha256") != prepared_tree_hash
                or existing_payload.get("target_true") != target_true
                or existing_payload.get("target_false") != target_false
            ):
                raise Cpt2ScaleError("existing validation selection belongs to another build")
            hashes = [
                cast(bytes, row[0])
                for row in connection.execute(
                    "SELECT theorem_hash FROM selected_validation ORDER BY theorem_hash"
                )
            ]
            if _selection_digest(hashes) != existing_payload.get("selected_hashes_sha256"):
                raise Cpt2ScaleError("validation selection table differs from its manifest")
            return {**existing_payload, "resumed": True}

        selected: list[bytes] = []
        current_true = 0
        current_false = 0
        score = target_true + target_false
        for theorem_hash, true_count, false_count in connection.execute(
            "SELECT theorem_hash, true_count, false_count FROM groups ORDER BY selection_key"
        ):
            next_true = current_true + cast(int, true_count)
            next_false = current_false + cast(int, false_count)
            next_score = abs(target_true - next_true) + abs(target_false - next_false)
            if next_score < score:
                selected.append(cast(bytes, theorem_hash))
                current_true = next_true
                current_false = next_false
                score = next_score
                if score == 0:
                    break
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM selected_validation")
            connection.executemany(
                "INSERT INTO selected_validation(theorem_hash) VALUES(?)",
                ((theorem_hash,) for theorem_hash in selected),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        payload: dict[str, Any] = {
            "artifact_kind": "cpt2_validation_group_selection",
            "version": SELECTION_VERSION,
            "run_id": run_id,
            "prepared_tree_sha256": prepared_tree_hash,
            "target_true": target_true,
            "target_false": target_false,
            "selected_true": current_true,
            "selected_false": current_false,
            "selected_rows": current_true + current_false,
            "selected_groups": len(selected),
            "absolute_label_target_error": score,
            "selected_hashes_sha256": _selection_digest(selected),
        }
        _atomic_text(manifest_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return {**payload, "resumed": False}
    finally:
        connection.close()


def _load_selected_hashes(database_path: Path, *, run_id: str) -> frozenset[bytes]:
    connection = _connect_group_index(database_path, run_id=run_id)
    try:
        return frozenset(
            cast(bytes, row[0])
            for row in connection.execute("SELECT theorem_hash FROM selected_validation")
        )
    finally:
        connection.close()


def _release_part_paths(release_root: Path, part: int, total_parts: int) -> tuple[Path, Path, Path]:
    return (
        release_root / f"train-{part:05d}-of-{total_parts:05d}.parquet",
        release_root / f"validation-{part:05d}-of-{total_parts:05d}.parquet",
        release_root.parent / "_state" / "release_shards" / f"part-{part:05d}.json",
    )


def _verify_release_receipt(
    *, release_root: Path, part: int, total_parts: int, run_id: str
) -> dict[str, Any] | None:
    train_path, validation_path, receipt_path = _release_part_paths(release_root, part, total_parts)
    if not receipt_path.exists():
        return None
    receipt = _read_json(receipt_path)
    if receipt.get("run_id") != run_id or receipt.get("part") != part:
        raise Cpt2ScaleError(f"release shard {part} belongs to another run")
    for split, path in (("train", train_path), ("validation", validation_path)):
        split_receipt = _require_mapping(receipt.get(split), context=f"release {part} {split}")
        if not path.is_file() or _sha256_file(path) != split_receipt.get("sha256"):
            raise Cpt2ScaleError(f"release {split} shard {part} differs from its receipt")
        parquet = pq.ParquetFile(path)
        if parquet.schema_arrow != RELEASE_SCHEMA:
            raise Cpt2ScaleError(f"release {split} shard {part} has the wrong schema")
        if parquet.metadata.num_rows != split_receipt.get("rows"):
            raise Cpt2ScaleError(f"release {split} shard {part} row count mismatch")
    return receipt


def _write_partitioned_part(
    *,
    output_root: Path,
    release_root: Path,
    row_groups: Sequence[int],
    part: int,
    total_parts: int,
    run_id: str,
    selected_hashes: frozenset[bytes],
    compression: str,
) -> dict[str, Any]:
    existing = _verify_release_receipt(
        release_root=release_root, part=part, total_parts=total_parts, run_id=run_id
    )
    if existing is not None:
        return {**existing, "resumed": True}
    train_path, validation_path, receipt_path = _release_part_paths(release_root, part, total_parts)
    train_path.parent.mkdir(parents=True, exist_ok=True)
    train_temporary = train_path.with_name(f".{train_path.name}.tmp-{os.getpid()}")
    validation_temporary = validation_path.with_name(f".{validation_path.name}.tmp-{os.getpid()}")
    value_set = pa.array(sorted(selected_hashes), type=pa.binary(32))
    train_labels: Counter[str] = Counter()
    validation_labels: Counter[str] = Counter()
    train_rows = 0
    validation_rows = 0
    try:
        with (
            pq.ParquetWriter(
                train_temporary, RELEASE_SCHEMA, compression=compression, write_statistics=True
            ) as train_writer,
            pq.ParquetWriter(
                validation_temporary,
                RELEASE_SCHEMA,
                compression=compression,
                write_statistics=True,
            ) as validation_writer,
        ):
            for row_group in row_groups:
                prepared_path, _ = _prepared_paths(output_root, row_group)
                table = pq.read_table(prepared_path)
                selected_mask = pc.is_in(table.column("theorem_hash"), value_set=value_set)
                selected_table = table.filter(selected_mask).select(RELEASE_SCHEMA.names)
                train_table = table.filter(pc.invert(selected_mask)).select(RELEASE_SCHEMA.names)
                train_writer.write_table(train_table)
                validation_writer.write_table(selected_table)
                train_batch_labels = cast(list[bool], train_table.column("label").to_pylist())
                validation_batch_labels = cast(
                    list[bool], selected_table.column("label").to_pylist()
                )
                train_labels.update(str(label).lower() for label in train_batch_labels)
                validation_labels.update(str(label).lower() for label in validation_batch_labels)
                train_rows += train_table.num_rows
                validation_rows += selected_table.num_rows
        for temporary in (train_temporary, validation_temporary):
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
        os.replace(train_temporary, train_path)
        os.replace(validation_temporary, validation_path)
    finally:
        train_temporary.unlink(missing_ok=True)
        validation_temporary.unlink(missing_ok=True)
    receipt: dict[str, Any] = {
        "artifact_kind": "cpt2_release_shard_pair",
        "run_id": run_id,
        "part": part,
        "total_parts": total_parts,
        "source_row_groups": list(row_groups),
        "train": {
            "file": train_path.name,
            "rows": train_rows,
            "labels": dict(train_labels),
            "bytes": train_path.stat().st_size,
            "sha256": _sha256_file(train_path),
        },
        "validation": {
            "file": validation_path.name,
            "rows": validation_rows,
            "labels": dict(validation_labels),
            "bytes": validation_path.stat().st_size,
            "sha256": _sha256_file(validation_path),
        },
    }
    _atomic_text(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return {**receipt, "resumed": False}


def compact_release(
    *,
    output_root: Path,
    selected_row_groups: Sequence[int],
    database_path: Path,
    run_id: str,
    compression: str,
    row_groups_per_shard: int,
) -> tuple[Path, list[dict[str, Any]], int, int]:
    """Partition whole theorem groups and write only the three core release fields."""

    release_root = output_root / "release"
    selected_hashes = _load_selected_hashes(database_path, run_id=run_id)
    total_parts = math.ceil(len(selected_row_groups) / row_groups_per_shard)
    receipts: list[dict[str, Any]] = []
    written = 0
    resumed = 0
    for part in range(total_parts):
        start = part * row_groups_per_shard
        row_groups = selected_row_groups[start : start + row_groups_per_shard]
        receipt = _write_partitioned_part(
            output_root=output_root,
            release_root=release_root,
            row_groups=row_groups,
            part=part,
            total_parts=total_parts,
            run_id=run_id,
            selected_hashes=selected_hashes,
            compression=compression,
        )
        was_resumed = bool(receipt.pop("resumed"))
        resumed += int(was_resumed)
        written += int(not was_resumed)
        receipts.append(receipt)
    return release_root, receipts, written, resumed


def _quantiles(arrays: Sequence[np.ndarray[Any, Any]]) -> dict[str, int | float]:
    if not arrays:
        return {"min": 0, "p25": 0, "p50": 0, "p75": 0, "p95": 0, "max": 0, "mean": 0.0}
    values = np.concatenate(arrays)
    return {
        "min": int(values.min()),
        "p25": int(np.quantile(values, 0.25, method="nearest")),
        "p50": int(np.quantile(values, 0.50, method="nearest")),
        "p75": int(np.quantile(values, 0.75, method="nearest")),
        "p95": int(np.quantile(values, 0.95, method="nearest")),
        "max": int(values.max()),
        "mean": float(values.mean()),
    }


def validate_release(
    *,
    release_root: Path,
    receipts: Sequence[Mapping[str, Any]],
    selected_hashes: frozenset[bytes],
) -> dict[str, Any]:
    """Re-open every final file and prove schema, hashes, labels, and group separation."""

    split_counts: dict[str, Counter[str]] = {
        "train": Counter(),
        "validation": Counter(),
    }
    theorem_lengths: list[np.ndarray[Any, Any]] = []
    body_lengths: list[np.ndarray[Any, Any]] = []
    files_checked = 0
    for receipt in receipts:
        for split in ("train", "validation"):
            split_receipt = _require_mapping(receipt.get(split), context=f"{split} receipt")
            path = release_root / str(split_receipt["file"])
            if _sha256_file(path) != split_receipt.get("sha256"):
                raise Cpt2ScaleError(f"final release hash mismatch: {path.name}")
            parquet = pq.ParquetFile(path)
            if parquet.schema_arrow != RELEASE_SCHEMA:
                raise Cpt2ScaleError(f"final release schema mismatch: {path.name}")
            observed_rows = 0
            for batch in parquet.iter_batches(batch_size=8192):
                table = pa.Table.from_batches([batch], schema=RELEASE_SCHEMA)
                if any(table.column(name).null_count for name in RELEASE_SCHEMA.names):
                    raise Cpt2ScaleError(f"null core value in {path.name}")
                theorems = cast(list[str], table.column("theorem").to_pylist())
                bodies = cast(list[str], table.column("body").to_pylist())
                labels = cast(list[bool], table.column("label").to_pylist())
                for theorem, body, label in zip(theorems, bodies, labels, strict=True):
                    if (
                        not isinstance(theorem, str)
                        or not isinstance(body, str)
                        or type(label) is not bool
                    ):
                        raise Cpt2ScaleError(f"core type drift in {path.name}")
                    selected = hashlib.sha256(theorem.encode("utf-8")).digest() in selected_hashes
                    if selected != (split == "validation"):
                        raise Cpt2ScaleError("theorem group crossed train/validation partitions")
                    split_counts[split][str(label).lower()] += 1
                theorem_lengths.append(
                    pc.utf8_length(table.column("theorem")).to_numpy(zero_copy_only=False)
                )
                body_lengths.append(
                    pc.utf8_length(table.column("body")).to_numpy(zero_copy_only=False)
                )
                observed_rows += table.num_rows
            if observed_rows != split_receipt.get("rows"):
                raise Cpt2ScaleError(f"final release row mismatch: {path.name}")
            files_checked += 1
    return {
        "status": "passed",
        "files_checked": files_checked,
        "splits": {
            split: {
                "rows": sum(counts.values()),
                "labels": dict(counts),
            }
            for split, counts in split_counts.items()
        },
        "lengths": {
            "theorem": _quantiles(theorem_lengths),
            "body": _quantiles(body_lengths),
        },
        "group_disjoint_by_exact_theorem_sha256": True,
        "core_schema": [[field.name, str(field.type)] for field in RELEASE_SCHEMA],
    }


def _sum_nested_counts(payloads: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for payload in payloads:
        values = payload.get(field, {})
        if not isinstance(values, dict):
            raise Cpt2ScaleError(f"receipt field {field} is not a mapping")
        counts.update({str(key): int(value) for key, value in values.items()})
    return dict(counts)


def _release_tree_hash(receipts: Sequence[Mapping[str, Any]]) -> str:
    entries = [
        {
            "train": receipt["train"]["sha256"],
            "validation": receipt["validation"]["sha256"],
        }
        for receipt in receipts
    ]
    return _sha256_bytes(_canonical_json(entries).encode("utf-8"))


def run_scale(
    *,
    snapshot: SourceSnapshot,
    source_path: Path,
    settings: ScaleSettings,
    blocklist_path: Path,
    task_code_sha256: str,
    code_revision: str,
) -> ScaleResult:
    """Run or resume the full string-only build through final validation."""

    started = time.perf_counter()
    if snapshot.schema != (
        ("source_code", "large_string"),
        ("validation", "large_string"),
        ("isValid", "bool"),
    ):
        raise Cpt2ScaleError(f"unexpected compiler_data schema: {snapshot.schema!r}")
    source_parquet = pq.ParquetFile(source_path)
    if source_parquet.num_row_groups != snapshot.row_group_count:
        raise Cpt2ScaleError("local source row-group count differs from pinned metadata")
    if source_parquet.metadata.num_rows != snapshot.row_count:
        raise Cpt2ScaleError("local source row count differs from pinned metadata")
    selected_count = (
        snapshot.row_group_count
        if settings.row_group_limit is None
        else min(settings.row_group_limit, snapshot.row_group_count)
    )
    selected_row_groups = tuple(range(selected_count))
    blocklist_hashes, blocklist_sha256 = load_gold_blocklist(blocklist_path)
    identity = _run_identity(
        snapshot=snapshot,
        settings=settings,
        blocklist_sha256=blocklist_sha256,
        task_code_sha256=task_code_sha256,
        selected_row_groups=selected_row_groups,
    )
    run_spec_path = settings.output_root / "_state" / "run_spec.json"
    run_id, run_resumed = _ensure_run_spec(run_spec_path, identity)
    source_receipts, written_source, resumed_source = extract_source_shards(
        source_path=source_path,
        output_root=settings.output_root,
        selected_row_groups=selected_row_groups,
        run_id=run_id,
        blocklist_hashes=blocklist_hashes,
        compression=settings.compression,
        workers=settings.workers,
    )
    database_path, group_count, grouped_rows, prepared_tree_hash = build_group_index(
        output_root=settings.output_root,
        selected_row_groups=selected_row_groups,
        run_id=run_id,
        selection_salt=settings.validation_salt,
    )
    selection = select_validation_groups(
        database_path=database_path,
        output_root=settings.output_root,
        run_id=run_id,
        prepared_tree_hash=prepared_tree_hash,
        target_true=settings.validation_true,
        target_false=settings.validation_false,
    )
    release_root, release_receipts, written_release, resumed_release = compact_release(
        output_root=settings.output_root,
        selected_row_groups=selected_row_groups,
        database_path=database_path,
        run_id=run_id,
        compression=settings.compression,
        row_groups_per_shard=settings.row_groups_per_release_shard,
    )
    selected_hashes = _load_selected_hashes(database_path, run_id=run_id)
    validation = validate_release(
        release_root=release_root,
        receipts=release_receipts,
        selected_hashes=selected_hashes,
    )

    input_rows = sum(int(receipt["input_rows"]) for receipt in source_receipts)
    emitted_rows = sum(int(receipt["emitted_rows"]) for receipt in source_receipts)
    source_labels = _sum_nested_counts(source_receipts, "source_labels")
    output_labels = _sum_nested_counts(source_receipts, "output_labels")
    skips = _sum_nested_counts(source_receipts, "skips")
    if input_rows != sum(source_labels.values()):
        raise Cpt2ScaleError("source labels do not account for every input row")
    if emitted_rows != sum(output_labels.values()) or emitted_rows != grouped_rows:
        raise Cpt2ScaleError("prepared labels/groups do not account for every emitted row")
    validated_splits = cast(dict[str, Any], validation["splits"])
    train_rows = int(validated_splits["train"]["rows"])
    validation_rows = int(validated_splits["validation"]["rows"])
    if emitted_rows != train_rows + validation_rows:
        raise Cpt2ScaleError("final partitions do not account for every emitted row")
    if validation_rows != selection["selected_rows"]:
        raise Cpt2ScaleError("validation data differs from whole-group selection")
    gold_hit_hashes = sorted(
        {
            str(value)
            for receipt in source_receipts
            for value in cast(list[object], receipt.get("gold_hit_hashes", []))
        }
    )
    elapsed = time.perf_counter() - started
    manifest: dict[str, Any] = {
        "artifact_kind": "cpt2_full_release",
        "scale_version": SCALE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_spec_sha256": _sha256_file(run_spec_path),
        "run_resumed": run_resumed,
        "source": snapshot_to_dict(snapshot),
        "source_file_sha256": _sha256_file(source_path),
        "source_license_redistribution_note": (
            "The pinned public source has no dataset card/license metadata; this derivative is "
            "private-first and this note is not a legal determination."
        ),
        "splitter": {
            "method": DECLARATION_AWARE_METHOD,
            "oracle_unique_source_rows": 500,
            "scale_lean_rows": 0,
            "round_trip_failures": 0,
        },
        "input_rows": input_rows,
        "source_labels": source_labels,
        "output_rows": emitted_rows,
        "output_labels": output_labels,
        "skips": skips,
        "gold_screen": {
            "blocklist_sha256": blocklist_sha256,
            "comparison": "sha256(exact theorem prefix)",
            "action": "excluded",
            "hit_count": int(skips.get("gold_exact_hash_hit", 0)),
            "hit_hashes": gold_hit_hashes,
        },
        "grouping": {
            "key": "sha256(exact theorem prefix)",
            "groups": group_count,
            "prepared_tree_sha256": prepared_tree_hash,
            "train_validation_disjoint": True,
            "selection": {key: value for key, value in selection.items() if key != "resumed"},
        },
        "release": {
            "schema": [[field.name, str(field.type)] for field in RELEASE_SCHEMA],
            "tree_sha256": _release_tree_hash(release_receipts),
            "shards": release_receipts,
            "validation": validation,
        },
        "durability": {
            "source_shards": len(source_receipts),
            "written_source_shards_current_run": written_source,
            "resumed_source_shards_current_run": resumed_source,
            "release_shard_pairs": len(release_receipts),
            "written_release_shards_current_run": written_release,
            "resumed_release_shards_current_run": resumed_release,
            "journal": str(settings.output_root / "_state" / "journal.jsonl"),
            "completion": "immutable run spec plus atomic shard files plus manifest-last receipts",
        },
        "code_revision": code_revision,
        "task_code_sha256": task_code_sha256,
        "elapsed_seconds_current_run": elapsed,
        "rows_per_second_current_run": emitted_rows / elapsed if elapsed else None,
        "training_started": False,
        "publication": {
            "destination": "Lemmy00/leanfaith-cpt2-proof-validity-v1",
            "visibility": "private",
            "status": "not_yet_published",
        },
    }
    manifest_path = release_root / "manifest.json"
    if manifest_path.exists():
        existing = _read_json(manifest_path)
        stable_keys = (
            "artifact_kind",
            "scale_version",
            "schema_version",
            "run_id",
            "source",
            "source_file_sha256",
            "splitter",
            "input_rows",
            "source_labels",
            "output_rows",
            "output_labels",
            "skips",
            "gold_screen",
            "grouping",
            "release",
            "task_code_sha256",
            "training_started",
            "publication",
        )
        if any(existing.get(key) != manifest.get(key) for key in stable_keys):
            raise Cpt2ScaleError("completed release manifest differs from deterministic rebuild")
    else:
        _atomic_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return ScaleResult(
        output_root=settings.output_root,
        release_root=release_root,
        manifest_path=manifest_path,
        input_rows=input_rows,
        output_rows=emitted_rows,
        train_rows=train_rows,
        validation_rows=validation_rows,
        written_source_shards=written_source,
        resumed_source_shards=resumed_source,
        written_release_shards=written_release,
        resumed_release_shards=resumed_release,
        elapsed_seconds=elapsed,
    )


__all__ = [
    "RELEASE_SCHEMA",
    "SCALE_VERSION",
    "Cpt2ScaleError",
    "ScaleResult",
    "ScaleSettings",
    "download_pinned_source",
    "run_scale",
]
