from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2a.budget import PersistentProviderBudget
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.models import SFT2AProductionConfig
from leanfaith.sft2a.pilot import verify_pilot_replay
from leanfaith.sft2a.pilot_audit import pilot_audit_indices, run_pilot_lemex_audit
from leanfaith.sft2a.providers import ProviderCallResult
from leanfaith.sft2a.readiness import load_pilot_readiness


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
    readiness: object,
    output: Path,
) -> list[dict[str, object]]:
    assert hasattr(readiness, "config_hash") and hasattr(readiness, "config")
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
        "readiness_config_hash": readiness.config_hash,  # type: ignore[attr-defined]
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
    base = load_sft2a_config(Path("configs/sft2a/one_root_opus5_v1.yaml"))
    readiness = load_pilot_readiness(base)
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
