from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes
from leanfaith.sft2b.source_review_v4_terra_retry1 import (
    TerraRetryError,
    _object,
    _prove_pre_inference_schema_rejection,
    _strip_unique_items,
    load_retry,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG = _REPO_ROOT / "configs/sft2b/source_review_contract_v4_terra_retry1.json"


def test_transport_schema_removes_only_unsupported_unique_items() -> None:
    logical = _object(_REPO_ROOT / "configs/sft2b/source_review_output_schema_v4.json")
    transport = _object(
        _REPO_ROOT / "configs/sft2b/source_review_output_schema_v4_terra_retry1.json"
    )
    assert _strip_unique_items(logical) == transport
    assert "uniqueItems" in json.dumps(logical)
    assert "uniqueItems" not in json.dumps(transport)


def test_pre_inference_proof_rejects_any_model_item() -> None:
    events = (
        {"type": "thread.started", "thread_id": "thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {
            "type": "error",
            "message": "invalid_json_schema uniqueItems not permitted",
        },
        {"type": "turn.failed", "error": {"message": "failed"}},
    )
    stdout = b"".join(canonical_json_bytes(event) + b"\n" for event in events)
    with pytest.raises(TerraRetryError, match="pre-inference shape"):
        _prove_pre_inference_schema_rejection(
            stdout,
            expected_code="invalid_json_schema",
            expected_keyword="uniqueItems",
        )


def test_real_retry_preflight_binds_initial_failure_without_calls() -> None:
    initial_root = Path(
        "/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/source_reviews/"
        "source_review_contract_v4_model_panel_smoke/"
        "sft2b_model_review_run:cfb529a7d7a86e6e0d45d797f7d685a8ef665dfc1caea75b8d09e3bbf3e3e604"
    )
    if not initial_root.is_dir():
        pytest.skip("initial v4 model-panel evidence is unavailable on this host")
    loaded = load_retry(_REPO_ROOT, _CONFIG)
    assert loaded.lineage.original_terra_model_answer_produced is False
    assert loaded.lineage.original_terra_usage_event_produced is False
    assert loaded.config.authorization.maximum_provider_calls == 1
    assert loaded.config.authorization.successful_opus_recall_authorized is False
