"""Mathlib repository adapter (PLAN.md LF-011): pinned-checkout file inventory.

Produces the deterministic file frame (relative path + content hash) that
LF-012 extraction iterates over via ``FileCommand``. Declaration extraction
itself is Lean-aware and never regex (§8.9, §12.2).
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path, PurePosixPath

from pydantic import Field

from leanfaith.config.hashing import hash_file
from leanfaith.config.models import StrictModel

ADAPTER_VERSION = "mathlib_adapter_v1"
_HASH_CHUNK_SIZE = 1 << 20
_REGULAR_FILE_MODES = frozenset({"100644", "100755"})
_GIT = ("git", "--no-replace-objects")
_PINNED_PROJECT_INPUTS = (
    ".gitattributes",
    ".gitmodules",
    "lakefile.lean",
    "lakefile.toml",
    "lake-manifest.json",
    "lean-toolchain",
    "leanpkg.toml",
)


class RepoFileEntry(StrictModel):
    relative_path: str
    sha256: str


class RepoInventory(StrictModel):
    """Deterministic file frame over a pinned checkout."""

    adapter_version: str = ADAPTER_VERSION
    source: str
    revision: str
    root_module: str
    globs: tuple[str, ...]
    file_count: int
    files: tuple[RepoFileEntry, ...] = Field(repr=False)


class CheckoutMismatchError(RuntimeError):
    """The local checkout is not at the pinned revision."""


@dataclass(frozen=True)
class _GitTreeEntry:
    mode: str
    object_type: str
    object_id: str
    relative_path: str


def verify_checkout_revision(checkout: Path, expected_revision: str) -> str:
    """Fail closed if the checkout's HEAD differs from the pin (§6.2)."""
    result = subprocess.run(
        [*_GIT, "rev-parse", "HEAD"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    head = result.stdout.strip()
    if result.returncode != 0:
        raise CheckoutMismatchError(f"{checkout} is not a git checkout: {result.stderr[:200]}")
    if head != expected_revision:
        raise CheckoutMismatchError(
            f"{checkout} is at {head}, pinned revision is {expected_revision}; "
            "refusing to inventory a drifted checkout"
        )
    return head


def _normalized_pattern_parts(pattern: str) -> tuple[str, ...]:
    if not pattern:
        raise ValueError("glob patterns must not be empty")
    pure_pattern = PurePosixPath(pattern)
    if pure_pattern.is_absolute() or ".." in pure_pattern.parts:
        raise ValueError(f"glob pattern must stay relative to the checkout: {pattern!r}")
    return pure_pattern.parts


@cache
def _matches_glob(relative_path: str, pattern: str) -> bool:
    """Match a Git path with pathlib-style recursive glob semantics."""
    path_parts = PurePosixPath(relative_path).parts
    pattern_parts = _normalized_pattern_parts(pattern)

    @cache
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], pattern_part)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _git_tree(checkout: Path, revision: str) -> tuple[_GitTreeEntry, ...]:
    result = subprocess.run(
        [*_GIT, "ls-tree", "-r", "-z", "--full-tree", revision],
        cwd=checkout,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise CheckoutMismatchError(
            f"cannot read pinned revision {revision} in {checkout}: {stderr[:200]}"
        )

    entries: list[_GitTreeEntry] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, raw_type, raw_object_id = metadata.split(b" ", 2)
        except ValueError as error:
            raise CheckoutMismatchError(
                f"malformed git tree entry at pinned revision {revision}"
            ) from error
        entries.append(
            _GitTreeEntry(
                mode=raw_mode.decode("ascii"),
                object_type=raw_type.decode("ascii"),
                object_id=raw_object_id.decode("ascii"),
                relative_path=raw_path.decode("utf-8", errors="surrogateescape"),
            )
        )
    return tuple(entries)


def _scope_pathspecs(root_module: str, globs: tuple[str, ...]) -> tuple[str, ...]:
    root = PurePosixPath(root_module)
    if not root_module or root.is_absolute() or ".." in root.parts:
        raise ValueError(f"root_module must stay relative to the checkout: {root_module!r}")
    patterns = tuple(_normalized_pattern_parts(pattern) for pattern in globs)
    normalized_globs = (PurePosixPath(*parts).as_posix() for parts in patterns)
    return (
        f":(top,literal){root.as_posix()}",
        f":(top,literal){root.as_posix()}.lean",
        *(f":(top,literal){path}" for path in _PINNED_PROJECT_INPUTS),
        *(f":(top,glob){pattern}" for pattern in normalized_globs),
    )


def _is_pinned_scope_path(
    relative_path: str,
    *,
    root_module: str,
    globs: tuple[str, ...],
) -> bool:
    root = PurePosixPath(root_module).as_posix()
    return (
        relative_path == root
        or relative_path.startswith(f"{root}/")
        or relative_path == f"{root}.lean"
        or relative_path in _PINNED_PROJECT_INPUTS
        or any(_matches_glob(relative_path, pattern) for pattern in globs)
    )


def _require_regular_tree_entry(entry: _GitTreeEntry) -> None:
    if entry.mode == "120000":
        raise CheckoutMismatchError(f"pinned scope path {entry.relative_path} is a symbolic link")
    if entry.object_type != "blob" or entry.mode not in _REGULAR_FILE_MODES:
        raise CheckoutMismatchError(
            f"pinned scope path {entry.relative_path} is not a regular Git blob "
            f"(mode={entry.mode}, type={entry.object_type})"
        )


def _verify_clean_inventory_scope(
    checkout: Path,
    *,
    root_module: str,
    globs: tuple[str, ...],
) -> None:
    pathspecs = _scope_pathspecs(root_module, globs)
    result = subprocess.run(
        [
            *_GIT,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--",
            *pathspecs,
        ],
        cwd=checkout,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise CheckoutMismatchError(
            f"cannot verify pinned inventory scope in {checkout}: {stderr[:200]}"
        )
    if result.stdout:
        status = result.stdout.decode("utf-8", errors="replace").replace("\0", "; ").strip("; ")
        raise CheckoutMismatchError(
            f"{checkout} has changes in the pinned inventory scope ({status[:300]}); "
            "refusing to inventory working-tree drift"
        )

    index = subprocess.run(
        [*_GIT, "ls-files", "-v", "-z", "--", *pathspecs],
        cwd=checkout,
        capture_output=True,
        check=False,
    )
    if index.returncode != 0:
        stderr = index.stderr.decode("utf-8", errors="replace")
        raise CheckoutMismatchError(
            f"cannot verify pinned inventory index state in {checkout}: {stderr[:200]}"
        )
    hidden = [
        record for record in index.stdout.split(b"\0") if record and not record.startswith(b"H ")
    ]
    if hidden:
        state = b"; ".join(hidden).decode("utf-8", errors="replace")
        raise CheckoutMismatchError(
            f"{checkout} has hidden or nonstandard index state in the pinned inventory "
            f"scope ({state[:300]})"
        )


def _hash_git_blobs(
    checkout: Path,
    entries: tuple[_GitTreeEntry, ...],
) -> tuple[RepoFileEntry, ...]:
    if not entries:
        return ()

    files: list[RepoFileEntry] = []
    hashes_by_object_id: dict[str, str] = {}
    with subprocess.Popen(
        [*_GIT, "cat-file", "--batch"],
        cwd=checkout,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise CheckoutMismatchError("failed to open git cat-file pipes")
        for entry in entries:
            sha256 = hashes_by_object_id.get(entry.object_id)
            if sha256 is None:
                process.stdin.write(entry.object_id.encode("ascii") + b"\n")
                process.stdin.flush()
                header = process.stdout.readline().rstrip(b"\n")
                fields = header.split()
                if len(fields) != 3:
                    raise CheckoutMismatchError(
                        f"cannot read pinned Git blob for {entry.relative_path}: "
                        f"{header.decode('utf-8', errors='replace')[:200]}"
                    )
                raw_object_id, object_type, raw_size = fields
                if raw_object_id.decode("ascii") != entry.object_id or object_type != b"blob":
                    raise CheckoutMismatchError(
                        f"unexpected Git object for pinned path {entry.relative_path}"
                    )
                try:
                    remaining = int(raw_size)
                except ValueError as error:
                    raise CheckoutMismatchError(
                        f"invalid Git blob size for pinned path {entry.relative_path}"
                    ) from error

                digest = hashlib.sha256()
                while remaining:
                    chunk = process.stdout.read(min(remaining, _HASH_CHUNK_SIZE))
                    if not chunk:
                        raise CheckoutMismatchError(
                            f"truncated Git blob for pinned path {entry.relative_path}"
                        )
                    digest.update(chunk)
                    remaining -= len(chunk)
                if process.stdout.read(1) != b"\n":
                    raise CheckoutMismatchError(
                        f"malformed Git blob terminator for pinned path {entry.relative_path}"
                    )
                sha256 = digest.hexdigest()
                hashes_by_object_id[entry.object_id] = sha256
            files.append(RepoFileEntry(relative_path=entry.relative_path, sha256=sha256))

        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        returncode = process.wait()
        if returncode != 0:
            raise CheckoutMismatchError(f"git cat-file failed in {checkout}: {stderr[:200]}")
    return tuple(files)


def _verify_worktree_files(
    checkout: Path,
    entries: tuple[_GitTreeEntry, ...],
    files: tuple[RepoFileEntry, ...],
) -> None:
    checked_directories: set[Path] = set()
    for entry, file in zip(entries, files, strict=True):
        parts = PurePosixPath(entry.relative_path).parts
        current = checkout
        for part in parts[:-1]:
            current /= part
            if current in checked_directories:
                continue
            try:
                directory_mode = current.lstat().st_mode
            except OSError as error:
                raise CheckoutMismatchError(
                    f"pinned inventory directory is unavailable: {current}"
                ) from error
            if stat.S_ISLNK(directory_mode):
                raise CheckoutMismatchError(
                    f"pinned inventory path traverses a symbolic link: {current}"
                )
            if not stat.S_ISDIR(directory_mode):
                raise CheckoutMismatchError(
                    f"pinned inventory path traverses a non-directory: {current}"
                )
            checked_directories.add(current)

        path = checkout.joinpath(*parts)
        try:
            worktree_mode = path.lstat().st_mode
        except OSError as error:
            raise CheckoutMismatchError(
                f"pinned inventory file is unavailable in the worktree: {path}"
            ) from error
        if stat.S_ISLNK(worktree_mode):
            raise CheckoutMismatchError(f"pinned inventory path is a symbolic link: {path}")
        if not stat.S_ISREG(worktree_mode):
            raise CheckoutMismatchError(f"pinned inventory path is not a regular file: {path}")
        expected_executable = entry.mode == "100755"
        observed_executable = bool(worktree_mode & stat.S_IXUSR)
        if observed_executable != expected_executable:
            raise CheckoutMismatchError(
                f"worktree mode differs from pinned Git mode for {entry.relative_path}"
            )
        try:
            observed_sha256 = hash_file(path)
        except OSError as error:
            raise CheckoutMismatchError(f"cannot hash pinned worktree file: {path}") from error
        if observed_sha256 != file.sha256:
            raise CheckoutMismatchError(
                f"worktree bytes differ from pinned Git blob for {entry.relative_path}"
            )


def build_inventory(
    checkout: Path,
    *,
    source: str,
    revision: str,
    root_module: str,
    globs: tuple[str, ...],
    limit: int | None = None,
) -> RepoInventory:
    """Hash exact pinned-tree bytes for every glob-matched file, deterministically."""
    verify_checkout_revision(checkout, revision)
    _verify_clean_inventory_scope(checkout, root_module=root_module, globs=globs)

    matched: list[_GitTreeEntry] = []
    attested: list[_GitTreeEntry] = []
    for entry in _git_tree(checkout, revision):
        inventory_match = any(_matches_glob(entry.relative_path, pattern) for pattern in globs)
        if not _is_pinned_scope_path(
            entry.relative_path,
            root_module=root_module,
            globs=globs,
        ):
            continue
        _require_regular_tree_entry(entry)
        attested.append(entry)
        if inventory_match:
            matched.append(entry)

    unique = sorted(matched, key=lambda entry: entry.relative_path)
    if limit is not None:
        unique = unique[:limit]
    selected = tuple(unique)
    attested_entries = tuple(sorted(attested, key=lambda entry: entry.relative_path))
    attested_files = _hash_git_blobs(checkout, attested_entries)
    _verify_worktree_files(checkout, attested_entries, attested_files)
    attested_by_path = {file.relative_path: file for file in attested_files}
    files = tuple(attested_by_path[entry.relative_path] for entry in selected)

    # Catch checkout mutations that raced the exact-tree read.
    _verify_clean_inventory_scope(checkout, root_module=root_module, globs=globs)
    verify_checkout_revision(checkout, revision)
    return RepoInventory(
        source=source,
        revision=revision,
        root_module=root_module,
        globs=globs,
        file_count=len(files),
        files=files,
    )
