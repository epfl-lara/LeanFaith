"""Offline integrity tests for the exact 919-pair Sol/Fable v4 freeze."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_config
from leanfaith.generation.lf022_judge_design_rebind_v4 import (
    LF022JudgeDesignRebindError,
    LF022JudgeDesignRebindSpecV4,
    freeze_lf022_judge_design_v4,
    verify_lf022_judge_design_v4,
)

CONFIG = Path("configs/generation/lf022_sol_fable_public_rebind_v4.yaml")


def _source_paths() -> tuple[Path, ...]:
    spec = load_config(CONFIG, LF022JudgeDesignRebindSpecV4).config
    return tuple(
        Path(binding.path)
        for partition in spec.source_partitions
        for binding in (partition.manifest, partition.records)
    )


def _require_sources() -> None:
    missing = [path for path in _source_paths() if not path.is_file()]
    if missing:
        pytest.skip(f"fixed local LF-022 source artifacts unavailable: {missing}")


def test_v4_config_freezes_exact_distinct_family_roles() -> None:
    spec = load_config(CONFIG, LF022JudgeDesignRebindSpecV4).config
    assert spec.expected_record_count == 919
    assert spec.expected_proposer_counts == {"moonshot_kimi_k2": 201, "qwen3": 718}
    assert spec.judge_a.model == "gpt-5.6-sol"
    assert spec.judge_a.effort == "xhigh"
    assert spec.judge_b.model == "claude-fable-5"
    assert spec.judge_b.effort == "max"
    assert spec.primary_eval.family_id == "deepseek_v4"
    assert (
        len(
            {
                "moonshot_kimi_k2",
                spec.judge_a.family_id,
                spec.judge_b.family_id,
                spec.primary_eval.family_id,
            }
        )
        == 4
    )
    assert (
        len(
            {
                "qwen3",
                spec.judge_a.family_id,
                spec.judge_b.family_id,
                spec.primary_eval.family_id,
            }
        )
        == 4
    )


def test_exact_919_freeze_replays_and_preserves_v3_bytes(tmp_path: Path) -> None:
    _require_sources()
    before = {path: hash_file(path) for path in _source_paths()}
    output = tmp_path / "v4"
    frozen = freeze_lf022_judge_design_v4(config_path=CONFIG, output_dir=output)
    replayed = verify_lf022_judge_design_v4(output)
    after = {path: hash_file(path) for path in _source_paths()}

    assert before == after
    assert frozen.manifest == replayed.manifest
    assert frozen.records == replayed.records
    assert len(frozen.records) == 919
    assert len({item.pair_id for item in frozen.records}) == 919
    assert len({item.judge_visible_payload_sha256 for item in frozen.records}) == 919
    assert {item.proposer_family_id for item in frozen.records} == {
        "moonshot_kimi_k2",
        "qwen3",
    }
    assert all(item.primary_eval_family_id == "deepseek_v4" for item in frozen.records)
    assert all(not item.historical_codex_diagnostic_weak_vote for item in frozen.records)
    assert all(not item.semantic_labels_created for item in frozen.records)
    assert all(not item.training_eligible for item in frozen.records)
    for source in frozen.manifest.source_partitions:
        assert hash_file(output / source.source_manifest_artifact) == source.source_manifest_sha256
        assert hash_file(output / source.source_records_artifact) == source.source_records_sha256

    # An identical second freeze is an immutable replay, not a mutation.
    second = freeze_lf022_judge_design_v4(config_path=CONFIG, output_dir=output)
    assert second.manifest == frozen.manifest
    assert second.records == frozen.records


def test_v4_replay_rejects_tampered_source_copy(tmp_path: Path) -> None:
    _require_sources()
    output = tmp_path / "v4"
    frozen = freeze_lf022_judge_design_v4(config_path=CONFIG, output_dir=output)
    source = frozen.manifest.source_partitions[0]
    copied = output / source.source_records_artifact
    copied.write_bytes(copied.read_bytes() + b"\n")
    with pytest.raises(LF022JudgeDesignRebindError, match="source copy differs"):
        verify_lf022_judge_design_v4(output)


def test_v4_freeze_rejects_symlinked_output(tmp_path: Path) -> None:
    _require_sources()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(LF022JudgeDesignRebindError, match="symlink"):
        freeze_lf022_judge_design_v4(config_path=CONFIG, output_dir=link)
