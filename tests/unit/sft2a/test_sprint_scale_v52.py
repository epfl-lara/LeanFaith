"""Tests for the post-pilot scale path: pool config loading, zero-Lean pool preparation with
exclusions, shard freezing with chained provider configs, role-aware shard thresholds, and the
automatic chain decisions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a import sprint_pilot_v52, sprint_scale_v52
from leanfaith.sft2a.mechanisms import structured_signature_shape
from leanfaith.sft2a.models import ExecutionCeilings
from leanfaith.sft2a.provider_rehearsal_v52 import (
    LoadedProviderRehearsalV52,
    ProviderRehearsalV52Error,
    load_provider_rehearsal_v52,
)
from leanfaith.sft2a.sprint_pilot_v52 import chain_decision, evaluate_sprint_pilot_thresholds
from leanfaith.sft2a.sprint_scale_v52 import (
    SprintScaleError,
    freeze_sprint_shards,
    load_sprint_pool_config,
    prepare_sprint_reference_pool,
)

_POOL_CONFIG = Path("configs/sft2a/sprint_reference_pool_12k_v1.json")
_PILOT_CONFIG = Path("configs/sft2a/sprint_pilot_20roots_v1.json")
_SOURCES = ("mathlib", "physlib", "cslib", "compiler_data")


def _census_row(source: str, index: int, *, signature: str | None = None) -> dict[str, object]:
    root_id = f"{source}:census:{index:06d}"
    return {
        "root_id": root_id,
        "source": source,
        "source_revision": "a" * 40,
        "source_license": "Apache-2.0",
        "declaration_name": f"Decl{index}",
        "reference_signature": signature
        or f"∀ (a b c : ℕ), a ≤ b → b ≤ c → a + {index} ≤ c + {index}",
        "compile_context": {
            "project_id": "mathlib" if source in {"mathlib", "compiler_data"} else source,
            "project_revision": "a" * 40,
            "lean_version": "v4.31.0-rc1",
            "project_dir": "/tmp/project",
            "import_header": "import Mathlib",
            "command_preamble": "",
            "namespace_context": [],
            "open_context": [],
            "scoped_context": [],
            "options": {},
            "environment_schema_version": 1,
            "leaninteract_version": "0.11.4",
            "repl_revision": "v1.3.17",
            "memory_hard_limit_mb": 24576,
            "synchronous_elaboration": True,
            "workers": 1,
        },
        "source_locator": f"{source}/File.lean:{index}",
        "source_header": f"theorem Decl{index} : True :=",
        "source_header_sha256": hash_canonical({"header": index}),
        "domain": "algebra" if index % 2 else "order",
        "shape_id": f"shape-{index % 3}",
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))
    return path


def _pool_fixture(tmp_path: Path, *, per_source: int = 12) -> Any:
    real = load_sprint_pool_config(_POOL_CONFIG)
    census_root = tmp_path / "census"
    rows = [_census_row(source, index) for source in _SOURCES for index in range(per_source)]
    _write_jsonl(census_root / "eligible_roots.jsonl", rows)
    exclusion = _write_jsonl(
        tmp_path / "used.jsonl",
        [
            {
                "root": {"root_id": "mathlib:census:000000", "source": "mathlib"},
                "certified_reference": {
                    "closed_expr_hash": "x" * 64,
                    "rendered_goal_hash": "y" * 64,
                },
            }
        ],
    )
    document = {
        **real.document,
        "census_root": str(census_root),
        "census_eligible_sha256": hash_file(census_root / "eligible_roots.jsonl"),
        "exclusion_sample_paths": [str(exclusion)],
        "allocations": {"mathlib": 6, "physlib": 4, "cslib": 40, "compiler_data": 2},
        "output_root": str(tmp_path / "pool_run"),
        "shards": {
            **cast(dict[str, object], real.document["shards"]),
            "count": 2,
            "roots_per_shard": 5,
            "output_root": str(tmp_path / "shards"),
        },
    }
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(document))
    return load_sprint_pool_config(path)


def test_pool_config_loads_with_real_pins_and_rejects_bad_contracts(tmp_path: Path) -> None:
    loaded = load_sprint_pool_config(_POOL_CONFIG)
    assert loaded.allocations == {
        "mathlib": 7500,
        "physlib": 3000,
        "cslib": 1400,
        "compiler_data": 2000,
    }
    assert loaded.document["lean_workers"] == 2 and loaded.document["provider_calls_allowed"] == 0
    assert len(loaded.exclusion_sample_paths) == 3
    shards = cast(dict[str, object], loaded.document["shards"])
    assert shards["count"] == 10 and shards["roots_per_shard"] == 1000
    document = json.loads(_POOL_CONFIG.read_text())
    for update, message in (
        ({"lean_workers": 1}, "two workers/40 GiB"),
        ({"census_eligible_sha256": "0" * 64}, "hash differs"),
        ({"allocations": {"mathlib": 1}}, "four sources"),
    ):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({**document, **update}))
        with pytest.raises(SprintScaleError, match=message):
            load_sprint_pool_config(path)


def test_pool_preparation_excludes_used_roots_and_caps_by_availability(tmp_path: Path) -> None:
    loaded = _pool_fixture(tmp_path)
    manifest = prepare_sprint_reference_pool(loaded)
    assert manifest["excluded_used_roots"] == 1
    assert manifest["source_counts"] == {
        "compiler_data": 2,
        "cslib": 12,
        "mathlib": 6,
        "physlib": 4,
    }
    assert manifest["available_after_screens"]["mathlib"] == 11
    assert manifest["rejected"]["already_used_root"] == 1
    assert manifest["lean_requests_executed"] == 0 and manifest["provider_calls_executed"] == 0
    rows = [
        json.loads(line) for line in (loaded.output_root / "pool.jsonl").read_text().splitlines()
    ]
    assert all(row["root_id"] != "mathlib:census:000000" for row in rows)
    assert all(row["pool_phase"] == "sprint_12k" for row in rows)
    assert prepare_sprint_reference_pool(loaded) == manifest


def _fake_result(loaded: Any, row: dict[str, object], *, valid: bool = True) -> None:
    result_path = sprint_scale_v52._result_path(loaded.output_root, row)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path = loaded.output_root / "cache" / f"{row['root_id']}.json".replace(":", "_")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"cache_key": row["root_id"]}))
    goal = f"a b c : ℕ\n⊢ a ≤ b → b ≤ c → a + {row['root_id'][-3:]} ≤ c"
    certification = {
        "status": "valid" if valid else "invalid",
        "taxonomy": "valid" if valid else "term_elaboration_invalid",
        "route": "loaded_constant_type",
        "cache_key": f"key-{row['root_id']}",
        "cache_path": str(cache_path),
        "goal_v1": goal,
        "closed_expr_hash": hash_canonical({"expr": row["root_id"]}),
        "rendered_goal_hash": hash_canonical({"goal": row["root_id"]}),
        "sidecar_hash": hash_canonical({"sidecar": row["root_id"]}),
        "compile_context_id": "ctx:test",
    }
    result_path.write_text(
        json.dumps(
            {"root_id": row["root_id"], "source": row["source"], "certification": certification}
        )
    )


def test_freeze_shards_builds_disjoint_samples_and_chained_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _pool_fixture(tmp_path)
    prepare_sprint_reference_pool(loaded)
    rows = [
        json.loads(line) for line in (loaded.output_root / "pool.jsonl").read_text().splitlines()
    ]
    for index, row in enumerate(rows):
        _fake_result(loaded, row, valid=index != 3)
    shape = structured_signature_shape(
        "a b c : ℕ\n⊢ a ≤ b → b ≤ c → a ≤ c",
        {"k": "app", "fn": {"k": "const", "name": "LE.le"}, "arg": {"k": "bvar", "index": 0}},
    )
    from leanfaith.sft2a.certified_sample_v52 import CorrectedSampleError

    def verify(row: dict[str, object]) -> dict[str, object]:
        if str(cast(dict[str, object], row["root"])["root_id"]).endswith("000005"):
            raise CorrectedSampleError("certification rendered goal differs from raw Expr payload")
        return {"ok": True}

    monkeypatch.setattr(sprint_scale_v52, "verify_certified_reference_row", verify)
    monkeypatch.setattr(sprint_scale_v52, "certified_shape", lambda certified: (shape, "h" * 64))
    manifest = freeze_sprint_shards(loaded)
    assert manifest["shard_count"] == 2 and manifest["roots_per_shard"] == 5
    # Both mathlib:...000005 and cslib:...000005 are refused by the mocked verifier.
    assert manifest["screen_rejections"] == {
        "certificate_verification_failed": 2,
        "term_elaboration_invalid": 1,
    }
    assert manifest["certified_usable_roots"] == 21
    failures = [
        json.loads(line)
        for line in (loaded.shard_root / "certificate_verification_failures.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(failures) == 2 and all(row["root_id"].endswith("000005") for row in failures)
    shards = cast(list[dict[str, object]], manifest["shards"])
    ids: list[str] = []
    for receipt in shards:
        sample = [
            json.loads(line) for line in Path(str(receipt["sample_path"])).read_text().splitlines()
        ]
        assert len(sample) == 5
        assert all(
            set(row["mechanism_plan"]) == {"preserve_0", "preserve_1", "break_0", "break_1"}
            for row in sample
        )
        ids.extend(str(row["root"]["root_id"]) for row in sample)
        config = json.loads(Path(str(receipt["provider_config_path"])).read_text())
        assert config["sprint_role"] == "shard"
        assert config["provider_concurrency"] == 16 and config["fallback_provider_concurrency"] == 8
        assert config["controlled_stop_after_completed_roots"] == 0
        assert config["ceilings"]["maximum_roots"] == 5
        assert config["expected_source_mix"] == receipt["source_mix"]
        assert config["scale_10k_authorized"] is False
        assert (
            config["oracle_v2_gate_receipt_path"] == loaded.document["oracle_v2_gate_receipt_path"]
        )
    assert len(ids) == len(set(ids)) == 10
    first = json.loads(Path(str(shards[0]["provider_config_path"])).read_text())
    assert first["next_shard_config_path"] == shards[1]["provider_config_path"]
    assert (
        json.loads(Path(str(shards[1]["provider_config_path"])).read_text())[
            "next_shard_config_path"
        ]
        is None
    )
    loaded_shard = load_provider_rehearsal_v52(Path(str(shards[0]["provider_config_path"])))
    assert loaded_shard.kind == "sprint"
    assert loaded_shard.document["sprint_role"] == "shard"
    assert freeze_sprint_shards(loaded) == manifest


def test_shard_config_validation_requires_shard_fields(tmp_path: Path) -> None:
    document = json.loads(_PILOT_CONFIG.read_text())
    path = tmp_path / "shard.json"
    path.write_text(json.dumps({**document, "sprint_role": "shard"}))
    with pytest.raises(ProviderRehearsalV52Error, match="kimi_audit_fraction"):
        load_provider_rehearsal_v52(path)
    path.write_text(
        json.dumps(
            {
                **document,
                "sprint_role": "shard",
                "kimi_audit_fraction": 0.1,
                "kimi_audit_rows_maximum": 8,
                "fallback_provider_concurrency": 4,
                "minimum_accepted_rows_per_minute": 8.0,
                "next_shard_config_path": str(tmp_path / "missing.json"),
            }
        )
    )
    with pytest.raises(ProviderRehearsalV52Error, match="next_shard_config_path"):
        load_provider_rehearsal_v52(path)
    path.write_text(json.dumps({**document, "expected_source_mix": {"mathlib": 1}}))
    with pytest.raises(ProviderRehearsalV52Error, match="expected_source_mix"):
        load_provider_rehearsal_v52(path)
    path.write_text(
        json.dumps({**document, "next_stage_config_path": "configs/sft2a/missing.json"})
    )
    with pytest.raises(ProviderRehearsalV52Error, match="next_stage_config_path"):
        load_provider_rehearsal_v52(path)


def _loaded_for_role(tmp_path: Path, role: str, **extra: object) -> LoadedProviderRehearsalV52:
    sample = tmp_path / "sample.jsonl"
    sample.write_bytes(
        b"".join(
            canonical_json_bytes({"root": {"root_id": f"mathlib:t:{i}"}}) + b"\n" for i in range(20)
        )
    )
    return LoadedProviderRehearsalV52(
        path=tmp_path / "c.json",
        document={"sprint_role": role, **extra},
        sha256="d" * 64,
        base=cast(
            Any,
            SimpleNamespace(config=SimpleNamespace(staging_root=str(tmp_path)), repo_root=tmp_path),
        ),
        sample_path=sample,
        output_root=tmp_path / "run",
        ceilings=ExecutionCeilings.model_validate(
            {
                "maximum_roots": 20,
                "maximum_provider_calls": 10,
                "maximum_proposer_calls": 5,
                "maximum_opus_calls": 5,
                "maximum_lemex_calls": 0,
                "maximum_attempts_per_slot": 3,
                "maximum_reported_opus_spend_usd": 1.0,
                "codex_cost_status": "unavailable",
                "lemex_cost_status": "unavailable",
            }
        ),
        recovery_source=None,
        kind="sprint",
    )


def test_shard_thresholds_use_throughput_instead_of_wall_bound(tmp_path: Path) -> None:
    loaded = _loaded_for_role(tmp_path, "shard")
    common: dict[str, Any] = {
        "compaction": {"accepted_rows": 60, "self_pairs": 0, "candidate_duplicates": 0},
        "replay": {"provider_calls_executed": 0, "lean_requests_executed": 0, "reproducible": True},
        "malformed_injection": {"passed": True},
        "resume_check": {
            "manifests_unchanged": True,
            "provider_calls_for_completed_roots_after_resume": 0,
            "lean_requests_for_completed_roots_after_resume": 0,
        },
        "root_manifests": [
            {
                "counts": {"lean_invalid_attempts": 0, "candidate_attempts": 4},
                "lean": {"candidate_requests": 4},
            }
        ]
        * 20,
        "infrastructure": {"infrastructure_failure_rate": 0.0},
    }
    slow = evaluate_sprint_pilot_thresholds(
        loaded, generation_wall_seconds=3600.0, role="shard", **common
    )
    assert slow["role"] == "shard"
    assert slow["accepted_rows_per_minute"] == 1.0
    assert slow["passed"] is False
    assert slow["failed_checks"] == ["accepted_throughput_at_least_minimum"]
    assert "wall_time_at_most_30min" not in cast(dict[str, bool], slow["checks"])
    fast = evaluate_sprint_pilot_thresholds(
        loaded, generation_wall_seconds=300.0, role="shard", **common
    )
    assert fast["accepted_rows_per_minute"] == 12.0
    assert fast["passed"] is True
    pilot = evaluate_sprint_pilot_thresholds(loaded, generation_wall_seconds=3600.0, **common)
    assert pilot["role"] == "pilot"
    assert pilot["failed_checks"] == ["wall_time_at_most_30min"]


def test_chain_decision_covers_pilot_and_shard_outcomes(tmp_path: Path) -> None:
    pilot = _loaded_for_role(
        tmp_path, "pilot", next_stage_config_path="configs/sft2a/sprint_reference_pool_12k_v1.json"
    )
    assert chain_decision(
        pilot, terminal={"status": "complete"}, evaluation={"failed_checks": []}
    ) == {
        "action": "launch_pool_certification",
        "target": "configs/sft2a/sprint_reference_pool_12k_v1.json",
        "reason": "pilot_passed",
    }
    stopped = chain_decision(
        pilot,
        terminal={"status": "threshold_failed"},
        evaluation={"failed_checks": ["accepted_at_least_70pct"]},
    )
    assert stopped["action"] == "stop" and stopped["failed_checks"] == ["accepted_at_least_70pct"]
    shard = _loaded_for_role(
        tmp_path,
        "shard",
        next_shard_config_path=str(tmp_path / "next.json"),
        fallback_provider_concurrency=8,
    )
    assert (
        chain_decision(shard, terminal={"status": "complete"}, evaluation={"failed_checks": []})[
            "reason"
        ]
        == "shard_passed"
    )
    throttled = chain_decision(
        shard,
        terminal={"status": "threshold_failed"},
        evaluation={
            "failed_checks": ["infrastructure_failures_below_2pct"],
            "effective_provider_concurrency": 16,
        },
    )
    assert throttled["action"] == "launch_next_shard"
    assert throttled["reason"] == "throttling_fallback"
    assert throttled["provider_concurrency_override"] == 8
    repeated = chain_decision(
        shard,
        terminal={"status": "threshold_failed"},
        evaluation={
            "failed_checks": ["infrastructure_failures_below_2pct"],
            "effective_provider_concurrency": 8,
        },
    )
    assert repeated == {"action": "stop", "reason": "repeated_infrastructure_fault_at_fallback"}
    other = chain_decision(
        shard,
        terminal={"status": "threshold_failed"},
        evaluation={"failed_checks": ["lean_invalid_below_25pct"]},
    )
    assert other["action"] == "stop" and other["reason"] == "shard_threshold_failed"
    last = _loaded_for_role(tmp_path, "shard")
    assert (
        chain_decision(last, terminal={"status": "complete"}, evaluation={"failed_checks": []})[
            "reason"
        ]
        == "last_shard"
    )


def test_effective_concurrency_honours_durable_override(tmp_path: Path) -> None:
    loaded = _loaded_for_role(tmp_path, "shard", provider_concurrency=16)
    detached = tmp_path / "run/detached"
    assert sprint_pilot_v52._effective_concurrency(loaded, detached) == 16
    detached.mkdir(parents=True)
    (detached / "concurrency_override.json").write_text(json.dumps({"provider_concurrency": 8}))
    assert sprint_pilot_v52._effective_concurrency(loaded, detached) == 8


def test_read_only_snapshots_tolerate_many_in_flight_roots(tmp_path: Path) -> None:
    from leanfaith.sft2a.parallel_rehearsal import ParallelRootStateMachine

    loaded = _loaded_for_role(tmp_path, "pilot")
    states = ParallelRootStateMachine(loaded.output_root / "root_state.jsonl", maximum_workers=8)
    for index in range(8):
        states.claim(root_id=f"mathlib:t:{index}", worker_id=f"dyn-{index}")
    states.complete(root_id="mathlib:t:0", worker_id="dyn-0", manifest_hash="a" * 64)
    # Seven roots remain in flight; read-side helpers must not enforce the two-worker default.
    assert sprint_pilot_v52._completed_root_ids(loaded) == ["mathlib:t:0"]
    with pytest.raises(Exception, match="ceiling exceeded"):
        ParallelRootStateMachine(loaded.output_root / "root_state.jsonl").snapshot()
