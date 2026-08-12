"""Offline tests for v4-to-Sol/Fable weak-batch authoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.generation import lf022_sol_fable_batch_v1 as sol_fable
from leanfaith.generation.lf022_sol_fable_batch_v1 import (
    LF022SolFableBatchError,
    prepare_lf022_sol_fable_batch_v1,
)
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateRecord,
)

REPO_ROOT = Path(".").resolve()
V4_ROOT = Path(
    "/storage/milikic/leanfaith/lf022_judge_design/sol_fable_public_v4/"
    "e81d93c752d232da8847eb97db611dc0e31eaee0e4304de418ee5cfe21f9eb6a"
)
KEY = b"lf022-sol-fable-offline-test-key-v1"
GENERAL_SOL_XHIGH_SUMMARY = REPO_ROOT / "reports/generation/lf022_codex_sol_xhigh_v2_summary.json"
KIMI_SOL_XHIGH_SUMMARY = Path(
    "/storage/milikic/leanfaith/lf022_codex_audits/"
    "kimi_v4_641d13d_prefix256_v1_summary/summary.json"
)
HISTORICAL_SOL_XHIGH_SUMMARIES = (
    GENERAL_SOL_XHIGH_SUMMARY,
    KIMI_SOL_XHIGH_SUMMARY,
)


def _require_v4() -> None:
    if not (V4_ROOT / "manifest.json").is_file():
        pytest.skip(f"fixed local LF-022 v4 artifact unavailable: {V4_ROOT}")
    if any(not path.is_file() for path in HISTORICAL_SOL_XHIGH_SUMMARIES):
        pytest.skip("fixed historical Sol/xhigh summaries are unavailable")


def _prepare(tmp_path: Path, *, partition: str = "qwen_snapshot1019"):
    _require_v4()
    return prepare_lf022_sol_fable_batch_v1(
        repo_root=REPO_ROOT,
        v4_root=V4_ROOT,
        source_partition_id=partition,
        offset_pairs=0,
        limit_pairs=1,
        randomization_key=KEY,
        output_dir=tmp_path / "prepared",
        historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
    )


def test_one_pair_creates_exact_sol_and_fable_ab_ba_cells(tmp_path: Path) -> None:
    result = _prepare(tmp_path)

    assert result.authoring.selected_pair_count == 1
    assert result.authoring.dispatch_cell_count == 4
    assert result.authoring.unique_source_theorem_lineage_count == 1
    assert result.authoring.lineage_diversity_status == (
        "distinct_source_theorem_lineages_not_full_ancestry_certified"
    )
    assert result.authoring.schema_version == 3
    assert result.authoring.method_version == "lf022_sol_fable_batch_v3"
    assert result.authoring.source_v4_artifact_path == str(V4_ROOT)
    assert result.authoring.historical_sol_xhigh_pair_count == 694
    assert result.authoring.historical_sol_xhigh_exclusion_complete
    assert result.authoring.selected_pairs_absent_from_historical_sol_xhigh
    assert len(result.authoring.historical_sol_xhigh_corpora) == 2
    assert {binding.finding_count for binding in result.authoring.historical_sol_xhigh_corpora} == {
        201,
        493,
    }
    assert not (
        set(result.authoring.selected_pair_ids)
        & set(result.authoring.excluded_historical_sol_pair_ids)
    )
    assert result.authoring.selected_pair_ids == tuple(
        sorted({cell.pair_id for cell in result.dispatches})
    )
    assert result.authoring.randomization_key_persisted_in_bundle is False
    assert result.authoring.randomization_key_reconstruction_prerequisite == (
        "external_secret_bytes_matching_persisted_sha256_required"
    )
    assert result.authoring.regeneration_completeness == (
        "requires_bound_v4_artifact_and_external_randomization_key_bytes"
    )
    assert {
        (cell.judge_family_id, cell.judge_slot, cell.orientation) for cell in result.dispatches
    } == {
        ("openai_codex_sol", "judge_A", "AB"),
        ("openai_codex_sol", "judge_A", "BA"),
        ("anthropic_fable", "judge_B", "AB"),
        ("anthropic_fable", "judge_B", "BA"),
    }
    assert result.spec.primary_eval_family_id == "deepseek_v4"
    assert result.spec.judge_a.model == "openai/gpt-5.6-sol"
    assert result.spec.judge_a.revision == (
        "provider-deployment-snapshot:"
        "225c02955198c5ab9da6f8c0c0b56430c7e9c02c5fe3c28aef02f0bd7ffcdd88"
    )
    assert result.spec.judge_a.decoding["reasoning_effort"] == "xhigh"
    assert result.spec.judge_a.decoding["output_schema_sha256"] == (
        "9de1b73c98a5df344ac158f77ead4b1b6e118b4c2f5585335fd5a3bcf0dea4d4"
    )
    assert result.spec.judge_a.decoding["codex_cli_version"] == "codex-cli 0.144.1"
    assert result.spec.judge_a.decoding["shell_tool_disabled"] is True
    assert result.spec.judge_a.decoding["sandbox"] == "read-only"
    assert result.spec.judge_a.decoding["web_search"] == "disabled"
    assert result.spec.judge_b.model == "anthropic/claude-fable-5"
    assert result.spec.judge_b.revision == (
        "provider-deployment-snapshot:"
        "f913bc9dc00dc187c2f5429e7deddfe604372422dcff7193e881dc7ada8e3ce4"
    )
    assert result.spec.judge_b.decoding["effort"] == "max"
    assert result.spec.judge_b.decoding["output_schema_sha256"] == (
        "f043ad37e1dc98d8df5655f42aeb9140371bd69671f13fd3e9af07111014e1be"
    )
    assert result.spec.judge_b.decoding["safe_mode"] is True
    assert result.spec.judge_b.decoding["tools_disabled"] is True
    assert result.spec.judge_b.decoding["session_persistence"] is False
    assert not result.authoring.semantic_labels_created
    assert not result.authoring.training_eligible


def test_candidate_manifest_uses_truthful_seed_and_authoring_binds_final_spec(
    tmp_path: Path,
) -> None:
    result = _prepare(tmp_path)
    candidate_manifest = json.loads(
        (result.spec_path.parent / "inputs/candidate_manifest.json").read_text()
    )

    assert candidate_manifest["schema_version"] == 4
    assert candidate_manifest["method_version"] == ("lf022_supervision_candidate_inventory_v4")
    assert "spec_sha256" not in candidate_manifest
    assert candidate_manifest["selection_spec_seed_sha256"] == (
        result.authoring.candidate_manifest_selection_spec_seed_sha256
    )
    assert result.authoring.weak_batch_spec_sha256 == hash_file(result.spec_path)
    assert candidate_manifest["selection_spec_seed_sha256"] != hash_file(result.spec_path)


def test_selected_v3_bytes_and_source_hashes_are_preserved(tmp_path: Path) -> None:
    _require_v4()
    source_manifest = V4_ROOT / "inputs/qwen_snapshot1019/manifest.json"
    source_records = V4_ROOT / "inputs/qwen_snapshot1019/candidates.jsonl"
    before = {
        source_manifest: hash_file(source_manifest),
        source_records: hash_file(source_records),
    }

    result = _prepare(tmp_path)

    after = {path: hash_file(path) for path in before}
    assert before == after
    selected_bytes = (result.spec_path.parent / "inputs/candidates.jsonl").read_bytes()
    assert selected_bytes in source_records.read_bytes().splitlines(keepends=True)
    assert result.authoring.selected_source_bytes_preserved


def test_authoring_is_an_exact_immutable_replay(tmp_path: Path) -> None:
    first = _prepare(tmp_path)
    second = _prepare(tmp_path)

    assert second.authoring == first.authoring
    assert second.spec == first.spec
    assert second.dispatch_manifest == first.dispatch_manifest
    assert second.dispatches == first.dispatches
    assert hash_file(second.authoring_path) == hash_file(first.authoring_path)


def test_live_authorization_is_explicit_and_round_trips(tmp_path: Path) -> None:
    _require_v4()
    result = prepare_lf022_sol_fable_batch_v1(
        repo_root=REPO_ROOT,
        v4_root=V4_ROOT,
        source_partition_id="qwen_snapshot1019",
        offset_pairs=0,
        limit_pairs=1,
        randomization_key=KEY,
        output_dir=tmp_path / "authorized",
        historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        authorize_live_provider_calls=True,
    )
    assert result.spec.execution_authorization == ("live_provider_calls_explicitly_authorized")
    assert result.spec.live_provider_calls_authorized
    assert result.dispatch_manifest.live_provider_calls_authorized
    assert result.authoring.execution_authorization == ("live_provider_calls_explicitly_authorized")
    assert result.authoring.live_provider_calls_authorized


@pytest.mark.parametrize("limit", [0, 65])
def test_pair_cap_is_enforced(tmp_path: Path, limit: int) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="limit_pairs"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="kimi_prefix256",
            offset_pairs=0,
            limit_pairs=limit,
            randomization_key=KEY,
            output_dir=tmp_path / "prepared",
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )


def test_offset_overflow_is_rejected(tmp_path: Path) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="exceeds distinct theorem-lineage"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="qwen_snapshot1019",
            offset_pairs=409,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=tmp_path / "prepared",
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )


def test_incomplete_historical_sol_corpus_set_is_rejected(tmp_path: Path) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="exhaustive reviewed corpus set"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="kimi_prefix256",
            offset_pairs=0,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=tmp_path / "missing",
            historical_sol_xhigh_summary_paths=(GENERAL_SOL_XHIGH_SUMMARY,),
        )


def test_reused_pair_is_rejected_by_freshness_validation() -> None:
    _require_v4()
    verified = sol_fable.verify_lf022_judge_design_v4(V4_ROOT)
    historical_pair_ids = {
        json.loads(line)["pair_id"]
        for summary_path in HISTORICAL_SOL_XHIGH_SUMMARIES
        for line in Path(json.loads(summary_path.read_text())["findings_artifact"])
        .read_text()
        .splitlines()
    }
    reused = next(record for record in verified.records if record.pair_id in historical_pair_ids)
    with pytest.raises(LF022SolFableBatchError, match="already has a historical Sol/xhigh"):
        sol_fable._validate_selected_fresh((reused,), tuple(sorted(historical_pair_ids)))


def test_kimi_partition_has_no_fresh_pair(tmp_path: Path) -> None:
    _require_v4()
    with pytest.raises(LF022SolFableBatchError, match="exceeds distinct theorem-lineage"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="kimi_prefix256",
            offset_pairs=0,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=tmp_path / "kimi-exhausted",
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )


def test_qwen_partition_remains_homogeneous(tmp_path: Path) -> None:
    result = _prepare(tmp_path, partition="qwen_snapshot1019")
    assert result.authoring.proposer_family_id == "qwen3"
    assert {cell.proposer_family_id for cell in result.dispatches} == {"qwen3"}


def test_eight_pair_selection_has_distinct_theorem_lineages(tmp_path: Path) -> None:
    _require_v4()
    partition = "qwen_snapshot1019"
    result = prepare_lf022_sol_fable_batch_v1(
        repo_root=REPO_ROOT,
        v4_root=V4_ROOT,
        source_partition_id=partition,
        offset_pairs=0,
        limit_pairs=8,
        randomization_key=KEY,
        output_dir=tmp_path / partition,
        historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
    )
    candidates = tuple(
        LF022SupervisionCandidateRecord.model_validate_json(line)
        for line in (result.spec_path.parent / "inputs/candidates.jsonl").read_bytes().splitlines()
    )
    theorem_ids = {
        lineage
        for candidate in candidates
        for lineage in candidate.pair.source_record_ids
        if lineage.startswith("thm:")
    }
    assert len(theorem_ids) == 8
    assert result.authoring.unique_source_theorem_lineage_count == 8


def test_symlinked_output_is_rejected(tmp_path: Path) -> None:
    _require_v4()
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(LF022SolFableBatchError, match="symlink"):
        prepare_lf022_sol_fable_batch_v1(
            repo_root=REPO_ROOT,
            v4_root=V4_ROOT,
            source_partition_id="kimi_prefix256",
            offset_pairs=0,
            limit_pairs=1,
            randomization_key=KEY,
            output_dir=linked,
            historical_sol_xhigh_summary_paths=HISTORICAL_SOL_XHIGH_SUMMARIES,
        )
