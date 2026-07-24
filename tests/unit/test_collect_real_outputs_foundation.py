from __future__ import annotations

import datetime
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.collect_real_outputs import (
    LF021CollectionTerminalRecord,
    LF021FoundationError,
    run_lf021_offline_smoke,
    validate_lf021_foundation,
)
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.schemas.enums import ParseStatus


def _foundation_root(tmp_path: Path) -> RepoPaths:
    source_root = find_repo_root(Path(__file__))
    generation = tmp_path / "configs" / "generation"
    generation.mkdir(parents=True)
    for name in ("problem_pool_v1.yaml", "real_outputs_v1.yaml", "providers.yaml"):
        shutil.copyfile(source_root / "configs" / "generation" / name, generation / name)

    for relative in (
        "configs/sources/sft_classic.yaml",
        "configs/sources/sft_classic_numina.yaml",
        "configs/sources/lean_workbook.yaml",
        "configs/sources/public_replication.yaml",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    manifest_relative = "data/benchmarks/manifests/representation_signatures_v1.json"
    manifest = tmp_path / manifest_relative
    manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_root / manifest_relative, manifest)
    return RepoPaths(root=tmp_path)


def test_checked_in_lf021_foundation_is_hash_bound_and_fail_closed(tmp_path: Path) -> None:
    paths = _foundation_root(tmp_path)
    generated_at = datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC)

    result = validate_lf021_foundation(paths, generated_at=generated_at)

    assert result.report.generated_at == generated_at
    assert result.report.execution_authorized is False
    assert result.report.provider_calls_made == 0
    assert result.report.semantic_labels_created == 0
    assert all(result.report.checks.values())
    assert result.report.checks["four_family_full_track"]
    assert result.report.unresolved_requirements


def test_foundation_validation_rejects_enabled_provider_slot(tmp_path: Path) -> None:
    paths = _foundation_root(tmp_path)
    provider_path = paths.configs / "generation" / "providers.yaml"
    text = provider_path.read_text(encoding="utf-8")
    provider_path.write_text(
        text.replace("status: disabled_until_phase_5_adr", "status: enabled", 1),
        encoding="utf-8",
    )

    with pytest.raises(LF021FoundationError, match="enabled provider slots require"):
        validate_lf021_foundation(paths)


def test_cli_refuses_real_output_execution_without_authorized_mode() -> None:
    result = CliRunner().invoke(app, ["collect-real-outputs"])
    assert result.exit_code == 2
    assert "execution is not authorized" in result.output


def test_cli_dispatches_only_the_adr_bound_offline_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = find_repo_root(Path(__file__))
    observed: dict[str, object] = {}

    def fake_run(paths: RepoPaths, **kwargs: object) -> SimpleNamespace:
        observed["root"] = paths.root
        observed.update(kwargs)
        return SimpleNamespace(
            report=SimpleNamespace(passed=True),
            output_dir=root / "data" / "real_outputs" / "smoke" / "fixture",
            report_path=root / "data" / "real_outputs" / "smoke" / "fixture" / "report.json",
            run_manifest_path=root / "runs" / "fixture" / "manifest.json",
        )

    monkeypatch.setattr(
        "leanfaith.cli.collect_real_outputs.run_lf021_offline_smoke",
        fake_run,
    )
    result = CliRunner().invoke(
        app,
        [
            "collect-real-outputs",
            "--run-offline-smoke",
            "--root",
            str(root),
            "--output-dir",
            "data/real_outputs/smoke/fixture",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "offline smoke PASSED" in result.output
    assert "network_calls_made=0" in result.output
    assert observed["root"] == root
    assert observed["output_dir"] == Path("data/real_outputs/smoke/fixture")
    smoke_argv = observed["argv"]
    assert isinstance(smoke_argv, tuple)
    assert "--run-offline-smoke" in smoke_argv


def test_cli_rejects_conflicting_foundation_and_smoke_modes() -> None:
    result = CliRunner().invoke(
        app,
        [
            "collect-real-outputs",
            "--validate-foundation",
            "--run-offline-smoke",
        ],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_offline_smoke_rejects_existing_or_escaping_output_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    paths = RepoPaths(root=root)

    existing = root / "existing"
    existing.mkdir()
    with pytest.raises(LF021FoundationError, match="immutable and already exists"):
        run_lf021_offline_smoke(paths, output_dir=existing)

    outside = tmp_path / "outside"
    outside.mkdir()
    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    with pytest.raises(LF021FoundationError, match="inside repository root"):
        run_lf021_offline_smoke(paths, output_dir=escape / "run")


def test_smoke_only_terminal_cannot_claim_semantic_pool_admission() -> None:
    payload = {
        "terminal_id": f"collection_terminal:{'0' * 64}",
        "problem_record_id": "problem:test",
        "provider_request_hash": "1" * 64,
        "call_id": "call:test",
        "attempt_id": "call_attempt:test",
        "seed": 0,
        "parse_status": ParseStatus.PARSED,
        "terminal_status": "materialized_smoke_only",
        "materializer_outcome_id": "real_output:test",
        "candidate_theorem_id": "thm:test",
        "representation_id": "repr:test",
        "screening_id": "candidate_screen:test",
    }

    terminal = LF021CollectionTerminalRecord.model_validate(payload)
    assert terminal.semantic_pool_admitted is False
    assert terminal.pair_ids == ()
    assert terminal.nl_lean_id is None

    with pytest.raises(ValueError, match="cannot enter semantic pools"):
        LF021CollectionTerminalRecord.model_validate(
            {
                **payload,
                "semantic_pool_admitted": True,
            }
        )
