from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.loading import load_config, load_yaml_mapping
from leanfaith.generation import remote_provider_portfolio_v2 as remote_portfolio
from leanfaith.generation.lf022_family_matrix_freeze import (
    LF022FamilyMatrixFreezeConfig,
    LF022FamilyMatrixFreezeError,
    build_lf022_family_matrix_freeze,
    verify_lf022_family_matrix_freeze,
    write_lf022_family_matrix_freeze,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/generation/lf022_production_family_matrix_freeze_v1.yaml"
CONFIG_V2 = ROOT / "configs/generation/lf022_production_family_matrix_freeze_v2.yaml"


def test_canonical_freeze_inputs_are_tracked_for_clean_checkout_replay() -> None:
    loaded = load_config(CONFIG, LF022FamilyMatrixFreezeConfig).config
    bindings = (
        loaded.rcp_catalog_wire,
        loaded.remote_portfolio,
        loaded.successful_lf022_smoke_config,
        loaded.lf022_smoke_report,
        loaded.failed_fourth_family_smoke_config,
        loaded.codex_qualification,
        loaded.local_generator_matrix,
    )
    paths = [CONFIG.relative_to(ROOT).as_posix()]
    paths.extend(binding.path for binding in bindings)
    portfolio_path = ROOT / loaded.remote_portfolio.path
    portfolio_config = remote_portfolio.RemoteProviderPortfolioV2.model_validate(
        load_yaml_mapping(portfolio_path)
    )
    paths.extend(
        binding.artifact for binding in remote_portfolio._iter_portfolio_bindings(portfolio_config)
    )

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_family_matrix_freeze_cli_replays_without_network() -> None:
    result = CliRunner().invoke(app, ["freeze-lf022-family-matrix"])

    assert result.exit_code == 0
    assert "BLOCKED_PENDING_FOURTH_SUPERVISION_FAMILY_QUALIFICATION" in result.output
    assert "network_requests=0" in result.output


def _copy_fixture(tmp_path: Path) -> Path:
    loaded = load_config(CONFIG, LF022FamilyMatrixFreezeConfig)
    bindings = (
        loaded.config.rcp_catalog_wire,
        loaded.config.remote_portfolio,
        loaded.config.successful_lf022_smoke_config,
        loaded.config.lf022_smoke_report,
        loaded.config.failed_fourth_family_smoke_config,
        loaded.config.codex_qualification,
        loaded.config.local_generator_matrix,
    )
    for binding in bindings:
        destination = tmp_path / binding.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / binding.path, destination)
    config = tmp_path / CONFIG.relative_to(ROOT)
    config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CONFIG, config)
    return config


def test_freeze_is_structurally_valid_but_scientifically_blocked() -> None:
    bundle = build_lf022_family_matrix_freeze(
        repo_root=ROOT,
        config_path=CONFIG,
    )

    matrix = bundle.family_matrix
    assert matrix.proposer_family_ids == ("moonshot_kimi_k2", "qwen3", "glm5")
    assert matrix.judge_family_ids == (
        "moonshot_kimi_k2",
        "qwen3",
        "glm5",
        "deepseek_v4",
    )
    assert matrix.sci_validator_family_ids == matrix.judge_family_ids
    assert matrix.heldout_eval_family_id == "openai_codex"
    assert "openai_codex" not in {
        *matrix.proposer_family_ids,
        *matrix.judge_family_ids,
        *matrix.sci_validator_family_ids,
    }
    assert len({pin.canonical_family for pin in matrix.family_registry}) == 5
    assert {pin.model_id for pin in matrix.family_registry} == {
        "moonshotai/Kimi-K2.7-Code",
        "Qwen/Qwen3.5-397B-A17B",
        "zai-org/GLM-5.2",
        "deepseek-ai/DeepSeek-V4-Pro",
        "openai/gpt-5.6-terra",
    }
    assert "moonshotai/Kimi-K2.6" not in {pin.model_id for pin in matrix.family_registry}
    assert "Qwen/Qwen3.6-35B-A3B" not in {pin.model_id for pin in matrix.family_registry}

    report = bundle.report
    assert report.status == "BLOCKED_PENDING_FOURTH_SUPERVISION_FAMILY_QUALIFICATION"
    assert report.provider_calls_performed == 0
    assert report.network_requests_performed == 0
    assert report.route_execution_authorized is False
    assert report.semantic_labels_created is False
    assert report.supervision_eligible is False
    assert report.training_eligible is False
    assert report.evaluation_independence_claim_eligible is False
    assert report.gate_credit_eligible is False
    assert "DeepSeek-V4-Pro" in report.blockers[0]
    assert {pin.family_id for pin in report.inactive_local_families} == {
        "goedel_formalizer_v2_8b",
        "kimina_autoformalizer_7b",
        "stepfun_formalizer_7b",
        "reform_8b",
    }
    assert report.same_family_or_mode_alternatives_not_counted == (
        "moonshotai/Kimi-K2.6",
        "Qwen/Qwen3.6-35B-A3B",
        "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "Qwen/Qwen3-VL-235B-A22B-Thinking",
    )


def test_v2_freeze_replaces_failed_glm_proposer_with_deepseek() -> None:
    loaded = load_config(CONFIG_V2, LF022FamilyMatrixFreezeConfig).config
    bundle = build_lf022_family_matrix_freeze(
        repo_root=ROOT,
        config_path=CONFIG_V2,
    )

    assert bundle.family_matrix.proposer_family_ids == (
        "moonshot_kimi_k2",
        "qwen3",
        "deepseek_v4",
    )
    assert "glm5" not in bundle.family_matrix.proposer_family_ids
    assert bundle.family_matrix.judge_family_ids == (
        "moonshot_kimi_k2",
        "qwen3",
        "glm5",
        "deepseek_v4",
    )
    assert bundle.report.network_requests_performed == 0
    assert bundle.report.route_execution_authorized is False
    assert bundle.report.semantic_labels_created is False
    assert bundle.report.training_eligible is False

    persisted_matrix = ROOT / loaded.outputs.family_matrix
    persisted_report = ROOT / loaded.outputs.freeze_report
    assert persisted_matrix.read_bytes() == canonical_json_bytes(
        bundle.family_matrix.model_dump(mode="json")
    )
    assert persisted_report.read_bytes() == canonical_json_bytes(
        bundle.report.model_dump(mode="json")
    )


def test_catalog_pins_bind_exact_persisted_deployment_selectors() -> None:
    bundle = build_lf022_family_matrix_freeze(
        repo_root=ROOT,
        config_path=CONFIG,
    )

    assert bundle.rcp_catalog.provider_id == "epfl_rcp"
    assert {(item.model_id, item.deployment_id) for item in bundle.rcp_catalog.deployments} == {
        ("moonshotai/Kimi-K2.7-Code", "moonshotai/Kimi-K2.7-Code"),
        ("Qwen/Qwen3.5-397B-A17B", "Qwen/Qwen3.5-397B-A17B"),
        ("zai-org/GLM-5.2", "zai-org/GLM-5.2"),
        ("deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Pro"),
    }
    assert bundle.codex_catalog.provider_id == "openai_codex_exec"
    assert [(item.model_id, item.deployment_id) for item in bundle.codex_catalog.deployments] == [
        ("openai/gpt-5.6-terra", "gpt-5.6-terra")
    ]
    for pin in bundle.family_matrix.family_registry:
        assert pin.pin_kind == "provider_deployment_snapshot"
        assert pin.checkpoint_revision is None
        assert pin.underlying_checkpoint_revision_status == "provider_not_disclosed"


def test_immutable_write_and_offline_replay(tmp_path: Path) -> None:
    config = _copy_fixture(tmp_path)
    written = write_lf022_family_matrix_freeze(
        repo_root=tmp_path,
        config_path=config,
    )
    replayed = verify_lf022_family_matrix_freeze(
        repo_root=tmp_path,
        config_path=config,
    )
    assert replayed == written

    loaded = load_config(config, LF022FamilyMatrixFreezeConfig).config
    for relative_path, model in (
        (loaded.outputs.rcp_catalog, written.rcp_catalog),
        (loaded.outputs.codex_catalog, written.codex_catalog),
        (loaded.outputs.family_matrix, written.family_matrix),
        (loaded.outputs.freeze_report, written.report),
    ):
        assert (tmp_path / relative_path).read_bytes() == canonical_json_bytes(
            model.model_dump(mode="json")
        )

    matrix_path = tmp_path / loaded.outputs.family_matrix
    matrix_path.write_bytes(matrix_path.read_bytes() + b"\n")
    with pytest.raises(
        LF022FamilyMatrixFreezeError,
        match="persisted freeze artifact differs",
    ):
        verify_lf022_family_matrix_freeze(
            repo_root=tmp_path,
            config_path=config,
        )


def test_bound_evidence_hash_drift_fails_closed(tmp_path: Path) -> None:
    config = _copy_fixture(tmp_path)
    loaded = load_config(config, LF022FamilyMatrixFreezeConfig).config
    catalog = tmp_path / loaded.rcp_catalog_wire.path
    catalog.write_bytes(catalog.read_bytes() + b"\n")

    with pytest.raises(LF022FamilyMatrixFreezeError, match="artifact hash mismatch"):
        build_lf022_family_matrix_freeze(
            repo_root=tmp_path,
            config_path=config,
        )


def test_fourth_family_failure_cannot_be_silently_rewritten(tmp_path: Path) -> None:
    config = _copy_fixture(tmp_path)
    loaded = load_config(config, LF022FamilyMatrixFreezeConfig).config
    report_path = tmp_path / loaded.lf022_smoke_report.path
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["terminal_attempts"][1]["diagnosis"] = "pretend success"
    report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")

    config_text = config.read_text(encoding="utf-8")
    config.write_text(
        config_text.replace(
            loaded.lf022_smoke_report.sha256,
            hash_file(report_path),
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        LF022FamilyMatrixFreezeError,
        match="structured-output failure is not preserved",
    ):
        build_lf022_family_matrix_freeze(
            repo_root=tmp_path,
            config_path=config,
        )
