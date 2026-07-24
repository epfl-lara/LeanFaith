"""Fail-closed CLI/config fixture binding for local LF-021 qualification."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.local_qualification import load_local_qualification_config

ROOT = find_repo_root(Path(__file__).parent)
CONFIG_PATH = ROOT / "configs/generation/local_qualification_goedel_v1.yaml"
FIXTURE_PATH = ROOT / "examples/lf021_goedel_mathlib_nat_comm_20260723_v1.json"
HEADER_PATH = ROOT / "examples/lf021_goedel_mathlib_standard_header_v1.lean"


def _load_launcher() -> Any:
    path = ROOT / "scripts/07_qualify_local_kimina.py"
    spec = importlib.util.spec_from_file_location("lf021_fixture_binding_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_exact_configured_fixture_header_and_project_binding_is_accepted() -> None:
    launcher = _load_launcher()
    configured = load_local_qualification_config(
        CONFIG_PATH,
        repo_root=ROOT,
    ).config.qualification_fixture
    assert configured is not None
    launcher._validate_configured_fixture_binding(
        configured=configured,
        repo_root=ROOT,
        fixture_path=FIXTURE_PATH,
        fixture_sha256=hash_file(FIXTURE_PATH),
        import_header_path=HEADER_PATH,
        project_registry_key="mathlib",
    )


@pytest.mark.parametrize(
    ("fixture_path", "fixture_sha256", "header_path", "project_registry_key"),
    [
        (
            ROOT / "examples/lf021_kimina_mathlib_nat_comm_20260723_v1.json",
            hash_file(ROOT / "examples/lf021_kimina_mathlib_nat_comm_20260723_v1.json"),
            HEADER_PATH,
            "mathlib",
        ),
        (
            FIXTURE_PATH,
            hash_file(FIXTURE_PATH),
            ROOT / "examples/lf021_kimina_mathlib_nat_header_v1.lean",
            "mathlib",
        ),
        (
            FIXTURE_PATH,
            hash_file(FIXTURE_PATH),
            HEADER_PATH,
            "fixtures",
        ),
    ],
)
def test_any_cli_override_of_configured_fixture_binding_is_rejected(
    fixture_path: Path,
    fixture_sha256: str,
    header_path: Path,
    project_registry_key: str,
) -> None:
    launcher = _load_launcher()
    configured = load_local_qualification_config(
        CONFIG_PATH,
        repo_root=ROOT,
    ).config.qualification_fixture
    assert configured is not None
    with pytest.raises(SystemExit, match="differs from the loaded config"):
        launcher._validate_configured_fixture_binding(
            configured=configured,
            repo_root=ROOT,
            fixture_path=fixture_path,
            fixture_sha256=fixture_sha256,
            import_header_path=header_path,
            project_registry_key=project_registry_key,
        )
