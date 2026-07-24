"""Fail-closed tests for the one-problem RCP Qwen qualification."""

from __future__ import annotations

import datetime
import json
import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.generation import rcp_qualification_v1 as shared
from leanfaith.generation.rcp_qwen_qualification_v1 import (
    RCPQwenQualificationError,
    RCPQwenTerminalStatus,
    execute_one_qwen_qualification,
    load_completed_qwen_run,
    load_qwen_qualification,
    verify_qwen_qualification,
)
from leanfaith.lean.leaninteract_backend import BackendSettings
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/generation/rcp_qwen_qualification_v1.yaml"
UTC = datetime.datetime(2026, 7, 24, 4, 0, tzinfo=datetime.UTC)


class FakeTransport:
    def __init__(self, *, post_status: int = 200) -> None:
        self.get_count = 0
        self.post_count = 0
        self.post_status = post_status
        self.last_payload: Mapping[str, object] | None = None

    def get(
        self,
        *,
        url: str,
        api_key: str,
        timeout_seconds: int,
    ) -> shared.RCPHTTPResponse:
        del url, api_key, timeout_seconds
        self.get_count += 1
        return shared.RCPHTTPResponse(
            status_code=200,
            body=canonical_json_bytes(
                {
                    "data": [
                        {"id": "Qwen/Qwen3.5-397B-A17B"},
                        {"id": "Qwen/Qwen3.6-35B-A3B"},
                    ]
                }
            ),
        )

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> shared.RCPHTTPResponse:
        del url, api_key, timeout_seconds
        self.post_count += 1
        self.last_payload = payload
        if self.post_status != 200:
            return shared.RCPHTTPResponse(
                status_code=self.post_status,
                body=canonical_json_bytes({"error": {"message": "unsupported"}}),
            )
        return shared.RCPHTTPResponse(
            status_code=200,
            body=canonical_json_bytes(
                {
                    "id": "qwen-fixture",
                    "model": "Qwen/Qwen3.6-35B-A3B",
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "```lean4\n"
                                    "theorem leanfaith_rcp_qwen_qualification_v1 "
                                    "(x : ℝ) (hx : 0 ≤ x) : "
                                    "x - x ^ 3 / 6 ≤ Real.sin x\n"
                                    "```"
                                ),
                                "reasoning_content": "fixture reasoning",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 18,
                        "total_tokens": 30,
                    },
                }
            ),
        )


def _clock() -> datetime.datetime:
    return UTC


def _test_config(tmp_path: Path) -> tuple[Path, Path]:
    output_root = f"runs/test_rcp_qwen_{tmp_path.name}"
    text = CONFIG.read_text(encoding="utf-8").replace(
        "data/raw/real_outputs/rcp_qwen_qualification_v1/v1",
        output_root,
    )
    config = tmp_path / "qwen.yaml"
    config.write_text(text, encoding="utf-8")
    return config, ROOT / output_root


def test_qwen_loader_binds_full_envelope_and_one_family() -> None:
    loaded = load_qwen_qualification(CONFIG, repo_root=ROOT)
    config = loaded.loaded_config.config

    assert config.primary_model_id == "Qwen/Qwen3.6-35B-A3B"
    assert config.no_call_ablation_model_id == "Qwen/Qwen3.5-397B-A17B"
    assert config.all_qwen_checkpoints_one_family is True
    assert config.request_budget.maximum_chat_completion_requests == 1
    assert config.request_budget.maximum_dedicated_capability_requests == 0
    assert config.request_budget.retries_with_removed_fields == 0
    assert config.capability_policy.exact_application_proof_available is False
    assert config.policy.semantic_labels_created is False
    assert config.policy.supervision_eligible is False
    assert config.policy.gate_credit_claimed is False
    assert loaded.reference_blind_audit.reference_transmission_performed is False
    assert set(loaded.bound_artifact_hashes) == {
        "shared_transport_module",
        "engine_module",
        "cli_script",
        "prompt_template",
        "response_contract",
        "proposal",
        "execution_policy",
        "provider_portfolio",
        "remote_generation_policy",
    }


def test_one_request_sends_exact_qwen_envelope_and_replays(
    tmp_path: Path,
) -> None:
    config_path, test_root = _test_config(tmp_path)
    transport = FakeTransport()
    try:
        loaded = load_qwen_qualification(config_path, repo_root=ROOT)
        run = execute_one_qwen_qualification(
            loaded,
            credentials=shared.RCPCredentials(
                base_url="https://inference.rcp.epfl.ch/v1",
                api_key="qwen-unit-secret-never-persist",
            ),
            repo_root=ROOT,
            transport=transport,
            clock=_clock,
        )
        replay = load_completed_qwen_run(loaded, repo_root=ROOT)

        assert transport.get_count == 1
        assert transport.post_count == 1
        assert transport.last_payload is not None
        assert {
            key: transport.last_payload[key]
            for key in (
                "temperature",
                "top_p",
                "top_k",
                "min_p",
                "presence_penalty",
                "repetition_penalty",
                "max_tokens",
                "stream",
                "chat_template_kwargs",
            )
        } == {
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
            "max_tokens": 4096,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": True},
        }
        assert run.terminal.status is RCPQwenTerminalStatus.RAW_COLLECTED
        assert replay.resumed is True
        assert replay.manifest.manifest_id == run.manifest.manifest_id
        capability = json.loads(
            (run.output_directory / "attempts/0000/capability_evidence.json").read_text(
                encoding="utf-8"
            )
        )
        assert capability["combined_request_accepted"] is True
        assert capability["exact_field_application_proven"] is False
        assert capability["claim"] == "route_accepted_complete_payload_application_unproven"
        assert run.manifest.no_call_ablation_requests_performed == 0
        assert run.manifest.semantic_labels_created is False
        assert run.manifest.supervision_eligible is False
        assert run.manifest.gate_credit_claimed is False
        for path in run.output_directory.rglob("*"):
            if path.is_file():
                assert b"qwen-unit-secret-never-persist" not in path.read_bytes()
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_rejected_field_envelope_is_not_retried_or_reduced(tmp_path: Path) -> None:
    config_path, test_root = _test_config(tmp_path)
    transport = FakeTransport(post_status=400)
    try:
        loaded = load_qwen_qualification(config_path, repo_root=ROOT)
        run = execute_one_qwen_qualification(
            loaded,
            credentials=shared.RCPCredentials(
                base_url="https://inference.rcp.epfl.ch/v1",
                api_key="rejection-test-secret",
            ),
            repo_root=ROOT,
            transport=transport,
            clock=_clock,
        )

        assert transport.get_count == 1
        assert transport.post_count == 1
        assert run.terminal.status is RCPQwenTerminalStatus.REQUEST_FAILED
        capability = json.loads(
            (run.output_directory / "attempts/0000/capability_evidence.json").read_text(
                encoding="utf-8"
            )
        )
        assert capability["combined_request_accepted"] is False
        assert capability["exact_field_application_proven"] is False
        assert set(capability["per_field_status"].values()) == {
            "request_failed_application_unproven"
        }
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_partial_claim_blocks_all_repeat_network_requests(tmp_path: Path) -> None:
    config_path, test_root = _test_config(tmp_path)
    transport = FakeTransport()
    try:
        loaded = load_qwen_qualification(config_path, repo_root=ROOT)
        loaded.output_directory.mkdir(parents=True, exist_ok=True)
        (loaded.output_directory / "execution_claim.json").write_text(
            '{"partial":true}\n',
            encoding="utf-8",
        )
        with pytest.raises(
            RCPQwenQualificationError,
            match="partial or concurrent",
        ):
            execute_one_qwen_qualification(
                loaded,
                credentials=shared.RCPCredentials(
                    base_url="https://inference.rcp.epfl.ch/v1",
                    api_key="partial-test-secret",
                ),
                repo_root=ROOT,
                transport=transport,
                clock=_clock,
            )
        assert transport.get_count == 0
        assert transport.post_count == 0
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_config_drift_after_load_fails_before_network(tmp_path: Path) -> None:
    config_path, test_root = _test_config(tmp_path)
    transport = FakeTransport()
    try:
        loaded = load_qwen_qualification(config_path, repo_root=ROOT)
        config_path.write_text(
            config_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        with pytest.raises(
            RCPQwenQualificationError,
            match="frozen artifact drift",
        ):
            execute_one_qwen_qualification(
                loaded,
                credentials=shared.RCPCredentials(
                    base_url="https://inference.rcp.epfl.ch/v1",
                    api_key="config-drift-secret",
                ),
                repo_root=ROOT,
                transport=transport,
                clock=_clock,
            )
        assert transport.get_count == 0
        assert transport.post_count == 0
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_bound_artifact_drift_fails_before_any_transport(tmp_path: Path) -> None:
    text = CONFIG.read_text(encoding="utf-8").replace(
        "765b4d661fa1f78459bfc3e3f5ba4a80a80f61c08ab8b9248494266be52bd1a4",
        "f" * 64,
    )
    config = tmp_path / "drift.yaml"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(RCPQwenQualificationError, match="hash drift"):
        load_qwen_qualification(config, repo_root=ROOT)


def test_offline_lean_validation_and_verification_are_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, test_root = _test_config(tmp_path)
    transport = FakeTransport()
    audit_paths: list[Path] = []

    class FakeBackend:
        def __init__(self, settings: BackendSettings) -> None:
            self.settings = settings

        def run(self, request: LeanRequest) -> LeanResult:
            code = request.code
            request_id = request.request_id
            context_id = request.context_id
            raw_dir = self.settings.raw_response_dir
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw = raw_dir / "fixture.json"
            raw.write_text('{"fixture":"leaninteract"}\n', encoding="utf-8")
            return LeanResult(
                request_id=request_id,
                request_hash=hash_canonical({"code": code}),
                context_id=context_id,
                context_fingerprint=context_id.removeprefix("ctx:"),
                status=LeanStatus.VALID_WITH_SORRY,
                sorries=({"kind": "sorry"},),
                declarations=({"full_name": "leanfaith_rcp_qwen_qualification_v1"},),
                raw_response_path=str(raw),
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "leanfaith.generation.rcp_qwen_qualification_v1.LeanInteractBackend",
        FakeBackend,
    )
    try:
        loaded = load_qwen_qualification(config_path, repo_root=ROOT)
        execute_one_qwen_qualification(
            loaded,
            credentials=shared.RCPCredentials(
                base_url="https://inference.rcp.epfl.ch/v1",
                api_key="verification-unit-secret",
            ),
            repo_root=ROOT,
            transport=transport,
            clock=_clock,
        )
        first = verify_qwen_qualification(
            loaded,
            repo_root=ROOT,
            credential="verification-unit-secret",
            mathlib_project_dir=ROOT,
        )
        audit_paths.append(first.report_path)
        second = verify_qwen_qualification(
            loaded,
            repo_root=ROOT,
            credential="verification-unit-secret",
        )

        assert first.report_sha256 == second.report_sha256
        assert first.report.verification_id == second.report.verification_id
        assert first.report.provider_calls_performed == 0
        assert first.report.network_requests_performed == 0
        assert (
            first.report.capability_claim == "route_accepted_complete_payload_application_unproven"
        )
        assert first.operational_validation.status == "valid_with_sorry"
        assert first.report.semantic_faithfulness_assessed is False
        assert first.report.semantic_labels_created is False
        assert first.report.gate_credit_claimed is False
    finally:
        shutil.rmtree(test_root, ignore_errors=True)
        for path in audit_paths:
            path.unlink(missing_ok=True)
