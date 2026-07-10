"""LF-002: repository root discovery and §7 path authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config import RepoPaths, find_repo_root
from leanfaith.config.paths import RepoRootNotFoundError


def test_find_repo_root_from_nested_directory() -> None:
    here = Path(__file__).resolve()
    root = find_repo_root(here.parent)
    assert (root / "PLAN.md").is_file()
    assert (root / "pyproject.toml").is_file()


def test_find_repo_root_fails_outside_repo(tmp_path: Path) -> None:
    with pytest.raises(RepoRootNotFoundError):
        find_repo_root(tmp_path)


def test_declared_directories_are_root_children() -> None:
    paths = RepoPaths.discover(Path(__file__).parent)
    assert paths.configs == paths.root / "configs"
    assert paths.policies == paths.root / "policies"
    assert paths.data == paths.root / "data"
    assert paths.artifacts == paths.root / "artifacts"
    assert paths.reports == paths.root / "reports"
    assert paths.runs == paths.root / "runs"


def test_relative_to_root() -> None:
    paths = RepoPaths.discover(Path(__file__).parent)
    assert paths.relative_to_root(paths.configs / "splits" / "v0.yaml") == Path(
        "configs/splits/v0.yaml"
    )
