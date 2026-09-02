from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import leanfaith.sft2b.scale_sprint_v3 as scale
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.paths import find_repo_root

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_CONFIG = _REPO_ROOT / "configs/sft2b/reform_diverse_core_scale_sprint_v3.json"
_REAL_BUNDLE = Path(
    "/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/source_inputs/"
    "reform_diverse_full_v3_mechanical_conservative_v1"
)


def _spec() -> scale.ScaleSprintV3Spec:
    return scale.ScaleSprintV3Spec.model_validate(json.loads(_CONFIG.read_text(encoding="utf-8")))


def test_checked_config_is_exact_core_only_activation() -> None:
    spec, _ = scale.load_spec(_REPO_ROOT, _CONFIG)

    assert spec.authorization.baseline_git_revision == ("0f7f2d352e75d73018af85d9e50085108d357b81")
    assert spec.authorization.core_enabled
    assert not spec.authorization.tail_enabled
    assert spec.input.revision == "a9b2d76d0f6c12e87c86434b6ad3744d13c50fee"
    assert spec.input.path_prefix.endswith("v3_mechanical_conservative_v1")
    assert spec.input.files["SHA256SUMS"] == (
        "adadb46402c72fb208e04278b324fda3904736096dcfdb0aa5835d3adab6d2ed"
    )
    assert tuple(item.shard_id for item in spec.shards) == scale.SHARD_IDS
    assert tuple((item.start, item.stop) for item in spec.shards) == scale.EXPECTED_SLICES
    assert all(item.expected_sources == 12500 for item in spec.shards)
    assert spec.model.revision == "80e9d9d83998d8c118c512bd6a35d1cdf11b57c8"
    assert spec.model.max_model_len == 5063
    assert spec.model.checkpoint_dtype == "bfloat16"
    assert spec.model.visible_devices == tuple(range(8))
    assert (spec.model.data_parallel_size, spec.model.tensor_parallel_size) == (4, 2)
    assert spec.model.concurrency == 64
    assert spec.model.max_num_seqs == 16
    assert spec.runtime.vllm_version == "0.12.0"
    assert not spec.evidence.waived_historical_receipts_resurrected
    assert spec.downstream.invalid_candidates_are_semantic_negatives is False


@pytest.mark.skipif(not _REAL_BUNDLE.is_dir(), reason="private mechanical-v3 bundle is not mounted")
def test_real_bundle_freezes_four_disjoint_views_and_exact_core_concat() -> None:
    spec, _ = scale.load_spec(_REPO_ROOT, _CONFIG)
    verified = scale.verify_source_bundle(spec, bundle_root=_REAL_BUNDLE)

    assert len(verified.rows) == 54144
    assert len(verified.core_ids) == 50000
    assert len(verified.tail_ids) == 4144
    concatenated = tuple(source_id for view in verified.views for source_id in view.source_ids)
    assert concatenated == verified.core_ids
    assert len(set(concatenated)) == 50000
    assert [scale.hashlib.sha256(item.payload).hexdigest() for item in verified.views] == [
        item.spec.artifact_sha256 for item in verified.views
    ]


def test_plan_is_exact_product_and_config_hash_is_provenance_only() -> None:
    spec = _spec()
    ids = tuple(f"sft2b_source:{index:064x}" for index in range(12500))
    shard = scale.VerifiedShardView(spec=spec.shards[0], source_ids=ids, payload=b"fixture\n")

    first = scale._build_plan(spec=spec, config_sha256="1" * 64, shard=shard)
    second = scale._build_plan(spec=spec, config_sha256="2" * 64, shard=shard)

    assert first.run_id == second.run_id
    assert first.cells == second.cells
    assert len(first.cells) == 50000
    assert len({item.cell_id for item in first.cells}) == 50000
    assert tuple(item.source_id for item in first.cells[:4]) == (ids[0],) * 4
    assert tuple(item.slot.value for item in first.cells[:4]) == (
        "slot_0",
        "slot_1",
        "slot_2",
        "slot_3",
    )
    assert tuple(item.seed for item in first.cells[:4]) == (0, 1, 2, 3)


def test_view_artifact_hash_binds_slice_and_core_hash() -> None:
    spec = _spec()
    ids = tuple(f"sft2b_source:{index:064x}" for index in range(50000))
    shard = spec.shards[0]
    payload = scale._view_payload(shard, ids, spec.input.files["matched_50000_source_ids.json"])
    expected_ids = ids[shard.start : shard.stop]
    modified = shard.model_copy(
        update={
            "source_ids_sha256": hash_canonical(expected_ids),
            "artifact_sha256": scale.hashlib.sha256(payload).hexdigest(),
        }
    )

    assert hash_canonical(expected_ids) == modified.source_ids_sha256
    assert (
        scale.hashlib.sha256(
            scale._view_payload(modified, ids, spec.input.files["matched_50000_source_ids.json"])
        ).hexdigest()
        == modified.artifact_sha256
    )
    tampered = scale._view_payload(
        modified,
        ids,
        "0" * 64,
    )
    assert scale.hashlib.sha256(tampered).hexdigest() != modified.artifact_sha256


def test_accepted_evidence_opens_current_manifests_without_receipt_requirements(
    tmp_path: Path,
) -> None:
    spec = _spec()
    lean_path = tmp_path / "lean.json"
    judge_path = tmp_path / "judge.json"
    lean_path.write_bytes(
        canonical_json_bytes(
            {
                "gate_passed": True,
                "counts": {"valid_references": 500, "valid_candidates": 865},
            }
        )
        + b"\n"
    )
    judge_path.write_bytes(
        canonical_json_bytes({"gate_passed": True, "counts": {"votes": 300, "unknown": 0}}) + b"\n"
    )
    spec = spec.model_copy(
        update={
            "evidence": spec.evidence.model_copy(
                update={
                    "lean_audit_manifest_sha256": hash_file(lean_path),
                    "judge_manifest_sha256": hash_file(judge_path),
                }
            )
        }
    )

    result = scale.verify_accepted_evidence(
        spec,
        lean_manifest_path=lean_path,
        judge_manifest_path=judge_path,
    )

    assert result["waived_receipts_required"] is False


def test_gpu_inventory_requires_exact_eight_supported_idle_80gb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    lines = "\n".join(
        f"{index}, NVIDIA H100 80GB HBM3, GPU-{index}, 81559, 4" for index in range(8)
    )
    monkeypatch.setattr(scale, "_run_checked", lambda *args, **kwargs: lines)

    inventory = scale._gpu_inventory(spec)

    assert len(inventory) == 8
    bad = lines.replace("NVIDIA H100 80GB HBM3", "NVIDIA RTX 4090", 1)
    monkeypatch.setattr(scale, "_run_checked", lambda *args, **kwargs: bad)
    with pytest.raises(scale.ScaleSprintV3Error, match="supported A100/H100"):
        scale._gpu_inventory(spec)


def test_downstream_queue_is_duplicate_safe_and_preserves_invalid_routing(
    tmp_path: Path,
) -> None:
    spec = _spec().model_copy(
        update={"runtime": _spec().runtime.model_copy(update={"run_root": tmp_path})}
    )
    runtime = SimpleNamespace(
        shard=SimpleNamespace(spec=SimpleNamespace(shard_id="core_00")),
        plan=SimpleNamespace(run_id="sft2b_full_reform_run:" + "1" * 64),
    )
    publication = {"revision": "2" * 40, "remote_prefix": "outputs/core_00"}

    scale.queue_downstream(spec=spec, runtime=runtime, publication=publication)  # type: ignore[arg-type]
    scale.queue_downstream(spec=spec, runtime=runtime, publication=publication)  # type: ignore[arg-type]

    rows = scale._jsonl_objects(tmp_path / "downstream/queue.jsonl")
    assert len(rows) == 1
    assert rows[0]["state"] == "queued_for_separate_resources"
    assert rows[0]["invalid_candidates_are_semantic_negatives"] is False
    assert rows[0]["lean"] == {
        "compile_each_novel_candidate_once": True,
        "maximum_host_rss_gib": 40.0,
        "persistent_workers": 2,
    }
