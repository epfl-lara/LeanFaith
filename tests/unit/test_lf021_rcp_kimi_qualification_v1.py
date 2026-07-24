"""RCP Kimi qualification transport, privacy, and lineage tests."""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation.rcp_qualification_v1 import (
    RCPCatalogError,
    RCPCredentialError,
    RCPHTTPResponse,
    RCPQualificationConfig,
    RCPQualificationError,
    RCPTerminalStatus,
    execute_one_rcp_qualification,
    load_rcp_qualification,
    probe_rcp_catalog,
    redact_rcp_text,
    resolve_rcp_credentials,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/generation/rcp_kimi_qualification_v1.yaml"
UTC = datetime.datetime(2026, 7, 24, 2, 0, tzinfo=datetime.UTC)


class FakeTransport:
    def __init__(
        self,
        *,
        model_ids: tuple[str, ...] = (
            "moonshotai/Kimi-K2.6",
            "moonshotai/Kimi-K2.7-Code",
        ),
        post_responses: tuple[RCPHTTPResponse, ...] = (),
    ) -> None:
        self.model_ids = model_ids
        self.post_responses = list(post_responses)
        self.get_calls: list[tuple[str, str, int]] = []
        self.post_calls: list[tuple[str, str, Mapping[str, object], int]] = []

    def get(self, *, url: str, api_key: str, timeout_seconds: int) -> RCPHTTPResponse:
        self.get_calls.append((url, api_key, timeout_seconds))
        body = canonical_json_bytes(
            {
                "object": "list",
                "data": [{"id": model_id, "object": "model"} for model_id in self.model_ids],
            }
        )
        return RCPHTTPResponse(status_code=200, body=body)

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPHTTPResponse:
        self.post_calls.append((url, api_key, payload, timeout_seconds))
        if not self.post_responses:
            raise AssertionError("unexpected provider call")
        return self.post_responses.pop(0)


def _success_response() -> RCPHTTPResponse:
    return RCPHTTPResponse(
        status_code=200,
        body=canonical_json_bytes(
            {
                "id": "chatcmpl-fixture",
                "model": "moonshotai/Kimi-K2.7-Code",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": (
                                "```lean4\n"
                                "theorem leanfaith_rcp_qualification_v1 "
                                "{x : ℝ} (hx : 0 ≤ x) : "
                                "x - x ^ 3 / 6 ≤ Real.sin x\n"
                                "```"
                            ),
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                },
            }
        ),
    )


def _clock() -> datetime.datetime:
    return UTC


def test_config_models_share_family_and_are_generator_only() -> None:
    loaded = load_rcp_qualification(CONFIG, repo_root=ROOT)
    config = loaded.loaded_config.config

    assert isinstance(config, RCPQualificationConfig)
    assert config.models.primary.model_id == "moonshotai/Kimi-K2.7-Code"
    assert config.models.fallback.model_id == "moonshotai/Kimi-K2.6"
    assert config.models.primary.family_id == config.models.fallback.family_id
    assert config.models.primary.counts_as_independent_family is False
    assert config.models.fallback.counts_as_independent_family is False
    assert config.models.primary.judge_eligible is False
    assert config.models.fallback.judge_eligible is False
    assert config.policy.bulk_execution_available is False


def test_prompt_is_reference_blind_and_public_only() -> None:
    loaded = load_rcp_qualification(CONFIG, repo_root=ROOT)
    prompt = loaded.rendered_prompt
    reference = loaded.reference_theorems[0]

    assert loaded.problem.nl_statement in prompt
    assert loaded.problem.problem_id not in prompt
    assert loaded.problem.problem_record_id not in prompt
    assert loaded.problem.nl_source_link not in prompt
    assert reference.theorem_id not in prompt
    assert reference.declaration_name not in prompt
    assert reference.proof_stripped_declaration not in prompt
    assert "reference_transmission_performed" not in prompt
    assert loaded.reference_blind_audit.reference_transmission_performed is False


def test_credentials_read_only_exact_rcp_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = load_rcp_qualification(CONFIG, repo_root=ROOT)
    monkeypatch.setenv("RCP_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("RCP_API_KEY", "secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "must-be-ignored")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-also-be-ignored")

    credentials = resolve_rcp_credentials(loaded.loaded_config.config)

    assert credentials.base_url == "https://inference.rcp.epfl.ch/v1"
    assert credentials.api_key == "secret-value"
    assert "secret-value" not in repr(credentials)
    assert "must-be-ignored" not in repr(credentials)


def test_credentials_fail_closed_on_missing_or_wrong_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = load_rcp_qualification(CONFIG, repo_root=ROOT)
    monkeypatch.delenv("RCP_BASE_URL", raising=False)
    monkeypatch.delenv("RCP_API_KEY", raising=False)
    with pytest.raises(RCPCredentialError, match="RCP_BASE_URL"):
        resolve_rcp_credentials(loaded.loaded_config.config)

    monkeypatch.setenv("RCP_BASE_URL", "https://untrusted.example/v1")
    monkeypatch.setenv("RCP_API_KEY", "secret")
    with pytest.raises(RCPCredentialError, match="frozen HTTPS endpoint"):
        resolve_rcp_credentials(loaded.loaded_config.config)


def test_catalog_requires_both_exact_model_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = load_rcp_qualification(CONFIG, repo_root=ROOT)
    monkeypatch.setenv("RCP_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("RCP_API_KEY", "secret")
    credentials = resolve_rcp_credentials(loaded.loaded_config.config)
    transport = FakeTransport(model_ids=("moonshotai/Kimi-K2.7-Code-int4",))

    with pytest.raises(RCPCatalogError, match="exact frozen model IDs"):
        probe_rcp_catalog(
            loaded,
            credentials=credentials,
            transport=transport,
            clock=_clock,
        )


def test_catalog_freezes_exact_ids_response_hash_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = load_rcp_qualification(CONFIG, repo_root=ROOT)
    monkeypatch.setenv("RCP_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("RCP_API_KEY", "secret")
    credentials = resolve_rcp_credentials(loaded.loaded_config.config)
    transport = FakeTransport()

    first = probe_rcp_catalog(
        loaded,
        credentials=credentials,
        transport=transport,
        clock=_clock,
    )
    second = probe_rcp_catalog(
        loaded,
        credentials=credentials,
        transport=transport,
        clock=_clock,
    )

    assert first == second
    assert first.observed_at == UTC
    assert first.exact_model_ids == (
        "moonshotai/Kimi-K2.7-Code",
        "moonshotai/Kimi-K2.6",
    )
    assert len(first.raw_response_sha256) == 64
    assert first.credential_serialized is False


def test_redaction_removes_exact_key_bearer_and_assignments() -> None:
    key = "rcp-secret-123"
    text = f"RCP_API_KEY={key} Authorization:Bearer {key} api_key:{key} raw={key}"

    redacted = redact_rcp_text(text, api_key=key)

    assert key not in redacted
    assert "<redacted>" in redacted


def test_one_call_persists_reference_blind_lineage_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_copy = tmp_path / "rcp.yaml"
    document = CONFIG.read_text(encoding="utf-8").replace(
        "data/raw/real_outputs/rcp_kimi_qualification_v1",
        "runs/test_rcp_qualification",
    )
    config_copy.write_text(document, encoding="utf-8")
    loaded = load_rcp_qualification(config_copy, repo_root=ROOT)
    monkeypatch.setenv("RCP_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("RCP_API_KEY", "unique-secret-never-persist")
    credentials = resolve_rcp_credentials(loaded.loaded_config.config)
    transport = FakeTransport(post_responses=(_success_response(),))
    catalog = probe_rcp_catalog(
        loaded,
        credentials=credentials,
        transport=transport,
        clock=_clock,
    )

    run = execute_one_rcp_qualification(
        loaded,
        catalog=catalog,
        credentials=credentials,
        repo_root=ROOT,
        transport=transport,
        clock=_clock,
        sleeper=lambda _seconds: None,
    )

    assert run.terminal.status is RCPTerminalStatus.RAW_COLLECTED
    assert run.terminal.semantic_labels_created is False
    assert run.terminal.supervision_eligible is False
    assert run.terminal.gate_credit_claimed is False
    assert run.parsed_declaration is not None
    assert len(transport.post_calls) == 1
    payload = transport.post_calls[0][2]
    assert payload["model"] == "moonshotai/Kimi-K2.7-Code"
    assert payload["reasoning_effort"] == "high"
    assert payload["chat_template_kwargs"] == {"enable_thinking": True}
    assert loaded.problem.reference_theorem_ids[0] not in json.dumps(payload)
    for path in run.output_directory.rglob("*"):
        if path.is_file():
            assert b"unique-secret-never-persist" not in path.read_bytes()
    assert hash_file(run.terminal_path)


def test_retry_lineage_is_append_only_and_cold_start_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_copy = tmp_path / "rcp.yaml"
    document = CONFIG.read_text(encoding="utf-8").replace(
        "data/raw/real_outputs/rcp_kimi_qualification_v1",
        "runs/test_rcp_retry",
    )
    config_copy.write_text(document, encoding="utf-8")
    loaded = load_rcp_qualification(config_copy, repo_root=ROOT)
    monkeypatch.setenv("RCP_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("RCP_API_KEY", "secret")
    credentials = resolve_rcp_credentials(loaded.loaded_config.config)
    transport = FakeTransport(
        post_responses=(
            RCPHTTPResponse(
                status_code=503,
                body=b'{"error":{"message":"model is loading"}}',
            ),
            _success_response(),
        )
    )
    catalog = probe_rcp_catalog(
        loaded,
        credentials=credentials,
        transport=transport,
        clock=_clock,
    )
    sleeps: list[float] = []

    run = execute_one_rcp_qualification(
        loaded,
        catalog=catalog,
        credentials=credentials,
        repo_root=ROOT,
        transport=transport,
        clock=_clock,
        sleeper=sleeps.append,
    )

    assert run.terminal.status is RCPTerminalStatus.RAW_COLLECTED
    assert len(run.attempt_paths) == 2
    assert sleeps == [5.0]
    first = json.loads(run.attempt_paths[0].read_text(encoding="utf-8"))
    second = json.loads(run.attempt_paths[1].read_text(encoding="utf-8"))
    assert first["status"] == "retryable_http_error"
    assert first["error_code"] == "cold_start"
    assert first["retryable"] is True
    assert second["status"] == "response_received"
    assert first["provider_attempt_id"] != second["provider_attempt_id"]
    assert first["request_hash"] == second["request_hash"]


def test_terminal_http_error_exhausts_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_copy = tmp_path / "rcp.yaml"
    document = CONFIG.read_text(encoding="utf-8").replace(
        "data/raw/real_outputs/rcp_kimi_qualification_v1",
        "runs/test_rcp_terminal_error",
    )
    config_copy.write_text(document, encoding="utf-8")
    loaded = load_rcp_qualification(config_copy, repo_root=ROOT)
    monkeypatch.setenv("RCP_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("RCP_API_KEY", "secret")
    credentials = resolve_rcp_credentials(loaded.loaded_config.config)
    transport = FakeTransport(
        post_responses=(RCPHTTPResponse(status_code=400, body=b'{"error":"bad request"}'),)
    )
    catalog = probe_rcp_catalog(
        loaded,
        credentials=credentials,
        transport=transport,
        clock=_clock,
    )

    run = execute_one_rcp_qualification(
        loaded,
        catalog=catalog,
        credentials=credentials,
        repo_root=ROOT,
        transport=transport,
        clock=_clock,
        sleeper=lambda _seconds: pytest.fail("terminal error must not retry"),
    )

    assert run.terminal.status is RCPTerminalStatus.EXHAUSTED
    assert len(run.attempt_paths) == 1
    assert run.terminal.semantic_labels_created is False


def test_no_bulk_entrypoint_or_private_bypass() -> None:
    import leanfaith.generation.rcp_qualification_v1 as module

    assert not hasattr(module, "execute_bulk_rcp_collection")
    assert not hasattr(module, "send_private_source")
    assert module._EXPECTED_BASE_URL_ENV == "RCP_BASE_URL"
    assert module._EXPECTED_API_KEY_ENV == "RCP_API_KEY"


def test_artifact_hash_drift_fails_closed(tmp_path: Path) -> None:
    config_copy = tmp_path / "rcp.yaml"
    config_copy.write_text(
        CONFIG.read_text(encoding="utf-8").replace(
            "c8b0329b101269015d8e33cce6cb06d9c8b5dbb30a936f787a177f013fd4155c",
            '"' + "0" * 64 + '"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(RCPQualificationError, match="hash drift"):
        load_rcp_qualification(config_copy, repo_root=ROOT)
