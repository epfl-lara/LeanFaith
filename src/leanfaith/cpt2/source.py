"""Pinned, row-group-bounded source access for the CPT2 audits."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]
from huggingface_hub import HfApi, HfFileSystem


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    repo_id: str
    requested_revision: str
    resolved_revision: str
    parquet_path: str
    parquet_sha256: str
    parquet_bytes: int
    row_count: int
    row_group_count: int
    schema: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class SourceRow:
    source_id: str
    row_group: int
    row_offset: int
    source_code: str
    is_valid: bool

    def __post_init__(self) -> None:
        if type(self.is_valid) is not bool:
            raise TypeError("compiler_data isValid must be preserved as a bool")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inspect_snapshot(
    *,
    repo_id: str,
    revision: str,
    parquet_path: str,
) -> SourceSnapshot:
    """Resolve a revision and inspect Parquet metadata without downloading the corpus."""

    info = HfApi().dataset_info(repo_id, revision=revision, files_metadata=True)
    resolved = str(info.sha)
    siblings = info.siblings or []
    sibling = next(item for item in siblings if item.rfilename == parquet_path)
    parquet_sha = sibling.lfs.sha256 if sibling.lfs is not None else ""
    parquet_bytes = sibling.size or 0
    fs = HfFileSystem()
    remote = f"datasets/{repo_id}@{resolved}/{parquet_path}"
    with fs.open(remote, "rb") as stream:
        parquet = pq.ParquetFile(stream)
        schema = tuple((field.name, str(field.type)) for field in parquet.schema_arrow)
        return SourceSnapshot(
            repo_id=repo_id,
            requested_revision=revision,
            resolved_revision=resolved,
            parquet_path=parquet_path,
            parquet_sha256=parquet_sha,
            parquet_bytes=parquet_bytes,
            row_count=parquet.metadata.num_rows,
            row_group_count=parquet.num_row_groups,
            schema=schema,
        )


def evenly_spaced_row_groups(total: int, count: int) -> tuple[int, ...]:
    if total <= 0 or count <= 0:
        raise ValueError("row-group totals and sample count must be positive")
    if count >= total:
        return tuple(range(total))
    return tuple(round(index * (total - 1) / (count - 1)) for index in range(count))


def _quotas(total: int, buckets: int) -> tuple[int, ...]:
    base, remainder = divmod(total, buckets)
    return tuple(base + (index < remainder) for index in range(buckets))


def read_balanced_sample(
    snapshot: SourceSnapshot,
    *,
    sample_size: int,
    source_shards: int = 8,
) -> tuple[SourceRow, ...]:
    """Read a deterministic, label-balanced sample from bounded row groups."""

    if sample_size <= 0 or sample_size % 2:
        raise ValueError("sample_size must be a positive even integer")
    if snapshot.schema != (
        ("source_code", "large_string"),
        ("validation", "large_string"),
        ("isValid", "bool"),
    ):
        raise ValueError(f"unexpected compiler_data schema: {snapshot.schema!r}")
    intended_groups = evenly_spaced_row_groups(snapshot.row_group_count, source_shards)
    per_group = _quotas(sample_size // 2, len(intended_groups))
    selected: list[SourceRow] = []
    used_groups: set[int] = set()
    fs = HfFileSystem()
    remote = f"datasets/{snapshot.repo_id}@{snapshot.resolved_revision}/{snapshot.parquet_path}"
    with fs.open(remote, "rb") as stream:
        parquet = pq.ParquetFile(stream)
        for group_position, intended_group in enumerate(intended_groups):
            quota = per_group[group_position]
            candidates = sorted(
                range(snapshot.row_group_count),
                key=lambda candidate: (abs(candidate - intended_group), candidate),
            )
            for row_group in candidates:
                if row_group in used_groups:
                    continue
                needed = {False: quota, True: quota}
                group_rows: list[SourceRow] = []
                row_offset = 0
                for batch in parquet.iter_batches(
                    batch_size=1024,
                    row_groups=[row_group],
                    columns=["source_code", "isValid"],
                ):
                    payload = batch.to_pydict()
                    sources = payload["source_code"]
                    labels = payload["isValid"]
                    for local_offset, (source, label) in enumerate(
                        zip(sources, labels, strict=True)
                    ):
                        if not isinstance(source, str) or type(label) is not bool:
                            raise TypeError("compiler_data source_code/isValid schema drift")
                        if needed[label] <= 0:
                            continue
                        absolute_offset = row_offset + local_offset
                        identity = (
                            f"{snapshot.repo_id}\0{snapshot.resolved_revision}\0"
                            f"{snapshot.parquet_path}\0{row_group}\0{absolute_offset}"
                        )
                        group_rows.append(
                            SourceRow(
                                source_id=_sha256_text(identity),
                                row_group=row_group,
                                row_offset=absolute_offset,
                                source_code=source,
                                is_valid=label,
                            )
                        )
                        needed[label] -= 1
                        if needed[False] == 0 and needed[True] == 0:
                            break
                    row_offset += len(sources)
                    if needed[False] == 0 and needed[True] == 0:
                        break
                if needed[False] == 0 and needed[True] == 0:
                    selected.extend(group_rows)
                    used_groups.add(row_group)
                    break
            else:
                raise ValueError(
                    f"no row group can meet balanced quota near intended group {intended_group}"
                )
    if len(selected) != sample_size:
        raise AssertionError(f"expected {sample_size} sampled rows, observed {len(selected)}")
    selected.sort(key=lambda row: (row.row_group, row.row_offset))
    return tuple(selected)


def snapshot_to_dict(snapshot: SourceSnapshot) -> dict[str, Any]:
    return {
        "repo_id": snapshot.repo_id,
        "requested_revision": snapshot.requested_revision,
        "resolved_revision": snapshot.resolved_revision,
        "parquet_path": snapshot.parquet_path,
        "parquet_sha256": snapshot.parquet_sha256,
        "parquet_bytes": snapshot.parquet_bytes,
        "row_count": snapshot.row_count,
        "row_group_count": snapshot.row_group_count,
        "schema": [list(item) for item in snapshot.schema],
    }
