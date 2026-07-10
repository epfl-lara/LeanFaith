"""Phase 0 task 8: `leanfaith doctor --write-lock` and the real-repo doctor."""

from __future__ import annotations

import subprocess
from pathlib import Path

from leanfaith.cli.doctor import canonical_environment_lock, run_doctor, write_lock
from leanfaith.config.paths import RepoPaths
from leanfaith.lean.project_registry import load_environment_lock


def _bare_root(tmp_path: Path) -> RepoPaths:
    (tmp_path / "PLAN.md").write_text("plan\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "configs").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return RepoPaths(root=tmp_path)


def test_write_lock_creates_canonical_lock(tmp_path: Path) -> None:
    paths = _bare_root(tmp_path)
    changed, message = write_lock(paths)
    assert changed and "wrote" in message
    assert load_environment_lock(paths) == canonical_environment_lock()


def test_write_lock_idempotent_when_matching(tmp_path: Path) -> None:
    paths = _bare_root(tmp_path)
    write_lock(paths)
    changed, message = write_lock(paths)
    assert not changed
    assert "matches" in message


def test_write_lock_refuses_divergent_without_force(tmp_path: Path) -> None:
    paths = _bare_root(tmp_path)
    write_lock(paths)
    lock_path = paths.configs / "environment.lock.yaml"
    lock_path.write_text(
        lock_path.read_text().replace("accepted_lean: v4.31.0-rc1", "accepted_lean: v4.30.0")
    )
    changed, message = write_lock(paths)
    assert not changed
    assert "--force" in message
    changed, _ = write_lock(paths, force=True)
    assert changed
    assert load_environment_lock(paths) == canonical_environment_lock()


def test_repo_lock_matches_canonical() -> None:
    """The committed configs/environment.lock.yaml must equal the code constants."""
    paths = RepoPaths.discover(Path(__file__).parent)
    assert load_environment_lock(paths) == canonical_environment_lock()


def test_doctor_passes_on_this_repository() -> None:
    """Gate 0/1 evidence: the real repo (lock + projects + fixture) is healthy."""
    paths = RepoPaths.discover(Path(__file__).parent)
    report = run_doctor(paths)
    assert report.ok, [c for c in report.checks if not c.passed]
    checked = {c.name for c in report.checks}
    assert "project_toolchain:fixtures" in checked
