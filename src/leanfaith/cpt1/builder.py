"""Deterministic, resumable construction of the CPT1 ``{text}`` corpus.

The module is intentionally pure string/schema/file work.  It does not import or invoke Lean.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import resource
import sqlite3
import subprocess
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.compute as pc  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import yaml

Recipe = Literal["text", "question_answer"]
_WHITESPACE = re.compile(r"\s+")
_TRAINER_SCHEMA = pa.schema([pa.field("text", pa.string(), nullable=False)])
_PROVENANCE_SCHEMA = pa.schema(
    [
        pa.field("row_id", pa.string(), nullable=False),
        pa.field("text_sha256", pa.string(), nullable=False),
        pa.field("text_blake2b", pa.string(), nullable=False),
        pa.field("utf8_bytes", pa.int64(), nullable=False),
        pa.field("characters", pa.int64(), nullable=False),
        pa.field("source_name", pa.string(), nullable=False),
        pa.field("source_repo", pa.string(), nullable=False),
        pa.field("source_revision", pa.string(), nullable=False),
        pa.field("source_config", pa.string(), nullable=False),
        pa.field("source_split", pa.string(), nullable=False),
        pa.field("source_row_index", pa.int64(), nullable=False),
        pa.field("source_native_id", pa.string()),
        pa.field("column_recipe", pa.string(), nullable=False),
        pa.field("source_payload_sha256", pa.string(), nullable=False),
        pa.field("question_sha256", pa.string()),
        pa.field("answer_sha256", pa.string()),
    ]
)


class Cpt1Error(ValueError):
    """The CPT1 input or release violates its frozen contract."""


@dataclass(frozen=True, slots=True)
class SourceSpec:
    name: str
    repo_id: str
    revision: str
    config: str
    split: str
    recipe: Recipe
    native_id_column: str
    expected_rows: int

    @property
    def required_columns(self) -> tuple[str, ...]:
        if self.recipe == "text":
            return (self.native_id_column, "text")
        return (self.native_id_column, "question", "answer")


@dataclass(frozen=True, slots=True)
class BuildConfig:
    schema_version: str
    output_root: Path
    cache_dir: Path
    blocklist_path: Path
    destination_repo: str
    rows_per_shard: int
    compression: str
    feedback_test_excluded_rows: int
    sources: tuple[SourceSpec, ...]


@dataclass(frozen=True, slots=True)
class BuildResult:
    output_root: Path
    release_root: Path
    manifest_path: Path
    validation_path: Path
    rows: int
    resumed_chunks: int
    written_chunks: int


@dataclass(frozen=True, slots=True)
class _PreparedRow:
    text: str
    source_native_id: str | None
    source_payload_sha256: str
    question_sha256: str | None
    answer_sha256: str | None


@dataclass(frozen=True, slots=True)
class _SourceRuntime:
    spec: SourceSpec
    resolved_revision: str
    reported_rows: int | None
    selected_files: tuple[dict[str, object], ...]
    excluded_files: tuple[dict[str, object], ...]
    dataset_fingerprint: str | None = None
    cache_files: tuple[str, ...] = ()
    load_seconds: float | None = None


def _require_dict(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise Cpt1Error(f"{context} must be a mapping")
    return cast(dict[str, object], value)


def _require_str(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise Cpt1Error(f"{context} must be a non-empty string")
    return value


def _require_int(value: object, *, context: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise Cpt1Error(f"{context} must be an integer >= {minimum}")
    return value


def load_config(path: Path) -> BuildConfig:
    """Load and strictly validate the task-owned YAML configuration."""

    raw = _require_dict(yaml.safe_load(path.read_text(encoding="utf-8")), context="config")
    source_values = raw.get("sources")
    if not isinstance(source_values, list) or not source_values:
        raise Cpt1Error("config.sources must be a non-empty list")
    sources: list[SourceSpec] = []
    names: set[str] = set()
    for index, item in enumerate(source_values):
        payload = _require_dict(item, context=f"sources[{index}]")
        name = _require_str(payload.get("name"), context=f"sources[{index}].name")
        if name in names:
            raise Cpt1Error(f"duplicate source name {name!r}")
        names.add(name)
        recipe = _require_str(payload.get("recipe"), context=f"sources[{index}].recipe")
        if recipe not in {"text", "question_answer"}:
            raise Cpt1Error(f"unsupported recipe {recipe!r}")
        revision = _require_str(
            payload.get("revision"), context=f"sources[{index}].revision"
        )
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise Cpt1Error(f"sources[{index}].revision must be a 40-character commit")
        sources.append(
            SourceSpec(
                name=name,
                repo_id=_require_str(
                    payload.get("repo_id"), context=f"sources[{index}].repo_id"
                ),
                revision=revision,
                config=_require_str(
                    payload.get("config"), context=f"sources[{index}].config"
                ),
                split=_require_str(payload.get("split"), context=f"sources[{index}].split"),
                recipe=cast(Recipe, recipe),
                native_id_column=_require_str(
                    payload.get("native_id_column"),
                    context=f"sources[{index}].native_id_column",
                ),
                expected_rows=_require_int(
                    payload.get("expected_rows"),
                    context=f"sources[{index}].expected_rows",
                    minimum=1,
                ),
            )
        )
    config = BuildConfig(
        schema_version=_require_str(raw.get("schema_version"), context="schema_version"),
        output_root=Path(_require_str(raw.get("output_root"), context="output_root")),
        cache_dir=Path(_require_str(raw.get("cache_dir"), context="cache_dir")),
        blocklist_path=Path(
            _require_str(raw.get("blocklist_path"), context="blocklist_path")
        ),
        destination_repo=_require_str(
            raw.get("destination_repo"), context="destination_repo"
        ),
        rows_per_shard=_require_int(
            raw.get("rows_per_shard"), context="rows_per_shard", minimum=1
        ),
        compression=_require_str(raw.get("compression"), context="compression"),
        feedback_test_excluded_rows=_require_int(
            raw.get("feedback_test_excluded_rows"),
            context="feedback_test_excluded_rows",
            minimum=1,
        ),
        sources=tuple(sources),
    )
    if config.schema_version != "cpt1_v1.0":
        raise Cpt1Error("schema_version must be cpt1_v1.0")
    return config


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _blake2b_text(value: str) -> str:
    return hashlib.blake2b(value.encode("utf-8"), digest_size=32).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_parquet(path: Path, table: pa.Table, *, compression: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    pq.write_table(table, temporary, compression=compression, write_statistics=True)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_blocklist(path: Path) -> tuple[frozenset[str], str]:
    raw_bytes = path.read_bytes()
    raw = _require_dict(json.loads(raw_bytes), context="golden blocklist")
    versions = raw.get("version")
    values = raw.get("near_dup_hashes")
    if (
        not isinstance(versions, list)
        or "golden_blocklist_v1" not in versions
        or not isinstance(values, list)
        or not all(isinstance(value, str) for value in values)
    ):
        raise Cpt1Error("golden blocklist does not match golden_blocklist_v1")
    hashes = frozenset(cast(list[str], values))
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
        raise Cpt1Error("golden blocklist contains an invalid hash")
    return hashes, _sha256_bytes(raw_bytes)


def _gold_hash(text: str) -> str:
    """Apply the blocklist's frozen whitespace-collapsed signature hash."""

    return _sha256_text(_WHITESPACE.sub(" ", text).strip())


def _prepare_row(spec: SourceSpec, row: Mapping[str, object]) -> _PreparedRow:
    missing = [column for column in spec.required_columns if column not in row]
    if missing:
        raise Cpt1Error(f"{spec.name} row is missing columns: {missing}")
    native_value = row[spec.native_id_column]
    if native_value is not None and not isinstance(native_value, str):
        raise Cpt1Error(f"{spec.name}.{spec.native_id_column} must be string or null")
    native_id = native_value
    if spec.recipe == "text":
        text = row["text"]
        if not isinstance(text, str):
            raise Cpt1Error(f"{spec.name}.text must be a non-null string")
        digest = _sha256_text(text)
        return _PreparedRow(
            text=text,
            source_native_id=native_id,
            source_payload_sha256=digest,
            question_sha256=None,
            answer_sha256=None,
        )
    question = row["question"]
    answer = row["answer"]
    if not isinstance(question, str) or not isinstance(answer, str):
        raise Cpt1Error(f"{spec.name}.question and .answer must be non-null strings")
    text = question + answer
    return _PreparedRow(
        text=text,
        source_native_id=native_id,
        source_payload_sha256=_sha256_text(text),
        question_sha256=_sha256_text(question),
        answer_sha256=_sha256_text(answer),
    )


def _stable_row_id(spec: SourceSpec, source_row_index: int, native_id: str | None) -> str:
    identity = {
        "config": spec.config,
        "native_id": native_id,
        "recipe": spec.recipe,
        "repo_id": spec.repo_id,
        "revision": spec.revision,
        "source_row_index": source_row_index,
        "split": spec.split,
    }
    return _sha256_text(_canonical_json(identity))


def _config_binding(
    config: BuildConfig, *, limit_per_source: int | None, source_access_mode: str
) -> str:
    revision = _git_revision()
    payload = {
        "blocklist_sha256": _sha256_file(config.blocklist_path),
        "builder_code_config_sha256": revision.get("task_code_config_sha256"),
        "compression": config.compression,
        "feedback_test_excluded_rows": config.feedback_test_excluded_rows,
        "limit_per_source": limit_per_source,
        "rows_per_shard": config.rows_per_shard,
        "schema_version": config.schema_version,
        "source_access_mode": source_access_mode,
        "sources": [asdict(source) for source in config.sources],
    }
    return _sha256_text(_canonical_json(payload))


def _connect_state(path: Path, binding: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS run (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen (
            text_sha256 TEXT PRIMARY KEY,
            text_blake2b TEXT NOT NULL,
            utf8_bytes INTEGER NOT NULL,
            row_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_key TEXT PRIMARY KEY,
            ordinal INTEGER NOT NULL UNIQUE,
            payload TEXT NOT NULL
        );
        """
    )
    existing = connection.execute("SELECT value FROM run WHERE key='binding'").fetchone()
    if existing is None:
        connection.execute("INSERT INTO run(key, value) VALUES('binding', ?)", (binding,))
        connection.commit()
    elif existing[0] != binding:
        raise Cpt1Error("resume state belongs to a different configuration/input binding")
    return connection


def _append_journal(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical_json(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _chunk_payload(connection: sqlite3.Connection, key: str) -> dict[str, object] | None:
    row = connection.execute("SELECT payload FROM chunks WHERE chunk_key=?", (key,)).fetchone()
    if row is None:
        return None
    return _require_dict(json.loads(row[0]), context=f"chunk {key}")


def _verify_resumed_chunk(release_root: Path, payload: Mapping[str, object]) -> None:
    for prefix in ("data", "provenance"):
        relative = payload.get(f"{prefix}_path")
        expected = payload.get(f"{prefix}_sha256")
        if relative is None and expected is None:
            continue
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise Cpt1Error(f"resumed chunk has invalid {prefix} receipt")
        path = release_root / relative
        if not path.is_file() or _sha256_file(path) != expected:
            raise Cpt1Error(f"resumed chunk file does not match receipt: {path}")


def _process_chunk(
    *,
    connection: sqlite3.Connection,
    journal_path: Path,
    release_root: Path,
    spec: SourceSpec,
    source_ordinal: int,
    chunk_ordinal: int,
    start: int,
    rows: Sequence[Mapping[str, object]],
    blocklist: frozenset[str],
    compression: str,
) -> tuple[dict[str, object], bool]:
    end = start + len(rows)
    chunk_key = f"{source_ordinal:03d}-{spec.name}-{start:012d}-{end:012d}"
    existing = _chunk_payload(connection, chunk_key)
    if existing is not None:
        _verify_resumed_chunk(release_root, existing)
        return existing, True

    texts: list[str] = []
    provenance: dict[str, list[object]] = {field.name: [] for field in _PROVENANCE_SCHEMA}
    blank_rows = 0
    duplicates = 0
    contamination = 0
    connection.execute("BEGIN IMMEDIATE")
    try:
        for offset, row in enumerate(rows):
            source_row_index = start + offset
            prepared = _prepare_row(spec, row)
            if not prepared.text.strip():
                blank_rows += 1
            if _gold_hash(prepared.text) in blocklist:
                contamination += 1
                continue
            text_sha256 = _sha256_text(prepared.text)
            text_blake2b = _blake2b_text(prepared.text)
            utf8_bytes = len(prepared.text.encode("utf-8"))
            row_id = _stable_row_id(spec, source_row_index, prepared.source_native_id)
            inserted = connection.execute(
                "INSERT OR IGNORE INTO seen(text_sha256, text_blake2b, utf8_bytes, row_id) "
                "VALUES(?, ?, ?, ?)",
                (text_sha256, text_blake2b, utf8_bytes, row_id),
            ).rowcount
            if not inserted:
                prior = connection.execute(
                    "SELECT text_blake2b, utf8_bytes FROM seen WHERE text_sha256=?",
                    (text_sha256,),
                ).fetchone()
                if prior is None or prior != (text_blake2b, utf8_bytes):
                    raise Cpt1Error("SHA-256 collision detected during exact-string deduplication")
                duplicates += 1
                continue
            texts.append(prepared.text)
            values: dict[str, object] = {
                "row_id": row_id,
                "text_sha256": text_sha256,
                "text_blake2b": text_blake2b,
                "utf8_bytes": utf8_bytes,
                "characters": len(prepared.text),
                "source_name": spec.name,
                "source_repo": spec.repo_id,
                "source_revision": spec.revision,
                "source_config": spec.config,
                "source_split": spec.split,
                "source_row_index": source_row_index,
                "source_native_id": prepared.source_native_id,
                "column_recipe": "text unchanged"
                if spec.recipe == "text"
                else "question + answer (zero inserted bytes)",
                "source_payload_sha256": prepared.source_payload_sha256,
                "question_sha256": prepared.question_sha256,
                "answer_sha256": prepared.answer_sha256,
            }
            for name, value in values.items():
                provenance[name].append(value)

        data_relative: str | None = None
        data_sha256: str | None = None
        provenance_relative: str | None = None
        provenance_sha256: str | None = None
        if texts:
            stem = f"train-{chunk_key}.parquet"
            data_relative = f"data/{stem}"
            provenance_relative = f"provenance/{stem}"
            data_path = release_root / data_relative
            provenance_path = release_root / provenance_relative
            _atomic_parquet(
                data_path,
                pa.Table.from_pydict({"text": texts}, schema=_TRAINER_SCHEMA),
                compression=compression,
            )
            _atomic_parquet(
                provenance_path,
                pa.Table.from_pydict(provenance, schema=_PROVENANCE_SCHEMA),
                compression=compression,
            )
            data_sha256 = _sha256_file(data_path)
            provenance_sha256 = _sha256_file(provenance_path)
        payload: dict[str, object] = {
            "blank_rows": blank_rows,
            "chunk_key": chunk_key,
            "contamination_rows": contamination,
            "data_path": data_relative,
            "data_sha256": data_sha256,
            "duplicates": duplicates,
            "end": end,
            "kept": len(texts),
            "provenance_path": provenance_relative,
            "provenance_sha256": provenance_sha256,
            "raw": len(rows),
            "source": spec.name,
            "start": start,
        }
        connection.execute(
            "INSERT INTO chunks(chunk_key, ordinal, payload) VALUES(?, ?, ?)",
            (chunk_key, chunk_ordinal, _canonical_json(payload)),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    _append_journal(journal_path, {"event": "chunk_complete", **payload})
    return payload, False


def _iter_sequence_chunks(
    rows: Sequence[Mapping[str, object]], rows_per_shard: int, limit: int | None
) -> Iterator[tuple[int, Sequence[Mapping[str, object]]]]:
    total = len(rows) if limit is None else min(len(rows), limit)
    for start in range(0, total, rows_per_shard):
        yield start, rows[start : min(total, start + rows_per_shard)]


def _iter_dataset_chunks(
    dataset: Any,
    columns: Sequence[str],
    rows_per_shard: int,
    limit: int | None,
) -> Iterator[tuple[int, Sequence[Mapping[str, object]]]]:
    total_rows = len(dataset)
    total = total_rows if limit is None else min(total_rows, limit)
    for start in range(0, total, rows_per_shard):
        end = min(total, start + rows_per_shard)
        raw_batch = dataset[start:end]
        if not isinstance(raw_batch, dict):
            raise Cpt1Error("datasets slice did not return a column mapping")
        batch = cast(dict[str, list[object]], raw_batch)
        rows = [
            {column: batch[column][offset] for column in columns}
            for offset in range(end - start)
        ]
        yield start, rows


def _iter_stream_chunks(
    rows: Iterable[Mapping[str, object]], rows_per_shard: int, limit: int | None
) -> Iterator[tuple[int, Sequence[Mapping[str, object]]]]:
    iterator = iter(rows)
    start = 0
    while limit is None or start < limit:
        wanted = rows_per_shard if limit is None else min(rows_per_shard, limit - start)
        chunk: list[Mapping[str, object]] = []
        for _ in range(wanted):
            try:
                chunk.append(next(iterator))
            except StopIteration:
                break
        if not chunk:
            break
        yield start, chunk
        start += len(chunk)


def _repo_file_receipt(sibling: Any) -> dict[str, object]:
    name = cast(str, sibling.rfilename)
    size = cast(int | None, sibling.size)
    lfs = sibling.lfs
    lfs_sha256: str | None = None
    if isinstance(lfs, dict):
        value = lfs.get("sha256")
        lfs_sha256 = value if isinstance(value, str) else None
    elif lfs is not None:
        value = getattr(lfs, "sha256", None)
        lfs_sha256 = value if isinstance(value, str) else None
    return {"path": name, "size": size, "lfs_sha256": lfs_sha256}


def _resolve_live_sources(config: BuildConfig) -> tuple[_SourceRuntime, ...]:
    from datasets import get_dataset_config_info
    from huggingface_hub import HfApi

    api = HfApi()
    runtimes: list[_SourceRuntime] = []
    for source in config.sources:
        info = api.dataset_info(source.repo_id, revision=source.revision, files_metadata=True)
        if info.sha != source.revision:
            raise Cpt1Error(
                f"{source.repo_id}@{source.revision} resolved to unexpected commit {info.sha}"
            )
        config_info = get_dataset_config_info(
            source.repo_id, config_name=source.config, revision=source.revision
        )
        split_value = (config_info.splits or {}).get(source.split)
        reported_rows: int | None = None
        if isinstance(split_value, dict):
            raw_count = split_value.get("num_examples")
            reported_rows = raw_count if isinstance(raw_count, int) else None
        elif split_value is not None:
            raw_count = getattr(split_value, "num_examples", None)
            reported_rows = raw_count if isinstance(raw_count, int) else None
        selected: list[dict[str, object]] = []
        excluded: list[dict[str, object]] = []
        for sibling in info.siblings or []:
            receipt = _repo_file_receipt(sibling)
            path = cast(str, receipt["path"])
            if (source.recipe == "text" and path.endswith(".jsonl")) or (
                source.recipe == "question_answer" and path.startswith("data/train-")
            ):
                selected.append(receipt)
            elif source.recipe == "question_answer" and path.startswith("data/test-"):
                excluded.append(receipt)
        if not selected:
            raise Cpt1Error(f"no selected source files resolved for {source.name}")
        runtimes.append(
            _SourceRuntime(
                spec=source,
                resolved_revision=info.sha,
                reported_rows=reported_rows,
                selected_files=tuple(selected),
                excluded_files=tuple(excluded),
            )
        )
    return tuple(runtimes)


def _git_revision() -> dict[str, object]:
    root = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        names = subprocess.run(
            [
                "git",
                "status",
                "--short",
                "--",
                "src/leanfaith/cpt1",
                "configs/data/cpt1",
                "tests/unit/cpt1",
                "plans/10_cpt1.md",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "task_paths_dirty": None}
    task_files = sorted(
        [
            *root.joinpath("src/leanfaith/cpt1").glob("*.py"),
            *root.joinpath("configs/data/cpt1").glob("*.yaml"),
        ]
    )
    digest = hashlib.sha256()
    for path in task_files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        contents = path.read_bytes()
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return {
        "commit": commit,
        "task_paths_dirty": bool(names),
        "task_code_config_sha256": digest.hexdigest(),
    }


def _source_manifest(runtime: _SourceRuntime) -> dict[str, object]:
    return {
        "cache_files": list(runtime.cache_files),
        "config": runtime.spec.config,
        "dataset_fingerprint": runtime.dataset_fingerprint,
        "excluded_files": list(runtime.excluded_files),
        "expected_rows": runtime.spec.expected_rows,
        "name": runtime.spec.name,
        "recipe": "text unchanged"
        if runtime.spec.recipe == "text"
        else "question + answer (zero inserted bytes)",
        "repo_id": runtime.spec.repo_id,
        "reported_rows": runtime.reported_rows,
        "resolved_revision": runtime.resolved_revision,
        "revision": runtime.spec.revision,
        "selected_files": list(runtime.selected_files),
        "split": runtime.spec.split,
    }


def _quantiles(
    release_root: Path, chunk_payloads: Sequence[Mapping[str, object]]
) -> dict[str, int]:
    arrays: list[pa.Array] = []
    for payload in chunk_payloads:
        relative = payload.get("provenance_path")
        if isinstance(relative, str):
            arrays.append(pq.read_table(release_root / relative, columns=["utf8_bytes"])[0])
    if not arrays:
        return {"min": 0, "p50": 0, "p90": 0, "p99": 0, "max": 0}
    values = pa.chunked_array(arrays)
    computed = pc.quantile(values, q=[0.5, 0.9, 0.99], interpolation="nearest").to_pylist()
    return {
        "min": cast(int, pc.min(values).as_py()),
        "p50": int(computed[0]),
        "p90": int(computed[1]),
        "p99": int(computed[2]),
        "max": cast(int, pc.max(values).as_py()),
    }


def _reproducibility_hash(
    release_root: Path, chunk_payloads: Sequence[Mapping[str, object]]
) -> str:
    digest = hashlib.sha256()
    for payload in chunk_payloads:
        relative = payload.get("provenance_path")
        if not isinstance(relative, str):
            continue
        parquet = pq.ParquetFile(release_root / relative)
        for batch in parquet.iter_batches(columns=["text_sha256"], batch_size=8192):
            for value in batch.column(0).to_pylist():
                digest.update(bytes.fromhex(cast(str, value)))
    return digest.hexdigest()


def _read_chunks(connection: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        _require_dict(json.loads(row[0]), context="chunk payload")
        for row in connection.execute("SELECT payload FROM chunks ORDER BY ordinal")
    ]


def _initial_metrics(
    *,
    connection: sqlite3.Connection,
    release_root: Path,
    runtimes: Sequence[_SourceRuntime],
    chunks: Sequence[Mapping[str, object]],
    elapsed_seconds: float,
    resumed_chunks: int,
) -> dict[str, object]:
    existing = connection.execute("SELECT value FROM run WHERE key='initial_metrics'").fetchone()
    if existing is not None:
        return _require_dict(json.loads(existing[0]), context="initial metrics")
    total_rows = sum(cast(int, payload["kept"]) for payload in chunks)
    metrics: dict[str, object] = {
        "elapsed_seconds": elapsed_seconds,
        "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "resumed_chunks": resumed_chunks,
        "rows_per_second": total_rows / elapsed_seconds if elapsed_seconds else None,
        "source_load_seconds": {
            runtime.spec.name: runtime.load_seconds for runtime in runtimes
        },
        "text_utf8_length_quantiles": _quantiles(release_root, chunks),
    }
    connection.execute(
        "INSERT INTO run(key, value) VALUES('initial_metrics', ?)",
        (_canonical_json(metrics),),
    )
    connection.commit()
    return metrics


def _write_readme(config: BuildConfig, release_root: Path) -> None:
    lean_docs = config.sources[0]
    feedback = config.sources[1]
    content = f"""---
configs:
- config_name: default
  data_files:
  - split: train
    path: data/*.parquet
- config_name: provenance
  data_files:
  - split: train
    path: provenance/*.parquet
---

# LeanFaith CPT1 v1

The default configuration is the minimal continued-pretraining view with exactly one string
column, `text`. It combines `{lean_docs.repo_id}@{lean_docs.revision}` `text` values unchanged and
`{feedback.repo_id}@{feedback.revision}` training rows as the exact expression `question + answer`
with zero inserted bytes. The {config.feedback_test_excluded_rows:,} blank-answer feedback test
rows are excluded. No Lean process or model training is part of this release.

See `manifest.json` and the `provenance` configuration for hash-bound lineage and exclusions.
"""
    _atomic_text(release_root / "README.md", content)


def _write_manifest_and_checksums(
    *,
    config: BuildConfig,
    release_root: Path,
    state_root: Path,
    binding: str,
    blocklist_sha256: str,
    runtimes: Sequence[_SourceRuntime],
    chunks: Sequence[Mapping[str, object]],
    metrics: Mapping[str, object],
    source_access_mode: str,
) -> tuple[Path, Path]:
    totals: dict[str, dict[str, int]] = {
        source.name: {
            "blank_rows": 0,
            "contamination_rows": 0,
            "duplicates": 0,
            "kept": 0,
            "raw": 0,
        }
        for source in config.sources
    }
    for payload in chunks:
        source_totals = totals[cast(str, payload["source"])]
        for key in source_totals:
            source_totals[key] += cast(int, payload[key])
    total_rows = sum(value["kept"] for value in totals.values())
    manifest: dict[str, object] = {
        "binding_sha256": binding,
        "builder": _git_revision(),
        "deduplication": {
            "algorithm": "exact UTF-8 text keyed by SHA-256 with BLAKE2b-256 collision check",
            "source_order": [source.name for source in config.sources],
        },
        "destination": {"private": True, "repo_id": config.destination_repo},
        "excluded_feedback_test": {
            "action": "excluded without row iteration",
            "reason": "blank-answer question-only test rows",
            "rows": config.feedback_test_excluded_rows,
        },
        "gold_blocklist": {
            "action": "exact hash matches excluded from default",
            "algorithm": "sha256(whitespace-collapsed UTF-8 text)",
            "path": str(config.blocklist_path),
            "sha256": blocklist_sha256,
        },
        "hub": {"data_commit": None, "release_commit_recorded_in": "plans/10_cpt1.md"},
        "metrics": dict(metrics),
        "output": {
            "chunks": list(chunks),
            "reproducibility_hash_algorithm": "sha256(ordered raw text_sha256 digests)",
            "reproducibility_sha256": _reproducibility_hash(release_root, chunks),
            "rows": total_rows,
            "trainer_schema": {"text": "string"},
        },
        "schema_version": config.schema_version,
        "source_access_mode": source_access_mode,
        "source_counts": totals,
        "sources": [_source_manifest(runtime) for runtime in runtimes],
        "validation": {
            "duplicate_row_ids": 0,
            "null_texts": 0,
            "status": "passed",
            "trainer_columns": ["text"],
        },
    }
    manifest_path = release_root / "manifest.json"
    _atomic_text(
        manifest_path,
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    _write_readme(config, release_root)
    release_files = sorted(
        path
        for path in release_root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    checksum_lines = [
        f"{_sha256_file(path)}  {path.relative_to(release_root).as_posix()}"
        for path in release_files
    ]
    _atomic_text(release_root / "SHA256SUMS", "\n".join(checksum_lines) + "\n")
    validation_path = state_root / "validation.json"
    report = validate_release(release_root)
    _atomic_text(validation_path, json.dumps(report, indent=2, sort_keys=True) + "\n")
    return manifest_path, validation_path


def validate_release(release_root: Path) -> dict[str, object]:
    """Re-hash and inspect every local release shard."""

    manifest = _require_dict(
        json.loads((release_root / "manifest.json").read_text(encoding="utf-8")),
        context="manifest",
    )
    output = _require_dict(manifest.get("output"), context="manifest.output")
    chunks = output.get("chunks")
    if not isinstance(chunks, list):
        raise Cpt1Error("manifest.output.chunks must be a list")
    expected_sums: dict[str, str] = {}
    for line in (release_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        expected_sums[relative] = digest
    for relative, expected in expected_sums.items():
        path = release_root / relative
        if not path.is_file() or _sha256_file(path) != expected:
            raise Cpt1Error(f"checksum mismatch: {relative}")
    data_rows = 0
    provenance_rows = 0
    for item in chunks:
        payload = _require_dict(item, context="manifest chunk")
        data_relative = payload.get("data_path")
        provenance_relative = payload.get("provenance_path")
        if data_relative is None and provenance_relative is None:
            if payload.get("kept") != 0:
                raise Cpt1Error("chunk without files has nonzero kept rows")
            continue
        if not isinstance(data_relative, str) or not isinstance(provenance_relative, str):
            raise Cpt1Error("chunk data/provenance paths must be paired")
        data_path = release_root / data_relative
        provenance_path = release_root / provenance_relative
        if _sha256_file(data_path) != payload.get("data_sha256"):
            raise Cpt1Error(f"manifest data hash mismatch: {data_relative}")
        if _sha256_file(provenance_path) != payload.get("provenance_sha256"):
            raise Cpt1Error(f"manifest provenance hash mismatch: {provenance_relative}")
        data_file = pq.ParquetFile(data_path)
        provenance_file = pq.ParquetFile(provenance_path)
        if data_file.schema_arrow != _TRAINER_SCHEMA:
            raise Cpt1Error(f"trainer schema mismatch: {data_relative}")
        if provenance_file.schema_arrow != _PROVENANCE_SCHEMA:
            raise Cpt1Error(f"provenance schema mismatch: {provenance_relative}")
        if data_file.metadata.num_rows != provenance_file.metadata.num_rows:
            raise Cpt1Error(f"data/provenance row mismatch: {data_relative}")
        data_rows += data_file.metadata.num_rows
        provenance_rows += provenance_file.metadata.num_rows
        table = pq.read_table(data_path, columns=["text"])
        if table.column("text").null_count:
            raise Cpt1Error(f"null trainer text: {data_relative}")
    expected_rows = output.get("rows")
    if data_rows != expected_rows or provenance_rows != expected_rows:
        raise Cpt1Error("release row counts do not match manifest")
    return {
        "checked_files": len(expected_sums),
        "manifest_sha256": _sha256_file(release_root / "manifest.json"),
        "rows": data_rows,
        "schema": {"text": "string"},
        "sha256sums_sha256": _sha256_file(release_root / "SHA256SUMS"),
        "status": "passed",
    }


def _build(
    *,
    config: BuildConfig,
    output_root: Path,
    runtimes: Sequence[_SourceRuntime],
    chunk_iterators: Sequence[Iterable[tuple[int, Sequence[Mapping[str, object]]]]],
    limit_per_source: int | None,
    source_access_mode: str,
) -> BuildResult:
    started = time.monotonic()
    blocklist, blocklist_sha256 = _load_blocklist(config.blocklist_path)
    binding = _config_binding(
        config,
        limit_per_source=limit_per_source,
        source_access_mode=source_access_mode,
    )
    release_root = output_root / "release"
    state_root = output_root / "_state"
    release_root.mkdir(parents=True, exist_ok=True)
    connection = _connect_state(state_root / "state.sqlite3", binding)
    journal_path = state_root / "journal.jsonl"
    resumed_chunks = 0
    written_chunks = 0
    ordinal = 0
    try:
        for source_ordinal, (runtime, iterator) in enumerate(
            zip(runtimes, chunk_iterators, strict=True)
        ):
            for start, rows in iterator:
                _, resumed = _process_chunk(
                    connection=connection,
                    journal_path=journal_path,
                    release_root=release_root,
                    spec=runtime.spec,
                    source_ordinal=source_ordinal,
                    chunk_ordinal=ordinal,
                    start=start,
                    rows=rows,
                    blocklist=blocklist,
                    compression=config.compression,
                )
                resumed_chunks += int(resumed)
                written_chunks += int(not resumed)
                ordinal += 1
        chunks = _read_chunks(connection)
        metrics = _initial_metrics(
            connection=connection,
            release_root=release_root,
            runtimes=runtimes,
            chunks=chunks,
            elapsed_seconds=time.monotonic() - started,
            resumed_chunks=resumed_chunks,
        )
        manifest_path, validation_path = _write_manifest_and_checksums(
            config=config,
            release_root=release_root,
            state_root=state_root,
            binding=binding,
            blocklist_sha256=blocklist_sha256,
            runtimes=runtimes,
            chunks=chunks,
            metrics=metrics,
            source_access_mode=source_access_mode,
        )
        output_rows = cast(
            int, json.loads(manifest_path.read_text(encoding="utf-8"))["output"]["rows"]
        )
        return BuildResult(
            output_root=output_root,
            release_root=release_root,
            manifest_path=manifest_path,
            validation_path=validation_path,
            rows=output_rows,
            resumed_chunks=resumed_chunks,
            written_chunks=written_chunks,
        )
    finally:
        connection.close()


def build_live(
    config: BuildConfig,
    *,
    output_root: Path | None = None,
    limit_per_source: int | None = None,
    streaming: bool = False,
) -> BuildResult:
    """Build from the two exact Hub revisions, optionally as a bounded smoke/pilot."""

    from datasets import load_dataset

    if limit_per_source is not None and limit_per_source < 1:
        raise Cpt1Error("limit_per_source must be positive")
    root = output_root or config.output_root
    runtimes = list(_resolve_live_sources(config))
    iterators: list[Iterable[tuple[int, Sequence[Mapping[str, object]]]]] = []
    for index, runtime in enumerate(runtimes):
        before = time.monotonic()
        dataset = load_dataset(
            runtime.spec.repo_id,
            name=runtime.spec.config,
            revision=runtime.spec.revision,
            split=runtime.spec.split,
            streaming=streaming,
            cache_dir=str(config.cache_dir),
        )
        missing = set(runtime.spec.required_columns).difference(dataset.column_names)
        if missing:
            raise Cpt1Error(f"{runtime.spec.name} dataset is missing columns: {sorted(missing)}")
        dataset = dataset.select_columns(list(runtime.spec.required_columns))
        fingerprint = getattr(dataset, "_fingerprint", None)
        cache_files_raw = getattr(dataset, "cache_files", [])
        cache_files = tuple(
            cast(str, item["filename"])
            for item in cache_files_raw
            if isinstance(item, dict) and isinstance(item.get("filename"), str)
        )
        reported_rows = runtime.reported_rows
        if not streaming:
            observed_rows = len(dataset)
            if observed_rows != runtime.spec.expected_rows:
                raise Cpt1Error(
                    f"{runtime.spec.name} expected {runtime.spec.expected_rows} rows, "
                    f"observed {observed_rows}"
                )
            reported_rows = observed_rows
        runtimes[index] = replace(
            runtime,
            reported_rows=reported_rows,
            dataset_fingerprint=fingerprint if isinstance(fingerprint, str) else None,
            cache_files=cache_files,
            load_seconds=time.monotonic() - before,
        )
        if streaming:
            iterators.append(
                _iter_stream_chunks(
                    cast(Iterable[Mapping[str, object]], dataset),
                    config.rows_per_shard,
                    limit_per_source,
                )
            )
        else:
            iterators.append(
                _iter_dataset_chunks(
                    dataset,
                    runtime.spec.required_columns,
                    config.rows_per_shard,
                    limit_per_source,
                )
            )
    try:
        return _build(
            config=config,
            output_root=root,
            runtimes=runtimes,
            chunk_iterators=iterators,
            limit_per_source=limit_per_source,
            source_access_mode="streaming" if streaming else "memory_mapped",
        )
    finally:
        for iterator in iterators:
            with suppress(AttributeError):
                cast(Any, iterator).close()
        iterators.clear()
        gc.collect()


def verify_live_smoke(config: BuildConfig, release_root: Path) -> dict[str, object]:
    """Re-open pinned source rows and prove both one-row recipes byte-for-byte."""

    from datasets import load_dataset

    provenance_files = sorted((release_root / "provenance").glob("*.parquet"))
    data_files = sorted((release_root / "data").glob("*.parquet"))
    if len(provenance_files) != len(config.sources) or len(data_files) != len(config.sources):
        raise Cpt1Error("smoke release must contain exactly one shard per source")
    report: dict[str, object] = {}
    for spec, data_path, provenance_path in zip(
        config.sources, data_files, provenance_files, strict=True
    ):
        data = pq.read_table(data_path).to_pylist()
        provenance = pq.read_table(provenance_path).to_pylist()
        if len(data) != 1 or len(provenance) != 1:
            raise Cpt1Error(f"smoke source {spec.name} did not retain exactly one row")
        source_index = cast(int, provenance[0]["source_row_index"])
        stream = load_dataset(
            spec.repo_id,
            name=spec.config,
            revision=spec.revision,
            split=spec.split,
            streaming=True,
            cache_dir=str(config.cache_dir),
        ).select_columns(list(spec.required_columns))
        source_row: Mapping[str, object] | None = None
        for index, row in enumerate(cast(Iterable[Mapping[str, object]], stream)):
            if index == source_index:
                source_row = row
                break
        if source_row is None:
            raise Cpt1Error(f"could not re-open {spec.name} source row {source_index}")
        prepared = _prepare_row(spec, source_row)
        output_text = data[0]["text"]
        if not isinstance(output_text, str):
            raise Cpt1Error("smoke trainer text is not a string")
        output_bytes = output_text.encode("utf-8")
        expected_bytes = prepared.text.encode("utf-8")
        if output_bytes != expected_bytes:
            raise Cpt1Error(f"smoke output differs from pinned source recipe for {spec.name}")
        item: dict[str, object] = {
            "output_equals_recipe_bytes": True,
            "output_sha256": _sha256_bytes(output_bytes),
            "output_utf8_bytes": len(output_bytes),
            "source_row_index": source_index,
        }
        if spec.recipe == "text":
            item["input_text_unchanged"] = output_text == source_row["text"]
        else:
            question = cast(str, source_row["question"])
            answer = cast(str, source_row["answer"])
            question_bytes = question.encode("utf-8")
            answer_bytes = answer.encode("utf-8")
            item.update(
                {
                    "answer_nonblank": bool(answer.strip()),
                    "answer_utf8_bytes": len(answer_bytes),
                    "boundary_answer_head_hex": answer_bytes[:16].hex(),
                    "boundary_question_tail_hex": question_bytes[-16:].hex(),
                    "no_inserted_separator": output_bytes == question_bytes + answer_bytes,
                    "question_utf8_bytes": len(question_bytes),
                }
            )
            if not item["answer_nonblank"] or not item["no_inserted_separator"]:
                raise Cpt1Error("feedback smoke did not prove a nonblank zero-separator row")
        report[spec.name] = item
    return report


def build_fixture(
    config: BuildConfig,
    *,
    rows_by_source: Mapping[str, Sequence[Mapping[str, object]]],
    output_root: Path,
    limit_per_source: int | None = None,
) -> BuildResult:
    """Exercise the production shard/state path with in-memory rows for offline tests."""

    runtimes: list[_SourceRuntime] = []
    iterators: list[Iterable[tuple[int, Sequence[Mapping[str, object]]]]] = []
    for spec in config.sources:
        rows = rows_by_source.get(spec.name)
        if rows is None:
            raise Cpt1Error(f"fixture is missing source {spec.name}")
        runtimes.append(
            _SourceRuntime(
                spec=spec,
                resolved_revision=spec.revision,
                reported_rows=len(rows),
                selected_files=(),
                excluded_files=(),
            )
        )
        iterators.append(_iter_sequence_chunks(rows, config.rows_per_shard, limit_per_source))
    return _build(
        config=config,
        output_root=output_root,
        runtimes=runtimes,
        chunk_iterators=iterators,
        limit_per_source=limit_per_source,
        source_access_mode="fixture",
    )
