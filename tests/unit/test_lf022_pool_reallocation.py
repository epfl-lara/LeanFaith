"""Exact-replay tests for family-only LF-022 public-pool reallocation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.generation.lf022_pool_reallocation import (
    LF022PoolReallocationError,
    derive_lf022_pool_reallocation,
    verify_lf022_pool_reallocation,
)
from leanfaith.generation.lf022_public_pool import LF022PublicPoolAudit
from tests.unit.test_lf022_executor import REPOSITORY_ROOT, _fixture


def _pilot_parent_and_matrix(root: Path) -> tuple[Path, Path]:
    _fixture(root, profile="pilot_scaffold")
    matrix_path = root / "configs/generation/lf022_production_family_matrix_v2.json"
    matrix_path.write_bytes(
        (REPOSITORY_ROOT / "configs/generation/lf022_production_family_matrix_v2.json").read_bytes()
    )
    return root / "artifacts/public_pool_audit.json", matrix_path


def test_reallocation_exactly_replays_parent_sources(tmp_path: Path) -> None:
    parent_path, matrix_path = _pilot_parent_and_matrix(tmp_path)
    output = tmp_path / "artifacts/reallocated"

    first = derive_lf022_pool_reallocation(
        repo_root=tmp_path,
        parent_pool_audit_path=parent_path,
        replacement_family_matrix_path=matrix_path,
        output_directory=output,
    )
    first_bytes = {
        path.name: path.read_bytes() for path in sorted(output.iterdir()) if path.is_file()
    }
    second = derive_lf022_pool_reallocation(
        repo_root=tmp_path,
        parent_pool_audit_path=parent_path,
        replacement_family_matrix_path=matrix_path,
        output_directory=output,
    )
    second_bytes = {
        path.name: path.read_bytes() for path in sorted(output.iterdir()) if path.is_file()
    }

    parent = LF022PublicPoolAudit.model_validate_json(parent_path.read_bytes())
    verified = verify_lf022_pool_reallocation(
        repo_root=tmp_path,
        audit=first.materialized.audit,
        expected_code_tree_hash=first.derivation.attesting_code_tree_hash,
    )
    assert first_bytes == second_bytes
    assert second == first
    assert verified.audit == first.materialized.audit
    assert verified.parent_audit == parent
    assert verified.audit.schema_version == 2
    assert verified.audit.outputs.source_pool == parent.outputs.source_pool
    assert verified.audit.outputs.theorem_records == parent.outputs.theorem_records
    assert verified.audit.outputs.representation_records == parent.outputs.representation_records
    assert verified.audit.outputs.context_records == parent.outputs.context_records
    assert verified.audit.outputs.denylist_clearance_records == (
        parent.outputs.denylist_clearance_records
    )
    assert verified.audit.outputs.family_matrix != parent.outputs.family_matrix
    assert verified.family_matrix.proposer_family_ids == (
        "moonshot_kimi_k2",
        "qwen3",
        "deepseek_v4",
    )
    assert verified.plan.network_execution_authorized is False
    assert verified.plan.semantic_labels_created is False


def test_reallocation_rejects_matrix_tampering(tmp_path: Path) -> None:
    parent_path, matrix_path = _pilot_parent_and_matrix(tmp_path)
    result = derive_lf022_pool_reallocation(
        repo_root=tmp_path,
        parent_pool_audit_path=parent_path,
        replacement_family_matrix_path=matrix_path,
        output_directory=tmp_path / "artifacts/reallocated",
    )
    payload = json.loads(matrix_path.read_bytes())
    payload["heldout_eval_supervision_excluded"] = False
    matrix_path.write_bytes(canonical_json_bytes(payload) + b"\n")

    with pytest.raises(LF022PoolReallocationError, match="hash differs from its binding"):
        verify_lf022_pool_reallocation(
            repo_root=tmp_path,
            audit=result.materialized.audit,
        )


def test_reallocation_requires_clean_code_tree(tmp_path: Path) -> None:
    parent_path, matrix_path = _pilot_parent_and_matrix(tmp_path)
    (tmp_path / "fixture_code.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(LF022PoolReallocationError, match="clean, hashable code tree"):
        derive_lf022_pool_reallocation(
            repo_root=tmp_path,
            parent_pool_audit_path=parent_path,
            replacement_family_matrix_path=matrix_path,
            output_directory=tmp_path / "artifacts/reallocated",
        )


def test_reallocation_cli_reports_exact_lineage(tmp_path: Path) -> None:
    parent_path, matrix_path = _pilot_parent_and_matrix(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "reallocate-lf022-public-pool",
            "--root",
            str(tmp_path),
            "--parent-pool-audit",
            str(parent_path),
            "--family-matrix",
            str(matrix_path),
            "--out-dir",
            "artifacts/reallocated",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "reallocated"
    assert payload["selected_count"] == 1
    assert payload["task_count"] == 2
    assert payload["network_execution_authorized"] is False
    assert payload["semantic_labels_created"] is False
    assert payload["training_eligible"] is False
