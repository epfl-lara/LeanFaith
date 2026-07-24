"""Deterministic pre-extraction stratified sampling for Gate 2."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import hash_file

RowIteratorFactory = Callable[[], Iterator[tuple[int, dict[str, Any]]]]


def _rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for row_index, line in enumerate(handle):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{row_index + 1}: expected JSON object")
            yield row_index, row


def _arrow_rows(paths: Sequence[Path]) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield stable global row indices across lexically ordered HF Arrow shards."""

    from datasets import Dataset

    row_offset = 0
    for path in sorted(paths):
        dataset = Dataset.from_file(str(path))
        for local_index, row in enumerate(dataset):
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{local_index}: expected mapping row")
            yield row_offset + local_index, row
        row_offset += len(dataset)


def _band(value: object, cutoffs: tuple[int, ...]) -> str:
    if value is None:
        return "missing"
    if not isinstance(value, (str, int, float)):
        return "invalid"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "invalid"
    lower = 0
    for upper in cutoffs:
        if number < upper:
            return f"{lower}_{upper - 1}"
        lower = upper
    return f"{lower}_plus"


def _canonical_bool(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int) and value in (0, 1):
        return str(bool(value)).lower()
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "false"}:
            return normalized
    if value is None:
        return "missing"
    return "invalid"


def _stratum(row: dict[str, Any], uuid_counts: Counter[str], split: str) -> str:
    uuid = str(row.get("uuid", ""))
    values = (
        f"split={split}",
        f"valid={_canonical_bool(row.get('valid'))}",
        f"nl_provenance={row.get('data_source', 'missing')!s}",
        f"token_band={_band(row.get('token_count'), (256, 512, 1024, 2048))}",
        f"tactic_band={_band(row.get('tactic_count'), (1, 6, 21, 51))}",
        f"docstring={('/--' in str(row.get('question', '')))}",
        f"duplicate_uuid={uuid_counts[uuid] > 1}",
    )
    return "|".join(values)


def _allocate(counts: Counter[str], sample_size: int) -> dict[str, int]:
    total = sum(counts.values())
    if sample_size <= 0 or sample_size > total:
        raise ValueError(f"sample_size must be in [1,{total}], got {sample_size}")
    active = sorted(counts)
    if len(active) > sample_size:
        # With more strata than slots, deterministically retain strata with
        # largest mass, then lexical key.  The omitted strata remain explicit
        # in the manifest with quota zero.
        retained = set(sorted(active, key=lambda key: (-counts[key], key))[:sample_size])
        return {key: int(key in retained) for key in active}
    quotas = dict.fromkeys(active, 1)
    remaining = sample_size - len(active)
    capacities = {key: counts[key] - 1 for key in active}
    capacity_total = sum(capacities.values())
    if remaining and capacity_total:
        exact = {key: remaining * capacities[key] / capacity_total for key in active}
        for key in active:
            quotas[key] += min(capacities[key], math.floor(exact[key]))
        left = sample_size - sum(quotas.values())
        order = sorted(
            active,
            key=lambda key: (-(exact[key] - math.floor(exact[key])), key),
        )
        for key in order:
            if left == 0:
                break
            if quotas[key] < counts[key]:
                quotas[key] += 1
                left -= 1
    if sum(quotas.values()) != sample_size:
        raise AssertionError("stratified quota allocation did not reconcile")
    return quotas


def _sample_gate2_rows(
    *,
    rows: RowIteratorFactory,
    input_partitions: list[dict[str, Any]],
    output_path: Path,
    manifest_path: Path,
    dataset_id: str,
    revision: str,
    split: str,
    sample_size: int,
) -> tuple[Path, Path]:
    """Three-pass bounded-memory sampling over a replayable row iterator."""

    uuid_counts: Counter[str] = Counter(str(row.get("uuid", "")) for _, row in rows())
    counts: Counter[str] = Counter(_stratum(row, uuid_counts, split) for _, row in rows())
    quotas = _allocate(counts, sample_size)
    heaps: dict[str, list[tuple[int, int, dict[str, Any]]]] = defaultdict(list)
    for row_index, row in rows():
        stratum = _stratum(row, uuid_counts, split)
        quota = quotas[stratum]
        if quota == 0:
            continue
        score = int.from_bytes(
            hashlib.sha256(
                f"gate2-sample-v1\0{dataset_id}\0{revision}\0{split}\0{row_index}".encode()
            ).digest(),
            "big",
        )
        item = (-score, -row_index, row)
        heap = heaps[stratum]
        if len(heap) < quota:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)

    selected: list[tuple[int, int, str, dict[str, Any]]] = []
    for stratum, heap in heaps.items():
        for negative_score, negative_index, row in heap:
            selected.append((-negative_index, -negative_score, stratum, row))
    selected.sort(key=lambda item: item[0])
    if len(selected) != sample_size:
        raise AssertionError(f"selected {len(selected)} rows, expected {sample_size}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row_index, score, stratum, row in selected:
            handle.write(
                json.dumps(
                    {
                        "source_row_index": row_index,
                        "sampling_score_sha256": f"{score:064x}",
                        "sampling_stratum": stratum,
                        "row": row,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    partition_digest_payload = json.dumps(
        input_partitions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    manifest = {
        "schema_version": 2,
        "sampling_version": "gate2_pre_extraction_stratified_hash_v1",
        "dataset_id": dataset_id,
        "revision": revision,
        "split": split,
        "input_partitions": input_partitions,
        "input_partitions_sha256": hashlib.sha256(partition_digest_payload).hexdigest(),
        "output_path": str(output_path),
        "output_sha256": hash_file(output_path),
        "population_rows": sum(counts.values()),
        "sample_rows": sample_size,
        "strata": {
            key: {
                "population": counts[key],
                "quota": quotas[key],
                "sampling_propensity": quotas[key] / counts[key],
            }
            for key in sorted(counts)
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return output_path, manifest_path


def sample_gate2_jsonl(
    *,
    input_path: Path,
    output_path: Path,
    manifest_path: Path,
    dataset_id: str,
    revision: str,
    split: str,
    sample_size: int,
) -> tuple[Path, Path]:
    """Three-pass bounded-memory sampling over fields known before extraction."""
    input_partition = {
        "path": str(input_path),
        "sha256": hash_file(input_path),
    }
    return _sample_gate2_rows(
        rows=lambda: _rows(input_path),
        input_partitions=[input_partition],
        output_path=output_path,
        manifest_path=manifest_path,
        dataset_id=dataset_id,
        revision=revision,
        split=split,
        sample_size=sample_size,
    )


def sample_gate2_arrow_shards(
    *,
    arrow_paths: Sequence[Path],
    output_path: Path,
    manifest_path: Path,
    dataset_id: str,
    revision: str,
    split: str,
    sample_size: int,
    expected_population_rows: int | None = None,
) -> tuple[Path, Path]:
    """Sample directly from pinned Hugging Face Arrow cache shards."""

    paths = tuple(sorted(arrow_paths))
    if not paths:
        raise ValueError("arrow_paths must contain at least one shard")
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing Arrow shards: {missing}")

    from datasets import Dataset

    partitions: list[dict[str, Any]] = []
    population_rows = 0
    for path in paths:
        rows = len(Dataset.from_file(str(path)))
        population_rows += rows
        partitions.append({"path": str(path), "rows": rows, "sha256": hash_file(path)})
    if expected_population_rows is not None and population_rows != expected_population_rows:
        raise ValueError(
            f"Arrow population has {population_rows} rows; expected {expected_population_rows}"
        )

    return _sample_gate2_rows(
        rows=lambda: _arrow_rows(paths),
        input_partitions=partitions,
        output_path=output_path,
        manifest_path=manifest_path,
        dataset_id=dataset_id,
        revision=revision,
        split=split,
        sample_size=sample_size,
    )
