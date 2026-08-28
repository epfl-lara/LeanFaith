"""Content-addressed, domain-stratified public mathlib file frames.

The ordinary :mod:`leanfaith.sources.mathlib` inventory records every file in
one pinned checkout.  This module deterministically freezes a smaller public
file frame for scale-out extraction.  Selection is proportional to the first
module component below ``Mathlib/`` and uses a content-bound hash rank within
each component.

The frame deliberately stores exact relative paths and source-file hashes.
Replay therefore fails closed if the pin, inventory, selection seed, or any
selected source bytes drift.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.sources.mathlib import ADAPTER_VERSION, RepoFileEntry, RepoInventory

MATHLIB_FRAME_SCHEMA_VERSION = 1
MATHLIB_FRAME_ALGORITHM = "mathlib_domain_progressive_proportional_hash_v1"

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_FRAME_ID = r"^mathlib_file_frame_v1:[0-9a-f]{64}$"
_INVENTORY_ID = r"^mathlib_repo_inventory_v1:[0-9a-f]{64}$"


class MathlibFrameError(ValueError):
    """A public mathlib frame cannot be built or replayed safely."""


class MathlibFrameMember(StrictModel):
    """One exact source file selected into the frozen frame."""

    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    domain: str = Field(min_length=1)
    selection_rank_sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _path_domain_agree(self) -> Self:
        expected = mathlib_domain(self.relative_path)
        if self.domain != expected:
            raise ValueError(
                f"member domain {self.domain!r} differs from path-derived domain {expected!r}"
            )
        return self


class MathlibDomainAllocation(StrictModel):
    """Progressive proportional allocation for one mathlib domain."""

    domain: str = Field(min_length=1)
    inventory_file_count: int = Field(ge=1)
    selected_file_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _selection_fits_domain(self) -> Self:
        if self.selected_file_count > self.inventory_file_count:
            raise ValueError("selected_file_count exceeds inventory_file_count")
        return self


class MathlibFileFrame(StrictModel):
    """Immutable semantic description of one selected public file frame."""

    schema_version: Literal[1] = 1
    frame_id: str = Field(pattern=_FRAME_ID)
    selection_algorithm: Literal["mathlib_domain_progressive_proportional_hash_v1"] = (
        "mathlib_domain_progressive_proportional_hash_v1"
    )
    source: Literal["mathlib"] = "mathlib"
    revision: str = Field(pattern=_HEX40)
    private_source: Literal[False] = False
    release_eligible: Literal[True] = True
    inventory_adapter_version: Literal["mathlib_adapter_v1"] = "mathlib_adapter_v1"
    inventory_id: str = Field(pattern=_INVENTORY_ID)
    inventory_file_count: int = Field(ge=1)
    eligible_file_count: int = Field(ge=1)
    excluded_file_count: int = Field(ge=0)
    excluded_domains: tuple[str, ...] = ()
    target_file_count: int = Field(ge=1)
    selected_file_count: int = Field(ge=1)
    selection_seed_sha256: str = Field(pattern=_HEX64)
    domain_allocations: tuple[MathlibDomainAllocation, ...]
    members: tuple[MathlibFrameMember, ...] = Field(repr=False)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _exact_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != MATHLIB_FRAME_SCHEMA_VERSION:
            raise ValueError("mathlib file-frame schema_version must be the JSON integer 1")
        return value

    @model_validator(mode="after")
    def _coherent_and_content_addressed(self) -> Self:
        domains = tuple(item.domain for item in self.domain_allocations)
        if domains != tuple(sorted(domains)) or len(domains) != len(set(domains)):
            raise ValueError("domain_allocations must be unique and sorted by domain")
        if self.excluded_domains != tuple(sorted(self.excluded_domains)) or len(
            self.excluded_domains
        ) != len(set(self.excluded_domains)):
            raise ValueError("excluded_domains must be unique and sorted")
        if set(domains) & set(self.excluded_domains):
            raise ValueError("excluded domains cannot appear in domain_allocations")
        if sum(item.inventory_file_count for item in self.domain_allocations) != (
            self.eligible_file_count
        ):
            raise ValueError("eligible domain inventory counts do not reconcile")
        if self.eligible_file_count + self.excluded_file_count != self.inventory_file_count:
            raise ValueError("eligible/excluded inventory counts do not reconcile")
        if sum(item.selected_file_count for item in self.domain_allocations) != (
            self.selected_file_count
        ):
            raise ValueError("domain selected counts do not reconcile")
        if self.target_file_count != self.selected_file_count:
            raise ValueError("target_file_count must equal selected_file_count")
        if self.selected_file_count > self.eligible_file_count:
            raise ValueError("selected frame exceeds the eligible pinned inventory")

        paths = tuple(member.relative_path for member in self.members)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("members must have unique paths sorted lexicographically")
        if len(self.members) != self.selected_file_count:
            raise ValueError("member count differs from selected_file_count")

        actual_counts: dict[str, int] = {}
        for member in self.members:
            actual_counts[member.domain] = actual_counts.get(member.domain, 0) + 1
        expected_counts = {
            item.domain: item.selected_file_count
            for item in self.domain_allocations
            if item.selected_file_count
        }
        if actual_counts != expected_counts:
            raise ValueError("member domains do not match domain allocations")

        expected_id = make_mathlib_frame_id(self.model_dump(mode="json", exclude={"frame_id"}))
        if self.frame_id != expected_id:
            raise ValueError("frame_id differs from normalized frame content")
        return self


def _validated_revision(revision: str) -> str:
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise MathlibFrameError("mathlib revision must be a lowercase 40-hex commit")
    return revision


def mathlib_domain(relative_path: str) -> str:
    """Return the first module component below ``Mathlib/``.

    The two direct modules ``Mathlib/Init.lean`` and ``Mathlib/Tactic.lean``
    use their file stem as the domain.  All nested modules use their first
    directory component, for example ``Mathlib/Algebra/...`` -> ``Algebra``.
    """

    if (
        not relative_path
        or relative_path != relative_path.strip()
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise MathlibFrameError(f"invalid mathlib relative path: {relative_path!r}")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or path.as_posix() != relative_path:
        raise MathlibFrameError(f"non-canonical mathlib relative path: {relative_path!r}")
    parts = path.parts
    if (
        len(parts) < 2
        or parts[0] != "Mathlib"
        or any(part in {"", ".", ".."} for part in parts)
        or path.suffix != ".lean"
    ):
        raise MathlibFrameError(f"path is not a public Mathlib Lean source: {relative_path!r}")
    return path.stem if len(parts) == 2 else parts[1]


def _canonical_inventory_files(inventory: RepoInventory) -> tuple[RepoFileEntry, ...]:
    if inventory.source != "mathlib":
        raise MathlibFrameError("public mathlib frame requires inventory.source == 'mathlib'")
    if inventory.root_module != "Mathlib":
        raise MathlibFrameError("public mathlib frame requires root_module == 'Mathlib'")
    if inventory.adapter_version != ADAPTER_VERSION:
        raise MathlibFrameError(
            f"unsupported mathlib inventory adapter {inventory.adapter_version!r}"
        )
    _validated_revision(inventory.revision)
    if inventory.file_count != len(inventory.files):
        raise MathlibFrameError("inventory file_count does not match its file entries")
    if not inventory.files:
        raise MathlibFrameError("mathlib inventory is empty")

    by_path: dict[str, RepoFileEntry] = {}
    for entry in inventory.files:
        mathlib_domain(entry.relative_path)
        if len(entry.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in entry.sha256
        ):
            raise MathlibFrameError(
                f"invalid inventory hash for {entry.relative_path!r}: {entry.sha256!r}"
            )
        if entry.relative_path in by_path:
            raise MathlibFrameError(f"duplicate inventory path: {entry.relative_path}")
        by_path[entry.relative_path] = entry
    return tuple(by_path[path] for path in sorted(by_path))


def make_mathlib_inventory_id(inventory: RepoInventory) -> str:
    """Hash the normalized pinned inventory independently of entry ordering."""

    files = _canonical_inventory_files(inventory)
    content = {
        "schema": "mathlib_repo_inventory_v1",
        "adapter_version": inventory.adapter_version,
        "source": inventory.source,
        "revision": inventory.revision,
        "root_module": inventory.root_module,
        "globs": tuple(sorted(inventory.globs)),
        "file_count": len(files),
        "files": tuple(entry.model_dump(mode="json") for entry in files),
    }
    return "mathlib_repo_inventory_v1:" + hash_canonical(content)


def make_mathlib_frame_id(content: dict[str, object]) -> str:
    """Return the semantic content ID for one normalized frame payload."""

    return "mathlib_file_frame_v1:" + hash_canonical({"schema": "mathlib_file_frame_v1", **content})


def _domain_allocations(
    files: tuple[RepoFileEntry, ...],
    *,
    target_file_count: int,
) -> tuple[MathlibDomainAllocation, ...]:
    grouped: dict[str, list[RepoFileEntry]] = {}
    for entry in files:
        grouped.setdefault(mathlib_domain(entry.relative_path), []).append(entry)

    total = len(files)
    # Progressive largest-deficit apportionment gives a proportional,
    # deterministic sequence rather than recomputing an independent Hamilton
    # allocation at each target.  Consequently frame(target=n) is always a
    # member subset of frame(target=n+k), which lets extraction add disjoint
    # strata after measuring actual declaration yield.
    selected = dict.fromkeys(grouped, 0)
    for progressive_total in range(1, target_file_count + 1):
        eligible = (domain for domain in grouped if selected[domain] < len(grouped[domain]))
        domain = min(
            eligible,
            key=lambda candidate: (
                -(progressive_total * len(grouped[candidate]) - selected[candidate] * total),
                candidate,
            ),
        )
        selected[domain] += 1

    return tuple(
        MathlibDomainAllocation(
            domain=domain,
            inventory_file_count=len(grouped[domain]),
            selected_file_count=selected[domain],
        )
        for domain in sorted(grouped)
    )


def _seed_digest(selection_seed: str) -> str:
    if (
        not isinstance(selection_seed, str)
        or not selection_seed
        or selection_seed != selection_seed.strip()
        or "\x00" in selection_seed
    ):
        raise MathlibFrameError("selection_seed must be nonempty stripped text without NUL")
    return hash_canonical(
        {"schema": "mathlib_file_frame_selection_seed_v1", "selection_seed": selection_seed}
    )


def build_mathlib_file_frame(
    inventory: RepoInventory,
    *,
    expected_revision: str,
    target_file_count: int,
    selection_seed: str,
    excluded_domains: tuple[str, ...] = (),
) -> MathlibFileFrame:
    """Select and freeze an exact proportional public mathlib file frame."""

    expected_revision = _validated_revision(expected_revision)
    files = _canonical_inventory_files(inventory)
    if inventory.revision != expected_revision:
        raise MathlibFrameError(
            f"inventory revision {inventory.revision} differs from expected {expected_revision}"
        )
    available_domains = {mathlib_domain(entry.relative_path) for entry in files}
    if excluded_domains != tuple(sorted(excluded_domains)) or len(excluded_domains) != len(
        set(excluded_domains)
    ):
        raise MathlibFrameError("excluded_domains must be unique and sorted")
    for domain in excluded_domains:
        if (
            not domain
            or domain != domain.strip()
            or "/" in domain
            or "\\" in domain
            or "\x00" in domain
        ):
            raise MathlibFrameError(f"invalid excluded mathlib domain: {domain!r}")
        if domain not in available_domains:
            raise MathlibFrameError(f"excluded mathlib domain is absent from inventory: {domain}")
    eligible_files = tuple(
        entry for entry in files if mathlib_domain(entry.relative_path) not in excluded_domains
    )
    if not eligible_files:
        raise MathlibFrameError("domain exclusions remove every mathlib file")
    if type(target_file_count) is not int or target_file_count < 1:
        raise MathlibFrameError("target_file_count must be a positive integer")
    if target_file_count > len(eligible_files):
        raise MathlibFrameError(
            f"target_file_count {target_file_count} exceeds eligible inventory size "
            f"{len(eligible_files)}"
        )
    seed_sha256 = _seed_digest(selection_seed)
    allocations = _domain_allocations(eligible_files, target_file_count=target_file_count)
    allocation_by_domain = {item.domain: item.selected_file_count for item in allocations}

    selected: list[MathlibFrameMember] = []
    grouped: dict[str, list[tuple[str, RepoFileEntry]]] = {}
    for entry in eligible_files:
        domain = mathlib_domain(entry.relative_path)
        rank = hash_canonical(
            {
                "schema": MATHLIB_FRAME_ALGORITHM,
                "selection_seed": selection_seed,
                "source": "mathlib",
                "revision": inventory.revision,
                "domain": domain,
                "relative_path": entry.relative_path,
                "sha256": entry.sha256,
            }
        )
        grouped.setdefault(domain, []).append((rank, entry))

    for domain in sorted(grouped):
        ranked = sorted(grouped[domain], key=lambda item: (item[0], item[1].relative_path))
        for rank, entry in ranked[: allocation_by_domain[domain]]:
            selected.append(
                MathlibFrameMember(
                    relative_path=entry.relative_path,
                    sha256=entry.sha256,
                    domain=domain,
                    selection_rank_sha256=rank,
                )
            )
    members = tuple(sorted(selected, key=lambda item: item.relative_path))

    content: dict[str, object] = {
        "schema_version": MATHLIB_FRAME_SCHEMA_VERSION,
        "selection_algorithm": MATHLIB_FRAME_ALGORITHM,
        "source": "mathlib",
        "revision": inventory.revision,
        "private_source": False,
        "release_eligible": True,
        "inventory_adapter_version": inventory.adapter_version,
        "inventory_id": make_mathlib_inventory_id(inventory),
        "inventory_file_count": len(files),
        "eligible_file_count": len(eligible_files),
        "excluded_file_count": len(files) - len(eligible_files),
        "excluded_domains": excluded_domains,
        "target_file_count": target_file_count,
        "selected_file_count": len(members),
        "selection_seed_sha256": seed_sha256,
        "domain_allocations": tuple(item.model_dump(mode="json") for item in allocations),
        "members": tuple(item.model_dump(mode="json") for item in members),
    }
    return MathlibFileFrame.model_validate({"frame_id": make_mathlib_frame_id(content), **content})


def verify_mathlib_file_frame(
    frame: MathlibFileFrame,
    inventory: RepoInventory,
    *,
    expected_revision: str,
    selection_seed: str,
) -> None:
    """Replay selection and require byte-semantic equality with ``frame``."""

    replayed = build_mathlib_file_frame(
        inventory,
        expected_revision=expected_revision,
        target_file_count=frame.target_file_count,
        selection_seed=selection_seed,
        excluded_domains=frame.excluded_domains,
    )
    if frame != replayed:
        raise MathlibFrameError(
            "mathlib frame differs from deterministic replay; "
            "inventory, revision, seed, or selection content drifted"
        )


def mathlib_frame_additions(
    previous: MathlibFileFrame,
    expanded: MathlibFileFrame,
) -> tuple[MathlibFrameMember, ...]:
    """Return the exact additive stratum between two cumulative frames.

    Both frames must bind the same full inventory, revision, algorithm, and
    seed.  Callers should replay-verify each frame against that full inventory
    before using the returned files for extraction.
    """

    identity_fields = (
        "selection_algorithm",
        "source",
        "revision",
        "private_source",
        "release_eligible",
        "inventory_adapter_version",
        "inventory_id",
        "inventory_file_count",
        "eligible_file_count",
        "excluded_file_count",
        "excluded_domains",
        "selection_seed_sha256",
    )
    if any(getattr(previous, field) != getattr(expanded, field) for field in identity_fields):
        raise MathlibFrameError("additive frames do not bind the same inventory and selection")
    if expanded.target_file_count <= previous.target_file_count:
        raise MathlibFrameError("expanded frame target must be larger than the previous target")

    previous_by_path = {member.relative_path: member for member in previous.members}
    expanded_by_path = {member.relative_path: member for member in expanded.members}
    if any(expanded_by_path.get(path) != member for path, member in previous_by_path.items()):
        raise MathlibFrameError("expanded frame does not preserve every previous member")
    additions = tuple(
        member for member in expanded.members if member.relative_path not in previous_by_path
    )
    if len(additions) != expanded.target_file_count - previous.target_file_count:
        raise MathlibFrameError("additive frame delta does not reconcile target counts")
    return additions


def _absolute_without_resolve(path: Path) -> Path:
    """Make ``path`` absolute without following any filesystem entry."""

    return Path(os.path.abspath(os.fspath(path)))


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_directory_chain(path: Path, *, create: bool) -> int:
    """Open an absolute directory component-by-component without following links."""

    absolute = _absolute_without_resolve(path)
    flags = _directory_open_flags()
    try:
        current = os.open("/", flags)
    except OSError as exc:  # pragma: no cover - a functioning POSIX process has a root
        raise MathlibFrameError("filesystem root cannot be opened safely") from exc
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o755, dir_fd=current)
                child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode):
            raise MathlibFrameError(f"mathlib frame parent is not a directory: {absolute}")
        return current
    except OSError as exc:
        os.close(current)
        raise MathlibFrameError(
            f"mathlib frame directory path is missing, invalid, or contains a symlink: {absolute}"
        ) from exc
    except BaseException:
        os.close(current)
        raise


def _read_fd_bytes(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular_file_at(
    *,
    parent_descriptor: int,
    filename: str,
    absolute: Path,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(filename, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise MathlibFrameError(
            f"mathlib frame cannot be opened without following symlinks: {absolute}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MathlibFrameError(f"mathlib frame is not a regular file: {absolute}")
        payload = _read_fd_bytes(descriptor)
        after = os.fstat(descriptor)
        if _stable_file_identity(after) != _stable_file_identity(before):
            raise MathlibFrameError(f"mathlib frame changed while it was read: {absolute}")
        return payload
    finally:
        os.close(descriptor)


def _read_regular_file_nofollow(path: Path) -> tuple[Path, bytes]:
    absolute = _absolute_without_resolve(path)
    if not absolute.name:
        raise MathlibFrameError(f"mathlib frame is not a file path: {absolute}")
    parent_descriptor = _open_directory_chain(absolute.parent, create=False)
    try:
        return absolute, _read_regular_file_at(
            parent_descriptor=parent_descriptor,
            filename=absolute.name,
            absolute=absolute,
        )
    finally:
        os.close(parent_descriptor)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(text: str) -> float:
    raise ValueError(f"non-finite JSON constant {text!r}")


def _parse_mathlib_file_frame(path: Path, payload: bytes) -> MathlibFileFrame:
    try:
        data = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise MathlibFrameError(f"invalid mathlib frame {path}: {exc}") from exc
    try:
        return MathlibFileFrame.model_validate(data)
    except ValidationError as exc:
        raise MathlibFrameError(f"invalid mathlib frame {path}:\n{exc}") from exc


def write_mathlib_file_frame(frame: MathlibFileFrame, path: Path) -> str:
    """Write canonical immutable frame bytes and return their SHA-256."""

    payload = canonical_json_bytes(frame.model_dump(mode="json")) + b"\n"
    absolute = _absolute_without_resolve(path)
    if not absolute.name:
        raise MathlibFrameError(f"mathlib frame is not a file path: {absolute}")
    parent_descriptor = _open_directory_chain(absolute.parent, create=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(
            absolute.name,
            flags,
            0o644,
            dir_fd=parent_descriptor,
        )
    except FileExistsError:
        try:
            existing = _read_regular_file_at(
                parent_descriptor=parent_descriptor,
                filename=absolute.name,
                absolute=absolute,
            )
        except MathlibFrameError:
            raise MathlibFrameError(f"immutable mathlib frame conflicts with {absolute}") from None
        finally:
            os.close(parent_descriptor)
        if existing != payload:
            raise MathlibFrameError(f"immutable mathlib frame conflicts with {absolute}") from None
        return sha256_hex(existing)
    except OSError as exc:
        os.close(parent_descriptor)
        raise MathlibFrameError(
            f"immutable mathlib frame cannot be created safely: {absolute}"
        ) from exc
    created: os.stat_result | None = None
    try:
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode):
            raise MathlibFrameError(f"mathlib frame is not a regular file: {absolute}")
        view = memoryview(payload)
        written = 0
        while written < len(view):
            chunk_size = os.write(descriptor, view[written:])
            if chunk_size == 0:
                raise MathlibFrameError("mathlib frame write made no progress")
            written += chunk_size
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
    except BaseException:
        if created is not None:
            try:
                current = os.stat(
                    absolute.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(absolute.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except OSError:
                pass
        raise
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)
    return sha256_hex(payload)


def load_and_verify_mathlib_file_frame(
    path: Path,
    *,
    inventory: RepoInventory,
    expected_revision: str,
    selection_seed: str,
) -> MathlibFileFrame:
    """Load canonical JSON, validate its content ID, and replay selection."""

    absolute, payload = _read_regular_file_nofollow(path)
    frame = _parse_mathlib_file_frame(absolute, payload)
    canonical = canonical_json_bytes(frame.model_dump(mode="json")) + b"\n"
    if payload != canonical:
        raise MathlibFrameError(f"mathlib frame is not canonical JSON: {absolute}")
    verify_mathlib_file_frame(
        frame,
        inventory,
        expected_revision=expected_revision,
        selection_seed=selection_seed,
    )
    return frame


__all__ = [
    "MATHLIB_FRAME_ALGORITHM",
    "MATHLIB_FRAME_SCHEMA_VERSION",
    "MathlibDomainAllocation",
    "MathlibFileFrame",
    "MathlibFrameError",
    "MathlibFrameMember",
    "build_mathlib_file_frame",
    "load_and_verify_mathlib_file_frame",
    "make_mathlib_frame_id",
    "make_mathlib_inventory_id",
    "mathlib_domain",
    "mathlib_frame_additions",
    "verify_mathlib_file_frame",
    "write_mathlib_file_frame",
]
