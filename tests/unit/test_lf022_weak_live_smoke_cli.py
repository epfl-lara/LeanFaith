from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import hash_file
from leanfaith.generation import lf022_weak_live_smoke as live
from leanfaith.generation.rcp_provider import UrllibOpenAICompatibleRCPTransport


def test_weak_live_smoke_commands_are_explicit_in_help() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "freeze-lf022-weak-live-smoke",
        "prepare-lf022-weak-live-smoke",
        "execute-lf022-weak-live-smoke",
        "replay-lf022-weak-live-smoke",
    ):
        assert command in result.stdout


def test_execute_weak_live_smoke_requires_explicit_flag_before_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("RCP_BASE_URL", raising=False)
    monkeypatch.delenv("RCP_API_KEY", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "execute-lf022-weak-live-smoke",
            "--root",
            str(tmp_path),
            "--batch-root",
            "batch",
            "--admission",
            "admission.json",
            "--admission-sha256",
            "0" * 64,
        ],
    )

    assert result.exit_code == 2
    assert "--execute-public-provisional is required" in result.output


def test_execute_weak_live_smoke_requires_exact_runtime_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RCP_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("RCP_API_KEY", "secret-that-must-not-print")

    result = CliRunner().invoke(
        app,
        [
            "execute-lf022-weak-live-smoke",
            "--root",
            str(tmp_path),
            "--batch-root",
            "batch",
            "--admission",
            "admission.json",
            "--admission-sha256",
            "0" * 64,
            "--execute-public-provisional",
        ],
    )

    assert result.exit_code == 2
    assert "exact runtime RCP_BASE_URL" in result.output
    assert "secret-that-must-not-print" not in result.output


def test_weak_live_smoke_cli_boundaries_use_core_apis_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch = tmp_path / "batch"
    batch.mkdir()
    frozen_dir = tmp_path / "frozen"
    frozen_dir.mkdir()
    config_path = frozen_dir / "live_smoke_config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    admission_path = batch / "live_smoke/admission.json"
    admission_path.parent.mkdir()
    admission_path.write_text("{}\n", encoding="utf-8")

    def fake_freeze(**kwargs: object) -> SimpleNamespace:
        assert kwargs["repo_root"] == tmp_path
        assert kwargs["batch_root"] == batch
        return SimpleNamespace(
            config_path=config_path,
            config_sha256=hash_file(config_path),
            judge_a_claim_path=frozen_dir / "judge_A_claim.json",
            judge_b_claim_path=frozen_dir / "judge_B_claim.json",
        )

    def fake_prepare(**kwargs: object) -> tuple[SimpleNamespace, SimpleNamespace]:
        assert kwargs["repo_root"] == tmp_path
        assert kwargs["batch_root"] == batch
        assert kwargs["config_path"] == config_path
        return (
            SimpleNamespace(
                eligible_candidate_count=718,
                selected_pair_id="pair:" + "1" * 64,
            ),
            SimpleNamespace(admission_id="lf022_weak_live_admission:" + "2" * 64),
        )

    monkeypatch.setattr(live, "freeze_lf022_weak_live_smoke_inputs", fake_freeze)
    monkeypatch.setattr(live, "prepare_lf022_weak_live_smoke", fake_prepare)
    runner = CliRunner()
    frozen = runner.invoke(
        app,
        [
            "freeze-lf022-weak-live-smoke",
            "--root",
            str(tmp_path),
            "--batch-root",
            "batch",
            "--production-catalog",
            "production.json",
            "--raw-rcp-catalog",
            "raw.json",
            "--code-bundle",
            "bundle.tar.gz",
            "--output-dir",
            "frozen",
        ],
    )
    assert frozen.exit_code == 0, frozen.output
    frozen_payload = json.loads(frozen.stdout)
    assert frozen_payload["network_calls_this_run"] == 0
    assert frozen_payload["training_eligible"] is False

    prepared = runner.invoke(
        app,
        [
            "prepare-lf022-weak-live-smoke",
            "--root",
            str(tmp_path),
            "--batch-root",
            "batch",
            "--config",
            str(config_path),
            "--config-sha256",
            hash_file(config_path),
        ],
    )
    assert prepared.exit_code == 0, prepared.output
    prepared_payload = json.loads(prepared.stdout)
    assert prepared_payload["eligible_candidate_count"] == 718
    assert prepared_payload["selected_pair_count"] == 1
    assert prepared_payload["admitted_cells"] == 4
    assert prepared_payload["network_calls_this_run"] == 0


def test_execute_and_replay_cli_report_nontraining_outputs_without_real_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "runtime-secret-that-must-not-print"
    monkeypatch.setenv("RCP_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("RCP_API_KEY", secret)
    calls: list[str] = []
    manifest = SimpleNamespace(
        execution_id="lf022_weak_live_execution:" + "3" * 64,
        status_counts={"succeeded": 4},
        transport_attempt_count=4,
        parsed_evidence_count=4,
        weak_candidate_count=1,
    )

    def fake_execute(**kwargs: object) -> tuple[tuple[object, ...], SimpleNamespace]:
        calls.append("execute")
        assert kwargs["execute_public_provisional"] is True
        credentials = kwargs["credentials"]
        assert isinstance(credentials, live.LF022WeakRuntimeCredentials)
        assert credentials.api_key == secret
        transports = kwargs["transports"]
        assert isinstance(transports, dict)
        assert isinstance(transports["judge_A"], UrllibOpenAICompatibleRCPTransport)
        assert transports["judge_A"] is transports["judge_B"]
        return ((object(),) * 4, manifest)

    def fake_replay(**kwargs: object) -> tuple[tuple[object, ...], SimpleNamespace]:
        calls.append("replay")
        assert set(kwargs) == {
            "repo_root",
            "batch_root",
            "admission_path",
            "expected_admission_sha256",
        }
        return ((object(),) * 4, manifest)

    monkeypatch.setattr(live, "execute_lf022_weak_live_smoke", fake_execute)
    monkeypatch.setattr(live, "replay_lf022_weak_live_smoke", fake_replay)
    args = [
        "--root",
        str(tmp_path),
        "--batch-root",
        "batch",
        "--admission",
        "admission.json",
        "--admission-sha256",
        "4" * 64,
    ]
    runner = CliRunner()
    executed = runner.invoke(
        app,
        ["execute-lf022-weak-live-smoke", *args, "--execute-public-provisional"],
    )
    assert executed.exit_code == 0, executed.output
    assert secret not in executed.output
    execute_payload = json.loads(executed.stdout)
    assert execute_payload["transport_attempt_count"] == 4
    assert execute_payload["semantic_labels_created"] == 0
    assert execute_payload["silver_records_created"] == 0
    assert execute_payload["training_eligible"] is False

    replayed = runner.invoke(app, ["replay-lf022-weak-live-smoke", *args])
    assert replayed.exit_code == 0, replayed.output
    replay_payload = json.loads(replayed.stdout)
    assert replay_payload["network_calls_this_run"] == 0
    assert replay_payload["training_eligible"] is False
    assert calls == ["execute", "replay"]
