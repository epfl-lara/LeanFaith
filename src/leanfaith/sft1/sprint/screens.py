"""Lean-free screens applied to every candidate pair before it becomes a row."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.representations.views import signature_near_dup_hash

DAGGER = "✝"
_INSTANCE_DAGGER = re.compile(r"^inst✝[⁰¹²³⁴⁵⁶⁷⁸⁹]*$")


class ScreenError(RuntimeError):
    """Raised when a screen input is malformed."""


def local_names(goal_text: str) -> list[str]:
    """Names of the locals declared above the turnstile, in order."""

    names: list[str] = []
    for line in goal_text.split("\n"):
        if line.startswith("⊢"):
            break
        head, separator, _ = line.partition(" : ")
        if not separator:
            continue
        names.extend(head.split())
    return names


def residue_violation(goal_text: str) -> str | None:
    """Exact-text residue policy shared by both endpoints of every pair.

    Returns the violation class or ``None``.  Generated instance names such as
    ``inst✝`` are allowed and counted separately by :func:`instance_dagger_count`;
    any other dagger, ``[anonymous]``, ``⋯``, or a turnstile count other than
    one rejects the text.
    """

    if goal_text.count("⊢") != 1:
        return "wrong_turnstile_count"
    if "[anonymous]" in goal_text:
        return "anonymous_binder_name"
    if "⋯" in goal_text:
        return "forbidden_rendered_placeholder"
    if DAGGER in goal_text:
        for name in local_names(goal_text):
            if DAGGER in name and _INSTANCE_DAGGER.match(name) is None:
                return "dagger_on_ordinary_local"
        target = goal_text.split("\n⊢", 1)[1] if "\n⊢" in goal_text else goal_text
        if DAGGER in target:
            return "dagger_in_target"
    return None


def instance_dagger_count(goal_text: str) -> int:
    return sum(1 for name in local_names(goal_text) if _INSTANCE_DAGGER.match(name) is not None)


@dataclass(frozen=True, slots=True)
class GoldBlocklist:
    path: str
    sha256: str
    near_dup_hashes: frozenset[str]
    group_keys: frozenset[str]

    @classmethod
    def load(cls, path: Path, *, expected_sha256: str | None = None) -> GoldBlocklist:
        digest = hash_file(path)
        if expected_sha256 is not None and digest != expected_sha256:
            raise ScreenError(
                f"gold blocklist hash mismatch: expected {expected_sha256}, got {digest}"
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        hashes = document.get("near_dup_hashes")
        groups = document.get("group_keys")
        if not isinstance(hashes, list) or not isinstance(groups, list):
            raise ScreenError("gold blocklist is malformed")
        return cls(
            path=str(path),
            sha256=digest,
            near_dup_hashes=frozenset(str(value) for value in hashes),
            group_keys=frozenset(str(value) for value in groups),
        )

    def hit(self, text: str) -> bool:
        return signature_near_dup_hash(text) in self.near_dup_hashes


def unordered_pair_key(reference_render_hash: str, candidate_render_hash: str) -> str:
    return hash_canonical(sorted((reference_render_hash, candidate_render_hash)))


def render_hash(goal_text: str) -> str:
    return sha256_hex(goal_text.encode("utf-8"))


def stable_row_hash(payload: Mapping[str, object]) -> str:
    return hash_canonical(dict(payload))


@dataclass(frozen=True, slots=True)
class DedupOutcome:
    kept: list[dict[str, object]]
    duplicate_count: int
    conflict_count: int
    conflict_keys: tuple[str, ...]


def deduplicate(records: Sequence[Mapping[str, object]]) -> DedupOutcome:
    """Canonical unordered-pair deduplication with conflicting-label rejection.

    Each record must carry ``unordered_pair_key``, ``row_hash``, and ``label``.
    Same-label duplicates keep the minimum stable row hash; any class with
    conflicting labels is rejected entirely.
    """

    classes: dict[str, list[Mapping[str, object]]] = {}
    for record in records:
        key = str(record["unordered_pair_key"])
        classes.setdefault(key, []).append(record)
    kept: list[dict[str, object]] = []
    duplicates = 0
    conflicts: list[str] = []
    for key in sorted(classes):
        members = classes[key]
        labels = {bool(member["label"]) for member in members}
        if len(labels) > 1:
            conflicts.append(key)
            continue
        winner = min(members, key=lambda member: str(member["row_hash"]))
        duplicates += len(members) - 1
        kept.append(dict(winner))
    kept.sort(key=lambda member: str(member["row_hash"]))
    return DedupOutcome(
        kept=kept,
        duplicate_count=duplicates,
        conflict_count=len(conflicts),
        conflict_keys=tuple(conflicts),
    )
