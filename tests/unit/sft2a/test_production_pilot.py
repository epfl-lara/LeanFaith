from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a.activation import (
    PilotActivationError,
    build_authorized_activation,
    load_pilot_activation,
)
from leanfaith.sft2a.budget import PersistentProviderBudget
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.detached import (
    DetachedPilotError,
    _exclusive_lock,
    launch_detached_pilot,
    preflight_detached_launch,
)
from leanfaith.sft2a.models import SFT2AProductionConfig
from leanfaith.sft2a.pilot import prepare_pilot_sample, verify_pilot_replay
from leanfaith.sft2a.pilot_audit import pilot_audit_indices, run_pilot_lemex_audit
from leanfaith.sft2a.providers import ProviderCallResult
from leanfaith.sft2a.readiness import (
    LoadedPilotReadiness,
    PilotReadinessError,
    load_pilot_readiness,
    require_pilot_authorization,
)


def _judge(verdict: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "verdict": verdict,
        "confidence": "high",
        "relation_class": "logical_restatement" if verdict == "equivalent" else "other",
        "error_type": "none",
        "rationale": "Synthetic blinded pilot audit judgment.",
    }


class FakeAuditor:
    def __init__(
        self,
        root: Path,
        provider_id: str,
        responses: Sequence[dict[str, object]],
    ) -> None:
        self.root = root
        self.provider_id = provider_id
        self.responses = list(responses)
        self.calls = 0

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        del prompt, input_ids
        index = self.calls
        self.calls += 1
        return ProviderCallResult(
            call_key=f"lemex-audit:{index}",
            provider_id=self.provider_id,
            structured=self.responses[index],
            usage={"input_tokens": 10, "output_tokens": 4},
            cost_usd=None,
            elapsed_seconds=0.1,
            cache_hit=False,
            terminal_path=self.root / f"lemex-{index}.json",
        )


def _temporary_production(tmp_path: Path) -> LoadedSFT2AConfig:
    loaded = load_sft2a_config(Path("configs/sft2a/production_pilot_v1.yaml"))
    assert isinstance(loaded.config, SFT2AProductionConfig)
    layout = loaded.config.run_layout.model_copy(update={"shared_cache_root": str(tmp_path)})
    config = loaded.config.model_copy(update={"staging_root": str(tmp_path), "run_layout": layout})
    return replace(
        loaded,
        config=config,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )


def _write_fake_completed_pilot(
    loaded: LoadedSFT2AConfig,
    readiness: LoadedPilotReadiness,
    output: Path,
) -> list[dict[str, object]]:
    sidecars: list[dict[str, object]] = []
    sample_rows: list[dict[str, object]] = []
    root_receipts: list[dict[str, object]] = []
    for root_index in range(12):
        root = loaded.config.root.model_copy(
            update={"root_id": f"mathlib:synthetic:{root_index:02d}"}
        )
        sample_rows.append(
            {
                "root": root.model_dump(mode="json"),
                "context_id": "mathlib_full",
                "source_locator": "synthetic replay test",
            }
        )
        root_config = loaded.config.model_copy(update={"root": root})
        root_dir = output / "roots/mathlib" / hash_canonical(root.root_id)[:16]
        root_dir.mkdir(parents=True)
        root_core: list[dict[str, object]] = []
        root_sidecars: list[dict[str, object]] = []
        for slot_index in range(4):
            preserving = slot_index < 2
            row_id = f"row-{root_index:02d}-{slot_index}"
            goal = f"⊢ Synthetic{root_index}_{slot_index}"
            root_core.append({"reference": "⊢ Reference", "candidate": goal, "label": preserving})
            sidecar = {
                "row_id": row_id,
                "root_id": root.root_id,
                "slot_id": f"slot-{slot_index}",
                "requested_polarity": "preserving" if preserving else "breaking",
                "claude_judge": {"verdict": "equivalent" if preserving else "non_equivalent"},
                "reference_repr": {"record": {"goal_v1": "⊢ Reference"}},
                "candidate_repr": {"record": {"goal_v1": goal}},
            }
            root_sidecars.append(sidecar)
            sidecars.append(sidecar)
        core_path = root_dir / "new_core/core.jsonl"
        sidecar_path = root_dir / "new_core/sidecar.jsonl"
        core_path.parent.mkdir(parents=True)
        core_path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in root_core))
        sidecar_path.write_bytes(
            b"".join(canonical_json_bytes(row) + b"\n" for row in root_sidecars)
        )
        manifest = {
            "config_hash": hash_canonical(root_config.model_dump(mode="json")),
            "counts": {
                "accepted": 4,
                "accepted_positive": 2,
                "accepted_negative": 2,
                "invalid_attempts": 0,
                "unknown_rows": 0,
                "judge_disagreements": 0,
                "gold_contamination": 0,
                "cross_root_duplicates": 0,
                "retry_slots": 0,
                "attempts": 4,
            },
            "lean": {
                "candidate_requests": 4,
                "candidate_cache_hits": 0,
                "candidate_executed": 4,
                "candidate_elapsed_seconds": 1.0,
            },
            "llm": {
                "proposer_calls": 4,
                "proposer_cache_hits": 0,
                "claude_calls": 4,
                "claude_cache_hits": 0,
                "nominal_cost_usd": 0.1,
                "executed_cost_usd": 0.1,
                "latency_seconds": 2.0,
            },
            "artifacts": {
                "new_core/core.jsonl": {"sha256": hash_file(core_path)},
                "new_core/sidecar.jsonl": {"sha256": hash_file(sidecar_path)},
            },
        }
        manifest_path = root_dir / "manifest.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
        root_receipts.append(
            {
                "root_id": root.root_id,
                "manifest_path": str(manifest_path.relative_to(output)),
                "manifest_sha256": hash_file(manifest_path),
                "replayed": False,
            }
        )
    sample_path = output / "sample.jsonl"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in sample_rows))
    sample_manifest = {
        "sample_sha256": hash_file(sample_path),
        "source_mix": {"mathlib": 12},
        "selected_roots": [row["root"]["root_id"] for row in sample_rows],  # type: ignore[index]
    }
    (output / "sample_manifest.json").write_bytes(canonical_json_bytes(sample_manifest) + b"\n")
    pilot_manifest = {
        "version": "leanfaith_sft2a_diverse_root_pilot_v2",
        "config_hash": loaded.config_hash,
        "readiness_config_hash": readiness.config_hash,
        "root_count": 12,
        "root_manifests": root_receipts,
        "pilot_completed": True,
    }
    (output / "manifest.json").write_bytes(canonical_json_bytes(pilot_manifest) + b"\n")
    return sidecars


def test_production_config_is_additive_and_policy_bound() -> None:
    loaded = load_sft2a_config(Path("configs/sft2a/production_pilot_v1.yaml"))
    assert isinstance(loaded.config, SFT2AProductionConfig)
    assert loaded.config.labeling_defaults_policy.sha256 == (
        "4554a071b06b1af9015b253b5e64b2a0a4d013630e5224ef7729bbf65757646f"
    )
    assert (loaded.config.proposer.model, loaded.config.proposer.effort) == (
        "gpt-5.6-terra",
        "high",
    )
    assert (loaded.config.claude_judge.model, loaded.config.claude_judge.effort) == (
        "opus",
        "high",
    )
    assert (loaded.config.lemex_auditor.model, loaded.config.lemex_auditor.effort) == (
        "moonshotai/Kimi-K2.7-Code",
        "high",
    )
    assert hash_file(Path("configs/sft2a/one_root_opus5_v1.yaml")) == (
        "936f025d8e96048c788f6a57ecf2a717cfae68333fcda552b7815d8103c32aac"
    )


def test_completed_pilot_replay_and_combined_audit_share_budget_and_exclude_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = load_sft2a_config(Path("configs/sft2a/production_pilot_v1.yaml"))
    readiness = load_pilot_readiness(base, Path("configs/sft2a/pilot_readiness_production_v1.yaml"))
    loaded = _temporary_production(tmp_path)
    output = tmp_path / readiness.config.sample_output_subdir
    sidecars = _write_fake_completed_pilot(loaded, readiness, output)
    first_receipt = verify_pilot_replay(loaded, readiness)
    assert first_receipt["provider_calls_executed"] == 0
    assert first_receipt["lean_requests_executed"] == 0
    assert first_receipt["durable_artifacts_changed"] == 0
    first_hash = hash_file(output / "pilot_reproducibility_receipt.json")
    assert verify_pilot_replay(loaded, readiness) == first_receipt
    assert hash_file(output / "pilot_reproducibility_receipt.json") == first_hash

    selected, _allocations = pilot_audit_indices(sidecars, cap=8)
    assert len(selected) <= 8
    responses = []
    for position, index in enumerate(selected):
        source_verdict = sidecars[index]["claude_judge"]["verdict"]  # type: ignore[index]
        if position == 0:
            source_verdict = "non_equivalent" if source_verdict == "equivalent" else "equivalent"
        responses.append(_judge(str(source_verdict)))
    budget = PersistentProviderBudget(
        output / "provider_budget_journal.jsonl", readiness.config.ceilings
    )
    budget.record(
        kind="proposer",
        result=ProviderCallResult(
            call_key="prior-proposer",
            provider_id=loaded.config.proposer.provider_id,
            structured={},
            usage={},
            cost_usd=None,
            elapsed_seconds=0.1,
            cache_hit=False,
            terminal_path=tmp_path / "prior-proposer.json",
        ),
    )
    monkeypatch.setattr(
        "leanfaith.sft2a.pilot_audit.implementation_identity",
        lambda _root: {"implementation_commit": "a" * 40, "implementation_tree": "b" * 40},
    )
    auditor = FakeAuditor(tmp_path, loaded.config.lemex_auditor.provider_id, responses)
    manifest = run_pilot_lemex_audit(loaded, readiness, auditor=auditor)
    assert manifest["selected_rows"] == len(selected)
    assert manifest["disagreements"] == 1
    assert manifest["systematic_disagreement_blocks_scale"] is True
    assert manifest["persistent_provider_budget"]["unique_provider_calls"] == (  # type: ignore[index]
        1 + len(selected)
    )
    release = json.loads((output / "audit_lemex_v1/releasable_core/manifest.json").read_text())
    assert release["released_rows"] == 47
    quality = json.loads((output / "pilot_quality_manifest.json").read_text())
    assert quality["audit"]["disagreements"] == 1
    assert quality["systematic_disagreement_blocks_scale"] is True
    assert run_pilot_lemex_audit(loaded, readiness, auditor=auditor) == manifest
    assert auditor.calls == len(selected)


def test_audit_selection_is_stratified_and_hard_capped() -> None:
    rows: list[dict[str, object]] = []
    for index in range(200):
        rows.append(
            {
                "row_id": f"row-{index}",
                "requested_polarity": "preserving" if index % 2 == 0 else "breaking",
                "claude_judge": {"verdict": "equivalent" if index % 4 < 2 else "non_equivalent"},
            }
        )
    selected, allocations = pilot_audit_indices(rows, cap=8)
    assert len(selected) == 8
    assert sum(allocations.values()) == 8
    assert all(value > 0 for value in allocations.values())


def test_production_readiness_binds_smoke_and_refuses_unauthorized_launch() -> None:
    loaded = load_sft2a_config(Path("configs/sft2a/production_pilot_v1.yaml"))
    readiness = load_pilot_readiness(
        loaded, Path("configs/sft2a/pilot_readiness_production_v1.yaml")
    )
    assert readiness.config_hash == (
        "5b26cf8cf21a4e5542b0ba7c72a1c110e4bd415681eb2ddf2d3abd14e7203a02"
    )
    assert readiness.exact_settings_smoke is not None
    assert readiness.exact_settings_smoke["successful"] is True
    assert readiness.authorization["authorized"] is False
    assert readiness.config.detached_launch.session_name == ("leanfaith-sft2a-production-pilot-v1")
    with pytest.raises(PilotReadinessError, match="does not authorize execution"):
        launch_detached_pilot(loaded, readiness, resume=False)


def test_failed_pilot_recovery_corrects_ckm_and_seeds_cumulative_budget(
    tmp_path: Path,
) -> None:
    loaded = load_sft2a_config(Path("configs/sft2a/production_pilot_v1.yaml"))
    readiness = load_pilot_readiness(
        loaded,
        Path("configs/sft2a/pilot_recovery_readiness_production_v3.yaml"),
    )
    source = Path(loaded.config.staging_root) / "runs/diverse_root_production_defaults_pilot_v2"
    frozen_hashes = {
        relative: hash_file(source / relative)
        for relative in (
            "sample.jsonl",
            "sample_manifest.json",
            "provider_budget_journal.jsonl",
            "detached/terminal_status.json",
        )
    }
    manifest = prepare_pilot_sample(
        loaded,
        readiness,
        implementation={
            "implementation_commit": "a" * 40,
            "implementation_tree": "b" * 40,
        },
        output_root=tmp_path / "recovery",
    )
    recovery = tmp_path / "recovery"
    assert manifest["sample_sha256"] == (
        "52edf04e5cfddefcd6626dfcb0ee0785f4a0f1e9dbd4cfd0851407e6134ccea4"
    )
    assert manifest["pilot_authorized"] is False
    assert manifest["catalog_corrections_sha256"] == (
        "6a562f4b9e397ede3b8096ba1ce3d59bee977ee6dd66a0dac8133ce67f3f54b6"
    )
    assert (
        hash_file(recovery / "provider_budget_journal.jsonl")
        == frozen_hashes["provider_budget_journal.jsonl"]
    )
    budget = PersistentProviderBudget(
        recovery / "provider_budget_journal.jsonl",
        readiness.config.ceilings,
    ).snapshot()
    assert budget["unique_provider_calls"] == 73
    assert budget["reported_opus_spend_usd"] == pytest.approx(0.754154)
    rows = [json.loads(line) for line in (recovery / "sample.jsonl").read_text().splitlines()]
    ckm = next(row for row in rows if row["root"]["root_id"] == "physlib:ckm_row_norm")
    assert ckm["root"]["reference_signature"] == (
        "∀ (V : Quotient CKMMatrixSetoid) (i : Fin 3), "
        "VAbs i 0 V ^ 2 + VAbs i 1 V ^ 2 + VAbs i 2 V ^ 2 = 1"
    )
    with pytest.raises(PilotReadinessError, match="does not authorize execution"):
        require_pilot_authorization(readiness)
    assert {relative: hash_file(source / relative) for relative in frozen_hashes} == frozen_hashes


def test_detached_run_lock_refuses_duplicate_start(tmp_path: Path) -> None:
    lock = tmp_path / "detached/run.lock"
    with (
        _exclusive_lock(lock, label="test pilot run lock"),
        pytest.raises(DetachedPilotError, match="duplicate start refused"),
        _exclusive_lock(lock, label="test pilot run lock"),
    ):
        pytest.fail("duplicate lock unexpectedly acquired")


def test_authorization_transition_uses_fresh_root_and_stops_before_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = load_sft2a_config(Path("configs/sft2a/production_pilot_v1.yaml"))
    activation = load_pilot_activation(loaded)
    source = activation.source_readiness
    old_output = Path(loaded.config.staging_root) / source.config.sample_output_subdir
    old_sample = old_output / "sample.jsonl"
    old_manifest = old_output / "sample_manifest.json"
    old_hashes = (hash_file(old_sample), hash_file(old_manifest))
    with pytest.raises(PilotReadinessError, match="does not authorize execution"):
        require_pilot_authorization(source)
    with pytest.raises(PilotActivationError, match="exact pilot authorization sentence"):
        build_authorized_activation(
            activation,
            authorization_sentence="not authorized",
            implementation={
                "implementation_commit": "a" * 40,
                "implementation_tree": "b" * 40,
            },
        )
    identity = {
        "implementation_commit": "a" * 40,
        "implementation_tree": "b" * 40,
    }
    artifacts = build_authorized_activation(
        activation,
        authorization_sentence=activation.expected_authorization_sentence,
        implementation=identity,
    )
    assert artifacts.authorization_receipt["authorized"] is True
    assert artifacts.readiness.status == "authorized_pilot"
    assert artifacts.readiness.sample_output_subdir == (
        "runs/diverse_root_production_defaults_pilot_v2"
    )
    assert artifacts.tmux_session == "leanfaith-sft2a-production-pilot-v2"
    authorized = LoadedPilotReadiness(
        config=artifacts.readiness,
        path=tmp_path / "pilot_readiness_production_v2.yaml",
        config_hash=artifacts.readiness_hash,
        repo_root=loaded.repo_root,
        authorization=artifacts.authorization_receipt,
        historical_seal=source.historical_seal,
        exact_settings_smoke=source.exact_settings_smoke,
    )
    require_pilot_authorization(authorized)
    temporary = _temporary_production(tmp_path)
    monkeypatch.setattr("leanfaith.sft2a.detached._tmux_session_exists", lambda _name: False)
    preflight = preflight_detached_launch(
        temporary,
        authorized,
        resume=False,
        implementation=identity,
    )
    fresh_output = tmp_path / artifacts.readiness.sample_output_subdir
    assert fresh_output != old_output
    assert hash_file(fresh_output / "sample.jsonl") == old_hashes[0]
    assert preflight["sample_sha256"] == old_hashes[0]
    assert preflight["boundary"] == "tmux_start_not_executed"
    assert preflight["provider_calls_executed"] == 0
    assert preflight["lean_requests_executed"] == 0
    assert preflight["tmux_sessions_started"] == 0
    assert (hash_file(old_sample), hash_file(old_manifest)) == old_hashes
    assert hash_file(loaded.repo_root / activation.plan.target_authorization_receipt_path) == (
        "e00195d887692fe309ec024f46d52867b9ec6b2bd52488fdf4b8f465e9ea0b6c"
    )
    assert hash_file(loaded.repo_root / activation.plan.target_readiness_config_path) == (
        "fefdc00a8e694974fe75a64295a78122ac5f5083d036c99d3a1fbb3d90c58473"
    )
