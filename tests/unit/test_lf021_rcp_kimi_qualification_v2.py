"""The v2 RCP qualification envelope binds all executable artifacts."""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.generation import rcp_qualification_v1 as engine
from leanfaith.generation.rcp_qualification_v2 import (
    RCPQualificationV2Error,
    execute_one_rcp_qualification_v2,
    load_rcp_qualification_v2,
    probe_rcp_catalog_v2,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/generation/rcp_kimi_qualification_v2.yaml"
UTC = datetime.datetime(2026, 7, 24, 2, 30, tzinfo=datetime.UTC)


class FakeTransport:
    def __init__(self) -> None:
        self.post_count = 0

    def get(self, *, url: str, api_key: str, timeout_seconds: int) -> engine.RCPHTTPResponse:
        del url, api_key, timeout_seconds
        return engine.RCPHTTPResponse(
            status_code=200,
            body=canonical_json_bytes(
                {
                    "data": [
                        {"id": "moonshotai/Kimi-K2.6"},
                        {"id": "moonshotai/Kimi-K2.7-Code"},
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
    ) -> engine.RCPHTTPResponse:
        del url, api_key, timeout_seconds
        self.post_count += 1
        assert payload["reasoning_effort"] == "high"
        assert payload["chat_template_kwargs"] == {"enable_thinking": True}
        return engine.RCPHTTPResponse(
            status_code=200,
            body=canonical_json_bytes(
                {
                    "id": "fixture",
                    "model": "moonshotai/Kimi-K2.7-Code",
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "```lean4\n"
                                    "theorem leanfaith_rcp_qualification_v1 "
                                    ": ∀ n : Nat, n = n\n"
                                    "```"
                                )
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                    },
                }
            ),
        )


def _clock() -> datetime.datetime:
    return UTC


def test_v2_loader_binds_every_execution_and_policy_artifact() -> None:
    loaded = load_rcp_qualification_v2(CONFIG, repo_root=ROOT)

    assert set(loaded.bound_artifact_hashes) == {
        "engine_config",
        "engine_module",
        "wrapper_module",
        "cli_script",
        "prompt_template",
        "provider_portfolio",
        "remote_generation_policy",
    }
    assert loaded.loaded_config.config.contamination.contamination_status == "unknown"
    assert loaded.loaded_config.config.contamination.unseen_claim_eligible is False
    assert loaded.loaded_config.config.bulk_execution_available is False
    assert loaded.engine_loaded.loaded_config.config_hash == loaded.loaded_config.config_hash


def test_v2_hash_drift_blocks_before_provider(tmp_path: Path) -> None:
    original = CONFIG.read_text(encoding="utf-8")
    engine_hash = json.loads(
        json.dumps(load_rcp_qualification_v2(CONFIG, repo_root=ROOT).bound_artifact_hashes)
    )["engine_module"]
    modified = original.replace(engine_hash, '"' + "0" * 64 + '"')
    temporary = tmp_path / "rcp_kimi_qualification_v2.drift-test.yaml"
    temporary.write_text(modified, encoding="utf-8")
    try:
        with pytest.raises(RCPQualificationV2Error, match="hash drift"):
            load_rcp_qualification_v2(temporary, repo_root=ROOT)
    finally:
        temporary.unlink(missing_ok=True)


def test_v2_one_call_writes_manifest_with_no_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = CONFIG.read_text(encoding="utf-8")
    test_config = tmp_path / "rcp-v2.yaml"
    test_config.write_text(
        original.replace(
            "data/raw/real_outputs/rcp_kimi_qualification_v2/v2",
            "runs/test_rcp_qualification_v2/v2",
        ),
        encoding="utf-8",
    )
    loaded = load_rcp_qualification_v2(test_config, repo_root=ROOT)
    monkeypatch.setenv("RCP_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("RCP_API_KEY", "v2-secret-never-persist")
    credentials = engine.resolve_rcp_credentials(loaded.engine_loaded.loaded_config.config)
    transport = FakeTransport()
    catalog = probe_rcp_catalog_v2(
        loaded,
        credentials=credentials,
        transport=transport,
        clock=_clock,
    )

    run = execute_one_rcp_qualification_v2(
        loaded,
        catalog=catalog,
        credentials=credentials,
        repo_root=ROOT,
        transport=transport,
        clock=_clock,
        sleeper=lambda _seconds: None,
    )

    assert transport.post_count == 1
    assert run.engine_run.terminal.status is engine.RCPTerminalStatus.RAW_COLLECTED
    assert run.manifest.config_hash == loaded.loaded_config.config_hash
    assert run.manifest.bound_artifact_hashes == loaded.bound_artifact_hashes
    assert run.manifest.contamination_status == "unknown"
    assert run.manifest.unseen_claim_eligible is False
    assert run.manifest.heldout_claim_eligible is False
    assert run.manifest.evaluation_claim_eligible is False
    assert run.manifest.semantic_labels_created is False
    assert run.manifest.supervision_eligible is False
    assert run.manifest.gate_credit_claimed is False
    for path in run.engine_run.output_directory.rglob("*"):
        if path.is_file():
            assert b"v2-secret-never-persist" not in path.read_bytes()
