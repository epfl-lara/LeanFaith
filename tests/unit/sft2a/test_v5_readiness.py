from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.sft2a.canaries import load_closure_canaries
from leanfaith.sft2a.census import prepare_rehearsal_sample, run_zero_lean_census
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.dedup import PersistentCandidateRegistry
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.lean_oracle import SignatureOracleResult
from leanfaith.sft2a.mechanisms import (
    MechanismAssignment,
    plan_mechanism_rotation,
    shortcut_violation,
)
from leanfaith.sft2a.models import SFT2AV5Config
from leanfaith.sft2a.pipeline import run_one_root
from leanfaith.sft2a.providers import ProviderCallResult
from leanfaith.sft2a.rehearsal import (
    RehearsalError,
    exclude_audit_unknowns,
    load_rehearsal_authorization,
    preflight_rehearsal_launch,
    project_50000_root_attempts,
    require_rehearsal_authorization,
)

_CONFIG = Path("configs/sft2a/closure_aware_v5.yaml")


def _loaded() -> LoadedSFT2AConfig:
    loaded = load_sft2a_config(_CONFIG)
    assert isinstance(loaded.config, SFT2AV5Config)
    return loaded


def _judge(verdict: str, rationale: str) -> dict[str, object]:
    return {
        "schema_version": 5,
        "verdict": verdict,
        "confidence": "high",
        "relation_class": "logical_restatement" if verdict == "equivalent" else "other",
        "error_type": "none",
        "rationale": rationale,
        "closure_checks": {
            "entire_universally_closed_proposition": True,
            "argument_swapping": "checked_no_effect",
            "symmetry": "checked_no_effect",
            "antisymmetry": "checked_no_effect",
            "extensionality": "not_applicable",
            "recoverable_boundary_cases": "checked_no_effect",
        },
    }


class ScriptedProvider:
    def __init__(
        self,
        root: Path,
        provider_id: str,
        responses: Sequence[dict[str, object]],
    ) -> None:
        self.root = root
        self.provider_id = provider_id
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        del prompt
        index = len(self.calls)
        self.calls.append(tuple(input_ids))
        return ProviderCallResult(
            call_key=f"{self.provider_id}:{index}",
            provider_id=self.provider_id,
            structured=self.responses[index],
            usage={"input_tokens": 2, "output_tokens": 2},
            cost_usd=0.01,
            elapsed_seconds=0.01,
            cache_hit=False,
            terminal_path=self.root / f"{self.provider_id}-{index}.json",
        )


class IdentityOracle:
    def __init__(self, loaded: LoadedSFT2AConfig, identity_mode: str) -> None:
        self.loaded = loaded
        self.identity_mode = identity_mode
        self.calls: list[tuple[str, str]] = []

    def elaborate(
        self,
        signature: str,
        *,
        endpoint_role: Literal["reference", "candidate"],
    ) -> SignatureOracleResult:
        self.calls.append((signature, endpoint_role))
        reference_hash = "a" * 64
        if endpoint_role == "reference":
            goal = self.loaded.config.root.expected_reference_goal_v1
            expr_hash = reference_hash
        else:
            goal = f"⊢ {signature}"
            expr_hash = sha256_hex(signature.encode())
            if signature == "AliasCandidate" and self.identity_mode == "expr":
                expr_hash = reference_hash
            if signature == "AliasCandidate" and self.identity_mode == "goal":
                goal = self.loaded.config.root.expected_reference_goal_v1
        digest = sha256_hex(f"{endpoint_role}:{signature}".encode())
        return SignatureOracleResult(
            status="valid",
            cache_key=f"cache:{digest}",
            cache_hit=False,
            signature_sha256=digest,
            goal_v1=goal,
            sidecar={
                "record": {
                    "goal_v1": goal,
                    "provenance": {"expr_hash": expr_hash},
                }
            },
            lean_status="valid",
            request_hash=f"request:{digest}",
            elapsed_ms=10,
            raw_response_path=None,
            detail="v5 identity regression oracle",
        )

    def close(self) -> None:
        pass


def _temporary_v5(tmp_path: Path) -> LoadedSFT2AConfig:
    loaded = _loaded()
    assert isinstance(loaded.config, SFT2AV5Config)
    layout = loaded.config.run_layout.model_copy(update={"shared_cache_root": str(tmp_path)})
    config = loaded.config.model_copy(update={"staging_root": str(tmp_path), "run_layout": layout})
    temporary = replace(
        loaded,
        config=config,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )
    canary = run_paths(temporary).one_root / "closure_canaries_v5/manifest.json"
    canary.parent.mkdir(parents=True)
    canary.write_bytes(canonical_json_bytes({"all_passed": True}) + b"\n")
    return temporary


def _rotation(loaded: LoadedSFT2AConfig) -> dict[str, MechanismAssignment]:
    assert isinstance(loaded.config, SFT2AV5Config)
    return plan_mechanism_rotation(
        [{"root": loaded.config.root.model_dump(mode="json")}],
        salt=loaded.config.mechanism_rotation.salt,
        maximum_family_fraction_per_polarity=(
            loaded.config.mechanism_rotation.maximum_family_fraction_per_polarity
        ),
    )[loaded.config.root.root_id]


def _proposal(polarity: str, mechanism: str, signature: str) -> dict[str, object]:
    return {
        "schema_version": 5,
        "requested_polarity": polarity,
        "mechanism": mechanism,
        "applicability_reason": "The frozen family applies to this synthetic proposition.",
        "candidate_signature": signature,
        "change_summary": "A substantive synthetic transformation.",
        "judge_trap": "Inspect the entire universal closure.",
        "informative": True,
        "substantive_change": True,
        "proof_free": True,
    }


@pytest.mark.parametrize("identity_mode", ["expr", "goal"])
def test_closed_expr_or_rendered_goal_self_pair_is_retried_before_judging(
    tmp_path: Path, identity_mode: str
) -> None:
    loaded = _temporary_v5(tmp_path)
    rotation = _rotation(loaded)
    proposer_responses = []
    for slot in loaded.config.slots:
        if slot.slot_id == "preserve_0":
            proposer_responses.append(
                _proposal(slot.requested_polarity, rotation[slot.slot_id].family, "AliasCandidate")
            )
        proposer_responses.append(
            _proposal(
                slot.requested_polarity,
                rotation[slot.slot_id].family,
                f"Candidate_{slot.slot_id}",
            )
        )
    proposer = ScriptedProvider(tmp_path, loaded.config.proposer.provider_id, proposer_responses)
    judge = ScriptedProvider(
        tmp_path,
        loaded.config.claude_judge.provider_id,
        [
            _judge(
                "equivalent" if slot.requested_polarity == "preserving" else "non_equivalent",
                (
                    "The closed propositions are logically equivalent."
                    if slot.requested_polarity == "preserving"
                    else "The closed propositions are non-equivalent."
                ),
            )
            for slot in loaded.config.slots
        ],
    )
    result = run_one_root(
        loaded,
        proposer=proposer,
        claude_judge=judge,
        oracle=IdentityOracle(loaded, identity_mode),
    )

    assert result.manifest["counts"]["self_pairs_rejected"] == 1  # type: ignore[index]
    assert result.manifest["counts"]["accepted"] == 4  # type: ignore[index]
    assert len(proposer.calls) == 5
    assert len(judge.calls) == 4


def test_malformed_verdict_rationale_retries_once_but_disagreement_does_not(
    tmp_path: Path,
) -> None:
    contradiction = ScriptedProvider(
        tmp_path,
        "judge",
        [
            _judge(
                "equivalent", "The statements are not equivalent and express a different claim."
            ),
            _judge("equivalent", "They are logically equivalent over the full closure."),
        ],
    )
    recovered = call_consistent_judge(
        contradiction,
        prompt="judge",
        input_ids=("row",),
        closure_aware=True,
        malformed_retries=1,
    )
    assert recovered.judgment is not None
    assert len(recovered.calls) == 2
    assert len(recovered.malformed_attempts) == 1

    disagreement = ScriptedProvider(
        tmp_path,
        "judge",
        [_judge("non_equivalent", "The closed propositions are non-equivalent.")],
    )
    genuine = call_consistent_judge(
        disagreement,
        prompt="judge",
        input_ids=("row",),
        closure_aware=True,
        malformed_retries=1,
    )
    assert genuine.judgment is not None
    assert genuine.judgment.verdict == "non_equivalent"
    assert len(genuine.calls) == 1
    assert genuine.malformed_attempts == ()


def test_closure_canaries_are_exact_equivalence_regressions() -> None:
    rows = load_closure_canaries(_loaded())
    assert {row["canary_id"] for row in rows} == {
        "nat_add_comm/break_1",
        "set_union_comm/break_1",
        "nat_gcd_comm/break_0",
    }
    assert all(row["required_verdict"] == "equivalent" for row in rows)


def test_zero_lean_census_sample_shards_and_rotation_are_frozen() -> None:
    loaded = _loaded()
    assert isinstance(loaded.config, SFT2AV5Config)
    census = run_zero_lean_census(loaded)
    sample = prepare_rehearsal_sample(loaded)
    output = Path(loaded.config.staging_root) / loaded.config.rehearsal.output_subdir
    rows = [json.loads(line) for line in (output / "sample.jsonl").read_text().splitlines()]

    assert census["provider_calls_executed"] == census["lean_requests_executed"] == 0
    assert sample["provider_calls_executed"] == sample["lean_requests_executed"] == 0
    assert sample["root_count"] == 100
    assert sample["slot_count"] == 400
    assert sample["source_mix"] == {
        "compiler_data": 16,
        "cslib": 17,
        "mathlib": 42,
        "physlib": 25,
    }
    assert len({row["root"]["root_id"] for row in rows}) == 100
    domains: dict[str, set[str]] = {}
    for row in rows:
        domains.setdefault(row["root"]["source"], set()).add(row["domain"])
    assert all(len(values) >= 4 for values in domains.values())
    shards = sample["shards"]
    assert isinstance(shards, list)
    for receipt in shards:
        assert isinstance(receipt, dict)
        path = output / receipt["path"]
        assert hash_file(path) == receipt["sha256"]
    assert (
        sum(
            value
            for receipt in shards
            for value in (receipt["root_count"],)
            if isinstance(value, int)
        )
        == 100
    )
    for polarity in ("preserving", "breaking"):
        counts = Counter(
            assignment["family"]
            for row in rows
            for assignment in row["mechanism_plan"].values()
            if assignment["polarity"] == polarity
        )
        assert len(counts) >= 8
        assert max(counts.values()) / sum(counts.values()) <= 0.2


def test_shortcuts_projection_and_audit_unknown_join() -> None:
    assert shortcut_violation("P", "P") == "exact_reference_copy"
    assert shortcut_violation("P", "True → P") == "vacuous_true_implication"
    assert shortcut_violation("P", "P ∧ True") == "vacuous_true_conjunction"
    assert shortcut_violation("P", "P ∧ x = x") == "reflexive_equality_padding"
    projection = project_50000_root_attempts(observed_retry_attempts=20, observed_slots=400)
    assert projection["base_candidate_slots"] == 200_000
    assert projection["projected_candidate_retries"] == 10_000
    assert projection["projected_candidate_attempts"] == 210_000
    core = [
        {"reference": "A", "candidate": "B", "label": True},
        {"reference": "C", "candidate": "D", "label": False},
    ]
    sidecars = [{"row_id": "keep"}, {"row_id": "disagree"}]
    assert exclude_audit_unknowns(core, sidecars, {"disagree"}) == [core[0]]


def test_cross_root_dedup_includes_closed_expr_identity(tmp_path: Path) -> None:
    registry = PersistentCandidateRegistry(tmp_path / "candidate_registry.jsonl")
    assert registry.claim(
        raw_signature="P",
        rendered_goal="⊢ P",
        closed_expr_hash="a" * 64,
        owner="root-a",
    )
    assert not registry.claim(
        raw_signature="NotationallyDifferentP",
        rendered_goal="x : Unit\n⊢ P",
        closed_expr_hash="a" * 64,
        owner="root-b",
    )


def _authorization_document(loaded: LoadedSFT2AConfig, *, authorized: bool) -> dict[str, object]:
    sample = prepare_rehearsal_sample(loaded)
    assert isinstance(loaded.config, SFT2AV5Config)
    return {
        "version": "leanfaith_sft2a_rehearsal_authorization_v5",
        "status": "authorized_rehearsal" if authorized else "ready_not_authorized",
        "authorized": authorized,
        "authorization_scope": "sft2a_v5_100_roots_400_slots_only",
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "sample_sha256": sample["sample_sha256"],
        "root_count": 100,
        "slot_count": 400,
        "ceilings": loaded.config.rehearsal.ceilings.model_dump(mode="json"),
        "legacy_rejudge_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "publication_authorized": False,
    }


def test_rehearsal_authorization_is_hash_bound_and_preflight_stops_at_tmux(
    tmp_path: Path,
) -> None:
    loaded = _loaded()
    path = tmp_path / "authorization.json"
    path.write_bytes(
        canonical_json_bytes(_authorization_document(loaded, authorized=False)) + b"\n"
    )
    readiness = load_rehearsal_authorization(loaded, path)
    with pytest.raises(RehearsalError, match="not authorized"):
        require_rehearsal_authorization(readiness)

    path.write_bytes(canonical_json_bytes(_authorization_document(loaded, authorized=True)) + b"\n")
    authorization = load_rehearsal_authorization(loaded, path)
    preflight = preflight_rehearsal_launch(loaded, authorization)
    assert preflight["boundary"] == "tmux_start_not_executed"
    assert preflight["provider_calls_executed"] == 0
    assert preflight["lean_requests_executed"] == 0
    assert preflight["tmux_sessions_started"] == 0
