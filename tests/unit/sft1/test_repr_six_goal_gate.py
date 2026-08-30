"""Lean-free safety and freeze tests for the SFT1 six-real-goal gate."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.representations.goal_v1 import (
    ClosedExprFailure,
    ClosedExprInput,
    ClosedExprProvenance,
    ClosedExprRecord,
    ClosedExprSidecar,
    RendererImplementationIdentity,
)
from leanfaith.sft1 import repr_six_goal_gate as gate_module
from leanfaith.sft1.repr_six_goal_gate import (
    EXPECTED_CASE_IDS,
    EXPECTED_EVIDENCE_BUNDLE_DIRECTORY,
    EXPECTED_EVIDENCE_BUNDLE_MANIFEST_FILE_SHA256,
    EXPECTED_EVIDENCE_DIRECTORY,
    EXPECTED_EXECUTION_CONFIG_FILE_SHA256,
    EXPECTED_EXECUTION_CONFIG_HASH,
    EXPECTED_EXECUTION_CONFIG_PATH,
    EXPECTED_GATE_CONFIG_FILE_SHA256,
    EXPECTED_GATE_CONFIG_HASH,
    EXPECTED_HELPER_FILE_SHA256,
    EXPECTED_HELPER_PREAMBLE_SHA256,
    EXPECTED_RECEIPT_FILE_SHA256,
    EXPECTED_RECEIPT_HASH,
    EXPECTED_RECEIPT_PATH,
    GateCaseOutcome,
    GateValidationError,
    ReferenceMode,
    SixGoalGateConfig,
    SixGoalGateResult,
    build_compile_context,
    build_session_body,
    freeze_passed_gate_receipt,
    load_six_goal_gate,
    run_six_goal_gate,
)

_REPO_ROOT = find_repo_root(Path(__file__))


def _receipt_payload() -> dict[str, Any]:
    payload: object = json.loads((_REPO_ROOT / EXPECTED_RECEIPT_PATH).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _result_from_frozen_receipt() -> SixGoalGateResult:
    receipt = _receipt_payload()
    cases = load_six_goal_gate(EXPECTED_EXECUTION_CONFIG_PATH).config.cases
    receipt_cases = receipt["cases"]
    assert isinstance(receipt_cases, list)
    outcomes = tuple(
        GateCaseOutcome(
            case_id=case.case_id,
            source=case.source_family,
            family=case.family_id,
            operation=case.operation_id,
            polarity=case.polarity,
            passed=True,
            exact_failure_class=None,
            request_hash=receipt_case["request_hash"],
            elapsed_ms=receipt_case["elapsed_ms"],
            evidence_path=receipt_case["evidence_path"],
            evidence_sha256=receipt_case["evidence_sha256"],
            failure_details=(),
            diagnostic_rendered_goals={},
        )
        for case, receipt_case in zip(cases, receipt_cases, strict=True)
    )
    return SixGoalGateResult(
        gate_id="sft1_repr_six_real_goal_direct_expr_v0_3_1",
        config_hash=EXPECTED_EXECUTION_CONFIG_HASH,
        config_file_sha256=EXPECTED_EXECUTION_CONFIG_FILE_SHA256,
        helper_file_sha256=EXPECTED_HELPER_FILE_SHA256,
        helper_preamble_sha256=EXPECTED_HELPER_PREAMBLE_SHA256,
        outcomes=outcomes,
        passed=True,
        stopped_case_id=None,
        evidence_directory=str(EXPECTED_EVIDENCE_DIRECTORY),
    )


def _sidecars_with_candidate_goal(candidate_goal: str) -> tuple[ClosedExprSidecar, ...]:
    loaded = load_six_goal_gate(EXPECTED_EXECUTION_CONFIG_PATH)
    case = loaded.config.cases[0]
    compile_context = build_compile_context(case, helper_preamble=loaded.helper_preamble)
    reference_input, candidate_input = gate_module._inputs(case)
    implementation = RendererImplementationIdentity(
        renderer_semantic_hash="1" * 64,
        lean_renderer_sha256="2" * 64,
        injected_helper_sha256="3" * 64,
        python_module_sha256="4" * 64,
        config_file_sha256="5" * 64,
        implementation_set_hash="6" * 64,
    )

    def make_sidecar(
        *,
        role: Literal["reference", "candidate"],
        goal: str,
        endpoint_input: ClosedExprInput,
    ) -> ClosedExprSidecar:
        provenance = ClosedExprProvenance(
            expr_hash=("7" if role == "reference" else "8") * 64,
            expr_hash_algorithm="sha256_canonical_closed_expr_alpha_tree_v1",
            input_level_params=(),
            canonical_level_params=(),
            universe_profile_id="goal_v1_first_occurrence_u_i_v1",
            universe_profile_hash="9" * 64,
            render_scope_id="sft1-repr-six:test",
            render_context_id="goal_v1_render_context_v1",
            render_context_hash="a" * 64,
            route_id="closed_expr_in_session",
            expr_origin=endpoint_input.expr_origin,
        )
        return ClosedExprSidecar(
            record=ClosedExprRecord(
                representation_id=f"repr:test:{role}",
                goal_v1=goal,
                goal_v1_source="closed_prop_expr",
                renderer_version="goal_v1.0",
                spec_hash="b" * 64,
                compile_context_id=compile_context.compile_context_id,
                endpoint_id=endpoint_input.endpoint_id,
                endpoint_role=role,
                source_material_hash=endpoint_input.source_material.material_hash,
                rendered_goal_hash=sha256_hex(goal.encode("utf-8")),
                provenance=provenance,
                implementation_identity=implementation,
            ),
            source_material=endpoint_input.source_material,
            compile_context=compile_context,
        )

    return (
        make_sidecar(
            role="reference",
            goal="x : Nat\n⊢ x = x",
            endpoint_input=reference_input,
        ),
        make_sidecar(
            role="candidate",
            goal=candidate_goal,
            endpoint_input=candidate_input,
        ),
    )


def test_checked_in_gate_config_and_fixed_preamble_are_hash_bound() -> None:
    loaded = load_six_goal_gate()

    assert loaded.config_hash == EXPECTED_GATE_CONFIG_HASH
    assert loaded.config_file_sha256 == EXPECTED_GATE_CONFIG_FILE_SHA256
    assert hash_file(loaded.path) == EXPECTED_GATE_CONFIG_FILE_SHA256
    assert loaded.helper_file_sha256 == EXPECTED_HELPER_FILE_SHA256
    assert hash_file(loaded.helper_path) == EXPECTED_HELPER_FILE_SHA256
    assert loaded.helper_preamble_sha256 == EXPECTED_HELPER_PREAMBLE_SHA256
    assert "import Lean" not in loaded.helper_preamble
    assert loaded.config.fixed_preamble.review_status == "reviewed_for_bounded_six_goal_gate"
    assert tuple(case.case_id for case in loaded.config.cases) == EXPECTED_CASE_IDS


def test_checked_in_execution_artifact_preserves_the_exact_preimage() -> None:
    loaded = load_six_goal_gate(EXPECTED_EXECUTION_CONFIG_PATH)

    assert loaded.config.status == "pending_execution"
    assert loaded.path == _REPO_ROOT / EXPECTED_EXECUTION_CONFIG_PATH
    assert loaded.config_file_sha256 == EXPECTED_EXECUTION_CONFIG_FILE_SHA256
    assert loaded.config_hash == EXPECTED_EXECUTION_CONFIG_HASH
    assert loaded.config.receipt_binding.passed is False
    assert loaded.config.receipt_binding.receipt_path is None


def test_gate_keeps_later_scale_authorization_closed_and_freezes_its_receipt() -> None:
    config = load_six_goal_gate().config

    assert config.authorization.repr_dependency_integration is True
    assert config.authorization.six_real_goal_gate is True
    assert config.authorization.one_example_gate is False
    assert config.authorization.hundred_root_gate is False
    assert config.authorization.ten_k_pilot is False
    assert config.authorization.row_generation is False
    assert config.authorization.bulk_scale is False
    assert config.authorization.publication is False
    assert config.status == "passed"
    receipt_path = config.receipt_binding.receipt_path
    assert receipt_path is not None
    assert Path(receipt_path) == EXPECTED_RECEIPT_PATH
    assert hash_file(_REPO_ROOT / EXPECTED_RECEIPT_PATH) == EXPECTED_RECEIPT_FILE_SHA256
    assert config.receipt_binding.receipt_file_sha256 == EXPECTED_RECEIPT_FILE_SHA256
    assert config.receipt_binding.regression_id == config.gate_id
    assert config.receipt_binding.receipt_hash == EXPECTED_RECEIPT_HASH
    assert (
        config.receipt_binding.execution_config_file_sha256 == EXPECTED_EXECUTION_CONFIG_FILE_SHA256
    )
    assert config.receipt_binding.execution_config_hash == EXPECTED_EXECUTION_CONFIG_HASH
    assert config.receipt_binding.passed is True
    assert config.receipt_binding.repr_consistency_check_receipt_is_substitutable is False


def test_frozen_receipt_replays_every_git_local_evidence_file() -> None:
    load_six_goal_gate()
    receipt = _receipt_payload()
    receipt_cases = receipt["cases"]
    assert isinstance(receipt_cases, list)
    assert len(receipt_cases) == 6
    bundle_directory = _REPO_ROOT / EXPECTED_EVIDENCE_BUNDLE_DIRECTORY
    manifest_path = bundle_directory / "manifest.json"
    assert hash_file(manifest_path) == EXPECTED_EVIDENCE_BUNDLE_MANIFEST_FILE_SHA256
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["successful_case_evidence_claim"] == ("no_forbidden_rendered_residue_survived")
    assert manifest["live_adversarial_rejection_probes_per_forbidden_string"] is False

    total_elapsed_ms = 0
    total_sidecar_bytes = 0
    for ordinal, (case_id, receipt_case, manifest_case) in enumerate(
        zip(EXPECTED_CASE_IDS, receipt_cases, manifest["cases"], strict=True),
        start=1,
    ):
        assert isinstance(receipt_case, dict)
        assert manifest_case["case_id"] == case_id
        evidence_path = bundle_directory / f"{ordinal:02d}_{case_id}.json"
        assert hash_file(evidence_path) == manifest_case["bundle_file_sha256"]
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        canonical = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert sha256_hex(canonical) == receipt_case["evidence_sha256"]
        assert sha256_hex(canonical) == manifest_case["canonical_evidence_sha256"]
        goals = tuple(sidecar["record"]["goal_v1"] for sidecar in evidence["complete_sidecars"])
        assert all("[anonymous]" not in goal and "⋯" not in goal for goal in goals)
        total_elapsed_ms += evidence["request"]["elapsed_ms"]
        total_sidecar_bytes += evidence["measurements"]["complete_sidecar_bytes_per_pair"]

    assert total_elapsed_ms == 21_546
    assert total_sidecar_bytes == 119_895


def test_checked_in_gate_replay_never_reads_the_storage_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_hash_file = hash_file

    def reject_storage_path(path: Path) -> str:
        if str(path).startswith("/storage/"):
            raise AssertionError("Git-local replay attempted to read storage evidence")
        return real_hash_file(path)

    monkeypatch.setattr(gate_module, "hash_file", reject_storage_path)
    load_six_goal_gate()


def test_freezing_the_replayed_result_reproduces_the_semantic_receipt(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"

    regression_id, receipt_hash = freeze_passed_gate_receipt(
        _result_from_frozen_receipt(), receipt_path
    )

    assert regression_id == "sft1_repr_six_real_goal_direct_expr_v0_3_1"
    assert receipt_hash == EXPECTED_RECEIPT_HASH
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["receipt_hash"] == EXPECTED_RECEIPT_HASH


def test_freeze_rejects_a_passing_result_with_a_tampered_evidence_hash(
    tmp_path: Path,
) -> None:
    result = _result_from_frozen_receipt()
    outcomes = list(result.outcomes)
    outcomes[0] = replace(outcomes[0], evidence_sha256="0" * 64)

    with pytest.raises(ValueError, match="evidence bundle case binding mismatch"):
        freeze_passed_gate_receipt(
            replace(result, outcomes=tuple(outcomes)),
            tmp_path / "receipt.json",
        )


def test_replay_rejects_a_missing_git_bundle_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_directory = Path("configs") / "missing-six-goal-bundle"
    monkeypatch.setattr(
        gate_module,
        "EXPECTED_EVIDENCE_BUNDLE_DIRECTORY",
        missing_directory,
    )

    with pytest.raises(ValueError, match="Git evidence bundle is unavailable"):
        load_six_goal_gate()
    assert not (tmp_path / "receipt.json").exists()


def test_evidence_replay_rejects_projection_drift(tmp_path: Path) -> None:
    config = load_six_goal_gate(EXPECTED_EXECUTION_CONFIG_PATH)
    receipt_case = dict(_receipt_payload()["cases"][0])
    original_path = _REPO_ROOT / EXPECTED_EVIDENCE_BUNDLE_DIRECTORY / "01_mathlib_add_pow.json"
    payload = json.loads(original_path.read_text(encoding="utf-8"))
    payload["model_facing_projection"]["reference"] += " drift"
    evidence_directory = tmp_path / "evidence"
    evidence_directory.mkdir()
    evidence_path = evidence_directory / original_path.name
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence_path.write_bytes(encoded)
    receipt_case["evidence_sha256"] = sha256_hex(encoded)

    with pytest.raises(ValueError, match="model-facing projection"):
        gate_module._verify_frozen_evidence(
            receipt_case,
            case=config.config.cases[0],
            config=config.config,
            helper_preamble=config.helper_preamble,
            evidence_path_override=evidence_path,
            evidence_file_sha256=hash_file(evidence_path),
        )


def test_exact_source_routes_and_project_contexts_are_frozen() -> None:
    cases = load_six_goal_gate().config.cases

    assert tuple(case.reference.mode for case in cases) == (
        ReferenceMode.IMPORTED_CONSTANT,
        ReferenceMode.IMPORTED_CONSTANT,
        ReferenceMode.IMPORTED_CONSTANT,
        ReferenceMode.IMPORTED_CONSTANT,
        ReferenceMode.EXTRACTED_SIGNATURE,
        ReferenceMode.EXTRACTED_SIGNATURE,
    )
    assert tuple(case.reference.constant_name for case in cases[:4]) == (
        "add_pow",
        "ClassicalMechanics.FreeParticle.kineticEnergy_conserved",
        "Cslib.Algorithms.Lean.TimeM.ret_merge",
        "Lean.Sym.Int.lt_eq_true",
    )
    assert cases[3].source_path == "Init/Sym/Lemmas.lean"
    assert tuple(case.backend_id for case in cases) == (
        "mathlib_bigoperators",
        "physlib_default",
        "cslib_default",
        "lean_compiler_default",
        "mathlib_default",
        "mathlib_bigoperators",
    )
    assert all(case.family_id == "P01" for case in cases)
    assert all(case.operation_id == "P01_ALPHA_RENAME_SINGLE_V1" for case in cases)
    assert all(case.polarity == "positive" for case in cases)
    assert all(case.production_admission is False for case in cases)
    assert all(case.namespace_context == () and case.open_context == () for case in cases)
    assert tuple(case.scoped_context for case in cases) == (
        ("BigOperators",),
        (),
        (),
        (),
        (),
        ("BigOperators",),
    )
    mathlib_bigoperators_cases = (cases[0], cases[5])
    assert (
        len(
            {
                build_compile_context(case, helper_preamble="p").fingerprint
                for case in mathlib_bigoperators_cases
            }
        )
        == 1
    )
    assert (
        build_compile_context(cases[4], helper_preamble="p").fingerprint
        != build_compile_context(cases[0], helper_preamble="p").fingerprint
    )
    assert all(case.options == {"Elab.async": False, "autoImplicit": False} for case in cases)
    assert all(case.source_syntax_normalization_id is None for case in cases[:-1])
    assert cases[-1].source_syntax_normalization_id == "consistencycheck_typed_finset_sum_syntax_v1"
    assert cases[-1].source_syntax_normalization_rule == (
        "replace_legacy_typed_finset_sum_in_binder_with_membership_binder_and_typed_lower_bound_v1"
    )
    assert (
        cases[-1].normalized_proposition_sha256
        == "039397842c7fa4d5f749d424126e153230f3a24ea3113dae02c33b6354a18f0a"
    )
    assert (
        cases[4].source_file_sha256
        == "a0c4d102a0ea4d2923cca85129c6cda054a11b1854462eed3d7e71e555b703ea"
    )
    assert (
        cases[4].source_formal_statement_sha256
        == "8b0061199a23b47539e6f30df775109d5c6776ea1c2206f452d5a9d48240aa7e"
    )


def test_cslib_does_not_activate_time_m_notation() -> None:
    loaded = load_six_goal_gate()
    case = loaded.config.cases[2]
    context = build_compile_context(case, helper_preamble=loaded.helper_preamble)
    body = build_session_body(case, render_scope_id="sft1-repr-six:cslib_ret_merge")

    assert case.case_id == "cslib_ret_merge"
    assert context.open_context == ()
    assert context.scoped_context == ()
    assert "open TimeM" not in context.command_preamble
    assert "open scoped TimeM" not in context.command_preamble
    assert "Cslib.Algorithms.Lean.TimeM.ret_merge" in body


def test_every_pair_is_one_unrolled_run_meta_with_one_emitter_per_endpoint() -> None:
    loaded = load_six_goal_gate()
    for case in loaded.config.cases:
        scope = f"sft1-repr-six:{case.case_id}"
        body = build_session_body(case, render_scope_id=scope)
        assert body.count("run_meta do") == 1
        assert body.lstrip().startswith("run_meta do")
        assert body.count("LeanFaith.GoalV1.emitClosedProp") == 2
        assert body.count(f'"{case.case_id}.reference"') == 1
        assert body.count(f'"{case.case_id}.candidate"') == 1
        assert body.count(f'"{scope}"') == 2
        assert "LeanFaith.GoalV1.renderClosedProp" not in body
        assert "Term.elabTerm" not in body
        assert "Parser.runParserCategory" not in body
        assert "ppExpr" not in body
        assert "ppGoal" not in body
        assert "addDecl" not in body
        assert ":= by" not in body
        assert "sorry" not in body
        assert "theorem " not in body
        assert "axiom " not in body
        assert "example " not in body


def test_signature_elaboration_is_confined_to_one_fixed_helper() -> None:
    loaded = load_six_goal_gate()
    helper = loaded.helper_preamble
    source = loaded.helper_path.read_text(encoding="utf-8")

    assert "TermElabM.run'" in helper
    assert "Term.elabTerm" in helper
    assert "elaborateReferenceProp" in helper
    assert "importedTheoremType" in helper
    assert "renderClosedProp" not in helper
    assert "ppGoal" not in helper
    assert "addDecl" not in helper
    assert "Name.anonymous" not in helper
    assert "structure " not in source
    assert "inductive " not in source
    for case in loaded.config.cases[-2:]:
        body = build_session_body(case, render_scope_id=f"sft1-repr-six:{case.case_id}")
        assert "elaborateReferenceProp" in body
        assert "Term.elabTerm" not in body


def test_structural_candidate_and_p23_allocator_are_hygienic_static_regressions() -> None:
    helper = load_six_goal_gate().helper_preamble
    alpha_start = helper.index("def alphaRenameGateCandidate")
    p23_start = helper.index("private partial def p23FreshBinderName")
    p23_end = helper.index("end LeanFaith.SFT1.RepresentationGate")
    alpha_slice = helper[alpha_start:p23_start]
    p23_slice = helper[p23_start:p23_end]

    assert ".forallE newName domain body binderInfo" in helper
    assert "isDefEq source candidate" in alpha_slice
    assert 'checkedClosedProp "alpha candidate"' in alpha_slice
    assert "Name.mkSimple" in p23_slice
    assert "used.contains candidate.toString" in p23_slice
    assert "candidate.isAnonymous" in p23_slice
    assert "assertP23BinderAllocatorHygiene" in p23_slice
    assert "Name.anonymous" not in p23_slice


def test_renderer_contract_and_forbidden_output_tokens_are_exact() -> None:
    config = load_six_goal_gate().config
    binding = config.repr_binding
    execution = config.execution_contract

    assert binding.freeze_commit == "176a783842c5a73b84413dfa8347670608b615d9"
    assert binding.route_id == "closed_expr_in_session"
    assert binding.python_entrypoint == "render_closed_expr_in_session"
    assert binding.endpoint_emitter == "LeanFaith.GoalV1.emitClosedProp"
    assert binding.model_facing_projection == "sidecar.core_text()"
    assert binding.universe_profile_id == "goal_v1_first_occurrence_u_i_v1"
    assert binding.render_context_id == "goal_v1_render_context_v1"
    assert execution.complete_sidecars_persisted is True
    assert execution.exact_turnstile_count == 1
    assert execution.reference_candidate_render_must_differ is True
    assert execution.forbidden_render_substrings == ("[anonymous]", "⋯")
    assert execution.cslib_time_m_scope_must_remain_closed is True


@pytest.mark.parametrize(
    ("forbidden", "exact_failure_class"),
    (
        ("[anonymous]", "anonymous_binder_name"),
        ("⋯", "forbidden_rendered_placeholder"),
    ),
)
def test_rendered_forbidden_residue_has_its_exact_failure_class(
    forbidden: str,
    exact_failure_class: str,
) -> None:
    case = load_six_goal_gate(EXPECTED_EXECUTION_CONFIG_PATH).config.cases[0]
    sidecars = _sidecars_with_candidate_goal(f"y : Nat\n⊢ y = {forbidden}")

    with pytest.raises(GateValidationError) as captured:
        gate_module._validate_sidecars(
            case,
            sidecars,
            forbidden_substrings=("[anonymous]", "⋯"),
        )

    assert captured.value.exact_failure_class == exact_failure_class


def test_closed_expr_failure_detail_maps_rendered_placeholder_exactly() -> None:
    failures = (
        ClosedExprFailure(
            endpoint_id="mathlib_add_pow.candidate",
            detail="candidate contains rendered placeholder ⋯",
        ),
    )

    assert gate_module._classify_closed_expr_failures(failures) == (
        "forbidden_rendered_placeholder"
    )


def test_typed_model_rejects_extra_fields_and_cslib_scope_drift() -> None:
    payload = load_six_goal_gate().config.model_dump(mode="python")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SixGoalGateConfig.model_validate(payload)

    payload = load_six_goal_gate().config.model_dump(mode="python")
    payload["cases"][2]["scoped_context"] = ["TimeM"]
    with pytest.raises(ValidationError, match="TimeM notation scope"):
        SixGoalGateConfig.model_validate(payload)

    payload = load_six_goal_gate().config.model_dump(mode="python")
    payload["cases"][-1]["normalized_proposition_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="normalized proposition hash does not replay"):
        SixGoalGateConfig.model_validate(payload)


def test_frozen_gate_refuses_new_execution_before_creating_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="receipt is frozen; execution is closed"):
        run_six_goal_gate({}, evidence_directory=evidence)
    assert not evidence.exists()


def test_failed_or_incomplete_gate_cannot_freeze_receipt(tmp_path: Path) -> None:
    failed = SixGoalGateResult(
        gate_id="sft1_repr_six_real_goal_direct_expr_v0_3_0",
        config_hash=EXPECTED_GATE_CONFIG_HASH,
        config_file_sha256=EXPECTED_GATE_CONFIG_FILE_SHA256,
        helper_file_sha256=EXPECTED_HELPER_FILE_SHA256,
        helper_preamble_sha256=EXPECTED_HELPER_PREAMBLE_SHA256,
        outcomes=(
            GateCaseOutcome(
                case_id="mathlib_add_pow",
                source="Mathlib",
                family="P01",
                operation="P01_ALPHA_RENAME_SINGLE_V1",
                polarity="positive",
                passed=False,
                exact_failure_class="reference_render_failure",
                request_hash="0" * 64,
                elapsed_ms=0,
                evidence_path=None,
                evidence_sha256=None,
                failure_details=("not run",),
                diagnostic_rendered_goals={},
            ),
        ),
        passed=False,
        stopped_case_id="mathlib_add_pow",
        evidence_directory=str(tmp_path),
    )
    receipt = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="cannot freeze"):
        freeze_passed_gate_receipt(failed, receipt)
    assert not receipt.exists()


def test_no_gate_module_side_effect_or_leaninteract_import() -> None:
    source = (_REPO_ROOT / "src/leanfaith/sft1/repr_six_goal_gate.py").read_text(encoding="utf-8")

    assert "lean_interact" not in source
    assert "LeanInteractBackend" not in source
    assert "if __name__ ==" not in source
    assert "render_closed_expr_in_session(" in source
    assert "complete_sidecars = [reference.to_dict(), candidate.to_dict()]" in source
    assert '"complete_sidecars": complete_sidecars' in source
    assert '"complete_sidecar_bytes_per_pair": complete_sidecar_bytes' in source
    assert '"exact_failure_class": exact_failure_class' in source
    assert '"operation": case.operation_id' in source
    assert '"polarity": case.polarity' in source
    assert '"reference": reference.core_text()' in source
    assert '"candidate": candidate.core_text()' in source


def test_fixed_helper_accepts_hygienic_source_syntax_binders() -> None:
    source = (_REPO_ROOT / "LeanFaith/Meta/SFT1/RepresentationGate.lean").read_text(
        encoding="utf-8"
    )

    assert "if binderInfo == .default && !name.isAnonymous then" in source
    assert "!name.hasMacroScopes" not in source
    assert "let candidate := Name.mkSimple text" in source
    assert "used.contains candidate.toString" in source
    assert "canonicalizeBinderMetadata #[] e" in source
    assert "freshDisplayedName name.eraseMacroScopes.toString used" in source
