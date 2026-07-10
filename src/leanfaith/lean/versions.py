"""Lean toolchain version parsing and the LeanInteract advertised range (§6.2).

Ordering rule: a release candidate precedes its release
(``v4.31.0-rc1 < v4.31.0``), so stable ``v4.31.0`` lies *outside* an
advertised range ending at ``v4.31.0-rc1`` and requires the ADR-0001
stable-exception mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from leanfaith.lean.api_probe import ADVERTISED_LEAN_MAX, ADVERTISED_LEAN_MIN

_VERSION_PATTERN = re.compile(
    r"^(?:leanprover/lean4:)?v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-rc(?P<rc>\d+))?$"
)


class LeanVersionError(ValueError):
    """Raised for unparsable Lean toolchain strings."""


@dataclass(frozen=True, slots=True, order=True)
class LeanVersion:
    """Comparable Lean version; ``rc`` is ``None`` for stable releases."""

    major: int
    minor: int
    patch: int
    rc_rank: int  # rc number, or a large sentinel for stable so rc < stable

    _STABLE_RANK = 10**6

    @property
    def is_release_candidate(self) -> bool:
        return self.rc_rank != self._STABLE_RANK

    def __str__(self) -> str:
        base = f"v{self.major}.{self.minor}.{self.patch}"
        return base if not self.is_release_candidate else f"{base}-rc{self.rc_rank}"


def parse_lean_version(text: str) -> LeanVersion:
    """Parse ``v4.31.0``, ``v4.31.0-rc1``, or ``leanprover/lean4:v4.31.0-rc1``."""
    match = _VERSION_PATTERN.match(text.strip())
    if match is None:
        raise LeanVersionError(f"unparsable Lean version {text!r}")
    rc = match.group("rc")
    return LeanVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        rc_rank=int(rc) if rc is not None else LeanVersion._STABLE_RANK,
    )


ADVERTISED_MIN = parse_lean_version(ADVERTISED_LEAN_MIN)
ADVERTISED_MAX = parse_lean_version(ADVERTISED_LEAN_MAX)


def in_advertised_range(version: LeanVersion) -> bool:
    """Whether ``version`` lies inside the binding LeanInteract range (§6.2)."""
    return ADVERTISED_MIN <= version <= ADVERTISED_MAX
