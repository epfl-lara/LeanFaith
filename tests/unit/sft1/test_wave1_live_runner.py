from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import load_config
from leanfaith.host_resources import Reservation
from leanfaith.lean.protocol import LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import CompileContext
from leanfaith.sft1.wave1_live_readiness import (
    EXPECTED_OPERATION_IDS,
    LoadedWave1LiveReadiness,
    Wave1RuntimeConfig,
    Wave1RuntimeFixtures,
    assemble_runtime_preamble,
    build_fixture_compile_context,
    n31_phase_receipt_id,
)
from leanfaith.sft1.wave1_readiness import Wave1CacheKey, compute_wave1_cache_key_hash
from leanfaith.sft1.wave1_live_runner import (
    TASK_RECEIPT_MARKER,
    HashChainJournal,
    OrchestratorDependencies,
    Wave1LiveRunnerError,
    _run_positive_readiness_checkpoint,
    build_direct_meta_command,
    build_n31_phase_one_session,
    build_n31_phase_two_session,
    build_p01_runtime_replay_receipt,
    build_positive_rejection_session,
    build_positive_success_session,
    build_positive_symbol_resolution_session,
    execute_n31_resolution_proposal_evidence,
    extract_task_receipt,
    install_immutable_json,
    measured_process_tree_rss_bytes,
    persist_p01_runtime_replay,
    replay_hash_chain_journal,
    runner_process_identity,
    synthesized_instance_hashes,
    validate_p01_runtime_replay_receipt,
    validate_positive_rejection_task_receipt,
    validate_positive_success_task_receipt,
    verify_clean_git_implementation_identity,
    write_durable_heartbeat,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/sft1/wave1_v0_3_6.yaml"
RUNTIME_CONFIG = ROOT / "configs/transformations/sft1_value_first_v1/wave1_runtime_v0_3_6.yaml"


def _loaded() -> LoadedWave1LiveReadiness:
    fixtures = load_config(FIXTURES, Wave1RuntimeFixtures).config
    return cast(LoadedWave1LiveReadiness, SimpleNamespace(fixtures=fixtures))


def _loaded_runtime() -> LoadedWave1LiveReadiness:
    loaded_fixtures = load_config(FIXTURES, Wave1RuntimeFixtures)
    loaded_config = load_config(RUNTIME_CONFIG, Wave1RuntimeConfig)
    return cast(
        LoadedWave1LiveReadiness,
        SimpleNamespace(
            config=loaded_config.config,
            config_path=RUNTIME_CONFIG,
            config_hash=loaded_config.config_hash,
            config_file_sha256=hash_file(RUNTIME_CONFIG),
            fixtures=loaded_fixtures.config,
            fixture_path=FIXTURES,
            fixture_hash=loaded_fixtures.config_hash,
            fixture_file_sha256=hash_file(FIXTURES),
        ),
    )


@pytest.mark.parametrize("operation_id", EXPECTED_OPERATION_IDS[:4])
def test_positive_success_session_unrolls_two_shared_emitters(operation_id: str) -> None:
    body, inputs = build_positive_success_session(
        _loaded(),
        project_id="mathlib",
        operation_id=operation_id,
        receipt_id=f"mathlib.{operation_id}.success",
        render_scope_id=f"scope:{operation_id}",
    )
    assert body.startswith("run_meta do\n")
    assert body.count("LeanFaith.GoalV1.emitClosedProp") == 2
    assert "emitPositiveSuccessReceipt" in body
    assert "theorem " not in body
    assert "sorry" not in body
    assert tuple(item.endpoint_role for item in inputs) == ("reference", "candidate")
    assert inputs[1].source_material.kind == "constructed_expr_no_source_text"


@pytest.mark.parametrize("operation_id", EXPECTED_OPERATION_IDS[:4])
def test_positive_rejection_session_has_no_candidate_or_renderer(operation_id: str) -> None:
    body = build_positive_rejection_session(
        _loaded(),
        project_id="physlib",
        operation_id=operation_id,
        receipt_id=f"physlib.{operation_id}.reject",
    )
    assert body.startswith("run_meta do\n")
    assert "emitPositiveRejectionReceipt" in body
    assert "emitClosedProp" not in body
    assert "candidateExpr" not in body


def test_session_builders_reject_n31_and_unsafe_ids() -> None:
    with pytest.raises(Wave1LiveRunnerError, match="outside authorized"):
        build_positive_rejection_session(
            _loaded(),
            project_id="mathlib",
            operation_id="N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
            receipt_id="n31",
        )
    with pytest.raises(Wave1LiveRunnerError, match="unsafe receipt"):
        build_positive_success_session(
            _loaded(),
            project_id="mathlib",
            operation_id="P01_ALPHA_RENAME_SINGLE_V1",
            receipt_id='bad"id',
            render_scope_id="scope",
        )


def test_symbol_resolution_session_is_fixture_independent_and_candidate_free() -> None:
    body = build_positive_symbol_resolution_session(
        project_id="mathlib",
        operation_id="P01_ALPHA_RENAME_SINGLE_V1",
        receipt_id="mathlib.P01_ALPHA_RENAME_SINGLE_V1.symbols",
    )
    assert body.startswith("run_meta do\n")
    assert "LeanFaith.SFT1.Wave1.dispatchAt" in body
    assert "LeanFaith.SFT1.Wave1.discover" in body
    assert "LeanFaith.SFT1.Wave1.replayCertificate" in body
    assert "LeanFaith.GoalV1.emitClosedProp" not in body
    assert "candidateExpr" not in body
    assert "theorem " not in body
    assert "sorry" not in body


def test_p01_runtime_receipt_replays_every_named_rejection_and_rejects_tamper() -> None:
    import tests.unit.sft1.test_wave1_runtime as runtime_fixtures

    receipt = build_p01_runtime_replay_receipt(
        runtime_fixtures._valid_chain(), project_id="mathlib"
    )
    assert validate_p01_runtime_replay_receipt(receipt) == receipt
    assert set(cast(dict[str, object], receipt["named_adversarial_rejections"])) == {
        "missing_certificate",
        "failed_certificate",
        "equal_render",
        "equal_model_text",
        "wrong_operation_edge",
        "nonadjacent_repeat",
        "third_occurrence",
        "multiple_p01_hops",
    }
    tampered = dict(receipt)
    tampered["required_policy_semantic_hash"] = "0" * 64
    tampered_core = dict(tampered)
    tampered_core.pop("receipt_hash")
    tampered["receipt_hash"] = hash_canonical(tampered_core)
    with pytest.raises(Wave1LiveRunnerError, match="receipt drift"):
        validate_p01_runtime_replay_receipt(tampered)


def test_git_identity_verification_rejects_wrong_ids_and_dirty_checkout(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "wave1-test@example.invalid")
    git("config", "user.name", "Wave1 Test")
    tracked = repository / "tracked.txt"
    tracked.write_text("frozen\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-q", "-m", "fixture")
    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    receipt = verify_clean_git_implementation_identity(repository, commit, tree)
    assert receipt["worktree_clean"] is True
    with pytest.raises(Wave1LiveRunnerError, match="caller Git IDs"):
        verify_clean_git_implementation_identity(repository, "0" * 40, tree)
    (repository / "untracked.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(Wave1LiveRunnerError, match="not clean"):
        verify_clean_git_implementation_identity(repository, commit, tree)


def test_direct_command_preserves_context_without_repr_helper_copy() -> None:
    context = CompileContext(
        project_id="mathlib",
        project_revision="d568c8c09630de097a046763c17b9ea99f95f950",
        lean_version="v4.31.0-rc1",
        import_header="import Mathlib",
        command_preamble="namespace Bound\ndef marker : Nat := 1\nend Bound",
        options={"Elab.async": False, "autoImplicit": False},
    )
    command = build_direct_meta_command(context, "run_meta do\n  pure ()")
    assert command.startswith("import Lean\nimport Mathlib\n")
    assert "set_option Elab.async false" in command
    assert "set_option autoImplicit false" in command
    assert "LFGOALV1EXPRJSON" not in command
    assert "emitClosedProp" not in command
    with pytest.raises(Wave1LiveRunnerError, match="forbidden"):
        build_direct_meta_command(
            context, 'run_meta do\n  LeanFaith.GoalV1.emitClosedProp "a" "b" "c" e'
        )


def test_task_receipt_parser_requires_one_exact_marker() -> None:
    payload = {
        "receipt_id": "case.one",
        "receipt_kind": "positive_success",
        "passed": True,
    }
    messages = ({"data": "prefix\n" + TASK_RECEIPT_MARKER + json.dumps(payload)},)
    assert (
        extract_task_receipt(messages, receipt_id="case.one", receipt_kind="positive_success")
        == payload
    )
    with pytest.raises(Wave1LiveRunnerError, match="found 2"):
        extract_task_receipt(messages * 2, receipt_id="case.one", receipt_kind="positive_success")
    duplicate = ({"data": TASK_RECEIPT_MARKER + '{"receipt_id":"x","receipt_id":"x"}'},)
    with pytest.raises(Wave1LiveRunnerError, match="duplicate"):
        extract_task_receipt(duplicate, receipt_id="x", receipt_kind="positive_success")


def test_strict_positive_rejection_receipt_rejects_invented_candidate() -> None:
    receipt: dict[str, object] = {
        "schema_version": 1,
        "receipt_kind": "positive_typed_not_applicable",
        "receipt_id": "case.reject",
        "source_version": "sft1_wave1_runtime_readiness_v0_3_6",
        "operation_id": "P15_SWAP_IFF_SIDES_V1",
        "selector": {"kind": "outerTarget"},
        "source_expr_hash": "1",
        "structural_expr_fingerprint_id": "lean_hashable_expr_uint64_decimal_v1",
        "terminal": "typedNotApplicable",
        "reason": "operationNotApplicable",
        "candidate_constructed": False,
        "candidate_serialized": False,
        "row_or_gate_emitted": False,
    }
    assert (
        validate_positive_rejection_task_receipt(
            receipt,
            receipt_id="case.reject",
            operation_id="P15_SWAP_IFF_SIDES_V1",
            expected_reason="operationNotApplicable",
        )
        == receipt
    )
    receipt["candidate_constructed"] = True
    with pytest.raises(Wave1LiveRunnerError, match="contract drift"):
        validate_positive_rejection_task_receipt(
            receipt,
            receipt_id="case.reject",
            operation_id="P15_SWAP_IFF_SIDES_V1",
            expected_reason="operationNotApplicable",
        )


def _instance_evidence(endpoint_role: str, value: int, *, exact: bool) -> dict[str, object]:
    return {
        "basis_id": "sft1_wave1_typed_inst_implicit_inventory_v0_3_6",
        "endpoint_role": endpoint_role,
        "application_path": [value],
        "path_semantics": "flattened_app_head_0_args_i_plus_1_other_expr_children_v1",
        "head_kind": "constant",
        "head_name": "Example.head",
        "head_expr_hash": str(value + 10),
        "application_expr_hash": str(value + 20),
        "argument_index": value,
        "argument_expr_hash": str(value + 30),
        "expected_type_hash": str(value + 40) if exact else None,
        "declaration_type_hash": str(value + 50) if exact else None,
        "binder_info": "instImplicit" if exact else None,
        "typing_evidence_class": (
            "instImplicit_binder_with_instantiated_expected_type"
            if exact
            else "checked_closed_prop_endpoint_conservative_application_argument"
        ),
        "exact_instance_implicit": exact,
        "conservative_possible_instance": not exact,
    }


def _endpoint_inventory(
    endpoint_role: str, endpoint_expr_hash: str, evidence: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "endpoint_role": endpoint_role,
        "endpoint_expr_hash": endpoint_expr_hash,
        "checked_closed_prop": True,
        "structurally_complete": not any(
            item["conservative_possible_instance"] is True for item in evidence
        ),
        "exact_instance_implicit_count": sum(
            item["exact_instance_implicit"] is True for item in evidence
        ),
        "conservative_possible_instance_count": sum(
            item["conservative_possible_instance"] is True for item in evidence
        ),
        "evidence_count": len(evidence),
        "empty_inventory_proved": not evidence,
        "evidence": evidence,
    }


def _success_receipt(
    operation_id: str = "P15_SWAP_IFF_SIDES_V1",
) -> dict[str, object]:
    source_hash, candidate_hash = "101", "202"
    source_evidence = [_instance_evidence("source", 0, exact=True)]
    candidate_evidence = [_instance_evidence("candidate", 1, exact=False)]
    preimages = [*source_evidence, *candidate_evidence]
    selectors: dict[str, dict[str, object]] = {
        "P01_ALPHA_RENAME_SINGLE_V1": {"kind": "outerBinder", "ordinal": 0},
        "P15_SWAP_IFF_SIDES_V1": {"kind": "outerTarget"},
        "P18_SYMMETRIZE_EQUALITY_V1": {"kind": "outerTarget"},
        "P21_BETA_REDUCE_V1": {"kind": "subexpr", "position": "/", "position_nat": "1"},
    }
    certificates: dict[str, dict[str, object]] = {
        "P01_ALPHA_RENAME_SINGLE_V1": {
            "kind": "p01",
            "binder_ordinal": 0,
            "binder_site": "/",
            "binder_site_nat": "1",
            "source_name": "x",
            "candidate_name": "x_1",
            "binder_info": "default",
        },
        "P15_SWAP_IFF_SIDES_V1": {
            "kind": "p15",
            "target_site": "/",
            "target_site_nat": "1",
        },
        "P18_SYMMETRIZE_EQUALITY_V1": {
            "kind": "p18",
            "target_site": "/",
            "target_site_nat": "1",
        },
        "P21_BETA_REDUCE_V1": {
            "kind": "p21",
            "redex_site": "/",
            "redex_site_nat": "1",
        },
    }
    delta: dict[str, object] | None = None
    if operation_id == "P01_ALPHA_RENAME_SINGLE_V1":
        delta = {
            "operation_id": operation_id,
            "source_expr_hash": source_hash,
            "candidate_expr_hash": candidate_hash,
            "binder_ordinal": 0,
            "binder_site": "/",
            "binder_site_nat": "1",
            "source_name": "x",
            "candidate_name": "x_1",
            "binder_info": "default",
            "source_domain_hash": "1",
            "candidate_domain_hash": "1",
            "source_body_hash": "2",
            "candidate_body_hash": "2",
            "binder_site_matches_certificate": True,
            "source_name_matches_certificate": True,
            "candidate_name_matches_certificate": True,
            "binder_info_matches_certificate": True,
            "names_differ": True,
            "domains_exactly_equal": True,
            "bodies_exactly_equal": True,
            "source_candidate_alpha_equivalent": True,
            "source_candidate_exactly_different": True,
            "deterministic_candidate_replay_exact": True,
            "frozen_certificate_replay_passed": True,
            "exact_name_only_delta_passed": True,
        }
    return {
        "schema_version": 1,
        "receipt_kind": "positive_success",
        "receipt_id": "case.success",
        "source_version": "sft1_wave1_runtime_readiness_v0_3_6",
        "operation_id": operation_id,
        "selector": selectors[operation_id],
        "certificate": certificates[operation_id],
        "source_expr_hash": source_hash,
        "candidate_expr_hash": candidate_hash,
        "structural_expr_fingerprint_id": "lean_hashable_expr_uint64_decimal_v1",
        "operation_matches": True,
        "selector_matches": True,
        "frozen_replay": {"passed": True, "operation_id": operation_id, "reason": None},
        "certificate_constructor_matches": True,
        "deterministic_candidate_equality": True,
        "deterministic_certificate_equality": True,
        "discovered_candidate_count": 1,
        "selected_selector_rediscovery_count": 1,
        "selected_candidate_and_certificate_exact_count": 1,
        "selected_site_uniquely_rediscovered": True,
        "synthesized_instance_inventory": {
            "basis_id": "sft1_wave1_typed_inst_implicit_inventory_v0_3_6",
            "structural_expr_fingerprint_id": "lean_hashable_expr_uint64_decimal_v1",
            "hash_input_contract": "python_sha256_canonical_json_per_ordered_item_v1",
            "ordering": "source_preorder_then_candidate_preorder_v1",
            "scope": "all_exact_instImplicit_arguments_plus_conservative_unclassified_arguments",
            "source": _endpoint_inventory("source", source_hash, source_evidence),
            "candidate": _endpoint_inventory("candidate", candidate_hash, candidate_evidence),
            "ordered_cache_hash_preimage_count": 2,
            "ordered_cache_hash_preimages": preimages,
            "empty_inventory_proved": False,
            "cache_hash_basis_adequate": True,
        },
        "p01_exact_delta": delta,
        "candidate_exposed_to_caller_for_same_request_repr": True,
        "row_or_gate_emitted": False,
    }


def test_synthesized_instance_inventory_hashes_exact_ordered_preimages() -> None:
    receipt = _success_receipt()
    preimages = receipt["synthesized_instance_inventory"][  # type: ignore[index]
        "ordered_cache_hash_preimages"
    ]
    assert isinstance(preimages, list)
    assert synthesized_instance_hashes(receipt) == tuple(hash_canonical(item) for item in preimages)
    receipt["synthesized_instance_inventory"]["ordered_cache_hash_preimages"] = []  # type: ignore[index]
    receipt["synthesized_instance_inventory"]["ordered_cache_hash_preimage_count"] = 0  # type: ignore[index]
    with pytest.raises(Wave1LiveRunnerError, match="preimage replay"):
        synthesized_instance_hashes(receipt)


@pytest.mark.parametrize("operation_id", EXPECTED_OPERATION_IDS[:4])
def test_strict_positive_success_receipts_validate(operation_id: str) -> None:
    receipt = _success_receipt(operation_id)
    assert (
        validate_positive_success_task_receipt(
            receipt, receipt_id="case.success", operation_id=operation_id
        )
        == receipt
    )
    forged = dict(receipt)
    forged["unknown"] = True
    with pytest.raises(Wave1LiveRunnerError, match="field inventory"):
        validate_positive_success_task_receipt(
            forged, receipt_id="case.success", operation_id=operation_id
        )


def test_hash_chain_journal_replays_and_rejects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = HashChainJournal(path)
    first = journal.append({"event": "start"})
    second = journal.append({"event": "complete"})
    assert first != second
    assert replay_hash_chain_journal(path) == (2, second)
    records = path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(records[0])
    payload["event"]["event"] = "forged"
    records[0] = canonical_json_bytes(payload).decode("utf-8")
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    with pytest.raises(Wave1LiveRunnerError, match="hash chain"):
        replay_hash_chain_journal(path)


def test_immutable_json_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    first = install_immutable_json(path, {"value": 1})
    assert install_immutable_json(path, {"value": 1}) == first
    with pytest.raises(Wave1LiveRunnerError, match="immutable artifact conflict"):
        install_immutable_json(path, {"value": 2})


def test_current_process_rss_measurement_is_positive() -> None:
    assert measured_process_tree_rss_bytes(os.getpid()) > 0


def test_n31_sessions_are_exact_two_phase_nonactivation_requests() -> None:
    loaded = _loaded_runtime()
    phase_one_id = n31_phase_receipt_id("mathlib", "phase_one")
    phase_two_id = n31_phase_receipt_id("mathlib", "phase_two")
    phase_one = build_n31_phase_one_session(loaded, project_id="mathlib", receipt_id=phase_one_id)
    resolved = "1" * 64
    receipt_hash = "2" * 64
    phase_two = build_n31_phase_two_session(
        loaded,
        project_id="mathlib",
        receipt_id=phase_two_id,
        resolved_lean_hash=resolved,
        resolution_receipt_hash=receipt_hash,
    )
    assert phase_one.startswith("run_meta do\n")
    assert "emitN31ProposalResolutionReceipt" in phase_one
    assert 'resolvedLeanHash := ""' in phase_one
    assert 'resolutionReceiptHash := ""' in phase_one
    assert "emitN31FrozenNonActivationReceipt" in phase_two
    assert f'resolvedLeanHash := "{resolved}"' in phase_two
    assert f'resolutionReceiptHash := "{receipt_hash}"' in phase_two
    assert ".requiredGuard 1 .root" in phase_two
    assert "n31_ne_zero_hdiv_nat_v0_3_6" in phase_two
    for body in (phase_one, phase_two):
        assert "emitClosedProp" not in body
        assert "theorem " not in body
        assert "axiom " not in body
        assert "sorry" not in body
        assert "candidate" not in body.lower()


def test_positive_receipt_rejects_zero_for_subexpr_root_as_nat() -> None:
    receipt = _success_receipt("P21_BETA_REDUCE_V1")
    receipt["selector"]["position_nat"] = "0"  # type: ignore[index]
    receipt["certificate"]["redex_site_nat"] = "0"  # type: ignore[index]
    with pytest.raises(Wave1LiveRunnerError, match=r"selector drift|asNat value 1"):
        validate_positive_success_task_receipt(
            receipt,
            receipt_id="case.success",
            operation_id="P21_BETA_REDUCE_V1",
        )


def test_journal_and_heartbeat_reject_symlink_targets(tmp_path: Path) -> None:
    real = tmp_path / "real.jsonl"
    real.write_text("", encoding="utf-8")
    linked = tmp_path / "linked.jsonl"
    linked.symlink_to(real)
    with pytest.raises(Wave1LiveRunnerError, match="symlink"):
        HashChainJournal(linked)
    heartbeat_target = tmp_path / "heartbeat-target.json"
    heartbeat_target.write_text("{}\n", encoding="utf-8")
    heartbeat_link = tmp_path / "heartbeat.json"
    heartbeat_link.symlink_to(heartbeat_target)
    with pytest.raises(Wave1LiveRunnerError, match="safe regular"):
        write_durable_heartbeat(
            heartbeat_link,
            run_spec_hash="a" * 64,
            state="test",
            project_id=None,
            case_id=None,
            rss_bytes=1,
        )


def test_heartbeat_binds_process_incarnation_and_replaces_atomically(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    identity = runner_process_identity()
    first = write_durable_heartbeat(
        path,
        run_spec_hash="a" * 64,
        state="first",
        project_id="mathlib",
        case_id="case.one",
        rss_bytes=123,
        process_identity=identity,
    )
    second = write_durable_heartbeat(
        path,
        run_spec_hash="a" * 64,
        state="second",
        project_id="mathlib",
        case_id="case.two",
        rss_bytes=456,
        process_identity=identity,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert first != second
    assert payload["state"] == "second"
    assert payload["process_identity"] == identity
    assert not tuple(path.parent.glob("*.partial"))


def test_positive_orchestrator_claims_before_prepare_and_resumes_without_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import leanfaith.sft1.wave1_live_runner as live_runner

    monkeypatch.setattr(
        live_runner,
        "_validate_positive_success_artifact_closure",
        lambda *args, **kwargs: None,
    )

    def fake_p01_receipt_validator(payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return cast(dict[str, object], payload)

    monkeypatch.setattr(
        live_runner,
        "validate_p01_runtime_replay_receipt",
        fake_p01_receipt_validator,
    )
    loaded = _loaded_runtime()
    source_bindings = tuple(
        item.model_copy(
            update={"file_sha256": hash_file(ROOT / "src/leanfaith/sft1/wave1_live_runner.py")}
        )
        if item.role == "live_readiness_runner"
        else item
        for item in loaded.config.source_bindings
    )
    preamble = assemble_runtime_preamble(ROOT, loaded.config.source_bindings)
    loaded.config = loaded.config.model_copy(
        update={
            "source_bindings": source_bindings,
            "persistence_contract": loaded.config.persistence_contract.model_copy(
                update={"root": str((tmp_path / "run").resolve())}
            ),
            "preamble_contract": loaded.config.preamble_contract.model_copy(
                update={"assembled_preamble_sha256": preamble.sha256}
            ),
        }
    )
    events: list[str] = []
    reservation = Reservation(
        task="SFT1",
        lean_workers=1,
        lean_rss_gib=24.0,
        gpu=False,
        pid=os.getpid(),
        owner_session="unit-test",
        hostname="test-host",
        worktree=str(ROOT),
        created_at="2026-08-31T00:00:00+00:00",
    )

    class FakeSampler:
        peak_bytes = 1024

        def start(self) -> None:
            events.append("sampler:start")

        def check(self) -> int:
            return self.peak_bytes

        def stop(self) -> int:
            events.append("sampler:stop")
            return self.peak_bytes

    class FakeBackend:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def run(self, request: object) -> LeanResult:  # pragma: no cover - callbacks bypass it
            raise AssertionError(request)

        def run_batch(self, requests: object) -> list[LeanResult]:  # pragma: no cover
            raise AssertionError(requests)

        def close(self) -> None:
            events.append(f"close:{self.project_id}")

    raw_root = tmp_path / "run" / "raw" / "fake"

    def fake_result(receipt_id: str) -> LeanResult:
        path = raw_root / f"{receipt_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"fake":true}\n', encoding="utf-8")
        digest = hash_canonical({"receipt_id": receipt_id})
        return LeanResult(
            request_id=receipt_id,
            request_hash=digest,
            context_id="ctx:" + "a" * 64,
            context_fingerprint="a" * 64,
            status=LeanStatus.VALID,
            elapsed_ms=1,
            raw_response_path=str(path),
        )

    def success_executor(*args: object, **kwargs: object) -> SimpleNamespace:
        del args
        operation_id = cast(str, kwargs["operation_id"])
        project_id = cast(str, kwargs["project_id"])
        receipt_id = cast(str, kwargs["receipt_id"])
        events.append(f"success:{receipt_id}")
        receipt = _success_receipt(operation_id)
        receipt["receipt_id"] = receipt_id
        result = fake_result(receipt_id)
        return SimpleNamespace(
            receipt_id=receipt_id,
            operation_id=operation_id,
            project_id=project_id,
            task_receipt=receipt,
            result=result,
            attempt_request_hashes=(result.request_hash,),
        )

    def rejection_executor(*args: object, **kwargs: object) -> SimpleNamespace:
        del args
        operation_id = cast(str, kwargs["operation_id"])
        receipt_id = cast(str, kwargs["receipt_id"])
        events.append(f"reject:{receipt_id}")
        template = next(
            item
            for item in loaded.fixtures.templates
            if item.operation_id == operation_id and item.fixture_kind == "adversarial_rejection"
        )
        receipt = {
            "schema_version": 1,
            "receipt_kind": "positive_typed_not_applicable",
            "receipt_id": receipt_id,
            "source_version": "sft1_wave1_runtime_readiness_v0_3_6",
            "operation_id": operation_id,
            "selector": {
                "P01_ALPHA_RENAME_SINGLE_V1": {"kind": "outerBinder", "ordinal": 0},
                "P15_SWAP_IFF_SIDES_V1": {"kind": "outerTarget"},
                "P18_SYMMETRIZE_EQUALITY_V1": {"kind": "outerTarget"},
                "P21_BETA_REDUCE_V1": {
                    "kind": "subexpr",
                    "position": "/",
                    "position_nat": "1",
                },
            }[operation_id],
            "source_expr_hash": "1",
            "structural_expr_fingerprint_id": "lean_hashable_expr_uint64_decimal_v1",
            "terminal": "typedNotApplicable",
            "reason": template.expected_engine_reason,
            "candidate_constructed": False,
            "candidate_serialized": False,
            "row_or_gate_emitted": False,
        }
        result = fake_result(receipt_id)
        return SimpleNamespace(
            task_receipt=receipt,
            result=result,
            attempt_request_hashes=(result.request_hash,),
        )

    def positive_persister(*args: object, **kwargs: object) -> SimpleNamespace:
        execution = cast(SimpleNamespace, args[1])
        evidence_root = cast(Path, kwargs["evidence_root"])
        sidecar_root = cast(Path, kwargs["sidecar_root"])
        cache_root = cast(Path, kwargs["cache_root"])
        receipt_id = execution.receipt_id
        task = evidence_root / f"{receipt_id}.task.json"
        typed = evidence_root / f"{receipt_id}.typed.json"
        reference = sidecar_root / f"{receipt_id}.reference.json"
        candidate = sidecar_root / f"{receipt_id}.candidate.json"
        for path, text in (
            (task, "task"),
            (typed, "typed"),
            (reference, "reference"),
            (candidate, "candidate"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        key = hash_canonical({"receipt_id": receipt_id, "cache": True})
        cache = cache_root / "v1" / key[:2] / f"{key}.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("cache\n", encoding="utf-8")
        raw = Path(execution.result.raw_response_path)
        p01_runtime_replay = None
        if execution.operation_id == "P01_ALPHA_RENAME_SINGLE_V1":
            retention_manifest = evidence_root / f"{receipt_id}.retention.manifest.json"
            retention_journal = evidence_root / f"{receipt_id}.retention.journal.jsonl"
            retention_manifest.write_text("{}\n", encoding="utf-8")
            retention_journal.write_text("{}\n", encoding="utf-8")
            p01_core = {
                "cap_contract": {"complete_retention_scope_executed": True},
                "durable_readiness_retention_scope": {
                    "manifest_path": str(retention_manifest.resolve()),
                    "journal_path": str(retention_journal.resolve()),
                },
            }
            p01_receipt = {**p01_core, "receipt_hash": hash_canonical(p01_core)}
            p01_path = evidence_root / f"{receipt_id}.p01.json"
            p01_path.write_bytes(canonical_json_bytes(p01_receipt) + b"\n")
            p01_runtime_replay = SimpleNamespace(
                receipt=p01_receipt,
                receipt_hash=p01_receipt["receipt_hash"],
                receipt_path=p01_path,
                receipt_file_sha256=hash_file(p01_path),
                retention_manifest_path=retention_manifest,
                retention_manifest_file_sha256=hash_file(retention_manifest),
                retention_journal_path=retention_journal,
                retention_journal_file_sha256=hash_file(retention_journal),
            )
        import tests.unit.sft1.test_wave1_runtime as runtime_fixtures

        chain = runtime_fixtures._valid_chain()
        wave1_key = Wave1CacheKey(
            source_closed_expr_hash=chain.endpoints[0].closed_expr_hash,
            candidate_closed_expr_hash=chain.endpoints[-1].closed_expr_hash,
            canonical_universe_profile_id="goal_v1_first_occurrence_u_i_v1",
            canonical_universe_profile_hash=(
                "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
            ),
            source_expr_builder_version="test",
            candidate_expr_builder_version="test",
            lean_version="test",
            project_id=execution.project_id,
            project_revision="test",
            toolchain_revision="test",
            imports_hash=hash_canonical("imports"),
            options_hash=hash_canonical("options"),
            synthesized_instance_hashes=(),
            operation_id=execution.operation_id,
            operation_registry_entry_hash=hash_canonical("registry"),
            schema_lemma_procedure_hash=hash_canonical("procedure"),
            evidence_certificate_payload_hash=hash_canonical("certificate"),
            bank_resolved_lean_hash=hash_canonical("bank"),
            transparency="reducible",
            allowed_axiom_profile="test",
            typed_meta_validator_version="test",
            evidence_replay_version="test",
            evaluation_blocklist_sha256=(
                "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
            ),
            repr_replacement_commit="176a783842c5a73b84413dfa8347670608b615d9",
            render_context_id="goal_v1_render_context_v1",
            render_context_hash=(
                "5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"
            ),
            renderer_api_hash=(
                "c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"
            ),
            repr_spec_hash=(
                "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"
            ),
            environment_fingerprint_hash=hash_canonical("environment"),
            policy_config_hash=loaded.config_hash,
        )
        return SimpleNamespace(
            chain=chain,
            task_receipt_path=task,
            task_receipt_sha256=hash_file(task),
            typed_replay_path=typed,
            typed_replay_sha256=hash_file(typed),
            raw_response_path=raw,
            raw_response_sha256=hash_file(raw),
            reference_sidecar_path=reference,
            candidate_sidecar_path=candidate,
            wave1_cache_key=wave1_key,
            wave1_cache_key_hash=compute_wave1_cache_key_hash(wave1_key),
            central_cache_key_hash=key,
            central_cache_entry_path=cache,
            central_cache_entry_sha256=hash_file(cache),
            p01_runtime_replay=p01_runtime_replay,
        )

    def symbol_executor(*args: object, **kwargs: object) -> SimpleNamespace:
        del args
        project_id = cast(str, kwargs["project_id"])
        operation_id = cast(str, kwargs["operation_id"])
        evidence_root = cast(Path, kwargs["evidence_root"])
        receipt_id = f"{project_id}.{operation_id}.symbols"
        constructor = {
            "P01_ALPHA_RENAME_SINGLE_V1": (
                "LeanFaith.SFT1.Wave1.PrimaryOperation.p01AlphaRenameSingle"
            ),
            "P15_SWAP_IFF_SIDES_V1": ("LeanFaith.SFT1.Wave1.PrimaryOperation.p15SwapIffSides"),
            "P18_SYMMETRIZE_EQUALITY_V1": (
                "LeanFaith.SFT1.Wave1.PrimaryOperation.p18SymmetrizeEquality"
            ),
            "P21_BETA_REDUCE_V1": "LeanFaith.SFT1.Wave1.PrimaryOperation.p21BetaReduce",
        }[operation_id]
        receipt = {
            "schema_version": 1,
            "receipt_kind": "positive_symbol_resolution",
            "receipt_id": receipt_id,
            "source_version": "sft1_wave1_runtime_readiness_v0_3_6",
            "project_id": project_id,
            "operation_id": operation_id,
            "operation_constructor": constructor,
            "dispatch_symbol": "LeanFaith.SFT1.Wave1.dispatchAt",
            "discover_symbol": "LeanFaith.SFT1.Wave1.discover",
            "checker_symbol": "LeanFaith.SFT1.Wave1.replayCertificate",
            "frozen_engine_version": "sft1_wave1_expr_engine_v0_3_4",
            "dispatch_declaration_found": True,
            "discover_declaration_found": True,
            "checker_declaration_found": True,
            "typed_function_signatures_assigned": True,
            "bundle_identity_matches": True,
            "passed": True,
            "candidate_constructed": False,
            "row_or_gate_emitted": False,
        }
        task_path = evidence_root / project_id / "symbols" / f"{operation_id}.json"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_bytes(canonical_json_bytes(receipt) + b"\n")
        result = fake_result(receipt_id)
        storage_root = evidence_root.resolve().parent
        raw_path = storage_root / "raw" / "positive" / project_id / f"{receipt_id}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text('{"fake":true}\n', encoding="utf-8")
        project = next(
            item for item in loaded.fixtures.project_contexts if item.project_id == project_id
        )
        context = build_fixture_compile_context(
            project, assembled_preamble=cast(str, kwargs["assembled_preamble"])
        )
        helper_sha = next(
            item.file_sha256
            for item in loaded.config.source_bindings
            if item.role == "lean_runtime_helper"
        )
        events.append(f"symbols:{project_id}:{operation_id}")
        return SimpleNamespace(
            project_id=project_id,
            operation_id=operation_id,
            receipt_id=receipt_id,
            task_receipt=receipt,
            task_receipt_hash=hash_canonical(receipt),
            task_receipt_path=task_path,
            task_receipt_file_sha256=hash_file(task_path),
            raw_response_path=raw_path,
            raw_response_sha256=hash_file(raw_path),
            request_hash=result.request_hash,
            elapsed_ms=1,
            compile_context_id=context.compile_context_id,
            compile_context_fingerprint=context.fingerprint,
            assembled_preamble_sha256=loaded.config.preamble_contract.assembled_preamble_sha256,
            runtime_config_file_sha256=loaded.config_file_sha256,
            runtime_config_hash=loaded.config_hash,
            runtime_fixture_file_sha256=loaded.fixture_file_sha256,
            runtime_fixture_hash=loaded.fixture_hash,
            runtime_loader_file_sha256=hash_file(
                ROOT / "src/leanfaith/sft1/wave1_live_readiness.py"
            ),
            runtime_helper_sha256=helper_sha,
            storage_root=storage_root,
        )

    def prepare(settings: object) -> None:
        assert events and events[0] == "claim"
        assert settings.memory_hard_limit_mb == 24576  # type: ignore[attr-defined]
        assert settings.enable_parallel_elaboration is False  # type: ignore[attr-defined]
        events.append(f"prepare:{settings.context_fingerprint}")  # type: ignore[attr-defined]

    def make(settings: object) -> FakeBackend:
        assert settings.environment_is_prepared is True  # type: ignore[attr-defined]
        project = f"session-{len([event for event in events if event.startswith('make:')])}"
        events.append(f"make:{project}")
        return FakeBackend(project)

    def fake_git_identity(worktree: Path, commit: str, tree: str) -> dict[str, object]:
        core: dict[str, object] = {
            "schema_version": 1,
            "worktree": str(worktree.resolve()),
            "implementation_commit": commit,
            "implementation_tree": tree,
            "status_porcelain_sha256": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            "worktree_clean": True,
            "verified_before_resource_claim": True,
        }
        return {**core, "verification_hash": hash_canonical(core)}

    dependencies = OrchestratorDependencies(
        prepare_backend=prepare,
        make_backend=make,
        claim_worker=lambda **kwargs: events.append("claim") or reservation,
        release_worker=lambda: events.append("release") or reservation,
        sampler_factory=FakeSampler,
        verify_implementation_identity=fake_git_identity,
        symbol_resolution_executor=symbol_executor,
        positive_success_executor=success_executor,
        positive_rejection_executor=rejection_executor,
        positive_persister=positive_persister,
    )
    receipt = _run_positive_readiness_checkpoint(
        loaded,
        assembled_preamble=preamble.text,
        implementation_commit="a" * 40,
        implementation_tree="b" * 40,
        worktree=ROOT,
        storage_root=tmp_path / "run",
        owner_session="unit-test",
        resume_command="resume-positive-test",
        repository_receipt_path=tmp_path / "repo-receipt.json",
        dependencies=dependencies,
    )
    assert receipt["positive_case_count"] == 32
    assert receipt["n31_resolution_started"] is False
    assert events[0] == "claim"
    assert events[-1] == "release"
    assert len([item for item in events if item.startswith("prepare:")]) == 4
    assert len([item for item in events if item.startswith("make:")]) == 4
    assert len([item for item in events if item.startswith("success:")]) == 16
    assert len([item for item in events if item.startswith("reject:")]) == 16
    before_resume = tuple(events)
    replayed = _run_positive_readiness_checkpoint(
        loaded,
        assembled_preamble=preamble.text,
        implementation_commit="a" * 40,
        implementation_tree="b" * 40,
        worktree=ROOT,
        storage_root=tmp_path / "run",
        owner_session="unit-test",
        resume_command="resume-positive-test",
        repository_receipt_path=tmp_path / "repo-receipt.json",
        dependencies=dependencies,
    )
    assert replayed == receipt
    assert tuple(events) == before_resume

    events.clear()

    def failing_success(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        events.append("injected-failure")
        raise RuntimeError("fixture failure")

    failing_dependencies = OrchestratorDependencies(
        prepare_backend=prepare,
        make_backend=make,
        claim_worker=lambda **kwargs: events.append("claim") or reservation,
        release_worker=lambda: events.append("release") or reservation,
        sampler_factory=FakeSampler,
        verify_implementation_identity=fake_git_identity,
        symbol_resolution_executor=symbol_executor,
        positive_success_executor=failing_success,
        positive_rejection_executor=rejection_executor,
        positive_persister=positive_persister,
    )
    with pytest.raises(RuntimeError, match="fixture failure"):
        _run_positive_readiness_checkpoint(
            loaded,
            assembled_preamble=preamble.text,
            implementation_commit="a" * 40,
            implementation_tree="c" * 40,
            worktree=ROOT,
            storage_root=tmp_path / "failed-run",
            owner_session="unit-test",
            resume_command="resume-failed-positive-test",
            repository_receipt_path=None,
            dependencies=failing_dependencies,
        )
    assert events[0] == "claim"
    assert "injected-failure" in events
    assert any(event.startswith("close:") for event in events)
    assert events[-1] == "release"
    assert tuple((tmp_path / "failed-run" / "terminal").glob("positive.failed.*.json"))


def test_n31_fake_backend_replays_external_hashes_in_second_persistent_request(
    tmp_path: Path,
) -> None:
    import tests.unit.sft1.test_wave1_live_readiness as readiness_fixtures

    loaded = _loaded_runtime()
    preamble = assemble_runtime_preamble(ROOT, loaded.config.source_bindings)
    expected = readiness_fixtures._n31_project_payload(loaded, "mathlib", 2)
    phase_one = cast(dict[str, object], expected["phase_one_task_receipt"])
    phase_two = cast(dict[str, object], expected["phase_two_task_receipt"])
    expected_resolved_hash = cast(str, expected["resolved_lean_hash"])
    expected_receipt_hash = cast(str, expected["resolution_receipt_hash"])

    class FakeN31Backend:
        def __init__(self) -> None:
            self.requests: list[object] = []

        def run(self, request: object) -> LeanResult:
            self.requests.append(request)
            code = cast(str, request.code)  # type: ignore[attr-defined]
            if request.metadata["n31_receipt_kind"] == "n31_proposal_resolution":  # type: ignore[attr-defined]
                payload = phase_one
                ordinal = 1
            else:
                assert request.metadata["n31_receipt_kind"] == "n31_frozen_nonactivation"  # type: ignore[attr-defined]
                assert "emitN31FrozenNonActivationReceipt" in code
                assert expected_resolved_hash in code
                assert expected_receipt_hash in code
                payload = phase_two
                ordinal = 2
            raw = tmp_path / f"raw-{ordinal}.json"
            raw.write_text(json.dumps({"phase": ordinal}) + "\n", encoding="utf-8")
            return LeanResult(
                request_id=request.request_id,  # type: ignore[attr-defined]
                request_hash=hash_canonical({"code": code}),
                context_id=request.context_id,  # type: ignore[attr-defined]
                context_fingerprint=cast(str, request.context_id)[4:],  # type: ignore[attr-defined]
                status=LeanStatus.VALID,
                messages=({"data": TASK_RECEIPT_MARKER + json.dumps(payload, sort_keys=True)},),
                elapsed_ms=ordinal,
                raw_response_path=str(raw),
            )

        def run_batch(self, requests: object) -> list[LeanResult]:  # pragma: no cover
            raise AssertionError(requests)

        def close(self) -> None:
            return None

    backend = FakeN31Backend()
    phase_checkpoint_root = tmp_path / "n31-phase-checkpoints"
    execution = execute_n31_resolution_proposal_evidence(
        loaded,
        backend,
        project_id="mathlib",
        assembled_preamble=preamble.text,
        timeout_seconds=300,
        measured_peak_rss_bytes=1024,
        phase_checkpoint_root=phase_checkpoint_root,
    )
    assert len(backend.requests) == 2
    assert execution.proposal.resolved_lean_hash == expected_resolved_hash
    assert execution.proposal.resolution_receipt_hash == expected_receipt_hash
    assert execution.proposal.runtime_activated is False
    assert execution.proposal.semantic_success_conformance_performed is False
    assert execution.proposal.row_or_gate_emitted is False
    assert (phase_checkpoint_root / "mathlib" / "phase_one.json").is_file()
    assert (phase_checkpoint_root / "mathlib" / "phase_two.json").is_file()

    replay_backend = FakeN31Backend()

    def forbidden_run(request: object) -> LeanResult:
        raise AssertionError(f"resumed N31 phase unexpectedly executed: {request}")

    replay_backend.run = forbidden_run  # type: ignore[method-assign]
    replayed = execute_n31_resolution_proposal_evidence(
        loaded,
        replay_backend,
        project_id="mathlib",
        assembled_preamble=preamble.text,
        timeout_seconds=300,
        measured_peak_rss_bytes=1024,
        phase_checkpoint_root=phase_checkpoint_root,
    )
    assert replayed.proposal == execution.proposal
    assert replay_backend.requests == []
