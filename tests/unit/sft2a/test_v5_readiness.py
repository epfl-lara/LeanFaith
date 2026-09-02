from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.sft2a.canaries import load_closure_canaries
from leanfaith.sft2a.census import (
    _contexts_at_offsets,
    _has_slot_mechanism_coverage,
    prepare_rehearsal_sample,
    run_zero_lean_census,
)
from leanfaith.sft2a.config import LoadedSFT2AConfig, load_sft2a_config
from leanfaith.sft2a.dedup import PersistentCandidateRegistry
from leanfaith.sft2a.judgments import call_consistent_judge
from leanfaith.sft2a.layout import run_paths
from leanfaith.sft2a.lean_oracle import SignatureOracleResult
from leanfaith.sft2a.mechanisms import (
    MechanismAssignment,
    applicable_mechanisms,
    plan_mechanism_rotation,
    shortcut_violation,
)
from leanfaith.sft2a.models import SFT2AV5Config
from leanfaith.sft2a.pipeline import run_one_root
from leanfaith.sft2a.providers import ProviderCallResult, _codex_transport_schema
from leanfaith.sft2a.readiness import implementation_identity
from leanfaith.sft2a.rehearsal import (
    _V5_1_AUTHORIZATION_SENTENCE,
    _V5_AUTHORIZATION_SENTENCE,
    LoadedRehearsalAuthorization,
    RehearsalError,
    _authorization_profile,
    exclude_audit_unknowns,
    launch_detached_rehearsal,
    load_rehearsal_authorization,
    preflight_rehearsal_launch,
    project_50000_root_attempts,
    require_rehearsal_authorization,
)

_CONFIG = Path("configs/sft2a/closure_aware_v5.yaml")
_CORRECTED_CONFIG = Path("configs/sft2a/closure_aware_v5_1.yaml")


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


def test_certified_reference_skips_source_signature_elaboration(tmp_path: Path) -> None:
    loaded = _temporary_v5(tmp_path)
    rotation = _rotation(loaded)
    proposer = ScriptedProvider(
        tmp_path,
        loaded.config.proposer.provider_id,
        [
            _proposal(
                slot.requested_polarity,
                rotation[slot.slot_id].family,
                f"CertifiedCandidate_{slot.slot_id}",
            )
            for slot in loaded.config.slots
        ],
    )
    judge = ScriptedProvider(
        tmp_path,
        loaded.config.claude_judge.provider_id,
        [
            _judge(
                "equivalent" if slot.requested_polarity == "preserving" else "non_equivalent",
                (
                    "The full closures are logically equivalent."
                    if slot.requested_polarity == "preserving"
                    else "The full closures express different mathematical claims."
                ),
            )
            for slot in loaded.config.slots
        ],
    )
    oracle = IdentityOracle(loaded, "none")
    certified = SignatureOracleResult(
        status="valid",
        cache_key="certified-reference-cache",
        cache_hit=True,
        signature_sha256="1" * 64,
        goal_v1=loaded.config.root.expected_reference_goal_v1,
        sidecar={
            "record": {
                "goal_v1": loaded.config.root.expected_reference_goal_v1,
                "provenance": {"expr_hash": "a" * 64},
            }
        },
        lean_status="valid",
        request_hash="2" * 64,
        elapsed_ms=0,
        raw_response_path="cached-reference.json",
        detail="synthetic authoritative ConstantInfo.type cache hit",
    )
    result = run_one_root(
        loaded,
        proposer=proposer,
        claude_judge=judge,
        oracle=oracle,
        mechanism_plan=rotation,
        certified_reference=certified,
    )
    assert result.manifest["counts"]["accepted"] == 4  # type: ignore[index]
    assert oracle.calls
    assert all(role == "candidate" for _signature, role in oracle.calls)
    assert loaded.config.root.reference_signature not in {
        signature for signature, _role in oracle.calls
    }


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
    # The structured verdict is authoritative: a lexical verdict/rationale contradiction is
    # recorded as telemetry and never buys a paid retry.
    assert recovered.judgment is not None
    assert len(recovered.calls) == 1
    assert recovered.malformed_attempts == ()
    assert recovered.lexical_contradiction == "equivalent_verdict_with_non_equivalent_rationale"

    schema_invalid = ScriptedProvider(
        tmp_path,
        "judge",
        [
            {**_judge("equivalent", "They are logically equivalent."), "confidence": "low"},
            _judge("equivalent", "They are logically equivalent over the full closure."),
        ],
    )
    retried = call_consistent_judge(
        schema_invalid,
        prompt="judge",
        input_ids=("row",),
        closure_aware=True,
        malformed_retries=1,
    )
    assert retried.judgment is not None
    assert len(retried.calls) == 2
    assert len(retried.malformed_attempts) == 1
    assert retried.lexical_contradiction is None

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


def test_codex_transport_schema_adds_required_types_without_mutating_frozen_schema() -> None:
    original: dict[str, object] = {
        "type": "object",
        "properties": {
            "version": {"const": 5},
            "enabled": {"const": True},
            "verdict": {"enum": ["equivalent", "non_equivalent"]},
        },
    }
    transported = _codex_transport_schema(original)
    assert isinstance(transported, dict)
    properties = transported["properties"]
    assert isinstance(properties, dict)
    assert properties["version"] == {"const": 5, "type": "integer"}
    assert properties["enabled"] == {"const": True, "type": "boolean"}
    assert properties["verdict"] == {
        "enum": ["equivalent", "non_equivalent"],
        "type": "string",
    }
    frozen_properties = original["properties"]
    assert isinstance(frozen_properties, dict)
    assert frozen_properties["version"] == {"const": 5}


def test_census_marks_section_variables_unsafe_and_strips_open_in() -> None:
    source = """namespace Example
variable {α : Type*}
open Classical in
theorem unsafe {x : α} : x = x := rfl
end Example
theorem safe (n : Nat) : n = n := rfl
"""
    offsets = [source.index("theorem unsafe"), source.index("theorem safe")]
    contexts = _contexts_at_offsets(source, offsets)

    unsafe = contexts[offsets[0]]
    safe = contexts[offsets[1]]
    assert unsafe == (("Example",), ("Classical",), (), True)
    assert safe == ((), ("Classical",), (), False)
    assert "in" not in unsafe[1]


def test_rehearsal_root_has_two_applicable_mechanisms_per_slot_polarity() -> None:
    signature = "∀ {α : Type} [Preorder α] {a b c : α}, a ≤ b → b ≤ c → a ≤ c"

    assert not _has_slot_mechanism_coverage("P")
    assert _has_slot_mechanism_coverage(signature)
    assert len(applicable_mechanisms(signature, "preserving")) >= 2
    assert len(applicable_mechanisms(signature, "breaking")) >= 2


def test_corrected_rehearsal_has_distinct_fail_closed_authorization_profile() -> None:
    loaded = load_sft2a_config(_CORRECTED_CONFIG)
    assert isinstance(loaded.config, SFT2AV5Config)
    profile = _authorization_profile(loaded.config)

    assert loaded.config_hash == "add8445af25fddc99f3381dbf23d30121847f90c3ef0ddbbba4b09ee8e632f51"
    assert profile.scope == "sft2a_v5_1_100_roots_400_slots_only"
    assert profile.readiness_path == "configs/sft2a/rehearsal_readiness_v5_1.json"
    assert profile.sentence == _V5_1_AUTHORIZATION_SENTENCE
    assert sha256_hex(profile.sentence.encode()) == (
        "00a61d1f7c4c9e32069d5d980b2115834fd7baaacd037c9c769dc66abf4cb105"
    )


def _authorization_document(loaded: LoadedSFT2AConfig, *, authorized: bool) -> dict[str, object]:
    sample = prepare_rehearsal_sample(loaded)
    assert isinstance(loaded.config, SFT2AV5Config)
    implementation = implementation_identity(loaded.repo_root, require_clean=False)
    document: dict[str, object] = {
        "version": "leanfaith_sft2a_rehearsal_authorization_v5",
        "status": "authorized_rehearsal" if authorized else "ready_not_authorized",
        "authorized": authorized,
        "authorization_scope": "sft2a_v5_100_roots_400_slots_only",
        "config_hash": loaded.config_hash,
        "config_file_sha256": hash_file(loaded.path),
        "sample_sha256": sample["sample_sha256"],
        "sample_manifest_sha256": hash_file(
            Path(loaded.config.staging_root)
            / loaded.config.rehearsal.output_subdir
            / "sample_manifest.json"
        ),
        "census_manifest_sha256": hash_file(
            Path(loaded.config.staging_root)
            / loaded.config.source_census.output_subdir
            / "manifest.json"
        ),
        "census_inventory_sha256": hash_file(
            Path(loaded.config.staging_root)
            / loaded.config.source_census.output_subdir
            / "eligible_roots.jsonl"
        ),
        "root_count": 100,
        "slot_count": 400,
        "source_mix": sample["source_mix"],
        "ceilings": loaded.config.rehearsal.ceilings.model_dump(mode="json"),
        **implementation,
        "smoke_receipt_path": "configs/sft2a/closure_aware_v5.yaml",
        "smoke_receipt_sha256": hash_file(loaded.repo_root / "configs/sft2a/closure_aware_v5.yaml"),
        "output_root": str(
            Path(loaded.config.staging_root) / loaded.config.rehearsal.output_subdir
        ),
        "tmux_session": loaded.config.rehearsal.detached_launch.session_name,
        "legacy_rejudge_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "publication_authorized": False,
    }
    if authorized:
        document.update(
            {
                "authorization_text": _V5_AUTHORIZATION_SENTENCE,
                "authorization_text_sha256": sha256_hex(_V5_AUTHORIZATION_SENTENCE.encode()),
                "readiness_receipt_path": "configs/sft2a/rehearsal_readiness_v5.json",
                "readiness_receipt_sha256": hash_file(
                    loaded.repo_root / "configs/sft2a/rehearsal_readiness_v5.json"
                ),
            }
        )
    return document


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


def test_v5_detached_launcher_leaves_pty_redirection_to_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _loaded()
    authorization = LoadedRehearsalAuthorization(
        path=tmp_path / "authorization.json",
        document={"implementation_commit": "a" * 40, "implementation_tree": "b" * 40},
        sha256="c" * 64,
    )
    captured: list[tuple[str, ...]] = []

    def fake_run(args: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        "leanfaith.sft2a.rehearsal.preflight_rehearsal_launch",
        lambda *_args: {"boundary": "tmux_start_not_executed"},
    )
    monkeypatch.setattr("leanfaith.sft2a.rehearsal._output", lambda _loaded: tmp_path)
    monkeypatch.setattr("leanfaith.sft2a.rehearsal._session_exists", lambda _name: False)
    monkeypatch.setattr("leanfaith.sft2a.rehearsal.subprocess.run", fake_run)

    result = launch_detached_rehearsal(loaded, authorization)

    assert result["session_started"] is True
    tmux_command = captured[-1][-1]
    assert "detached-v5-rehearsal-worker" in tmux_command
    assert "</dev/null" not in tmux_command
    assert ">>" not in tmux_command
