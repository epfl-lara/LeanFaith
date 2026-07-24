"""Gate-2 sampling is deterministic and uses only pre-extraction fields."""

from __future__ import annotations

import json
from pathlib import Path

from datasets import Dataset

from leanfaith.sources.gate2_sampling import sample_gate2_arrow_shards, sample_gate2_jsonl


def test_gate2_sample_is_exact_replay_and_preserves_source_indices(tmp_path: Path) -> None:
    source = tmp_path / "all.jsonl"
    rows = [
        {
            "uuid": "dup" if index in (3, 7) else f"u{index}",
            "data_source": "a" if index % 2 else "b",
            "question": "/-- nl -/" if index % 3 else "no doc",
            "valid": index % 2 == 0,
            "token_count": index * 100,
            "tactic_count": index % 8,
        }
        for index in range(40)
    ]
    source.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    first = tmp_path / "first.jsonl"
    first_manifest = tmp_path / "first.json"
    second = tmp_path / "second.jsonl"
    second_manifest = tmp_path / "second.json"
    kwargs = {
        "input_path": source,
        "dataset_id": "owner/data",
        "revision": "a" * 40,
        "split": "train",
        "sample_size": 20,
    }
    sample_gate2_jsonl(output_path=first, manifest_path=first_manifest, **kwargs)
    sample_gate2_jsonl(output_path=second, manifest_path=second_manifest, **kwargs)
    assert first.read_bytes() == second.read_bytes()
    sampled = [json.loads(line) for line in first.read_text().splitlines()]
    assert len(sampled) == 20
    assert len({item["source_row_index"] for item in sampled}) == 20
    assert all(item["row"] == rows[item["source_row_index"]] for item in sampled)
    manifest = json.loads(first_manifest.read_text())
    assert sum(item["quota"] for item in manifest["strata"].values()) == 20
    assert manifest["schema_version"] == 2
    assert manifest["input_partitions"][0]["sha256"]


def test_gate2_arrow_shards_preserve_global_indices_and_validate_population(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "uuid": f"u{index}",
            "data_source": "source",
            "question": "/-- nl -/\ntheorem t : True := by trivial",
            "valid": "true" if index % 2 else "false",
            "token_count": index * 10,
            "tactic_count": index % 4,
        }
        for index in range(12)
    ]
    paths: list[Path] = []
    for shard_index, shard_rows in enumerate((rows[:5], rows[5:])):
        saved = tmp_path / f"saved-{shard_index}"
        Dataset.from_list(shard_rows).save_to_disk(saved, max_shard_size="1GB")
        arrow = next(saved.glob("*.arrow"))
        target = tmp_path / f"data-train-{shard_index:05d}.arrow"
        arrow.rename(target)
        paths.append(target)

    output = tmp_path / "sample.jsonl"
    manifest_path = tmp_path / "sample.json"
    sample_gate2_arrow_shards(
        arrow_paths=paths,
        output_path=output,
        manifest_path=manifest_path,
        dataset_id="owner/data",
        revision="b" * 40,
        split="train",
        sample_size=8,
        expected_population_rows=12,
    )
    sampled = [json.loads(line) for line in output.read_text().splitlines()]
    assert all(item["row"] == rows[item["source_row_index"]] for item in sampled)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["population_rows"] == 12
    assert [item["rows"] for item in manifest["input_partitions"]] == [5, 7]
