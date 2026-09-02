"""Focused v3 repair after shard 1: authoring views, plain-open-free v3 commands with validated
scoped entries, failure attribution, the pre-Lean inaccessible-name rejection, attributed
thresholds, the canary role and its chain, and the deterministic defect-class selections."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.representations.goal_v1 import CompileContext
from leanfaith.sft2a import lean_oracle, sprint_pilot_v52, sprint_repair_v3
from leanfaith.sft2a.lean_oracle import (
    COMMAND_TEMPLATE_VERSION_V2,
    COMMAND_TEMPLATE_VERSION_V3,
    INACCESSIBLE_NAME_MARK,
    ORACLE_METHOD_VERSION_V3,
    SignatureOracleResult,
    _authoring_view_command,
    _prelude_command,
    _signature_command,
    classify_lean_failure,
    effective_context_key,
    elaborator_sha256,
    prelude_line_count,
)
from leanfaith.sft2a.models import SlotConfig
from leanfaith.sft2a.prompts import (
    AUTHORING_VIEW_UNAVAILABLE,
    PromptRenderError,
    render_proposer_prompt,
)
from leanfaith.sft2a.provider_rehearsal_v52 import load_provider_rehearsal_v52
from leanfaith.sft2a.sprint_pilot_v52 import chain_decision, evaluate_sprint_pilot_thresholds

REPO = Path(__file__).resolve().parents[3]


def _context(**overrides: object) -> CompileContext:
    values: dict[str, Any] = {
        "project_id": "mathlib",
        "project_revision": "d568c8c09630de097a0" + "0" * 21,
        "lean_version": "v4.31.0-rc1",
        "import_header": "import Mathlib",
        "namespace_context": ("CochainComplex", "mappingCone"),
        "open_context": ("Category", "CategoryTheory", "hiding", "id"),
        "scoped_context": ("Classical",),
        "options": {"autoImplicit": True},
    }
    values.update(overrides)
    return CompileContext(**values)


# ---- command template ---------------------------------------------------------------------


def test_v3_command_emits_no_plain_opens_and_keeps_namespaces_and_scoped() -> None:
    effective = _context(open_context=())
    command = _signature_command(
        context=effective,
        signature="∀ (n : ℕ), Nat.succ n = n + 1",
        endpoint_id="sft2a-signature:candidate:abc",
        render_scope_id="sft2a-signature-v3:fp",
        cache_version="v3",
    )
    live = command[command.index("set_option autoImplicit") :]
    assert "\nopen Category\n" not in live and "open hiding" not in live
    assert live.index("namespace CochainComplex") < live.index("namespace mappingCone")
    assert live.index("namespace mappingCone") < live.index("open scoped Classical")
    assert live.index("open scoped Classical") < live.index("lfSft2aSignatureV3")
    assert command.count("Term.elabTerm") == 1
    assert command.count("LeanFaith.GoalV1.emitClosedProp") == 1
    assert "universe u_0 u_1 u_2 u_3 u_4 u_5 u_6 u_7" in command
    assert command.rstrip().endswith("end mappingCone\nend CochainComplex")


def test_v3_command_refuses_plain_open_context() -> None:
    with pytest.raises(lean_oracle.SignatureOracleError, match="plain open_context"):
        _signature_command(
            context=_context(),
            signature="True",
            endpoint_id="e",
            render_scope_id="s",
            cache_version="v3",
        )


def test_v3_identities_differ_from_v2_and_prelude_count_matches_command() -> None:
    assert elaborator_sha256("v3") != elaborator_sha256("v2")
    assert COMMAND_TEMPLATE_VERSION_V3 != COMMAND_TEMPLATE_VERSION_V2
    assert lean_oracle.oracle_method_version("v3") == ORACLE_METHOD_VERSION_V3
    effective = _context(open_context=())
    command = _signature_command(
        context=effective,
        signature="True",
        endpoint_id="e",
        render_scope_id="s",
        cache_version="v3",
    )
    lines = command.splitlines()
    count = prelude_line_count(effective, "v3")
    assert lines[count].startswith("lfSft2aSignatureV3")
    prelude = _prelude_command(effective)
    assert "lfSft2aSignatureV3 " not in prelude and prelude.rstrip().endswith("end CochainComplex")
    authoring = _authoring_view_command(
        context=effective,
        declaration_name="CochainComplex.mappingCone.map_inr",
        profile="raw",
        endpoint_id="sft2a-authoring:raw:x",
        render_scope_id="s",
    )
    assert '"CochainComplex.mappingCone.map_inr" "raw"' in authoring
    assert authoring.count("Term.elabTerm") == 1


def test_effective_context_key_ignores_nothing_but_is_stable() -> None:
    assert effective_context_key(_context()) == effective_context_key(_context())
    assert effective_context_key(_context()) != effective_context_key(_context(scoped_context=()))


# ---- attribution ----------------------------------------------------------------------------


def test_classify_lean_failure_prefers_prelude_then_dagger_then_candidate() -> None:
    prelude_error = [{"severity": "error", "start_pos": {"line": 390, "column": 5}, "data": "x"}]
    late_error = [{"severity": "error", "start_pos": {"line": 396, "column": 5}, "data": "x"}]
    assert (
        classify_lean_failure(signature="True", messages=prelude_error, prelude_lines=395)
        == "context_prelude"
    )
    assert (
        classify_lean_failure(
            signature=f"∀ [inst{INACCESSIBLE_NAME_MARK} : P], True",
            messages=late_error,
            prelude_lines=395,
        )
        == "copied_inaccessible_name"
    )
    assert (
        classify_lean_failure(signature="True", messages=late_error, prelude_lines=395)
        == "candidate_local"
    )
    warning_only = [{"severity": "warning", "start_pos": {"line": 1, "column": 0}, "data": "w"}]
    assert (
        classify_lean_failure(signature="True", messages=warning_only, prelude_lines=395)
        == "candidate_local"
    )


# ---- prompt rendering ------------------------------------------------------------------------


def _slot() -> SlotConfig:
    return SlotConfig(
        slot_id="preserve_0",
        requested_polarity="preserving",
        preferred_mechanism="recoverable_boundary_partition",
        max_attempts=3,
    )


def _loaded_with_prompt(prompt: str) -> Any:
    from leanfaith.sft2a.config import load_sft2a_config

    base = load_sft2a_config(
        REPO / "configs/sft2a/closure_aware_v5_2_sprint_v3_authoring.yaml", verify_binaries=False
    )
    return replace(base, proposer_prompt=prompt)


def test_v3_prompt_renders_view_context_override_and_unavailable_fallback() -> None:
    loaded = _loaded_with_prompt(
        (REPO / "prompts/sft2a/codex_proposer_sprint_v3.txt").read_text(encoding="utf-8")
    )
    rendered = render_proposer_prompt(
        loaded,
        slot=_slot(),
        attempt_number=1,
        attempt_feedback=None,
        reference_goal="α : Type u_0\ninst✝ : Preorder α\n⊢ True",
        authoring_view="∀ {α : Type u_0} [inst : Preorder α], True",
        compile_context={"namespace_context": ["Real"], "open_context": [], "scoped_context": []},
    )
    assert "∀ {α : Type u_0} [inst : Preorder α], True" in rendered
    assert '"open_context":[]' in rendered and "{{" not in rendered
    assert "pretty-printer artifact" in rendered and "rejected\nbefore Lean" in rendered.replace(
        "rejected\nbefore Lean", "rejected\nbefore Lean"
    )
    fallback = render_proposer_prompt(
        loaded, slot=_slot(), attempt_number=1, attempt_feedback=None, reference_goal="⊢ True"
    )
    assert AUTHORING_VIEW_UNAVAILABLE in fallback


def test_v2_prompt_rendering_is_unchanged_and_refuses_a_view() -> None:
    v2 = (REPO / "prompts/sft2a/codex_proposer_sprint_v2.txt").read_text(encoding="utf-8")
    loaded = _loaded_with_prompt(v2)
    rendered = render_proposer_prompt(
        loaded, slot=_slot(), attempt_number=1, attempt_feedback=None, reference_goal="⊢ True"
    )
    assert "SAFE AUTHORING VIEW" not in rendered
    with pytest.raises(PromptRenderError, match="no SAFE AUTHORING VIEW token"):
        render_proposer_prompt(
            loaded,
            slot=_slot(),
            attempt_number=1,
            attempt_feedback=None,
            reference_goal="⊢ True",
            authoring_view="∀ (p : Prop), p",
        )


# ---- pre-Lean inaccessible-name rejection through the executable root path --------------------


class _Calls:
    def __init__(self, provider_id: str, responses: Sequence[dict[str, object]]) -> None:
        self.provider_id = provider_id
        self.responses = list(responses)
        self.calls: list[str] = []

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> Any:
        from leanfaith.sft2a.providers import ProviderCallResult

        index = len(self.calls)
        self.calls.append(prompt)
        return ProviderCallResult(
            call_key=f"synthetic:{self.provider_id}:{index}",
            provider_id=self.provider_id,
            structured=dict(self.responses[index]),
            usage={"input_tokens": 1, "output_tokens": 1},
            cost_usd=0.0,
            elapsed_seconds=0.0,
            cache_hit=False,
            terminal_path=Path(f"/nonexistent/{self.provider_id}-{index}.json"),
        )


class _V3Oracle(sprint_pilot_v52._SyntheticOracle):
    cache_version = "v3"
    method_version = ORACLE_METHOD_VERSION_V3
    command_template_version = COMMAND_TEMPLATE_VERSION_V3

    def __init__(self, expected_reference_goal: str) -> None:
        super().__init__(expected_reference_goal)
        self.view_calls = 0

    def effective_context(self) -> Any:
        record = {
            "effective_payload": {"context": {"namespace_context": ["Real"]}},
            "scoped_validated": ["Classical"],
            "scoped_dropped": [],
            "plain_opens_dropped": ["Nat"],
            "prelude_line_count": 10,
            "combined_preflight": {"diagnostic_count": 0},
        }
        context = _context(
            namespace_context=("Real",), open_context=(), scoped_context=("Classical",)
        )
        return lean_oracle.EffectiveContextV3(
            raw=context, context=context, fingerprint="fp", record=record
        )

    def authoring_view(
        self,
        declaration_name: str,
        *,
        expected_closed_expr_hash: str,
        expected_level_params: Sequence[str],
    ) -> Any:
        self.view_calls += 1
        return lean_oracle.AuthoringViewResult(
            status="validated",
            declaration_name=declaration_name,
            profile="notation",
            text="∀ {α : Type u_0} [inst : Preorder α] {a b c : α}, a ≤ b → b ≤ c → a ≤ c",
            closed_expr_hash=expected_closed_expr_hash,
            canonical_level_params=tuple(expected_level_params),
            expected_closed_expr_hash=expected_closed_expr_hash,
            expected_level_params=tuple(expected_level_params),
            cache_key="k",
            cache_hit=False,
            lean_requests_executed=1,
            detail="authoring view validated",
            attempts=(),
        )


def test_dagger_candidates_are_rejected_before_lean_and_the_slot_regenerates(
    tmp_path: Path,
) -> None:
    from leanfaith.sft2a.config import load_sft2a_config
    from leanfaith.sft2a.mechanisms import (
        BREAKING_MECHANISMS,
        PRESERVING_MECHANISMS,
        MechanismAssignment,
    )
    from leanfaith.sft2a.pipeline import run_one_root

    base = load_sft2a_config(
        REPO / "configs/sft2a/closure_aware_v5_2_sprint_v3_authoring.yaml", verify_binaries=False
    )
    staging = str(tmp_path / "staging")
    config = base.config.model_copy(
        update={
            "staging_root": staging,
            "run_layout": base.config.run_layout.model_copy(update={"shared_cache_root": staging}),  # type: ignore[union-attr]
        }
    )
    loaded = replace(
        base, config=config, config_hash=hash_canonical(config.model_dump(mode="json"))
    )
    preserving = [
        spec for spec in PRESERVING_MECHANISMS if spec.applicability == "general"
    ] or list(PRESERVING_MECHANISMS)
    breaking = [spec for spec in BREAKING_MECHANISMS if spec.applicability == "general"] or list(
        BREAKING_MECHANISMS
    )
    plan = {
        "preserve_0": MechanismAssignment(
            preserving[0].family, "preserving", preserving[0].instruction, "general", "synthetic"
        ),
        "preserve_1": MechanismAssignment(
            preserving[-1].family, "preserving", preserving[-1].instruction, "general", "synthetic"
        ),
        "break_0": MechanismAssignment(
            breaking[0].family, "breaking", breaking[0].instruction, "general", "synthetic"
        ),
        "break_1": MechanismAssignment(
            breaking[-1].family, "breaking", breaking[-1].instruction, "general", "synthetic"
        ),
    }
    binders = "∀ {α : Type u_0} [inst : Preorder α] {a b c : α}, "
    dagger = f"∀ {{α : Type u_0}} [inst{INACCESSIBLE_NAME_MARK} : Preorder α] {{a b c : α}}, b ≤ c → a ≤ b → a ≤ c"
    proposer = _Calls(
        loaded.config.proposer.provider_id,
        [
            sprint_pilot_v52._proposal("preserving", plan["preserve_0"].family, dagger),
            sprint_pilot_v52._proposal(
                "preserving", plan["preserve_0"].family, binders + "b ≤ c → a ≤ b → a ≤ c"
            ),
            sprint_pilot_v52._proposal(
                "preserving", plan["preserve_1"].family, binders + "a ≤ b ∧ b ≤ c → a ≤ c"
            ),
            sprint_pilot_v52._proposal(
                "breaking", plan["break_0"].family, binders + "a ≤ b → b ≤ c → c ≤ a"
            ),
            sprint_pilot_v52._proposal(
                "breaking", plan["break_1"].family, binders + "a ≤ b → a ≤ c"
            ),
        ],
    )
    judge = _Calls(
        loaded.config.claude_judge.provider_id,
        [
            sprint_pilot_v52._judge("equivalent"),
            sprint_pilot_v52._judge("equivalent"),
            sprint_pilot_v52._judge("non_equivalent"),
            sprint_pilot_v52._judge("non_equivalent"),
        ],
    )
    oracle = _V3Oracle(loaded.config.root.expected_reference_goal_v1)
    result = run_one_root(
        loaded,
        proposer=proposer,
        claude_judge=judge,
        oracle=oracle,
        output_root=tmp_path / "root",
        enforce_expected_reference_goal=True,
        enforce_smoke_ceilings=False,
        mechanism_plan=plan,
        enforce_closure_canaries=False,
    )
    counts = cast(dict[str, object], result.manifest["counts"])
    assert counts["accepted"] == 4
    assert counts["inaccessible_name_rejections"] == 1
    assert counts["lean_invalid_attempts"] == 0
    # The dagger candidate never reached the synthetic oracle: five proposer calls, four
    # elaborations (the rejected slot regenerated), one authoring view per root.
    # (the synthetic oracle also elaborates the reference once)
    assert len(proposer.calls) == 5 and len(oracle.calls) == 5 and oracle.view_calls == 1
    assert "∀ {α : Type u_0} [inst : Preorder α] {a b c : α}" in proposer.calls[0]
    assert "inaccessible_name_rejected" in proposer.calls[1]
    manifest_view = cast(dict[str, object], result.manifest["authoring_view"])
    assert manifest_view["status"] == "validated" and manifest_view["profile"] == "notation"
    assert (
        cast(dict[str, object], result.manifest["lean"])["command_template_version"]
        == COMMAND_TEMPLATE_VERSION_V3
    )
    sidecar_path = result.output_root / "authoring_view.json"
    assert json.loads(sidecar_path.read_text())["authoring_view"]["status"] == "validated"
    attempts = [
        json.loads(line)
        for line in (result.output_root / "attempts/terminal_attempts.jsonl")
        .read_text()
        .splitlines()
    ]
    assert attempts[0]["status"] == "inaccessible_name_rejected" and attempts[0]["lean"] is None
    sidecar_rows = [
        json.loads(line)
        for line in (result.output_root / "new_core/sidecar.jsonl").read_text().splitlines()
    ]
    assert all(row["authoring_view"]["status"] == "validated" for row in sidecar_rows)


# ---- thresholds with attribution --------------------------------------------------------------


def _loaded_doc(tmp_path: Path, **document: object) -> Any:
    sample = tmp_path / "sample.jsonl"
    rows = [{"root": {"root_id": f"r{i}", "source": "mathlib"}} for i in range(20)]
    sample.write_text("".join(json.dumps(row) + "\n" for row in rows))
    from types import SimpleNamespace

    return SimpleNamespace(
        sample_path=sample,
        output_root=tmp_path / "out",
        document={"sprint_role": "canary", **document},
        base=SimpleNamespace(config_hash="h", repo_root=REPO),
        sha256="s",
    )


def _manifest(
    *, invalid: int, prelude: int, copied: int, local: int, requests: int, view: str = "validated"
) -> dict[str, object]:
    return {
        "counts": {
            "lean_invalid_attempts": invalid,
            "lean_invalid_context_prelude": prelude,
            "lean_invalid_copied_inaccessible_name": copied,
            "lean_invalid_candidate_local": local,
            "inaccessible_name_rejections": 2,
            "candidate_attempts": requests,
        },
        "lean": {"candidate_requests": requests},
        "authoring_view": {"status": view},
    }


def _evaluate(
    loaded: Any, manifests: list[dict[str, object]], **kwargs: object
) -> dict[str, object]:
    return evaluate_sprint_pilot_thresholds(
        loaded,
        compaction={"accepted_rows": 60, "self_pairs": 0, "candidate_duplicates": 0},
        replay={"provider_calls_executed": 0, "lean_requests_executed": 0, "reproducible": True},
        generation_wall_seconds=1200.0,
        malformed_injection={"passed": True},
        resume_check={
            "provider_calls_for_completed_roots_after_resume": 0,
            "lean_requests_for_completed_roots_after_resume": 0,
            "manifests_unchanged": True,
        },
        root_manifests=manifests,
        infrastructure={"infrastructure_failure_rate": 0.0},
        **kwargs,  # type: ignore[arg-type]
    )


def test_attributed_gate_makes_raw_rate_nonblocking_and_blocks_on_attributed_classes(
    tmp_path: Path,
) -> None:
    loaded = _loaded_doc(tmp_path)
    manifests = [_manifest(invalid=3, prelude=0, copied=0, local=3, requests=7)] * 20
    evaluation = _evaluate(loaded, manifests, role="canary", attribution_gate=True)
    assert evaluation["lean_invalid_rate"] > 0.25
    assert evaluation["genuine_lean_invalid_rate"] == pytest.approx(3 / 7)
    assert evaluation["failed_checks"] == ["genuine_lean_invalid_below_25pct"]
    assert "lean_invalid_below_25pct" not in evaluation["checks"]
    assert "wall_time_at_most_30min" not in evaluation["checks"]
    assert evaluation["accepted_rows_per_minute"] == pytest.approx(3.0)
    good = [_manifest(invalid=6, prelude=0, copied=0, local=1, requests=7)] * 20
    passed = _evaluate(loaded, good, role="canary", attribution_gate=True)
    assert passed["passed"] is True and passed["lean_invalid_rate"] > 0.25
    assert passed["lean_invalid_rate_raw_is_nonblocking"] is True
    prelude = [_manifest(invalid=1, prelude=1, copied=0, local=0, requests=7)] * 20
    assert _evaluate(loaded, prelude, role="canary", attribution_gate=True)["failed_checks"] == [
        "zero_context_prelude_failures"
    ]
    copied = [_manifest(invalid=1, prelude=0, copied=1, local=0, requests=7)] * 20
    assert _evaluate(loaded, copied, role="canary", attribution_gate=True)["failed_checks"] == [
        "zero_copied_inaccessible_name_failures"
    ]
    assert passed["authoring_view_status_counts"] == {"validated": 20}
    assert passed["inaccessible_name_rejections"] == 40


def test_v2_gate_semantics_are_unchanged_without_attribution(tmp_path: Path) -> None:
    loaded = _loaded_doc(tmp_path, sprint_role="pilot")
    manifests = [_manifest(invalid=3, prelude=0, copied=0, local=3, requests=7)] * 20
    evaluation = _evaluate(loaded, manifests, role="pilot")
    assert evaluation["failed_checks"] == ["lean_invalid_below_25pct"]
    assert "genuine_lean_invalid_below_25pct" not in evaluation["checks"]


# ---- chain: canary launches the first v3 shard; projection is nonblocking for v3 shards ---------


def test_canary_chain_and_nonblocking_projection(tmp_path: Path) -> None:
    from types import SimpleNamespace

    target = tmp_path / "shard_02.json"
    target.write_text("{}")
    canary = SimpleNamespace(
        document={"sprint_role": "canary", "next_shard_config_path": str(target)}
    )
    decision = chain_decision(
        cast(Any, canary), terminal={"status": "complete"}, evaluation={"failed_checks": []}
    )
    assert decision == {
        "action": "launch_next_shard",
        "target": str(target),
        "reason": "canary_passed",
    }
    failed = chain_decision(
        cast(Any, canary),
        terminal={"status": "threshold_failed"},
        evaluation={"failed_checks": ["x"]},
    )
    assert failed["action"] == "stop" and failed["reason"] == "canary_threshold_failed"
    shard = SimpleNamespace(
        document={
            "sprint_role": "shard",
            "shard_index": 2,
            "shard_count": 10,
            "next_shard_config_path": str(target),
            "sprint_deadline_utc": "2000-01-01T00:00:00+00:00",
            "projection_blocking": False,
        }
    )
    late = chain_decision(
        cast(Any, shard),
        terminal={"status": "complete"},
        evaluation={"failed_checks": [], "generation_wall_seconds": 100.0},
    )
    assert (
        late["action"] == "launch_next_shard"
        and late["reason"] == "shard_passed_projection_nonblocking"
    )
    blocking = SimpleNamespace(document={**shard.document, "projection_blocking": True})
    stopped = chain_decision(
        cast(Any, blocking),
        terminal={"status": "complete"},
        evaluation={"failed_checks": [], "generation_wall_seconds": 100.0},
    )
    assert stopped["action"] == "stop" and stopped["reason"] == "projection_exceeds_sprint_window"


# ---- deterministic defect-class selection -------------------------------------------------------


def test_select_by_class_orders_by_score_then_salted_hash_and_honours_caps() -> None:
    candidates = [
        ("a", "mathlib", 6.0),
        ("b", "mathlib", 6.0),
        ("c", "physlib", 5.0),
        ("d", "mathlib", 4.0),
        ("e", "cslib", 0.0),
    ]
    chosen = sprint_repair_v3.select_by_class(
        candidates, count=3, source_caps={"mathlib": 2, "physlib": 1}, salt="s", exclude=set()
    )
    assert set(chosen) == {"a", "b", "c"} and chosen == sprint_repair_v3.select_by_class(
        candidates, count=3, source_caps={"mathlib": 2, "physlib": 1}, salt="s", exclude=set()
    )
    capped = sprint_repair_v3.select_by_class(
        candidates, count=2, source_caps={"mathlib": 1, "physlib": 1}, salt="s", exclude=set()
    )
    assert "c" in capped and len([r for r in capped if r in {"a", "b"}]) == 1
    with pytest.raises(sprint_repair_v3.SprintRepairV3Error):
        sprint_repair_v3.select_by_class(
            candidates, count=5, source_caps={"mathlib": 3, "physlib": 1}, salt="s", exclude=set()
        )


def test_context_risk_and_open_failure_classifier() -> None:
    risk = sprint_repair_v3.context_risk(
        {
            "namespace_context": ["AddChar"],
            "open_context": ["Real", "exp", "hiding"],
            "scoped_context": ["BigOperators"],
        }
    )
    assert risk["lowercase_open_tokens"] == ["exp", "hiding"] and risk["score"] > 6
    pair = sprint_repair_v3.context_risk(
        {
            "namespace_context": [],
            "open_context": ["Category", "CategoryTheory"],
            "scoped_context": [],
        }
    )
    assert pair["prefix_pairs"] == [["Category", "CategoryTheory"]]
    assert sprint_repair_v3.is_open_rendering_failure(
        "unknown namespace `Category`; Variable name x"
    )
    assert sprint_repair_v3.is_open_rendering_failure(
        "unexpected token 'hiding'; expected 'private'"
    )
    assert not sprint_repair_v3.is_open_rendering_failure("expected token")
    assert not sprint_repair_v3.is_open_rendering_failure("Application type mismatch")


def test_select_adversarial_roots_is_deterministic_from_durable_artifacts() -> None:
    def row(
        root_id: str, goal: str, opens: list[str], scoped: list[str], source: str = "mathlib"
    ) -> dict[str, object]:
        return {
            "root": {
                "root_id": root_id,
                "source": source,
                "compile_context": {
                    "namespace_context": ["N"],
                    "open_context": opens,
                    "scoped_context": scoped,
                },
            },
            "certified_reference": {"goal_v1": goal},
        }

    rows = [
        row("d1", "inst✝ : A\ninst✝¹ : B\n⊢ P", [], []),
        row("d2", "inst✝ : A\n⊢ P", [], []),
        row("d3", "x✝ : A\ny✝ : B\nz✝ : C\n⊢ P", [], []),
        row("c1", "⊢ P", ["Category", "CategoryTheory"], []),
        row("c2", "⊢ P", ["Real", "exp", "hiding"], []),
        row("s1", "⊢ P", [], ["ENNReal", "NNReal"]),
        row("s2", "⊢ P", [], ["Topology"]),
        row("k1", "⊢ P", [], [], source="compiler_data"),
    ]
    attempts = {
        "c1": [{"status": "lean_invalid", "lean": {"detail": "unknown namespace `Category`"}}] * 3,
        "c2": [
            {"status": "lean_invalid", "lean": {"detail": "unexpected token 'hiding'; expected"}}
        ]
        * 2,
        "d1": [{"status": "lean_invalid", "lean": {"detail": "expected token"}}],
    }
    selection = sprint_repair_v3.select_adversarial_roots(
        rows, attempts, dagger_roots=2, context_roots=3
    )
    assert selection == {"dagger_heavy": ["d3", "d1"], "context": ["c1", "c2", "s1"]}
    assert selection == sprint_repair_v3.select_adversarial_roots(
        rows, attempts, dagger_roots=2, context_roots=3
    )


# ---- config contracts ---------------------------------------------------------------------------


def _write_sprint_config(tmp_path: Path, **overrides: object) -> Path:
    sample = tmp_path / "sample.jsonl"
    sample.write_text(json.dumps({"root": {"root_id": "r1"}}) + "\n")
    completed = tmp_path / "completed.jsonl"
    completed.write_text("")
    document: dict[str, object] = {
        "version": "leanfaith_sft2a_provider_rehearsal_v5_2_sprint_pilot_v1",
        "status": "sprint_authorized",
        "authorized": True,
        "base_config_path": "configs/sft2a/closure_aware_v5_2_sprint_v3_authoring.yaml",
        "base_config_sha256": "797e8cabd16420c6f4a22a54f645381346fced84e3ff20540018d5535450d972",
        "labeling_defaults_policy_path": "policies/sft2_llm_labeling_defaults_v1.yaml",
        "labeling_defaults_policy_sha256": "4554a071b06b1af9015b253b5e64b2a0a4d013630e5224ef7729bbf65757646f",
        "sample_path": str(sample),
        "sample_sha256": __import__("leanfaith.config.hashing", fromlist=["hash_file"]).hash_file(
            sample
        ),
        "completed_root_sample_paths": [str(completed)],
        "provider_output_root": str(tmp_path / "run"),
        "tmux_session": "t",
        "resource_task": "T",
        "sprint_role": "canary",
        "oracle_cache_version": "v3",
        "oracle_v3_gate_receipt_path": str(tmp_path / "gate.json"),
        "maximum_root_workers": 1,
        "maximum_total_lean_workers": 1,
        "maximum_measured_rss_gib": 16.0,
        "provider_concurrency": 16,
        "kimi_audit_rows": 8,
        "ceilings": {
            "maximum_roots": 1,
            "maximum_provider_calls": 736,
            "maximum_proposer_calls": 240,
            "maximum_opus_calls": 480,
            "maximum_lemex_calls": 16,
            "maximum_attempts_per_slot": 3,
            "maximum_reported_opus_spend_usd": 40.0,
            "codex_cost_status": "unavailable",
            "lemex_cost_status": "unavailable",
        },
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "scale_10k_authorized": False,
        "scale_50k_authorized": False,
        "training_authorized": False,
    }
    document.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document))
    return path


def test_canary_config_contract(tmp_path: Path) -> None:
    loaded = load_provider_rehearsal_v52(_write_sprint_config(tmp_path))
    assert loaded.kind == "sprint" and loaded.document["sprint_role"] == "canary"
    with pytest.raises(Exception, match="exactly one persistent Lean worker at 16 GiB"):
        load_provider_rehearsal_v52(_write_sprint_config(tmp_path, maximum_total_lean_workers=2))
    with pytest.raises(Exception, match="oracle_v3_gate_receipt_path"):
        load_provider_rehearsal_v52(
            _write_sprint_config(tmp_path, oracle_v3_gate_receipt_path=None)
        )
    with pytest.raises(Exception, match="oracle_cache_version"):
        load_provider_rehearsal_v52(_write_sprint_config(tmp_path, oracle_cache_version="v9"))


def test_repair_plan_and_generated_canary_config_load_if_present() -> None:
    plan = sprint_repair_v3.load_repair_plan_v3(REPO / "configs/sft2a/sprint_repair_v3_plan.json")
    assert plan.shards_v3["first_shard"] == 2 and plan.canary["dagger_roots"] == 10
    assert "{{AUTHORING_VIEW}}" in plan.base.proposer_prompt
    canary = REPO / "configs/sft2a/sprint_canary_20roots_v3.json"
    if canary.is_file():
        loaded = load_provider_rehearsal_v52(canary)
        assert loaded.document["oracle_cache_version"] == "v3"
        assert loaded.document["sprint_role"] == "canary"
        mix = cast(dict[str, int], loaded.document["expected_source_mix"])
        assert sum(mix.values()) == 20 and mix.get("compiler_data", 0) == 0


def test_signature_oracle_result_defaults_attribution_to_none() -> None:
    result = SignatureOracleResult(
        status="valid",
        cache_key="k",
        cache_hit=True,
        signature_sha256="s",
        goal_v1="⊢ True",
        sidecar=None,
        lean_status="valid",
        request_hash=None,
        elapsed_ms=0,
        raw_response_path=None,
        detail="d",
    )
    assert result.attribution is None
    literal: Literal["candidate_local"] = "candidate_local"
    assert lean_oracle._attribution_literal(literal) == "candidate_local"
    assert lean_oracle._attribution_literal("bogus") is None


# ---- in-run checkpoint and nonblocking candidate-local rate ---------------------------------------


def test_nonblocking_genuine_rate_and_contamination_check(tmp_path: Path) -> None:
    loaded = _loaded_doc(tmp_path, sprint_role="shard")
    manifests = [_manifest(invalid=3, prelude=0, copied=0, local=3, requests=7)] * 20
    evaluation = _evaluate(
        loaded,
        manifests,
        role="canary",
        attribution_gate=True,
        genuine_rate_blocking=False,
        accepted_contamination=0,
    )
    assert evaluation["passed"] is True
    assert "genuine_lean_invalid_below_25pct" not in evaluation["checks"]
    assert evaluation["genuine_lean_invalid_below_25pct_telemetry"] is False
    assert evaluation["checks"]["zero_accepted_contamination"] is True
    tainted = _evaluate(
        loaded,
        manifests,
        role="canary",
        attribution_gate=True,
        genuine_rate_blocking=False,
        accepted_contamination=1,
    )
    assert tainted["failed_checks"] == ["zero_accepted_contamination"]


def test_in_run_checkpoint_from_durable_root_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from leanfaith.config.hashing import hash_file
    from leanfaith.sft2a.parallel_rehearsal import ParallelRootStateMachine
    from leanfaith.sft2a.provider_rehearsal_v52 import _root_output

    base = _loaded_with_prompt("x")
    out = tmp_path / "run"
    loaded = SimpleNamespace(
        output_root=out,
        base=base,
        sample_path=tmp_path / "s.jsonl",
        document={},
        sha256="s",
        ceilings=None,
    )
    (tmp_path / "s.jsonl").write_text(
        "".join(json.dumps({"root": {"root_id": root_id}}) + "\n" for root_id in ("r1", "r2"))
    )
    states = ParallelRootStateMachine(out / "root_state.jsonl", maximum_workers=8)
    rows: list[tuple[str, str, str]] = [
        ("r1", "⊢ A", "h1"),
        ("r1", "⊢ B", "h2"),
        ("r1", "⊢ A1", "h1b"),
        ("r2", "⊢ C", "h3"),
        ("r2", "⊢ D", "h4"),
        ("r2", "⊢ C1", "h3b"),
    ]
    for root_id in ("r1", "r2"):
        states.claim(root_id=root_id, worker_id="w")
        root_out = _root_output(cast(Any, loaded), root_id)
        (root_out / "new_core").mkdir(parents=True)
        core = [
            json.dumps({"reference": "⊢ R", "candidate": goal, "label": True})
            for r, goal, _h in rows
            if r == root_id
        ]
        side = [
            json.dumps(
                {
                    "row_id": h,
                    "candidate_closed_expr_hash": h,
                    "reference_closed_expr_hash": "ref",
                    "raw_candidate_signature": goal,
                }
            )
            for r, goal, h in rows
            if r == root_id
        ]
        (root_out / "new_core/core.jsonl").write_text("".join(line + "\n" for line in core))
        (root_out / "new_core/sidecar.jsonl").write_text("".join(line + "\n" for line in side))
        manifest = {
            "counts": {
                "accepted": 3,
                "lean_invalid_attempts": 1,
                "lean_invalid_context_prelude": 0,
                "lean_invalid_copied_inaccessible_name": 0,
                "lean_invalid_candidate_local": 1,
                "inaccessible_name_rejections": 0,
                "candidate_attempts": 3,
            },
            "lean": {"candidate_requests": 3},
            "llm": {"usage": []},
        }
        (root_out / "manifest.json").write_text(json.dumps(manifest))
        states.complete(
            root_id=root_id, worker_id="w", manifest_hash=hash_file(root_out / "manifest.json")
        )
    monkeypatch.setattr(
        sprint_pilot_v52,
        "measure_infrastructure_failures",
        lambda _loaded: {"infrastructure_failure_rate": 0.0, "infrastructure_failures": 0},
    )
    checkpoint = sprint_pilot_v52.in_run_checkpoint_v52(cast(Any, loaded), checkpoint_roots=2)
    assert checkpoint["completed_roots"] == 2 and checkpoint["accepted_rows"] == 6
    assert checkpoint["passed"] is True, checkpoint["failed_checks"]
    assert checkpoint["genuine_lean_invalid_rate"] == pytest.approx(1 / 3)
    # Tamper with one manifest: terminal accounting must break the checkpoint.
    root_out = _root_output(cast(Any, loaded), "r2")
    (root_out / "manifest.json").write_text(
        json.dumps({"counts": {}, "lean": {}, "llm": {"usage": []}})
    )
    broken = sprint_pilot_v52.in_run_checkpoint_v52(cast(Any, loaded), checkpoint_roots=2)
    assert "terminal_accounting_intact" in broken["failed_checks"]
