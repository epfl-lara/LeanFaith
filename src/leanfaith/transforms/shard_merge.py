"""Operator-safe discovery and merge for a deterministic shard run.

The scientific merge implementation lives in :mod:`scale_merge`.  This module
adds the production boundary needed by the concurrent shard launcher: discover
the complete ``shard_XX`` set under one output root, reject incomplete or mixed
sets before expensive replay, hold the launcher's lock for the entire merge,
and delegate all scientific validation and content-addressed writes to the
authoritative merger.
"""

from __future__ import annotations

import fcntl
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.config.paths import RepoPaths
from leanfaith.transforms.scale_materializer import (
    DeterministicScaleError,
    DeterministicScaleManifest,
)
from leanfaith.transforms.scale_merge import (
    DeterministicScaleMergeArtifacts,
    merge_deterministic_scale_shards,
)

_SHARD_DIRECTORY = re.compile(r"^shard_(?P<index>[0-9]+)$")


class DeterministicShardMergeError(DeterministicScaleError):
    """Raised when a launcher output root is unsafe or incomplete to merge."""


def _canonical_manifest(path: Path) -> DeterministicScaleManifest:
    if path.is_symlink() or not path.is_file():
        raise DeterministicShardMergeError(
            f"completed shard manifest is not a regular file: {path}"
        )
    try:
        payload = path.read_bytes()
        parsed = DeterministicScaleManifest.model_validate(json.loads(payload))
    except (OSError, ValueError, TypeError) as exc:
        raise DeterministicShardMergeError(
            f"invalid completed shard manifest: {path}: {exc}"
        ) from exc
    expected = canonical_json_bytes(parsed.model_dump(mode="json")) + b"\n"
    if payload != expected:
        raise DeterministicShardMergeError(
            f"completed shard manifest is not canonical JSON: {path}"
        )
    return parsed


def discover_completed_deterministic_shards(
    output_root: Path,
    *,
    expected_shard_count: int | None = None,
) -> tuple[Path, ...]:
    """Return the complete canonical producer set in shard-index order.

    Discovery is intentionally strict.  A directory beginning with ``shard_``
    is never ignored: malformed names, missing manifests, symlinks, duplicate
    numeric indices, mixed shard counts, mixed lineage hashes, gaps, and extra
    producer directories all fail closed.
    """

    if expected_shard_count is not None and expected_shard_count < 1:
        raise ValueError("expected_shard_count must be positive")
    if output_root.is_symlink() or not output_root.is_dir():
        raise DeterministicShardMergeError(
            f"shard output root is not a real directory: {output_root}"
        )
    root = output_root.resolve()
    discovered: dict[int, tuple[Path, DeterministicScaleManifest]] = {}
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if not child.name.startswith("shard_"):
            continue
        match = _SHARD_DIRECTORY.fullmatch(child.name)
        if match is None:
            raise DeterministicShardMergeError(
                f"malformed shard directory under output root: {child}"
            )
        if child.is_symlink() or not child.is_dir():
            raise DeterministicShardMergeError(f"producer shard is not a real directory: {child}")
        index = int(match.group("index"))
        if index in discovered:
            raise DeterministicShardMergeError(f"multiple shard directories encode index {index}")
        manifest = _canonical_manifest(child / "manifest.json")
        if manifest.shard_index != index:
            raise DeterministicShardMergeError(f"shard directory/manifest index mismatch: {child}")
        discovered[index] = (child, manifest)

    if not discovered:
        raise DeterministicShardMergeError(f"no completed shard directories found under {root}")
    first_manifest = next(iter(discovered.values()))[1]
    shard_count = first_manifest.shard_count
    if expected_shard_count is not None and shard_count != expected_shard_count:
        raise DeterministicShardMergeError(
            "discovered shard_count differs from --expected-shard-count: "
            f"{shard_count} != {expected_shard_count}"
        )
    expected_indices = set(range(shard_count))
    actual_indices = set(discovered)
    if actual_indices != expected_indices:
        missing = sorted(expected_indices - actual_indices)
        extra = sorted(actual_indices - expected_indices)
        raise DeterministicShardMergeError(
            f"shard set is incomplete or contains extras; missing={missing} extra={extra}"
        )

    lineage_hash = first_manifest.shard_set_spec_hash
    for index in range(shard_count):
        child, manifest = discovered[index]
        if manifest.shard_count != shard_count:
            raise DeterministicShardMergeError(f"mixed shard_count in producer set: {child}")
        if manifest.shard_set_spec_hash != lineage_hash:
            raise DeterministicShardMergeError(
                f"mixed deterministic shard lineage in producer set: {child}"
            )

    width = max(2, len(str(shard_count - 1)))
    for index, (child, _) in discovered.items():
        expected_name = f"shard_{index:0{width}d}"
        if child.name != expected_name:
            raise DeterministicShardMergeError(
                f"noncanonical shard directory name {child.name!r}; expected {expected_name!r}"
            )
    return tuple(discovered[index][0] for index in range(shard_count))


@contextmanager
def _exclusive_shard_run_lock(output_root: Path) -> Iterator[IO[bytes]]:
    """Exclude launch/resume while discovery, replay, and merge are running."""

    orchestration = output_root.resolve() / "orchestration"
    if orchestration.exists() and (orchestration.is_symlink() or not orchestration.is_dir()):
        raise DeterministicShardMergeError(
            f"orchestration path is not a real directory: {orchestration}"
        )
    orchestration.mkdir(parents=True, exist_ok=True)
    lock_path = orchestration / "run.lock"
    if lock_path.exists() and (lock_path.is_symlink() or not lock_path.is_file()):
        raise DeterministicShardMergeError(f"shard-run lock is not a regular file: {lock_path}")
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeterministicShardMergeError(
                f"deterministic shard launch/resume is still active: {lock_path}"
            ) from exc
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def merge_deterministic_shard_run(
    *,
    paths: RepoPaths,
    output_root: Path,
    output_dir: Path,
    expected_shard_count: int | None = None,
) -> DeterministicScaleMergeArtifacts:
    """Discover and scientifically merge one complete launcher output root.

    The delegated merger validates every input/config/code hash, replays every
    shard through Lean, reconstructs lineage from the immutable source records,
    rejects cross-shard duplicates, writes deterministic projections, and uses
    immutable atomic files.  An interrupted invocation is therefore resumed by
    invoking this function again with the same arguments.
    """

    if output_root.is_symlink() or not output_root.is_dir():
        raise DeterministicShardMergeError(
            f"shard output root is not a real directory: {output_root}"
        )
    if output_dir.is_symlink():
        raise DeterministicShardMergeError(
            f"merged output directory cannot be a symlink: {output_dir}"
        )
    with _exclusive_shard_run_lock(output_root):
        shard_dirs = discover_completed_deterministic_shards(
            output_root,
            expected_shard_count=expected_shard_count,
        )
        return merge_deterministic_scale_shards(
            paths=paths,
            shard_output_dirs=shard_dirs,
            output_dir=output_dir,
        )


__all__ = [
    "DeterministicShardMergeError",
    "discover_completed_deterministic_shards",
    "merge_deterministic_shard_run",
]
