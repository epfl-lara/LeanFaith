"""Fail-closed checks for the LF-021 local-generator availability decision."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, cast

import yaml

from leanfaith.config.paths import find_repo_root

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_PINS = {
    (
        "AI-MO/Kimina-Autoformalizer-7B",
        "ddd47cb477d93b3ca990468e1c0d5ad6b60973dd",
    ),
    (
        "Goedel-LM/Goedel-Formalizer-V2-8B",
        "fe2d362d899601abe79d7d5e95eaa7fe9883a0cb",
    ),
    (
        "stepfun-ai/StepFun-Formalizer-7B",
        "fb0dc612761fecd64ebbc489c2a3417e9ea01968",
    ),
    (
        "GuoxinChen/ReForm-8B",
        "1589c832cfad679a280b222e694b987a33befd26",
    ),
}


def _root() -> Path:
    return find_repo_root(Path(__file__))


def _probe() -> dict[str, Any]:
    value = json.loads(
        (_root() / "reports/generation/lf021_local_model_probe_v1.json").read_text(encoding="utf-8")
    )
    return cast(dict[str, Any], value)


def test_local_generator_probe_pins_four_disabled_roles_without_gate_credit() -> None:
    probe = _probe()
    models = cast(list[dict[str, Any]], probe["models"])

    assert probe["status"] == "available_not_runtime_qualified"
    assert {(model["repo_id"], model["revision"]) for model in models} == _EXPECTED_PINS
    assert Counter(model["intended_role"] for model in models) == {
        "supervision_eligible_generator_1": 1,
        "supervision_eligible_generator_2": 1,
        "supervision_eligible_generator_3": 1,
        "supervision_excluded_heldout_generator": 1,
    }
    assert all(model["activation_status"].startswith("disabled_") for model in models)
    assert all(model["private"] is False and model["gated"] is False for model in models)
    assert all(model["runtime_qualified"] is False for model in models)
    assert all(model["gate_5g_credit"] is False for model in models)

    rules = cast(dict[str, Any], probe["rules"])
    assert rules["inference_execution_authorized"] is False
    assert rules["checkpoint_weights_downloaded"] is False
    assert rules["metadata_download_performed"] is True
    assert rules["external_inference_api_used"] is False
    assert rules["private_source_transmission_allowed"] is False
    assert rules["qualifies_for_gate_5g"] is False


def test_local_generator_probe_binds_all_pinned_metadata_artifacts() -> None:
    models = cast(list[dict[str, Any]], _probe()["models"])

    for model in models:
        artifacts = cast(dict[str, str], model["artifact_sha256"])
        assert set(artifacts) == {
            "README.md",
            "config.json",
            "tokenizer_config.json",
            "generation_config.json",
        }
        assert all(_HEX64.fullmatch(digest) is not None for digest in artifacts.values())
        assert cast(int, model["checkpoint_weight_bytes"]) > 0
        assert cast(int, model["parameter_count"]) > 0


def test_local_generator_adr_does_not_override_disabled_collection_config() -> None:
    root = _root()
    real_outputs = cast(
        dict[str, Any],
        yaml.safe_load(
            (root / "configs/generation/real_outputs_v1.yaml").read_text(encoding="utf-8")
        ),
    )
    providers = cast(
        dict[str, Any],
        yaml.safe_load((root / "configs/generation/providers.yaml").read_text(encoding="utf-8")),
    )
    adr = (root / "docs/adr/ADR-0006-lf021-local-generators.md").read_text(encoding="utf-8")

    assert real_outputs["generation_enabled"] is False
    assert real_outputs["execution"]["local_provider_calls_enabled"] is False
    assert real_outputs["execution"]["allowed_provider_slots"] == []
    assert providers["status"] == "external_slots_disabled_until_phase_5_adr"
    assert "the checked-in configuration authorizes zero" in adr
    assert "local model calls." in adr
    assert "`direct_autoformalization_v1` parser is deliberately stricter" in adr
    assert "it does not qualify any model under this ADR" in adr
    assert "Neither this ADR nor successful smoke fixtures close Gate 5G." in adr
