"""Audit-only unique-pair postprocessing for deterministic composition chains.

This module is deliberately separate from :mod:`composition_chain`: chain-v1
remains an immutable gross-lineage receipt.  The postprocessor binds one exact
seed directory to one exact chain directory and groups chains by the canonical
identity ``(original source theorem, final candidate code hash)``.  Reversible
cycles therefore remain auditable without being counted as novel pairs.

No semantic label, promotion, training/evaluation eligibility, or gate credit
is created here.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.representations import NORMALIZATION_VERSION
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import (
    _D0_RULES,
    _E2_RULES,
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
    _canonical_jsonl,
    _canonical_line,
    _without_id,
)
from leanfaith.transforms.composition_seed import CompositionSeedManifest, CompositionSeedRecord
from leanfaith.transforms.scale_materializer import _representation_payload_hash

_HEX64 = r"^[0-9a-f]{64}$"
_UNIQUE_PAIR_ID = r"^detcomp_unique_pair:[0-9a-f]{64}$"
_UNIQUE_PAIR_SET_ID = r"^detcomp_unique_pair_set:[0-9a-f]{64}$"
_CHAIN_FILES = frozenset({"chains.jsonl", "manifest.json"})
_SEED_FILES = frozenset({"seeds.jsonl", "theorems.jsonl", "representations.jsonl", "manifest.json"})
_OUTPUT_FILES = frozenset({"unique_pairs.jsonl", "manifest.json"})

_RENAME_NOREPLACE = 1
_RACE_HOOK: Callable[[str], None] | None = None

type ChainKind = Literal["P_to_P", "P_to_N"]


class CompositionUniquePairError(ValueError):
    """The bound composition inputs or immutable unique-pair replay failed."""


class DeterministicCompositionUniquePairRecord(StrictModel):
    """One exact source/final-code pair with all gross chain provenance."""

    schema_version: Literal[2] = 2
    unique_pair_id: str = Field(pattern=_UNIQUE_PAIR_ID)
    canonical_unique_key: str = Field(pattern=_HEX64)
    input_seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    context_id: str = Field(min_length=1)
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    original_source_theorem_id: str = Field(min_length=1)
    original_source_representation_id: str = Field(min_length=1)
    source_statement_content_hash: str = Field(pattern=_HEX64)
    source_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    intermediate_theorem_ids: tuple[str, ...] = Field(min_length=1)
    intermediate_representation_ids: tuple[str, ...] = Field(min_length=1)
    final_theorem_ids: tuple[str, ...] = Field(min_length=1)
    final_representation_ids: tuple[str, ...] = Field(min_length=1)
    final_candidate_code_hash: str = Field(pattern=_HEX64)
    final_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    chain_ids: tuple[str, ...] = Field(min_length=1)
    chain_sequences: tuple[str, ...] = Field(min_length=1)
    chain_kinds: tuple[ChainKind, ...] = Field(min_length=1)
    gross_chain_count: int = Field(ge=1)
    duplicate_excess_count: int = Field(ge=0)
    source_alpha_return: bool
    alpha_novel: bool
    quality_tier: Literal["provisional"] = "provisional"
    audit_only: Literal[True] = True
    intention_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> DeterministicCompositionUniquePairRecord:
        expected_key = hash_canonical(
            {
                "schema": "deterministic_v2_composition_unique_pair_key_v2",
                "original_source_theorem_id": self.original_source_theorem_id,
                "final_candidate_code_hash": self.final_candidate_code_hash,
            }
        )
        if self.canonical_unique_key != expected_key:
            raise ValueError("canonical unique key does not match source/final code")
        if self.unique_pair_id != f"detcomp_unique_pair:{expected_key}":
            raise ValueError("unique_pair_id does not match canonical unique key")
        for field_name in (
            "root_ancestry_ids",
            "intermediate_theorem_ids",
            "intermediate_representation_ids",
            "final_theorem_ids",
            "final_representation_ids",
            "chain_ids",
            "chain_sequences",
            "chain_kinds",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if self.gross_chain_count != len(self.chain_ids):
            raise ValueError("gross chain count does not match chain IDs")
        if self.duplicate_excess_count != self.gross_chain_count - 1:
            raise ValueError("pair duplicate excess does not reconcile")
        if self.source_alpha_return != (
            self.source_alpha_identity_fingerprint == self.final_alpha_identity_fingerprint
        ):
            raise ValueError("source alpha return does not reconcile")
        if self.alpha_novel == self.source_alpha_return:
            raise ValueError("alpha novelty must be the inverse of source alpha return")
        return self


class DeterministicCompositionUniquePairManifest(StrictModel):
    """Self-authenticating audit manifest for chain-v1 unique pairs."""

    schema_version: Literal[2] = 2
    artifact_kind: Literal["deterministic_v2_composition_unique_pair_set"] = (
        "deterministic_v2_composition_unique_pair_set"
    )
    method_version: Literal["deterministic_v2_composition_unique_pairs_v2"] = (
        "deterministic_v2_composition_unique_pairs_v2"
    )
    unique_pair_set_id: str = Field(pattern=_UNIQUE_PAIR_SET_ID)
    input_seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    input_seed_manifest_sha256: str = Field(pattern=_HEX64)
    input_seed_records_sha256: str = Field(pattern=_HEX64)
    input_seed_theorems_sha256: str = Field(pattern=_HEX64)
    input_seed_representations_sha256: str = Field(pattern=_HEX64)
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    input_chain_manifest_sha256: str = Field(pattern=_HEX64)
    input_chain_records_sha256: str = Field(pattern=_HEX64)
    gross_chain_count: int = Field(ge=0)
    unique_pair_count: int = Field(ge=0)
    duplicate_group_count: int = Field(ge=0)
    duplicate_excess_count: int = Field(ge=0)
    gross_source_alpha_return_count: int = Field(ge=0)
    unique_source_alpha_return_count: int = Field(ge=0)
    gross_alpha_novel_count: int = Field(ge=0)
    unique_alpha_novel_count: int = Field(ge=0)
    gross_chain_kind_counts: dict[str, int]
    unique_pair_chain_kind_membership_counts: dict[str, int]
    gross_sequence_counts: dict[str, int]
    unique_pair_sequence_membership_counts: dict[str, int]
    unique_output: Literal["unique_pairs.jsonl"] = "unique_pairs.jsonl"
    unique_output_sha256: str = Field(pattern=_HEX64)
    audit_only: Literal[True] = True
    reversible_cycles_are_novel: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _reconciles(self) -> DeterministicCompositionUniquePairManifest:
        expected = "detcomp_unique_pair_set:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "unique_pair_set_id")
        )
        if self.unique_pair_set_id != expected:
            raise ValueError("unique_pair_set_id does not match immutable payload")
        if self.unique_pair_count > self.gross_chain_count:
            raise ValueError("unique pair count exceeds gross chain count")
        if self.duplicate_excess_count != self.gross_chain_count - self.unique_pair_count:
            raise ValueError("duplicate excess does not reconcile")
        if self.duplicate_group_count > self.unique_pair_count:
            raise ValueError("duplicate group count exceeds unique pairs")
        counted_distributions = (
            self.gross_chain_kind_counts,
            self.unique_pair_chain_kind_membership_counts,
            self.gross_sequence_counts,
            self.unique_pair_sequence_membership_counts,
        )
        if any(count < 0 for counts in counted_distributions for count in counts.values()):
            raise ValueError("composition distribution counts cannot be negative")
        if sum(self.gross_chain_kind_counts.values()) != self.gross_chain_count:
            raise ValueError("gross chain-kind counts do not reconcile")
        if sum(self.gross_sequence_counts.values()) != self.gross_chain_count:
            raise ValueError("gross sequence counts do not reconcile")
        if (
            self.gross_source_alpha_return_count + self.gross_alpha_novel_count
            != self.gross_chain_count
        ):
            raise ValueError("gross alpha return/novel counts do not reconcile")
        if (
            self.unique_source_alpha_return_count + self.unique_alpha_novel_count
            != self.unique_pair_count
        ):
            raise ValueError("unique alpha return/novel counts do not reconcile")
        membership_counts = (
            self.unique_pair_chain_kind_membership_counts,
            self.unique_pair_sequence_membership_counts,
        )
        if any(
            count > self.unique_pair_count
            for counts in membership_counts
            for count in counts.values()
        ):
            raise ValueError("unique-pair membership count exceeds unique pairs")
        return self


@dataclass(frozen=True, slots=True)
class CompositionUniquePairArtifacts:
    """Paths and counts returned by a new postprocess or exact replay."""

    output_dir: Path
    manifest_path: Path
    unique_pairs_path: Path
    unique_pair_set_id: str
    gross_chain_count: int
    unique_pair_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _ChainInventory:
    root: Path
    manifest: DeterministicCompositionChainManifest
    manifest_sha256: str
    chains: tuple[DeterministicCompositionChainRecord, ...]
    snapshot: _HeldDirectorySnapshot


@dataclass(frozen=True, slots=True)
class _SeedInventory:
    root: Path
    manifest: CompositionSeedManifest
    manifest_sha256: str
    seeds: tuple[CompositionSeedRecord, ...]
    theorems: tuple[TheoremRecord, ...]
    representations: tuple[RepresentationRecord, ...]
    snapshot: _HeldDirectorySnapshot


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> _FileIdentity:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
        )


@dataclass(slots=True)
class _HeldDirectory:
    path: Path
    fd: int
    identity: _FileIdentity
    label: str

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> _HeldDirectory:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(slots=True)
class _HeldFile:
    name: str
    fd: int
    identity: _FileIdentity
    payload: bytes
    sha256: str

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


@dataclass(slots=True)
class _HeldDirectorySnapshot:
    directory: _HeldDirectory
    files: dict[str, _HeldFile]
    expected_names: frozenset[str]
    label: str

    @property
    def file_snapshot(self) -> tuple[tuple[str, str, int], ...]:
        return tuple(
            (name, self.files[name].sha256, self.files[name].identity.size)
            for name in sorted(self.files)
        )

    def payload(self, name: str) -> bytes:
        return self.files[name].payload

    def close(self) -> None:
        for held_file in self.files.values():
            held_file.close()

    def __enter__(self) -> _HeldDirectorySnapshot:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _absolute_path(path: Path) -> Path:
    """Return a normalized absolute path without resolving filesystem links."""

    return Path(os.path.abspath(os.fspath(path)))


def _require_descriptor_support() -> tuple[int, int]:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    no_follow_flag = getattr(os, "O_NOFOLLOW", None)
    required_dir_fd = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir)
    if (
        directory_flag is None
        or no_follow_flag is None
        or any(function not in os.supports_dir_fd for function in required_dir_fd)
        or os.stat not in os.supports_follow_symlinks
    ):
        raise CompositionUniquePairError(
            "descriptor-relative no-follow filesystem operations are unavailable"
        )
    return directory_flag, no_follow_flag


def _directory_open_flags() -> int:
    directory_flag, no_follow_flag = _require_descriptor_support()
    return os.O_RDONLY | directory_flag | no_follow_flag | getattr(os, "O_CLOEXEC", 0)


def _regular_file_open_flags() -> int:
    _, no_follow_flag = _require_descriptor_support()
    return os.O_RDONLY | no_follow_flag | getattr(os, "O_CLOEXEC", 0)


def _invoke_race_hook(event: str) -> None:
    if _RACE_HOOK is not None:
        _RACE_HOOK(event)


def _same_filesystem_object(left: _FileIdentity, right: _FileIdentity) -> bool:
    return (left.device, left.inode, stat.S_IFMT(left.mode)) == (
        right.device,
        right.inode,
        stat.S_IFMT(right.mode),
    )


def _open_held_directory(
    path: Path,
    *,
    label: str,
    create: bool = False,
    forbidden_identities: frozenset[tuple[int, int]] = frozenset(),
) -> _HeldDirectory:
    """Walk an absolute directory path using held, no-follow descriptors.

    ``forbidden_identities`` rejects an exact device/inode alias to a bound
    input root anywhere in the walk.  This catches ordinary bind-mount aliases
    of the root itself; filesystem metadata cannot prove ancestry when a mount
    exposes only a proper descendant of an input root.
    """

    candidate = _absolute_path(path)
    flags = _directory_open_flags()
    try:
        current_fd = os.open(candidate.anchor, flags)
    except OSError as exc:
        raise CompositionUniquePairError(f"{label} root is unavailable: {exc}") from exc
    try:
        current_metadata = os.fstat(current_fd)
    except OSError as exc:
        os.close(current_fd)
        raise CompositionUniquePairError(
            f"{label} root descriptor cannot be verified: {exc}"
        ) from exc
    if (current_metadata.st_dev, current_metadata.st_ino) in forbidden_identities:
        os.close(current_fd)
        raise CompositionUniquePairError(f"{label} aliases a bound input root")
    current_path = Path(candidate.anchor)
    try:
        for part in candidate.parts[1:]:
            current_path /= part
            try:
                metadata = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise CompositionUniquePairError(
                        f"{label} is unavailable: {current_path}"
                    ) from None
                try:
                    os.mkdir(part, mode=0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise CompositionUniquePairError(
                        f"{label} cannot be created: {current_path}: {exc}"
                    ) from exc
                try:
                    metadata = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                except OSError as exc:
                    raise CompositionUniquePairError(
                        f"{label} became unavailable: {current_path}: {exc}"
                    ) from exc
            except OSError as exc:
                raise CompositionUniquePairError(
                    f"{label} is unavailable: {current_path}: {exc}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise CompositionUniquePairError(f"{label} traverses a symlink: {current_path}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise CompositionUniquePairError(
                    f"{label} component is not a directory: {current_path}"
                )
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise CompositionUniquePairError(
                    f"{label} changed or traverses a symlink: {current_path}: {exc}"
                ) from exc
            try:
                child_metadata = os.fstat(child_fd)
            except OSError as exc:
                os.close(child_fd)
                raise CompositionUniquePairError(
                    f"{label} child descriptor cannot be verified: {current_path}: {exc}"
                ) from exc
            if (child_metadata.st_dev, child_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                os.close(child_fd)
                raise CompositionUniquePairError(
                    f"{label} changed during descriptor traversal: {current_path}"
                )
            if (child_metadata.st_dev, child_metadata.st_ino) in forbidden_identities:
                os.close(child_fd)
                raise CompositionUniquePairError(
                    f"{label} aliases a bound input root: {current_path}"
                )
            os.close(current_fd)
            current_fd = child_fd
            current_metadata = child_metadata
        return _HeldDirectory(
            path=candidate,
            fd=current_fd,
            identity=_FileIdentity.from_stat(current_metadata),
            label=label,
        )
    except BaseException:
        os.close(current_fd)
        raise


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _open_held_regular_file(directory: _HeldDirectory, name: str) -> _HeldFile:
    try:
        namespace_metadata = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    except OSError as exc:
        raise CompositionUniquePairError(
            f"{directory.label} input is unavailable: {name}: {exc}"
        ) from exc
    if stat.S_ISLNK(namespace_metadata.st_mode):
        raise CompositionUniquePairError(f"{directory.label} input cannot be a symlink: {name}")
    if not stat.S_ISREG(namespace_metadata.st_mode):
        raise CompositionUniquePairError(f"{directory.label} input is not a regular file: {name}")
    try:
        fd = os.open(name, _regular_file_open_flags(), dir_fd=directory.fd)
    except OSError as exc:
        raise CompositionUniquePairError(
            f"{directory.label} input changed or became unsafe: {name}: {exc}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise CompositionUniquePairError(
                f"{directory.label} input is not a regular file: {name}"
            )
        if (before.st_dev, before.st_ino) != (
            namespace_metadata.st_dev,
            namespace_metadata.st_ino,
        ):
            raise CompositionUniquePairError(
                f"{directory.label} input changed while opening: {name}"
            )
        payload = _read_all(fd)
        after = os.fstat(fd)
        if _FileIdentity.from_stat(before) != _FileIdentity.from_stat(after):
            raise CompositionUniquePairError(
                f"{directory.label} input changed while reading: {name}"
            )
        identity = _FileIdentity.from_stat(after)
        if len(payload) != identity.size:
            raise CompositionUniquePairError(
                f"{directory.label} input size changed while reading: {name}"
            )
        return _HeldFile(
            name=name,
            fd=fd,
            identity=identity,
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
        )
    except BaseException:
        os.close(fd)
        raise


def _snapshot_exact_directory(
    directory: _HeldDirectory,
    *,
    expected_names: frozenset[str],
) -> _HeldDirectorySnapshot:
    try:
        actual = frozenset(os.listdir(directory.fd))
    except (OSError, TypeError) as exc:
        raise CompositionUniquePairError(
            f"{directory.label} cannot be listed through its descriptor: {exc}"
        ) from exc
    if actual != expected_names:
        raise CompositionUniquePairError(f"{directory.label} is not exact")
    files: dict[str, _HeldFile] = {}
    try:
        for name in sorted(expected_names):
            files[name] = _open_held_regular_file(directory, name)
        if frozenset(os.listdir(directory.fd)) != expected_names:
            raise CompositionUniquePairError(
                f"{directory.label} changed while opening its exact files"
            )
        return _HeldDirectorySnapshot(
            directory=directory,
            files=files,
            expected_names=expected_names,
            label=directory.label,
        )
    except BaseException:
        for held_file in files.values():
            held_file.close()
        raise


def _verify_directory_path_identity(directory: _HeldDirectory) -> None:
    with _open_held_directory(directory.path, label=directory.label) as reopened:
        if not _same_filesystem_object(reopened.identity, directory.identity):
            raise CompositionUniquePairError(f"{directory.label} path identity changed")


def _verify_held_snapshot(
    snapshot: _HeldDirectorySnapshot,
    *,
    verify_path_identity: bool = True,
) -> None:
    try:
        actual = frozenset(os.listdir(snapshot.directory.fd))
    except (OSError, TypeError) as exc:
        raise CompositionUniquePairError(
            f"{snapshot.label} cannot be re-listed through its descriptor: {exc}"
        ) from exc
    if actual != snapshot.expected_names:
        raise CompositionUniquePairError(f"{snapshot.label} changed during postprocessing")
    for name, held_file in snapshot.files.items():
        namespace_metadata = os.stat(
            name,
            dir_fd=snapshot.directory.fd,
            follow_symlinks=False,
        )
        if (namespace_metadata.st_dev, namespace_metadata.st_ino) != (
            held_file.identity.device,
            held_file.identity.inode,
        ):
            raise CompositionUniquePairError(
                f"{snapshot.label} file identity changed during postprocessing: {name}"
            )
        before = _FileIdentity.from_stat(os.fstat(held_file.fd))
        payload = _read_all(held_file.fd)
        after = _FileIdentity.from_stat(os.fstat(held_file.fd))
        if before != held_file.identity or after != held_file.identity:
            raise CompositionUniquePairError(
                f"{snapshot.label} file metadata changed during postprocessing: {name}"
            )
        if payload != held_file.payload:
            raise CompositionUniquePairError(
                f"{snapshot.label} file content changed during postprocessing: {name}"
            )
    if verify_path_identity:
        _verify_directory_path_identity(snapshot.directory)


def _load_canonical_jsonl_bytes[ModelT: StrictModel](
    payload: bytes,
    model: type[ModelT],
    *,
    label: str,
) -> tuple[ModelT, ...]:
    output: list[ModelT] = []
    for line_number, raw_line in enumerate(payload.splitlines(keepends=True), start=1):
        if not raw_line.endswith(b"\n") or not raw_line.strip():
            raise CompositionUniquePairError(f"invalid JSONL framing in {label}:{line_number}")
        try:
            item = model.model_validate_json(raw_line)
        except ValueError as exc:
            raise CompositionUniquePairError(
                f"invalid {model.__name__} in {label}:{line_number}: {exc}"
            ) from exc
        if raw_line != _canonical_line(item):
            raise CompositionUniquePairError(
                f"non-canonical {model.__name__} in {label}:{line_number}"
            )
        output.append(item)
    return tuple(output)


def _load_seed_inventory(snapshot: _HeldDirectorySnapshot) -> _SeedInventory:
    raw_manifest = snapshot.payload("manifest.json")
    try:
        manifest = CompositionSeedManifest.model_validate_json(raw_manifest)
    except ValueError as exc:
        raise CompositionUniquePairError(f"invalid composition seed manifest: {exc}") from exc
    if raw_manifest != _canonical_line(manifest):
        raise CompositionUniquePairError("composition seed manifest is not canonical")
    expected_hashes = {
        manifest.seed_output: manifest.seed_output_sha256,
        manifest.theorem_output: manifest.theorem_output_sha256,
        manifest.representation_output: manifest.representation_output_sha256,
    }
    for name, expected_hash in expected_hashes.items():
        if snapshot.files[name].sha256 != expected_hash:
            raise CompositionUniquePairError(
                f"composition seed {name} partition differs from manifest"
            )
    seeds = _load_canonical_jsonl_bytes(
        snapshot.payload(manifest.seed_output),
        CompositionSeedRecord,
        label=manifest.seed_output,
    )
    theorems = _load_canonical_jsonl_bytes(
        snapshot.payload(manifest.theorem_output),
        TheoremRecord,
        label=manifest.theorem_output,
    )
    representations = _load_canonical_jsonl_bytes(
        snapshot.payload(manifest.representation_output),
        RepresentationRecord,
        label=manifest.representation_output,
    )
    if not (len(seeds) == len(theorems) == len(representations) == manifest.seed_count):
        raise CompositionUniquePairError("composition seed partition counts do not reconcile")
    if len({item.seed_id for item in seeds}) != len(seeds):
        raise CompositionUniquePairError("composition seed IDs are duplicated")
    seen_theorem_ids: set[str] = set()
    for seed, theorem, representation in zip(seeds, theorems, representations, strict=True):
        if seed.chain_depth != 1 or seed.seed_evidence_class != "E2":
            raise CompositionUniquePairError("N-derived or depth>1 composition source is forbidden")
        if seed.first_hop_rule_id not in _E2_RULES:
            raise CompositionUniquePairError("first hop is outside E2 P14-P18")
        if theorem.metadata.get("rule_id") in _D0_RULES:
            raise CompositionUniquePairError("N-derived composition source is forbidden")
        if (
            theorem.theorem_id != seed.intermediate_theorem_id
            or representation.representation_id != seed.intermediate_representation_id
            or representation.theorem_id != theorem.theorem_id
        ):
            raise CompositionUniquePairError("seed theorem/representation ordering differs")
        if theorem.parent_theorem_ids != (seed.source_theorem_id,):
            raise CompositionUniquePairError("seed intermediate is not exactly depth one")
        if theorem.source != "deterministic_transform":
            raise CompositionUniquePairError("seed intermediate is not a deterministic transform")
        if (
            theorem.metadata.get("rule_id") != seed.first_hop_rule_id
            or theorem.metadata.get("family_id") != seed.first_hop_family_id
        ):
            raise CompositionUniquePairError("seed theorem first-hop provenance differs")
        if (
            theorem.context_id != seed.context_id
            or representation.context_id != seed.context_id
            or theorem.root_ancestry_ids != seed.root_ancestry_ids
        ):
            raise CompositionUniquePairError("seed context or root ancestry differs")
        if representation.normalization_version != NORMALIZATION_VERSION:
            raise CompositionUniquePairError("seed representation normalization differs")
        if representation.content_hash != _representation_payload_hash(representation):
            raise CompositionUniquePairError("seed representation content hash is invalid")
        if theorem.theorem_id in seen_theorem_ids:
            raise CompositionUniquePairError("more than one seed maps to an intermediate theorem")
        seen_theorem_ids.add(theorem.theorem_id)
    return _SeedInventory(
        root=snapshot.directory.path,
        manifest=manifest,
        manifest_sha256=snapshot.files["manifest.json"].sha256,
        seeds=seeds,
        theorems=theorems,
        representations=representations,
        snapshot=snapshot,
    )


def _verify_seed_snapshot(inventory: _SeedInventory) -> None:
    _verify_held_snapshot(inventory.snapshot)


def _verify_chain_snapshot(inventory: _ChainInventory) -> None:
    _verify_held_snapshot(inventory.snapshot)


def _load_chain_inventory(
    snapshot: _HeldDirectorySnapshot,
    *,
    seed_manifest_sha256: str,
    seed_manifest: CompositionSeedManifest,
) -> _ChainInventory:
    raw_manifest = snapshot.payload("manifest.json")
    try:
        manifest = DeterministicCompositionChainManifest.model_validate_json(raw_manifest)
    except ValueError as exc:
        raise CompositionUniquePairError(f"invalid composition chain manifest: {exc}") from exc
    if raw_manifest != _canonical_line(manifest):
        raise CompositionUniquePairError("composition chain manifest is not canonical")

    if (
        manifest.input_seed_set_id != seed_manifest.seed_set_id
        or manifest.input_seed_manifest_sha256 != seed_manifest_sha256
        or manifest.input_seed_records_sha256 != seed_manifest.seed_output_sha256
        or manifest.input_seed_theorems_sha256 != seed_manifest.theorem_output_sha256
        or manifest.input_seed_representations_sha256 != seed_manifest.representation_output_sha256
        or manifest.input_seed_count != seed_manifest.seed_count
    ):
        raise CompositionUniquePairError("composition chain does not bind the exact seed set")

    if snapshot.files[manifest.chain_output].sha256 != manifest.chain_output_sha256:
        raise CompositionUniquePairError("composition chain partition differs from manifest")
    chains = _load_canonical_jsonl_bytes(
        snapshot.payload(manifest.chain_output),
        DeterministicCompositionChainRecord,
        label=manifest.chain_output,
    )
    if len(chains) != manifest.chain_count:
        raise CompositionUniquePairError("composition chain count differs from manifest")
    if len({item.chain_id for item in chains}) != len(chains):
        raise CompositionUniquePairError("composition chain IDs are duplicated")
    if dict(sorted(Counter(item.chain_kind for item in chains).items())) != (
        manifest.chain_kind_counts
    ):
        raise CompositionUniquePairError("composition chain-kind counts differ from manifest")
    if dict(sorted(Counter(item.second_hop_rule_id for item in chains).items())) != (
        manifest.second_hop_rule_counts
    ):
        raise CompositionUniquePairError("composition chain rule counts differ from manifest")
    inventory = _ChainInventory(
        root=snapshot.directory.path,
        manifest=manifest,
        manifest_sha256=snapshot.files["manifest.json"].sha256,
        chains=chains,
        snapshot=snapshot,
    )
    _verify_chain_snapshot(inventory)
    return inventory


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CompositionUniquePairError(message)


def _verify_chain_seed_binding(
    chain: DeterministicCompositionChainRecord,
    seed: CompositionSeedRecord,
) -> None:
    # The seed-set binding is checked at manifest and per-chain level by the
    # caller.  Every remaining mechanical lineage field is checked here.
    expected: tuple[tuple[object, object, str], ...] = (
        (chain.seed_id, seed.seed_id, "seed ID"),
        (chain.context_id, seed.context_id, "context"),
        (chain.root_ancestry_ids, seed.root_ancestry_ids, "root ancestry"),
        (chain.original_source_theorem_id, seed.source_theorem_id, "source theorem"),
        (
            chain.original_source_representation_id,
            seed.source_representation_id,
            "source representation",
        ),
        (chain.intermediate_theorem_id, seed.intermediate_theorem_id, "intermediate theorem"),
        (
            chain.intermediate_representation_id,
            seed.intermediate_representation_id,
            "intermediate representation",
        ),
        (
            chain.first_hop_root_binding_id,
            seed.first_hop_root_binding_id,
            "first-hop root",
        ),
        (chain.first_hop_result_id, seed.first_hop_result_id, "first-hop result"),
        (chain.first_hop_rule_id, seed.first_hop_rule_id, "first-hop rule"),
        (chain.first_hop_attempt_id, seed.first_hop_attempt_id, "first-hop attempt"),
        (chain.first_hop_draft_id, seed.first_hop_draft_id, "first-hop draft"),
        (chain.first_hop_audit_id, seed.first_hop_audit_id, "first-hop audit"),
        (chain.first_hop_variant_id, seed.first_hop_variant_id, "first-hop variant"),
        (
            chain.first_hop_certificate_kind,
            seed.certificate_kind,
            "first-hop certificate kind",
        ),
        (
            chain.first_hop_certificate_sha256,
            seed.certificate_sha256,
            "first-hop certificate hash",
        ),
    )
    for actual, wanted, description in expected:
        _require(actual == wanted, f"composition chain {description} differs from seed")
    _require(chain.intention_only is True, "composition chain is not intention-only")
    _require(chain.semantic_label_id is None, "composition chain embeds a semantic label")
    _require(chain.resolved_label_count == 0, "composition chain carries resolved labels")
    _require(chain.promoted_item_count == 0, "composition chain carries promoted items")
    _require(chain.training_eligible is False, "composition chain is training eligible")
    _require(chain.evaluation_eligible is False, "composition chain is evaluation eligible")
    _require(chain.gate_credit is False, "composition chain carries gate credit")


def _chain_sequence(chain: DeterministicCompositionChainRecord) -> str:
    return f"{chain.first_hop_rule_id}->{chain.second_hop_rule_id}"


def _canonical_unique_key(chain: DeterministicCompositionChainRecord) -> str:
    return hash_canonical(
        {
            "schema": "deterministic_v2_composition_unique_pair_key_v2",
            "original_source_theorem_id": chain.original_source_theorem_id,
            "final_candidate_code_hash": chain.final_candidate_code_hash,
        }
    )


def _unique_pairs(
    *,
    seed_set_id: str,
    chain_set_id: str,
    chains: Sequence[DeterministicCompositionChainRecord],
    seeds_by_id: Mapping[str, CompositionSeedRecord],
) -> tuple[DeterministicCompositionUniquePairRecord, ...]:
    grouped: dict[str, list[tuple[DeterministicCompositionChainRecord, CompositionSeedRecord]]] = (
        defaultdict(list)
    )
    for chain in chains:
        if chain.seed_set_id != seed_set_id:
            raise CompositionUniquePairError("composition chain seed-set ID differs")
        seed = seeds_by_id.get(chain.seed_id)
        if seed is None:
            raise CompositionUniquePairError("composition chain references a foreign seed")
        _verify_chain_seed_binding(chain, seed)
        grouped[_canonical_unique_key(chain)].append((chain, seed))

    output: list[DeterministicCompositionUniquePairRecord] = []
    for key, members in sorted(grouped.items()):
        first_chain, first_seed = members[0]
        invariants = (
            "context_id",
            "root_ancestry_ids",
            "original_source_theorem_id",
            "original_source_representation_id",
            "final_candidate_code_hash",
            "final_alpha_identity_fingerprint",
        )
        for chain, seed in members[1:]:
            if any(getattr(chain, name) != getattr(first_chain, name) for name in invariants):
                raise CompositionUniquePairError("canonical unique pair key collision detected")
            if (
                seed.source_statement_content_hash != first_seed.source_statement_content_hash
                or seed.source_alpha_identity_fingerprint
                != first_seed.source_alpha_identity_fingerprint
            ):
                raise CompositionUniquePairError("canonical unique pair source identity differs")
        data: dict[str, object] = {
            "canonical_unique_key": key,
            "input_seed_set_id": seed_set_id,
            "input_chain_set_id": chain_set_id,
            "context_id": first_chain.context_id,
            "root_ancestry_ids": first_chain.root_ancestry_ids,
            "original_source_theorem_id": first_chain.original_source_theorem_id,
            "original_source_representation_id": first_chain.original_source_representation_id,
            "source_statement_content_hash": first_seed.source_statement_content_hash,
            "source_alpha_identity_fingerprint": first_seed.source_alpha_identity_fingerprint,
            "intermediate_theorem_ids": tuple(
                sorted({chain.intermediate_theorem_id for chain, _ in members})
            ),
            "intermediate_representation_ids": tuple(
                sorted({chain.intermediate_representation_id for chain, _ in members})
            ),
            "final_theorem_ids": tuple(sorted({chain.final_theorem_id for chain, _ in members})),
            "final_representation_ids": tuple(
                sorted({chain.final_representation_id for chain, _ in members})
            ),
            "final_candidate_code_hash": first_chain.final_candidate_code_hash,
            "final_alpha_identity_fingerprint": first_chain.final_alpha_identity_fingerprint,
            "chain_ids": tuple(sorted(chain.chain_id for chain, _ in members)),
            "chain_sequences": tuple(sorted({_chain_sequence(chain) for chain, _ in members})),
            "chain_kinds": tuple(sorted({chain.chain_kind for chain, _ in members})),
            "gross_chain_count": len(members),
            "duplicate_excess_count": len(members) - 1,
            "source_alpha_return": (
                first_seed.source_alpha_identity_fingerprint
                == first_chain.final_alpha_identity_fingerprint
            ),
            "alpha_novel": (
                first_seed.source_alpha_identity_fingerprint
                != first_chain.final_alpha_identity_fingerprint
            ),
        }
        output.append(
            DeterministicCompositionUniquePairRecord.model_validate(
                {"unique_pair_id": f"detcomp_unique_pair:{key}", **data}
            )
        )
    return tuple(output)


def _child_directory_metadata(parent: _HeldDirectory, name: str) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CompositionUniquePairError(f"unique-pair output is unavailable: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise CompositionUniquePairError("unique-pair output cannot be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise CompositionUniquePairError("unique-pair output is not a directory")
    return metadata


def _open_child_directory(parent: _HeldDirectory, name: str) -> _HeldDirectory:
    metadata = _child_directory_metadata(parent, name)
    if metadata is None:
        raise CompositionUniquePairError("unique-pair output disappeared")
    try:
        fd = os.open(name, _directory_open_flags(), dir_fd=parent.fd)
    except OSError as exc:
        raise CompositionUniquePairError(
            f"unique-pair output changed or became unsafe: {exc}"
        ) from exc
    try:
        opened = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise CompositionUniquePairError(
            f"unique-pair output descriptor cannot be verified: {exc}"
        ) from exc
    if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        raise CompositionUniquePairError("unique-pair output identity changed while opening")
    return _HeldDirectory(
        path=parent.path / name,
        fd=fd,
        identity=_FileIdentity.from_stat(opened),
        label="unique-pair output directory",
    )


def _verify_existing_fd(output: _HeldDirectory, payloads: Mapping[str, bytes]) -> None:
    with _snapshot_exact_directory(output, expected_names=_OUTPUT_FILES) as snapshot:
        for index, (name, expected_payload) in enumerate(sorted(payloads.items())):
            held_file = snapshot.files[name]
            if held_file.payload != expected_payload:
                raise CompositionUniquePairError(
                    f"existing unique-pair output differs: {output.path / name}"
                )
            if index == 0:
                _invoke_race_hook("during_existing_output_verify")
        _verify_held_snapshot(snapshot, verify_path_identity=False)


def _final_verify_output(
    *,
    parent: _HeldDirectory,
    output: _HeldDirectory,
    output_name: str,
    payloads: Mapping[str, bytes],
) -> None:
    """Make held two-file content verification the final success operation."""

    _verify_child_identity(parent, output_name, output.identity)
    _verify_directory_path_identity(parent)
    _verify_directory_path_identity(output)
    _invoke_race_hook("before_final_output_content_verify")
    _verify_existing_fd(output, payloads)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise CompositionUniquePairError("short write while publishing unique-pair output")
        view = view[written:]


def _write_new_file_at(directory_fd: int, name: str, payload: bytes) -> None:
    _, no_follow_flag = _require_descriptor_support()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow_flag | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise CompositionUniquePairError(
            f"cannot create immutable unique-pair output file {name}: {exc}"
        ) from exc
    try:
        _write_all(fd, payload)
        os.fsync(fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(payload):
            raise CompositionUniquePairError(
                f"published unique-pair output file is invalid: {name}"
            )
    finally:
        os.close(fd)


def _rename_noreplace_at(parent_fd: int, source: str, destination: str) -> bool:
    """Rename within one held parent; return false when destination exists."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise CompositionUniquePairError(
            "renameat2(RENAME_NOREPLACE) is unavailable; publication fails closed"
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        return False
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise CompositionUniquePairError(
            "renameat2(RENAME_NOREPLACE) is unsupported; publication fails closed"
        )
    raise CompositionUniquePairError(
        f"cannot atomically publish unique-pair output: {os.strerror(error_number)}"
    )


def _remove_authoritative_namespace_entry(parent: _HeldDirectory, name: str) -> None:
    """Remove one private publication name without following its entry."""

    try:
        metadata = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except OSError:
        return
    operation = os.rmdir if stat.S_ISDIR(metadata.st_mode) else os.unlink
    with suppress(OSError):
        operation(name, dir_fd=parent.fd)


def _cleanup_private_directory(
    parent: _HeldDirectory,
    directory: _HeldDirectory,
    *,
    authoritative_names: frozenset[str],
) -> None:
    """Clean one held private directory and every in-parent inode alias."""

    try:
        entries = tuple(os.listdir(directory.fd))
    except OSError:
        entries = ()
    for entry in entries:
        with suppress(OSError):
            os.unlink(entry, dir_fd=directory.fd)

    # An adversary may park the bound temporary directory under another name.
    # Locate only the exact held inode; do not follow any namespace entry.
    try:
        parent_entries = tuple(os.listdir(parent.fd))
    except OSError:
        parent_entries = ()
    for entry in parent_entries:
        with suppress(OSError):
            metadata = os.stat(entry, dir_fd=parent.fd, follow_symlinks=False)
            if _same_filesystem_object(_FileIdentity.from_stat(metadata), directory.identity):
                os.rmdir(entry, dir_fd=parent.fd)

    # These randomized/private names belong to this publication attempt.
    # Replacements are removed by type without following them.
    for name in authoritative_names:
        _remove_authoritative_namespace_entry(parent, name)


def _verify_child_identity(
    parent: _HeldDirectory,
    name: str,
    expected: _FileIdentity,
) -> None:
    metadata = _child_directory_metadata(parent, name)
    if metadata is None or not _same_filesystem_object(_FileIdentity.from_stat(metadata), expected):
        raise CompositionUniquePairError("unique-pair output identity changed")


def _publish_or_verify_output(
    *,
    output_dir: Path,
    payloads: Mapping[str, bytes],
    forbidden_input_identities: frozenset[tuple[int, int]],
) -> bool:
    """Publish through a held parent descriptor; return whether this was replay.

    Success is preceded immediately by a held exact two-file verification.
    Arbitrary mutation by another same-UID writer after this function returns
    is outside the immutable-artifact protocol; callers require a trusted or
    cooperating output parent once publication completes.
    """

    output_dir = _absolute_path(output_dir)
    if output_dir == Path(output_dir.anchor) or not output_dir.name:
        raise CompositionUniquePairError("unique-pair output must name a child directory")
    with _open_held_directory(
        output_dir.parent,
        label="unique-pair output parent",
        create=True,
        forbidden_identities=forbidden_input_identities,
    ) as parent:
        existing = _child_directory_metadata(parent, output_dir.name)
        if existing is not None:
            with _open_child_directory(parent, output_dir.name) as output:
                _final_verify_output(
                    parent=parent,
                    output=output,
                    output_name=output_dir.name,
                    payloads=payloads,
                )
            return True

        temporary_name = f".{output_dir.name}.{secrets.token_hex(16)}"
        try:
            os.mkdir(temporary_name, mode=0o700, dir_fd=parent.fd)
        except OSError as exc:
            raise CompositionUniquePairError(
                f"cannot create unique-pair temporary directory: {exc}"
            ) from exc
        try:
            temporary = _open_child_directory(parent, temporary_name)
        except BaseException:
            _remove_authoritative_namespace_entry(parent, temporary_name)
            raise
        rename_succeeded = False
        try:
            for name, payload in payloads.items():
                _write_new_file_at(temporary.fd, name, payload)
            os.fsync(temporary.fd)
            _invoke_race_hook("before_output_publish")
            _verify_child_identity(parent, temporary_name, temporary.identity)
            if _rename_noreplace_at(parent.fd, temporary_name, output_dir.name):
                rename_succeeded = True
                temporary.path = output_dir
                os.fsync(parent.fd)
                _final_verify_output(
                    parent=parent,
                    output=temporary,
                    output_name=output_dir.name,
                    payloads=payloads,
                )
                return False

            _cleanup_private_directory(
                parent,
                temporary,
                authoritative_names=frozenset({temporary_name}),
            )
            with _open_child_directory(parent, output_dir.name) as output:
                _final_verify_output(
                    parent=parent,
                    output=output,
                    output_name=output_dir.name,
                    payloads=payloads,
                )
            return True
        except BaseException:
            authoritative_names = {temporary_name}
            if rename_succeeded:
                authoritative_names.add(output_dir.name)
            _cleanup_private_directory(
                parent,
                temporary,
                authoritative_names=frozenset(authoritative_names),
            )
            raise
        finally:
            temporary.close()


def postprocess_deterministic_v2_composition_unique_pairs(
    *,
    seed_dir: Path,
    chain_dir: Path,
    output_dir: Path,
) -> CompositionUniquePairArtifacts:
    """Revalidate exact chain-v1 inputs and emit immutable unique pairs."""

    seed_dir = _absolute_path(seed_dir)
    chain_dir = _absolute_path(chain_dir)
    output_dir = _absolute_path(output_dir)
    input_roots = (seed_dir, chain_dir)
    if any(output_dir == root or output_dir.is_relative_to(root) for root in input_roots):
        raise CompositionUniquePairError("unique-pair output cannot be inside an input")

    with (
        _open_held_directory(seed_dir, label="composition seed directory") as seed_root,
        _open_held_directory(chain_dir, label="composition chain directory") as chain_root,
    ):
        _invoke_race_hook("after_input_roots_bound")
        with (
            _snapshot_exact_directory(seed_root, expected_names=_SEED_FILES) as seed_snapshot,
            _snapshot_exact_directory(chain_root, expected_names=_CHAIN_FILES) as chain_snapshot,
        ):
            seed_inventory = _load_seed_inventory(seed_snapshot)
            chain_inventory = _load_chain_inventory(
                chain_snapshot,
                seed_manifest_sha256=seed_inventory.manifest_sha256,
                seed_manifest=seed_inventory.manifest,
            )
            seeds_by_id = {item.seed_id: item for item in seed_inventory.seeds}
            if len(seeds_by_id) != len(seed_inventory.seeds):
                raise CompositionUniquePairError("composition seed IDs are duplicated")
            unique_pairs = _unique_pairs(
                seed_set_id=seed_inventory.manifest.seed_set_id,
                chain_set_id=chain_inventory.manifest.chain_set_id,
                chains=chain_inventory.chains,
                seeds_by_id=seeds_by_id,
            )

            unique_payload = _canonical_jsonl(unique_pairs)
            gross_sequence_counts = Counter(
                _chain_sequence(item) for item in chain_inventory.chains
            )
            gross_kind_counts = Counter(item.chain_kind for item in chain_inventory.chains)
            unique_kind_membership = Counter(
                kind for item in unique_pairs for kind in item.chain_kinds
            )
            unique_sequence_membership = Counter(
                sequence for item in unique_pairs for sequence in item.chain_sequences
            )
            manifest_data: dict[str, object] = {
                "input_seed_set_id": seed_inventory.manifest.seed_set_id,
                "input_seed_manifest_sha256": seed_inventory.manifest_sha256,
                "input_seed_records_sha256": seed_inventory.manifest.seed_output_sha256,
                "input_seed_theorems_sha256": seed_inventory.manifest.theorem_output_sha256,
                "input_seed_representations_sha256": (
                    seed_inventory.manifest.representation_output_sha256
                ),
                "input_chain_set_id": chain_inventory.manifest.chain_set_id,
                "input_chain_manifest_sha256": chain_inventory.manifest_sha256,
                "input_chain_records_sha256": chain_inventory.manifest.chain_output_sha256,
                "gross_chain_count": len(chain_inventory.chains),
                "unique_pair_count": len(unique_pairs),
                "duplicate_group_count": sum(item.gross_chain_count > 1 for item in unique_pairs),
                "duplicate_excess_count": len(chain_inventory.chains) - len(unique_pairs),
                "gross_source_alpha_return_count": sum(
                    seeds_by_id[item.seed_id].source_alpha_identity_fingerprint
                    == item.final_alpha_identity_fingerprint
                    for item in chain_inventory.chains
                ),
                "unique_source_alpha_return_count": sum(
                    item.source_alpha_return for item in unique_pairs
                ),
                "gross_alpha_novel_count": sum(
                    seeds_by_id[item.seed_id].source_alpha_identity_fingerprint
                    != item.final_alpha_identity_fingerprint
                    for item in chain_inventory.chains
                ),
                "unique_alpha_novel_count": sum(item.alpha_novel for item in unique_pairs),
                "gross_chain_kind_counts": dict(sorted(gross_kind_counts.items())),
                "unique_pair_chain_kind_membership_counts": dict(
                    sorted(unique_kind_membership.items())
                ),
                "gross_sequence_counts": dict(sorted(gross_sequence_counts.items())),
                "unique_pair_sequence_membership_counts": dict(
                    sorted(unique_sequence_membership.items())
                ),
                "unique_output_sha256": hashlib.sha256(unique_payload).hexdigest(),
            }
            placeholder = DeterministicCompositionUniquePairManifest.model_construct(
                _fields_set=None,
                unique_pair_set_id=f"detcomp_unique_pair_set:{'0' * 64}",
                **manifest_data,
            )
            unique_pair_set_id = "detcomp_unique_pair_set:" + hash_canonical(
                _without_id(placeholder.model_dump(mode="json"), "unique_pair_set_id")
            )
            manifest = DeterministicCompositionUniquePairManifest.model_validate(
                {"unique_pair_set_id": unique_pair_set_id, **manifest_data}
            )
            payloads = {
                "unique_pairs.jsonl": unique_payload,
                "manifest.json": _canonical_line(manifest),
            }

            _verify_seed_snapshot(seed_inventory)
            _verify_chain_snapshot(chain_inventory)
            forbidden_input_identities = frozenset(
                {
                    (seed_root.identity.device, seed_root.identity.inode),
                    (chain_root.identity.device, chain_root.identity.inode),
                }
            )
            replayed = _publish_or_verify_output(
                output_dir=output_dir,
                payloads=payloads,
                forbidden_input_identities=forbidden_input_identities,
            )
    return CompositionUniquePairArtifacts(
        output_dir=output_dir,
        manifest_path=output_dir / "manifest.json",
        unique_pairs_path=output_dir / "unique_pairs.jsonl",
        unique_pair_set_id=unique_pair_set_id,
        gross_chain_count=len(chain_inventory.chains),
        unique_pair_count=len(unique_pairs),
        replayed=replayed,
    )


__all__ = [
    "CompositionUniquePairArtifacts",
    "CompositionUniquePairError",
    "DeterministicCompositionUniquePairManifest",
    "DeterministicCompositionUniquePairRecord",
    "postprocess_deterministic_v2_composition_unique_pairs",
]
