from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.sft2a.budget import PersistentProviderBudget, ProviderBudgetError
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.dedup import PersistentCandidateRegistry
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.lean_oracle import SignatureOracleResult
from leanfaith.sft2a.legacy import LegacyAdapterError, _blocklist
from leanfaith.sft2a.legacy_rejudge import (
    LegacyRejudgeError,
    _stratified_negative_indices,
    prepare_legacy_opus_sample,
    run_legacy_opus_rejudge,
)
from leanfaith.sft2a.models import SFT2AOpusConfig
from leanfaith.sft2a.pilot import (
    consolidate_pilot_quality,
    prepare_pilot_sample,
    run_multi_root_pilot,
)
from leanfaith.sft2a.pipeline import _gold_signature_hit, run_one_root, verify_one_root_replay
from leanfaith.sft2a.providers import ProviderCallResult
from leanfaith.sft2a.readiness import (
    PilotReadinessError,
    load_pilot_readiness,
    verify_historical_fable_seal,
)
from leanfaith.sft2a.release import materialize_post_audit_core


def _proposer(polarity: str, signature: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "requested_polarity": polarity,
        "mechanism": "other",
        "candidate_signature": signature,
        "change_summary": "Synthetic review-fix test candidate.",
        "judge_trap": "Compare only the propositions.",
        "informative": True,
        "proof_free": True,
    }


def _judge(verdict: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "verdict": verdict,
        "confidence": "high",
        "relation_class": "logical_restatement" if verdict == "equivalent" else "other",
        "error_type": "none",
        "rationale": "Independent test judgment.",
    }


class CachedProvider:
    def __init__(
        self,
        tmp_path: Path,
        provider_id: str,
        responses: Sequence[dict[str, object]],
        *,
        cache_hit: bool,
    ) -> None:
        self.tmp_path = tmp_path
        self.provider_id = provider_id
        self.responses = list(responses)
        self.cache_hit = cache_hit
        self.calls = 0

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        del prompt, input_ids
        index = self.calls
        self.calls += 1
        return ProviderCallResult(
            call_key=f"{self.provider_id}:{index}",
            provider_id=self.provider_id,
            structured=self.responses[index],
            usage={"input_tokens": 2, "output_tokens": 1},
            cost_usd=0.02,
            elapsed_seconds=0.2,
            cache_hit=self.cache_hit,
            terminal_path=self.tmp_path / f"{self.provider_id}-{index}.json",
        )


class CachedOracle:
    def __init__(self, loaded: LoadedSFT2AConfig) -> None:
        self.loaded = loaded
        self.calls = 0

    def elaborate(
        self,
        signature: str,
        *,
        endpoint_role: Literal["reference", "candidate"],
    ) -> SignatureOracleResult:
        self.calls += 1
        goal = (
            self.loaded.config.root.expected_reference_goal_v1
            if endpoint_role == "reference"
            else f"⊢ {signature}"
        )
        digest = sha256_hex(signature.encode())
        return SignatureOracleResult(
            status="valid",
            cache_key=f"lean:{digest}",
            cache_hit=True,
            signature_sha256=digest,
            goal_v1=goal,
            sidecar={"record": {"goal_v1": goal}},
            lean_status="valid",
            request_hash=digest,
            elapsed_ms=1,
            raw_response_path=None,
            detail="cached test result",
        )

    def close(self) -> None:
        pass


def _temp_opus(tmp_path: Path) -> LoadedSFT2AConfig:
    loaded = load_sft2a_config(Path("configs/sft2a/one_root_opus5_v1.yaml"))
    assert isinstance(loaded.config, SFT2AOpusConfig)
    layout = loaded.config.run_layout.model_copy(update={"shared_cache_root": str(tmp_path)})
    config = loaded.config.model_copy(update={"staging_root": str(tmp_path), "run_layout": layout})
    return replace(
        loaded,
        config=config,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )


def test_opus_config_is_additive_and_versioned_output_replays(tmp_path: Path) -> None:
    loaded = _temp_opus(tmp_path)
    assert loaded.config.claude_judge.model == "opus"
    assert loaded.config.claude_judge.effort == "max"
    assert loaded.config.claude_judge.provider_id == "claude_opus_alias_max_sft2a_smoke_v1"
    assert loaded.config.claude_judge.server_revision_status == (
        "unavailable_floating_provider_alias"
    )
    assert run_paths(loaded).one_root != run_paths(loaded).historical_fable_one_root

    proposer = CachedProvider(
        tmp_path,
        loaded.config.proposer.provider_id,
        [
            _proposer("preserving", "1 = 1"),
            _proposer("preserving", "2 = 2"),
            _proposer("breaking", "3 = 4"),
            _proposer("breaking", "5 = 6"),
        ],
        cache_hit=True,
    )
    judge = CachedProvider(
        tmp_path,
        loaded.config.claude_judge.provider_id,
        [
            _judge("equivalent"),
            _judge("equivalent"),
            _judge("non_equivalent"),
            _judge("non_equivalent"),
        ],
        cache_hit=False,
    )
    oracle = CachedOracle(loaded)
    first = run_one_root(loaded, proposer=proposer, claude_judge=judge, oracle=oracle)
    assert first.output_root == tmp_path / "runs/one_root_opus5_v1"
    assert first.manifest["llm"]["proposer_cache_hits"] == 4  # type: ignore[index]
    assert first.manifest["lean"]["candidate_executed"] == 0  # type: ignore[index]
    assert first.manifest["llm"]["claude_cache_hits"] == 0  # type: ignore[index]
    first_receipt = verify_one_root_replay(loaded)
    assert first_receipt["reproducible"] is True
    derived = first.output_root / "comparison_fable_opus_v1/manifest.json"
    derived.parent.mkdir(parents=True)
    derived.write_text("{}\n", encoding="utf-8")
    assert verify_one_root_replay(loaded) == first_receipt
    second = run_one_root(loaded, proposer=proposer, claude_judge=judge, oracle=oracle)
    assert second.replayed is True
    assert proposer.calls == judge.calls == 4

    budget_path = tmp_path / "restart-budget.jsonl"
    budget = PersistentProviderBudget(budget_path, loaded.config.pilot.ceilings)
    charged = ProviderCallResult(
        call_key="opus:charged",
        provider_id=loaded.config.claude_judge.provider_id,
        structured=_judge("equivalent"),
        usage={"input_tokens": 1, "output_tokens": 1},
        cost_usd=15.0,
        elapsed_seconds=1.0,
        cache_hit=False,
        terminal_path=tmp_path / "charged.json",
    )
    budget.record(kind="opus", result=charged)
    restarted = PersistentProviderBudget(budget_path, loaded.config.pilot.ceilings)
    assert restarted.snapshot()["reported_opus_spend_usd"] == 15.0
    restarted.record(kind="opus", result=replace(charged, cache_hit=True))
    assert restarted.snapshot()["reported_opus_spend_usd"] == 15.0
    with pytest.raises(ProviderBudgetError, match="spend ceiling reached"):
        restarted.ensure_can_attempt("opus")
    missing_cost = replace(charged, call_key="opus:missing-cost", cost_usd=None)
    with pytest.raises(ProviderBudgetError, match="lacks the required reported cost"):
        restarted.record(kind="opus", result=missing_cost)


def test_deterministic_multi_source_sample_and_legacy_sample_are_exact(
    tmp_path: Path,
) -> None:
    loaded = load_sft2a_config(Path("configs/sft2a/one_root_opus5_v1.yaml"))
    readiness = load_pilot_readiness(loaded)
    implementation = {"implementation_commit": "a" * 40, "implementation_tree": "b" * 40}
    pilot_output = tmp_path / "pilot"
    pilot = prepare_pilot_sample(
        loaded,
        readiness,
        implementation=implementation,
        output_root=pilot_output,
    )
    assert pilot["source_mix"] == {
        "compiler_data": 2,
        "cslib": 2,
        "mathlib": 5,
        "physlib": 3,
    }
    assert pilot["root_count"] == 12
    assert pilot["sample_sha256"] == (
        "d0568942cf276939a47b375a73715fcae489a9b9c380c9aa02bd780bd706ba75"
    )
    assert pilot["provider_calls_executed"] == 0
    assert [row["project_id"] for row in pilot["grouped_execution_order"]] == [  # type: ignore[union-attr,index]
        "cslib",
        "mathlib",
        "physlib",
    ]
    assert pilot["selected_roots"] == [
        "cslib:timem_ret_merge",
        "cslib:urm_write_read_self",
        "compiler_data:algebra_327937",
        "compiler_data:number_theory_607287",
        "mathlib:le_trans",
        "mathlib:list_reverse_reverse",
        "mathlib:nat_add_comm",
        "mathlib:nat_gcd_comm",
        "mathlib:set_union_comm",
        "physlib:ckm_row_norm",
        "physlib:flrw_limit_s_saddle",
        "physlib:free_particle_kinetic_energy_conserved",
    ]
    with pytest.raises(PilotReadinessError, match="does not authorize"):
        run_multi_root_pilot(loaded, readiness)

    legacy_output = tmp_path / "legacy"
    legacy = prepare_legacy_opus_sample(
        loaded,
        readiness,
        implementation=implementation,
        output_root=legacy_output,
    )
    assert legacy["counts"] == {
        "all_admitted_positives": 233,
        "stratified_admitted_negatives": 2000,
        "provider_call_rows": 2233,
        "renderable_unresolved_auxiliary_no_call": 3,
        "total_tracked_rows": 2236,
    }
    assert legacy["provider_calls_executed"] == 0
    assert len((legacy_output / "sample.jsonl").read_text().splitlines()) == 2233
    auxiliary = (legacy_output / "needs_second_judge/rows.jsonl").read_text().splitlines()
    assert len(auxiliary) == 3
    assert all(json.loads(row)["provider_call_allowed"] is False for row in auxiliary)
    with pytest.raises(LegacyRejudgeError, match="not authorized"):
        run_legacy_opus_rejudge(loaded, readiness)

    registry_path = tmp_path / "candidate-registry.jsonl"
    registry = PersistentCandidateRegistry(registry_path)
    assert registry.claim(raw_signature="1 = 1", rendered_goal="⊢ 1 = 1", owner="root-a")
    assert registry.claim(raw_signature="1 = 1", rendered_goal="⊢ 1 = 1", owner="root-a")
    restarted_registry = PersistentCandidateRegistry(registry_path)
    assert not restarted_registry.claim(
        raw_signature="1 = 1", rendered_goal="⊢ 1 = 1", owner="root-b"
    )


def test_legacy_stratification_and_rejudge_produce_only_agreed_double_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecars = [{"row_id": f"a{i}", "family": "a"} for i in range(7)] + [
        {"row_id": f"b{i}", "family": "b"} for i in range(3)
    ]
    assert _stratified_negative_indices(sidecars, 5, "salt") == _stratified_negative_indices(
        sidecars, 5, "salt"
    )
    assert len(_stratified_negative_indices(sidecars, 5, "salt")) == 5

    loaded = _temp_opus(tmp_path)
    base = load_sft2a_config(Path("configs/sft2a/one_root_opus5_v1.yaml"))
    readiness = load_pilot_readiness(base)
    legacy_policy = readiness.config.legacy_rejudge.model_copy(update={"authorized": True})
    readiness = replace(
        readiness,
        config=readiness.config.model_copy(
            update={"status": "authorized_pilot", "legacy_rejudge": legacy_policy}
        ),
        authorization={**readiness.authorization, "legacy_rejudge_authorized": True},
    )
    output = tmp_path / readiness.config.legacy_rejudge.output_subdir
    sample = [
        {
            "row_id": "p",
            "selection_kind": "all_admitted_positive",
            "reference": "⊢ True",
            "candidate": "⊢ True",
            "legacy_label": True,
            "family": "f",
            "record_id": "p",
        },
        {
            "row_id": "n",
            "selection_kind": "deterministic_stratified_admitted_negative",
            "reference": "⊢ True",
            "candidate": "⊢ False",
            "legacy_label": False,
            "family": "f",
            "record_id": "n",
        },
    ]
    output.mkdir(parents=True)
    (output / "sample.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in sample)
    )
    (output / "sample_manifest.json").write_bytes(canonical_json_bytes({}) + b"\n")
    monkeypatch.setattr(
        "leanfaith.sft2a.legacy_rejudge.prepare_legacy_opus_sample",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "leanfaith.sft2a.legacy_rejudge.implementation_identity",
        lambda _repo_root: {"implementation_commit": "a" * 40, "implementation_tree": "b" * 40},
    )
    judge = CachedProvider(
        tmp_path,
        loaded.config.claude_judge.provider_id,
        [_judge("equivalent"), _judge("non_equivalent")],
        cache_hit=False,
    )
    manifest = run_legacy_opus_rejudge(loaded, readiness, judge=judge)
    assert manifest["accepted_agreements"] == 2
    core_lines = (output / "legacy_double_judge/core.jsonl").read_text().splitlines()
    assert len(core_lines) == 2
    assert judge.calls == 2
    assert (output / "excluded/rows.jsonl").read_text() == ""


def test_post_audit_disagreement_is_excluded_by_stable_row_id(tmp_path: Path) -> None:
    source = tmp_path / "source"
    audit = tmp_path / "audit"
    output = tmp_path / "release"
    (source / "new_core").mkdir(parents=True)
    (audit / "audit").mkdir(parents=True)
    core = [
        {"reference": "⊢ A", "candidate": "⊢ A", "label": True},
        {"reference": "⊢ A", "candidate": "⊢ B", "label": False},
    ]
    sidecars = [{"row_id": "keep"}, {"row_id": "drop"}]
    (source / "new_core/core.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in core)
    )
    (source / "new_core/sidecar.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in sidecars)
    )
    (source / "manifest.json").write_bytes(canonical_json_bytes({"source": 1}) + b"\n")
    (audit / "audit/rows.jsonl").write_bytes(
        canonical_json_bytes({"row_id": "drop", "agrees": False}) + b"\n"
    )
    (audit / "manifest.json").write_bytes(
        canonical_json_bytes({"source_run_manifest_sha256": hash_file(source / "manifest.json")})
        + b"\n"
    )
    result = materialize_post_audit_core(source_run=source, audit_run=audit, output_root=output)
    assert result.manifest["excluded_disagreement_row_ids"] == ["drop"]
    released = [json.loads(line) for line in (output / "core.jsonl").read_text().splitlines()]
    assert released == [core[0]]
    assert list(released[0]) == ["candidate", "label", "reference"] or set(released[0]) == {
        "reference",
        "candidate",
        "label",
    }

    quality_output = tmp_path / "quality"
    quality_output.mkdir()
    sample_manifest = {
        "sample_sha256": "c" * 64,
        "source_mix": {"mathlib": 1},
        "selected_roots": ["mathlib:test"],
    }
    (quality_output / "sample_manifest.json").write_bytes(
        canonical_json_bytes(sample_manifest) + b"\n"
    )
    root_manifest = {
        "counts": {
            "accepted": 3,
            "accepted_positive": 2,
            "accepted_negative": 1,
            "invalid_attempts": 1,
            "unknown_rows": 0,
            "judge_disagreements": 1,
            "gold_contamination": 0,
            "cross_root_duplicates": 1,
            "retry_slots": 1,
            "attempts": 5,
        },
        "lean": {
            "candidate_requests": 5,
            "candidate_cache_hits": 2,
            "candidate_executed": 3,
            "candidate_elapsed_seconds": 6.0,
        },
        "llm": {
            "proposer_calls": 5,
            "proposer_cache_hits": 2,
            "claude_calls": 4,
            "claude_cache_hits": 0,
            "nominal_cost_usd": 0.4,
            "executed_cost_usd": 0.4,
            "latency_seconds": 8.0,
        },
    }
    root_manifest_path = quality_output / "root.json"
    root_manifest_path.write_bytes(canonical_json_bytes(root_manifest) + b"\n")
    quality = consolidate_pilot_quality(
        output=quality_output,
        sample_manifest=sample_manifest,
        root_manifest_paths=[root_manifest_path],
        implementation={"implementation_commit": "a" * 40, "implementation_tree": "b" * 40},
        budget_snapshot={"unique_provider_calls": 9, "reported_opus_spend_usd": 0.4},
        dedup_snapshot={"claimed_candidates": 4},
    )
    assert quality["counts"]["cross_root_duplicates"] == 1  # type: ignore[index]
    assert (quality_output / "pilot_quality_report.md").is_file()


def test_contamination_hit_and_legacy_gold_hash_are_enforced() -> None:
    digest = sha256_hex(b"blocked")
    assert _gold_signature_hit("anything", {digest})[0] is False
    from leanfaith.representations.views import signature_near_dup_hash

    actual = signature_near_dup_hash("anything")
    assert _gold_signature_hit("anything", {actual}) == (True, actual)
    loaded = load_sft2a_config(Path("configs/sft2a/one_root_opus5_v1.yaml"))
    bad_gold = loaded.config.gold_screen.model_copy(update={"sha256": "0" * 64})
    bad_config = loaded.config.model_copy(update={"gold_screen": bad_gold})
    with pytest.raises(LegacyAdapterError, match="hash differs"):
        _blocklist(replace(loaded, config=bad_config))


def test_historical_fable_config_bytes_remain_sealed() -> None:
    assert hash_file(Path("configs/sft2a/one_root_v1.yaml")) == (
        "af7f12139a507622a8e6c523781dcda5bc08765474d1627655229d935c4ac1dc"
    )
    assert hash_file(Path("configs/sft2a/one_root_opus5_v1.yaml")) == (
        "936f025d8e96048c788f6a57ecf2a717cfae68333fcda552b7815d8103c32aac"
    )
    loaded = load_sft2a_config(Path("configs/sft2a/one_root_opus5_v1.yaml"))
    readiness = load_pilot_readiness(loaded)
    assert verify_historical_fable_seal(readiness.historical_seal) == (
        "b1097b6855839bfbd10d04c72acfc7e0189380df3eba8ab5dcc92cbaf8a53a77"
    )
    staging = Path(loaded.config.staging_root)
    assert hash_file(staging / "runs/one_root_opus5_v1/manifest.json") == (
        "d4ea7cf0731f5348d06d7118b82fc533a1416c5b82b0f0da62ab29224c0abb4e"
    )
    assert hash_file(staging / "runs/one_root_opus5_v1/reproducibility_receipt.json") == (
        "4ba4e2bb76e9d8cb8aef211c4e613054acbbe354f1065ed9a7db53f285e78f87"
    )
