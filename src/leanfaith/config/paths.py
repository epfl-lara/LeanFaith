"""Repository path authority helpers (PLAN.md §7).

Only paths declared in the §7 tree (or children of declared directories) may
be referenced by phases and backlog items. ``RepoPaths`` exposes the declared
top-level directories relative to a discovered repository root so no module
hard-codes machine-local absolute paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RepoRootNotFoundError(RuntimeError):
    """Raised when no LeanFaith repository root is found above ``start``."""


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upward from ``start`` (default: cwd) to the directory holding PLAN.md."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "PLAN.md").is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise RepoRootNotFoundError(f"no LeanFaith repository root at or above {current}")


@dataclass(frozen=True, slots=True)
class RepoPaths:
    """Declared §7 top-level directories, anchored at the repository root."""

    root: Path

    @classmethod
    def discover(cls, start: Path | None = None) -> RepoPaths:
        return cls(root=find_repo_root(start))

    @property
    def policies(self) -> Path:
        return self.root / "policies"

    @property
    def configs(self) -> Path:
        return self.root / "configs"

    @property
    def prompts(self) -> Path:
        return self.root / "prompts"

    @property
    def annotation(self) -> Path:
        return self.root / "annotation"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def examples(self) -> Path:
        return self.root / "examples"

    @property
    def scripts(self) -> Path:
        return self.root / "scripts"

    def relative_to_root(self, path: Path) -> Path:
        """Express ``path`` relative to the repo root (for manifests and IDs)."""
        return path.resolve().relative_to(self.root)
