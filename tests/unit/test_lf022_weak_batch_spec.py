from __future__ import annotations

import json
import shutil
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation import lf022_weak_batch_spec as batch_spec_module
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
    LF022SupervisionCandidateRecord,
    _judge_visible_payload_hash,
)
from leanfaith.generation.lf022_weak_batch import (
    LF022WeakBatchError,
    LF022WeakBatchSpec,
    prepare_lf022_weak_batch,
)
from leanfaith.generation.lf022_weak_batch_spec import (
    freeze_lf022_qwen_weak_batch_spec,
)
from leanfaith.generation.lf022_weak_live_smoke import (
    lf022_weak_judge_route_for_slot,
)
from leanfaith.generation.weak_supervision import PublicLeanJudgePair
from leanfaith.schemas.ids import make_id

KEY = b"qwen-weak-judge-randomization-key-v1"


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hash_file(path)


def _pair() -> PublicLeanJudgePair:
    return PublicLeanJudgePair(
        pair_id="pair:" + "1" * 64,
        canonical_lean_a="theorem source (n : Nat) : n = n",
        canonical_lean_b="theorem candidate (n : Nat) : n ≤ n",
        optional_natural_language=None,
        source_record_ids=("thm:" + "2" * 64, "var:" + "3" * 64),
        source_is_public=True,
        private_source_content=False,
        external_transmission_allowed=True,
        denylist_checked=True,
    )


def _qwen_candidate() -> LF022SupervisionCandidateRecord:
    pair = _pair()
    source_item_id = "lf022_supervision_source:" + "5" * 64
    values: dict[str, object] = {
        "schema_version": 3,
        "collection_id": "qwen-fixture-v3",
        "pair_id": pair.pair_id,
        "variant_id": "var:" + "3" * 64,
        "lean_check_id": "lf022_lean_check:" + "4" * 64,
        "proposer_family_id": "qwen3",
        "proposer_model": "Qwen/Qwen3.5-397B-A17B",
        "pair": pair.model_dump(mode="json"),
        "pair_admission_sha256": pair.admission_sha256,
        "judge_visible_payload_sha256": _judge_visible_payload_hash(pair),
        "dispatch_status": "ready_for_two_family_judging",
        "canonical_dispatch_pair_id": pair.pair_id,
        "source_candidate_item_id": source_item_id,
        "canonical_dispatch_source_item_id": source_item_id,
        "required_judgment_cells": (
            "judge_A:AB",
            "judge_A:BA",
            "judge_B:AB",
            "judge_B:BA",
        ),
        "promotion_blockers": (
            "human_pilot_not_bound",
            "promotion_audit_missing",
            "silver_not_promoted",
            "swapped_order_judgments_missing",
            "two_family_judgments_missing",
        ),
        "candidate_state": "unresolved_awaiting_two_family_judging",
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    return LF022SupervisionCandidateRecord.model_validate(
        {
            **values,
            "candidate_inventory_record_id": make_id("lf022_supervision_candidate", values),
        }
    )


def _qwen_manifest(
    record_bytes: bytes,
    *,
    judge_a_family_id: str = "moonshot_kimi_k2",
) -> LF022SupervisionCandidateManifest:
    records_sha = sha256_hex(record_bytes)
    values: dict[str, object] = {
        "schema_version": 3,
        "method_version": "lf022_supervision_candidate_inventory_v3",
        "collection_id": "qwen-fixture-v3",
        "spec_sha256": "7" * 64,
        "checks_sha256": "8" * 64,
        "lean_check_manifest_sha256": "9" * 64,
        "codex_audit_manifest_sha256": None,
        "logical_input_binding_sha256": "a" * 64,
        "codex_response_artifact_set_sha256": None,
        "proposer_family_id": "qwen3",
        "proposer_model": "Qwen/Qwen3.5-397B-A17B",
        "judge_a_family_id": judge_a_family_id,
        "judge_b_family_id": "deepseek_v4",
        "primary_eval_judge_family_id": "openai_codex",
        "records_artifact": "candidates.jsonl",
        "records_sha256": records_sha,
        "public_sample_artifact": "public_sample.jsonl",
        "public_sample_sha256": records_sha,
        "public_sample_count": 1,
        "summary_artifact": "summary.md",
        "summary_sha256": "c" * 64,
        "record_count": 1,
        "unique_judge_visible_payload_count": 1,
        "exact_duplicate_record_count": 0,
        "dispatch_eligible_count": 1,
        "required_future_judge_call_count": 4,
        "codex_diagnostic_status": "absent",
        "codex_diagnostic_record_count": 0,
        "codex_same_claim_counts": {},
        "dispatch_status_counts": {"ready_for_two_family_judging": 1},
        "codex_is_diagnostic_only": True,
        "two_family_judgments_completed": False,
        "human_pilot_bound": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    id_values = {
        key: value
        for key, value in values.items()
        if key
        not in {
            "records_artifact",
            "public_sample_artifact",
            "summary_artifact",
            "spec_sha256",
        }
    }
    return LF022SupervisionCandidateManifest.model_validate(
        {
            **values,
            "inventory_id": make_id("lf022_supervision_inventory", id_values),
        }
    )


def _fixture(
    tmp_path: Path,
    *,
    judge_a_family_id: str = "moonshot_kimi_k2",
) -> tuple[Path, Path, Path, Path]:
    candidate = _qwen_candidate()
    record_bytes = canonical_json_bytes(candidate.model_dump(mode="json")) + b"\n"
    records_path = tmp_path / "source/candidates.jsonl"
    _write(records_path, record_bytes)
    manifest = _qwen_manifest(
        record_bytes,
        judge_a_family_id=judge_a_family_id,
    )
    manifest_path = tmp_path / "source/manifest.json"
    _write(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    )

    config_root = tmp_path / "configs/generation"
    config_root.mkdir(parents=True)
    matrix_path = config_root / "lf022_production_family_matrix_v2.json"
    catalog_path = config_root / "lf022_rcp_catalog_snapshot_v1.json"
    shutil.copyfile(
        "configs/generation/lf022_production_family_matrix_v2.json",
        matrix_path,
    )
    shutil.copyfile(
        "configs/generation/lf022_rcp_catalog_snapshot_v1.json",
        catalog_path,
    )
    shutil.copyfile(
        "configs/generation/lf022_codex_catalog_snapshot_v1.json",
        config_root / "lf022_codex_catalog_snapshot_v1.json",
    )
    return manifest_path, records_path, matrix_path, catalog_path


def _admit_synthetic_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest_path: Path,
    records_path: Path,
) -> None:
    manifest = LF022SupervisionCandidateManifest.model_validate_json(manifest_path.read_bytes())
    profile = replace(
        batch_spec_module._QWEN_SNAPSHOT_PROFILE,
        inventory_id=manifest.inventory_id,
        candidate_manifest_sha256=hash_file(manifest_path),
        candidate_records_sha256=hash_file(records_path),
        record_count=manifest.record_count,
        dispatch_pair_count=manifest.dispatch_eligible_count,
        required_judge_call_count=manifest.required_future_judge_call_count,
    )
    monkeypatch.setattr(batch_spec_module, "_QWEN_SNAPSHOT_PROFILE", profile)


def test_freeze_qwen_spec_is_canonical_replayable_and_preparable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, records_path, matrix_path, _ = _fixture(tmp_path)
    _admit_synthetic_fixture(
        monkeypatch,
        manifest_path=manifest_path,
        records_path=records_path,
    )
    output_dir = tmp_path / "artifacts/qwen_weak_spec"
    weak_config = Path("configs/judges/weak_supervision.yaml").resolve()

    frozen = freeze_lf022_qwen_weak_batch_spec(
        repo_root=tmp_path,
        candidate_manifest_path=manifest_path,
        candidate_records_path=records_path,
        weak_supervision_config_path=weak_config,
        production_family_matrix_path=matrix_path,
        randomization_key=KEY,
        output_dir=output_dir,
    )
    replayed = freeze_lf022_qwen_weak_batch_spec(
        repo_root=tmp_path,
        candidate_manifest_path=manifest_path,
        candidate_records_path=records_path,
        weak_supervision_config_path=weak_config,
        production_family_matrix_path=matrix_path,
        randomization_key=KEY,
        output_dir=output_dir,
    )

    assert replayed == frozen
    assert frozen.spec_sha256 == hash_file(frozen.spec_path)
    assert frozen.dispatch_pair_count == 1
    assert frozen.required_judge_call_count == 4
    spec = LF022WeakBatchSpec.model_validate_json(frozen.spec_path.read_bytes())
    assert (
        spec.judge_a.decoding
        == lf022_weak_judge_route_for_slot("judge_A").decoding.provider_decoding()
    )
    assert (
        spec.judge_b.decoding
        == lf022_weak_judge_route_for_slot("judge_B").decoding.provider_decoding()
    )
    assert spec.primary_eval_family_id == "openai_codex"
    assert frozen.spec_path.read_bytes() == (
        canonical_json_bytes(spec.model_dump(mode="json")) + b"\n"
    )

    dispatches, dispatch_manifest = prepare_lf022_weak_batch(
        repo_root=tmp_path,
        spec_path=frozen.spec_path,
        expected_spec_sha256=frozen.spec_sha256,
        randomization_key=KEY,
        output_dir=tmp_path / "prepared_batch",
    )
    assert len(dispatches) == 4
    assert dispatch_manifest.dispatch_pair_count == 1
    assert dispatch_manifest.judge_a_family_id == "moonshot_kimi_k2"
    assert dispatch_manifest.judge_b_family_id == "deepseek_v4"


def test_freeze_qwen_spec_rejects_candidate_role_and_catalog_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, records_path, matrix_path, catalog_path = _fixture(
        tmp_path,
        judge_a_family_id="qwen3",
    )
    weak_config = Path("configs/judges/weak_supervision.yaml").resolve()
    _admit_synthetic_fixture(
        monkeypatch,
        manifest_path=manifest_path,
        records_path=records_path,
    )
    with pytest.raises(LF022WeakBatchError, match="reviewed judge roles"):
        freeze_lf022_qwen_weak_batch_spec(
            repo_root=tmp_path,
            candidate_manifest_path=manifest_path,
            candidate_records_path=records_path,
            weak_supervision_config_path=weak_config,
            production_family_matrix_path=matrix_path,
            randomization_key=KEY,
            output_dir=tmp_path / "artifacts/rejected",
        )

    manifest = _qwen_manifest(records_path.read_bytes())
    _write(
        manifest_path,
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    )
    _admit_synthetic_fixture(
        monkeypatch,
        manifest_path=manifest_path,
        records_path=records_path,
    )
    catalog_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(LF022WeakBatchError, match="production catalog differs"):
        freeze_lf022_qwen_weak_batch_spec(
            repo_root=tmp_path,
            candidate_manifest_path=manifest_path,
            candidate_records_path=records_path,
            weak_supervision_config_path=weak_config,
            production_family_matrix_path=matrix_path,
            randomization_key=KEY,
            output_dir=tmp_path / "artifacts/rejected",
        )


def test_freeze_qwen_weak_batch_spec_cli_is_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, records_path, matrix_path, _ = _fixture(tmp_path)
    _admit_synthetic_fixture(
        monkeypatch,
        manifest_path=manifest_path,
        records_path=records_path,
    )
    key_path = tmp_path / "randomization.key"
    key_path.write_bytes(KEY)

    def unexpected_network(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("offline spec freeze attempted network access")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_network)
    result = CliRunner().invoke(
        app,
        [
            "freeze-lf022-qwen-weak-batch-spec",
            "--root",
            str(tmp_path),
            "--candidate-manifest",
            str(manifest_path),
            "--candidate-records",
            str(records_path),
            "--randomization-key-file",
            str(key_path),
            "--weak-supervision-config",
            str(Path("configs/judges/weak_supervision.yaml").resolve()),
            "--production-family-matrix",
            str(matrix_path),
            "--output-dir",
            "artifacts/cli_qwen_weak_spec",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dispatch_pair_count"] == 1
    assert payload["required_judge_call_count"] == 4
    assert payload["network_calls_this_run"] == 0
    assert payload["training_eligible"] is False
    assert Path(payload["spec_path"]).is_file()


def test_freeze_qwen_weak_batch_spec_help() -> None:
    result = CliRunner().invoke(app, ["freeze-lf022-qwen-weak-batch-spec", "--help"])

    assert result.exit_code == 0
    assert "--candidate-manifest" in result.stdout
    assert "--randomization-key-file" in result.stdout


def test_snapshot_specific_freezer_rejects_unreviewed_qwen_inventory(tmp_path: Path) -> None:
    manifest_path, records_path, matrix_path, _ = _fixture(tmp_path)
    with pytest.raises(LF022WeakBatchError, match="candidate manifest differs"):
        freeze_lf022_qwen_weak_batch_spec(
            repo_root=tmp_path,
            candidate_manifest_path=manifest_path,
            candidate_records_path=records_path,
            weak_supervision_config_path=Path("configs/judges/weak_supervision.yaml").resolve(),
            production_family_matrix_path=matrix_path,
            randomization_key=KEY,
            output_dir=tmp_path / "artifacts/unreviewed",
        )


def test_snapshot_specific_freezer_rejects_symlinked_ancestors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path, records_path, matrix_path, _ = _fixture(tmp_path)
    _admit_synthetic_fixture(
        monkeypatch,
        manifest_path=manifest_path,
        records_path=records_path,
    )
    linked_source = tmp_path / "linked_source"
    linked_source.symlink_to(manifest_path.parent, target_is_directory=True)
    kwargs = {
        "repo_root": tmp_path,
        "candidate_manifest_path": linked_source / manifest_path.name,
        "candidate_records_path": records_path,
        "weak_supervision_config_path": Path("configs/judges/weak_supervision.yaml").resolve(),
        "production_family_matrix_path": matrix_path,
        "randomization_key": KEY,
        "output_dir": tmp_path / "artifacts/symlinked_input",
    }
    with pytest.raises(LF022WeakBatchError, match="symlink component"):
        freeze_lf022_qwen_weak_batch_spec(**kwargs)

    real_output = tmp_path / "real_output"
    real_output.mkdir()
    linked_output = tmp_path / "linked_output"
    linked_output.symlink_to(real_output, target_is_directory=True)
    kwargs["candidate_manifest_path"] = manifest_path
    kwargs["output_dir"] = linked_output / "spec"
    with pytest.raises(LF022WeakBatchError, match="symlink component"):
        freeze_lf022_qwen_weak_batch_spec(**kwargs)
