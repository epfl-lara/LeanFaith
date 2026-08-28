"""Fail-closed unit tests for Kimi-v4 route-only eligibility."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation import lf022_kimi_v4_eligibility as eligibility_module
from leanfaith.generation.lf022_kimi_v4_eligibility import (
    LF022_KIMI_V4_ELIGIBILITY_PATH,
    LF022KimiV4EligibilityError,
    LF022KimiV4ProductionEligibility,
    verify_lf022_kimi_v4_production_eligibility,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.schemas.ids import make_id


def _binding(path: str, byte: str) -> dict[str, str]:
    return {"path": path, "sha256": byte * 64}


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "kimi_v4_challenge_replay_verified",
        "proposer_family_id": "moonshot_kimi_k2",
        "model_id": "moonshotai/Kimi-K2.7-Code",
        "deployment_id": "moonshotai/Kimi-K2.7-Code",
        "canonical_family": "moonshotai/kimi-k2",
        "provider_id": "epfl_rcp",
        "catalog_snapshot_id": f"lf022_provider_catalog:{'1' * 64}",
        "route_snapshot_revision": f"rcp-catalog-sha256:{'2' * 64}",
        "decoding_contract_id": "kimi_k2_7_public_proposer_v4",
        "decoding_contract_hash": "3" * 64,
        "v4_contract": _binding("configs/generation/lf022_kimi_k2_7_proposer_v4.yaml", "4"),
        "v4_prompt": _binding("prompts/proposers/lean_variant_v2.txt", "5"),
        "selection_id": f"lf022_kimi_v4_selection:{'6' * 64}",
        "selection": _binding(
            f"artifacts/generation/lf022_kimi_v4_challenge_selection_v2/{'6' * 64}.json",
            "7",
        ),
        "selection_code_tree_hash": "8" * 64,
        "selection_code_bundle": _binding("artifacts/code_bundle.tar.gz", "9"),
        "qualification_id": f"lf022_kimi_v4_qualification:{'a' * 64}",
        "qualification": _binding(
            f"data/lf022_kimi_v4_requalification/v1/{'6' * 64}/qualification.json",
            "b",
        ),
        "qualification_status": "passed",
        "qualification_terminal_count": 16,
        "strict_parse_success_count": 15,
        "replay_network_calls": 0,
        "family_matrix_id": f"lf022_family_matrix:{'c' * 64}",
        "family_matrix": _binding("artifacts/family_matrix.json", "d"),
        "judge_family_ids": ["deepseek_v4", "glm5", "qwen3"],
        "permitted_validator_family_ids": ["deepseek_v4", "glm5", "qwen3"],
        "heldout_eval_family_id": "openai_codex",
        "heldout_eval_supervision_excluded": True,
        "production_execution_scope": "public_provisional_g_open",
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "output_quality_tier": "provisional",
        "outputs_unresolved": True,
        "semantic_labels_created": False,
        "silver_promotion_enabled": False,
        "gold_promotion_enabled": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }


def _eligibility() -> LF022KimiV4ProductionEligibility:
    payload = _payload()
    return LF022KimiV4ProductionEligibility.model_validate(
        {
            **payload,
            "eligibility_id": make_id("lf022_kimi_v4_route_eligibility", payload),
        }
    )


def test_eligibility_is_route_only_and_content_addressed() -> None:
    record = _eligibility()
    assert record.qualification_status == "passed"
    assert record.strict_parse_success_count == 15
    assert record.outputs_unresolved is True
    assert record.semantic_labels_created is False
    assert record.training_eligible is False
    assert record.evaluation_eligible is False
    assert record.gate_credit_claimed is False

    with pytest.raises(ValidationError, match="eligibility_id"):
        LF022KimiV4ProductionEligibility.model_validate(
            {
                **record.model_dump(mode="json"),
                "eligibility_id": "lf022_kimi_v4_route_eligibility:" + "0" * 64,
            }
        )


def test_eligibility_rejects_self_judging_and_heldout_supervision() -> None:
    payload = _payload()
    payload["judge_family_ids"] = ["moonshot_kimi_k2", "qwen3"]
    with pytest.raises(ValidationError, match="cannot judge its own"):
        LF022KimiV4ProductionEligibility.model_validate(
            {
                **payload,
                "eligibility_id": make_id("lf022_kimi_v4_route_eligibility", payload),
            }
        )

    payload = _payload()
    payload["permitted_validator_family_ids"] = ["glm5", "openai_codex"]
    with pytest.raises(ValidationError, match="held-out evaluator"):
        LF022KimiV4ProductionEligibility.model_validate(
            {
                **payload,
                "eligibility_id": make_id("lf022_kimi_v4_route_eligibility", payload),
            }
        )


def test_verifier_reconstructs_exact_payload_and_replays_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _eligibility()
    path = tmp_path / LF022_KIMI_V4_ELIGIBILITY_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(record.model_dump(mode="json")) + b"\n")
    binding = LF022ArtifactBinding(
        path=LF022_KIMI_V4_ELIGIBILITY_PATH,
        sha256=hash_file(path),
    )
    selection = SimpleNamespace(selection_id=record.selection_id)
    qualification = SimpleNamespace(qualification_id=record.qualification_id)
    matrix = SimpleNamespace(matrix_id=record.family_matrix_id)
    replay_calls = 0

    monkeypatch.setattr(
        eligibility_module,
        "_load_frozen_selection",
        lambda **_: selection,
    )

    def replay(**_: object) -> object:
        nonlocal replay_calls
        replay_calls += 1
        return qualification

    monkeypatch.setattr(eligibility_module, "_replay_qualification", replay)
    monkeypatch.setattr(
        eligibility_module,
        "_verify_matrix",
        lambda **_: (
            matrix,
            record.judge_family_ids,
            record.permitted_validator_family_ids,
        ),
    )
    monkeypatch.setattr(
        eligibility_module,
        "_eligibility_payload",
        lambda **_: _payload(),
    )

    assert (
        verify_lf022_kimi_v4_production_eligibility(
            repo_root=tmp_path,
            eligibility_binding=binding,
        )
        == record
    )
    assert replay_calls == 1


def test_verifier_rejects_failed_offline_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _eligibility()
    path = tmp_path / LF022_KIMI_V4_ELIGIBILITY_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(record.model_dump(mode="json")) + b"\n")
    binding = LF022ArtifactBinding(
        path=LF022_KIMI_V4_ELIGIBILITY_PATH,
        sha256=hash_file(path),
    )
    monkeypatch.setattr(
        eligibility_module,
        "_load_frozen_selection",
        lambda **_: SimpleNamespace(selection_id=record.selection_id),
    )

    def reject(**_: object) -> object:
        raise LF022KimiV4EligibilityError("challenge did not pass")

    monkeypatch.setattr(eligibility_module, "_replay_qualification", reject)
    with pytest.raises(LF022KimiV4EligibilityError, match="did not pass"):
        verify_lf022_kimi_v4_production_eligibility(
            repo_root=tmp_path,
            eligibility_binding=binding,
        )
