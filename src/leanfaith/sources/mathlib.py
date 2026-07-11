"""Mathlib repository adapter (PLAN.md LF-011): pinned-checkout file inventory.

Produces the deterministic file frame (relative path + content hash) that
LF-012 extraction iterates over via ``FileCommand``. Declaration extraction
itself is Lean-aware and never regex (§8.9, §12.2).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import Field

from leanfaith.config.hashing import hash_file
from leanfaith.config.models import StrictModel

ADAPTER_VERSION = "mathlib_adapter_v1"


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


def verify_checkout_revision(checkout: Path, expected_revision: str) -> str:
    """Fail closed if the checkout's HEAD differs from the pin (§6.2)."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
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


def build_inventory(
    checkout: Path,
    *,
    source: str,
    revision: str,
    root_module: str,
    globs: tuple[str, ...],
    limit: int | None = None,
) -> RepoInventory:
    """Hash every glob-matched .lean file, sorted for determinism."""
    verify_checkout_revision(checkout, revision)
    paths: list[Path] = []
    for pattern in globs:
        paths.extend(checkout.glob(pattern))
    unique = sorted({path.relative_to(checkout) for path in paths if path.is_file()})
    if limit is not None:
        unique = unique[:limit]
    files = tuple(
        RepoFileEntry(relative_path=str(rel), sha256=hash_file(checkout / rel)) for rel in unique
    )
    return RepoInventory(
        source=source,
        revision=revision,
        root_module=root_module,
        globs=globs,
        file_count=len(files),
        files=files,
    )
