from __future__ import annotations

import json
from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.config.paths import find_repo_root

_REPO_ROOT = find_repo_root(Path(__file__).parent)


def test_32b_placement_is_matched_hash_pinned_and_refuses_local_gpu() -> None:
    path = _REPO_ROOT / "configs/sft2b/reform_32b_placement_v1.json"
    config = json.loads(path.read_text(encoding="utf-8"))

    assert config["status"] == "waiting_compute"
    assert config["model_revision"] == "80e9d9d83998d8c118c512bd6a35d1cdf11b57c8"
    assert config["repository_bytes"] == sum(item["size"] for item in config["remote_files"])
    assert len(config["remote_files"]) == 26
    assert config["hardware"]["minimum_vram_bytes"] == 80_000_000_000
    assert config["hardware"]["local_rtx_4090_forbidden"] is True
    assert config["source_config_sha256"] == hash_file(_REPO_ROOT / config["source_config_path"])
    assert config["prompt_sha256"] == hash_file(_REPO_ROOT / config["prompt_path"])
    assert [item["seed"] for item in config["candidate_slots"]] == [0, 1, 2, 3]
