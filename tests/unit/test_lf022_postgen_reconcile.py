"""Fail-closed LF-022 post-generation reconciliation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import leanfaith.generation.lf022_lean_check as checker
from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.generation.lf022_batch import (
    LF022BatchRouteFreezeRequest,
    LF022BatchRunPolicy,
    LF022PublicBatchManifest,
    freeze_lf022_public_batch,
    make_lf022_batch_freeze_request,
    run_lf022_public_batch,
)
from leanfaith.generation.lf022_execution import (
    LF022ExecutionArtifacts,
    LF022GOpenExecutionAdmission,
    LF022GOpenExecutionTask,
    LF022RCPDecodingContract,
    LF022RCPRouteBinding,
)
from leanfaith.generation.lf022_lean_check import (
    LF022LeanCheckError,
    check_lf022_provisional_candidates,
)
from leanfaith.generation.lf022_postgen_reconcile import (
    LF022PostgenReconciliationError,
    LF022PostgenTerminalSelector,
    _selected_historical_batch,
    reconcile_lf022_postgen,
    verify_lf022_postgen_terminal_selector,
    verify_lf022_postgen_terminal_selector_selected_only,
)
from leanfaith.generation.lf022_production import LF022ArtifactBinding
from leanfaith.schemas.ids import make_id
from tests.unit.test_lf022_executor import (
    FakeTransport,
    _batch_request_binding,
    _credentials,
    _fixture,
    _success_response,
)
from tests.unit.test_lf022_lean_check import FakeBackend


def _frozen_qwen_batch(tmp_path: Path):
    admission, task = _fixture(tmp_path, model_id="Qwen/Qwen3.5-397B-A17B")
    frozen = freeze_lf022_public_batch(
        repo_root=tmp_path,
        request_binding=_batch_request_binding(
            tmp_path,
            admission=admission,
            task=task,
        ),
    )
    binding = LF022ArtifactBinding(
        path=frozen.manifest_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(frozen.manifest_path),
    )
    return admission, task, frozen, binding


def _postgen_root(tmp_path: Path, suffix: str = "") -> Path:
    return tmp_path.parent / f"{tmp_path.name}-postgen{suffix}"


def test_reconciliation_keeps_missing_error_and_terminal_distinct(tmp_path: Path) -> None:
    admission, task, frozen, binding = _frozen_qwen_batch(tmp_path)
    task_id = task.execution_task_id

    offline = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=binding,
        policy=LF022BatchRunPolicy(),
    )
    assert offline.report.preflight_only_count == 1
    missing = reconcile_lf022_postgen(
        repo_root=tmp_path,
        manifest_path=frozen.manifest_path,
        output_root=_postgen_root(tmp_path),
    )
    assert missing.reconciliation.state == "live_retry_required"
    assert missing.reconciliation.terminal_task_ids == ()
    assert missing.reconciliation.error_task_ids == ()
    assert missing.reconciliation.missing_task_ids == (task_id,)
    assert missing.retry_plan is not None
    assert missing.retry_plan.routes[0].missing_task_ids == (task_id,)
    assert missing.terminal_selector is None

    # An unexpected executor exception creates an error journal event, not a
    # terminal.  Reconciliation must not make cardinality "work" by relabeling it.
    rejected = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=binding,
        policy=LF022BatchRunPolicy(),
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([]),
    )
    assert rejected.report.error_count == 1
    errored = reconcile_lf022_postgen(
        repo_root=tmp_path,
        manifest_path=frozen.manifest_path,
        output_root=_postgen_root(tmp_path),
    )
    assert errored.reconciliation.state == "live_retry_required"
    assert errored.reconciliation.terminal_task_ids == ()
    assert errored.reconciliation.error_task_ids == (task_id,)
    assert errored.reconciliation.missing_task_ids == ()
    assert errored.retry_plan is not None
    assert errored.retry_plan.routes[0].proposer_family_id == "qwen3"
    assert errored.retry_plan.routes[0].model_id == admission.route.model_id
    assert errored.retry_plan.routes[0].error_task_ids == (task_id,)

    # Explicit live resume recovers the ambiguous started transport as the
    # executor's genuine transport_unknown terminal without another network call.
    recovery_transport = FakeTransport([])
    resumed = run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=binding,
        policy=LF022BatchRunPolicy(),
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=recovery_transport,
    )
    assert recovery_transport.calls == 0
    assert resumed.report.new_terminal_count == 1
    assert resumed.report.terminal_status_counts == {"transport_unknown": 1}
    complete = reconcile_lf022_postgen(
        repo_root=tmp_path,
        manifest_path=frozen.manifest_path,
        output_root=_postgen_root(tmp_path),
    )
    assert complete.reconciliation.state == "offline_ready"
    assert complete.reconciliation.terminal_task_ids == (task_id,)
    assert complete.reconciliation.error_task_ids == ()
    assert complete.reconciliation.missing_task_ids == ()
    assert complete.reconciliation.historic_error_task_ids == (task_id,)
    assert complete.retry_plan is None
    assert complete.terminal_selector is not None
    assert complete.terminal_selector_path is not None
    assert complete.terminal_selector.task_count == 1
    assert complete.terminal_selector.routes[0].proposer_family_id == "qwen3"
    verified = verify_lf022_postgen_terminal_selector(
        repo_root=tmp_path,
        selector_path=complete.terminal_selector_path,
    )
    assert verified.selector == complete.terminal_selector
    assert verified.execution_task_ids == (task_id,)
    assert tuple(verified.task_content_hashes) == (task_id,)
    selected = verified.selector.routes[0].tasks[0]
    assert selected.terminal_id.startswith("lf022_execution_terminal:")
    assert selected.terminal_status == "transport_unknown"
    assert selected.terminal_event.path.startswith(frozen.manifest.journal_directory)


def test_reconciliation_is_content_addressed_and_cli_exit_three_is_explicit(
    tmp_path: Path,
) -> None:
    _, task, frozen, binding = _frozen_qwen_batch(tmp_path)
    run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=binding,
        policy=LF022BatchRunPolicy(),
    )
    first = reconcile_lf022_postgen(
        repo_root=tmp_path,
        manifest_path=frozen.manifest_path,
        output_root=_postgen_root(tmp_path),
    )
    second = reconcile_lf022_postgen(
        repo_root=tmp_path,
        manifest_path=frozen.manifest_path,
        output_root=_postgen_root(tmp_path),
    )
    assert first.reconciliation == second.reconciliation
    assert first.reconciliation_path == second.reconciliation_path
    assert first.reconciliation_path.read_bytes() == second.reconciliation_path.read_bytes()
    assert first.retry_plan_path is not None
    retry = json.loads(first.retry_plan_path.read_text(encoding="utf-8"))
    assert retry["routes"][0]["missing_task_ids"] == [task.execution_task_id]
    assert retry["network_calls_this_run"] == 0

    result = CliRunner().invoke(
        app,
        [
            "reconcile-lf022-postgen",
            "--root",
            str(tmp_path),
            "--manifest",
            str(frozen.manifest_path),
            "--output-root",
            str(_postgen_root(tmp_path, "_cli")),
            "--require-offline-ready",
        ],
    )
    assert result.exit_code == 3, result.output
    assert "state=live_retry_required" in result.output
    assert "network_calls_this_run=0" in result.output


def test_reconciliation_rejects_orphan_journal_task(tmp_path: Path) -> None:
    _, _, frozen, binding = _frozen_qwen_batch(tmp_path)
    run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=binding,
        policy=LF022BatchRunPolicy(),
    )
    event = next((tmp_path / frozen.manifest.journal_directory).glob("*/*.json"))
    orphan = event.parent.parent / ("f" * 64) / event.name
    orphan.parent.mkdir()
    orphan.write_bytes(event.read_bytes())

    # The directory itself cannot launder an event belonging to another task.
    with pytest.raises(LF022PostgenReconciliationError, match="noncanonical"):
        reconcile_lf022_postgen(
            repo_root=tmp_path,
            manifest_path=frozen.manifest_path,
            output_root=_postgen_root(tmp_path),
        )


def _completed_selector(tmp_path: Path):
    admission, task, frozen, binding = _frozen_qwen_batch(tmp_path)
    run_lf022_public_batch(
        repo_root=tmp_path,
        manifest_binding=binding,
        policy=LF022BatchRunPolicy(),
        execute_public_provisional=True,
        credentials=_credentials(),
        transport=FakeTransport([_success_response(admission.route.model_id)]),
    )
    result = reconcile_lf022_postgen(
        repo_root=tmp_path,
        manifest_path=frozen.manifest_path,
        output_root=_postgen_root(tmp_path),
    )
    assert result.terminal_selector is not None
    assert result.terminal_selector_path is not None
    return task, frozen, result


def _rewrite_selector(path: Path, mutate) -> Path:
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    from leanfaith.schemas.ids import make_id

    payload = {key: item for key, item in value.items() if key != "selector_id"}
    value["selector_id"] = make_id(
        "lf022_postgen_terminal_selector",
        payload,
    )
    destination = path.with_name("tampered_selector.json")
    destination.write_bytes(canonical_json_bytes(value) + b"\n")
    return destination


def test_selector_replay_rejects_noncanonical_terminal_and_event_paths(tmp_path: Path) -> None:
    task, frozen, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    selected = result.terminal_selector.routes[0].tasks[0]

    canonical_terminal = tmp_path / selected.terminal.path
    alternate_terminal = canonical_terminal.parent / "alternate-terminal.json"
    alternate_terminal.write_bytes(canonical_terminal.read_bytes())
    alternate_terminal_selector = _rewrite_selector(
        result.terminal_selector_path,
        lambda value: value["routes"][0]["tasks"][0]["terminal"].update(
            {
                "path": alternate_terminal.relative_to(tmp_path).as_posix(),
                "sha256": hash_file(alternate_terminal),
            }
        ),
    )
    with pytest.raises(LF022PostgenReconciliationError, match="noncanonical"):
        verify_lf022_postgen_terminal_selector(
            repo_root=tmp_path,
            selector_path=alternate_terminal_selector,
        )

    canonical_event = tmp_path / selected.terminal_event.path
    alternate_event = canonical_event.with_name("terminal-alternate.json")
    alternate_event.write_bytes(canonical_event.read_bytes())
    alternate_event_selector = _rewrite_selector(
        result.terminal_selector_path,
        lambda value: value["routes"][0]["tasks"][0]["terminal_event"].update(
            {
                "path": alternate_event.relative_to(tmp_path).as_posix(),
                "sha256": hash_file(alternate_event),
            }
        ),
    )
    with pytest.raises(LF022PostgenReconciliationError, match="noncanonical"):
        verify_lf022_postgen_terminal_selector(
            repo_root=tmp_path,
            selector_path=alternate_event_selector,
        )
    assert task.execution_task_id.split(":", 1)[1] in selected.terminal.path
    assert frozen.manifest.batch_id == result.terminal_selector.batch_id


def test_selector_replay_rejects_status_and_symlink_tampering(tmp_path: Path) -> None:
    _, _, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    status_selector = _rewrite_selector(
        result.terminal_selector_path,
        lambda value: value["routes"][0]["tasks"][0].update(
            {"terminal_status": "provider_exhausted"}
        ),
    )
    with pytest.raises(LF022PostgenReconciliationError, match="event binding differs"):
        verify_lf022_postgen_terminal_selector(
            repo_root=tmp_path,
            selector_path=status_selector,
        )

    selector_link = result.terminal_selector_path.with_name("selector-link.json")
    selector_link.symlink_to(result.terminal_selector_path)
    with pytest.raises(LF022PostgenReconciliationError, match="symlink"):
        verify_lf022_postgen_terminal_selector(
            repo_root=tmp_path,
            selector_path=selector_link,
        )


def test_selector_requires_terminal_event_in_bound_journal_snapshot(tmp_path: Path) -> None:
    _, _, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    selected = result.terminal_selector.routes[0].tasks[0]

    def remove_terminal_event(value):
        value["journal_snapshot"] = [
            binding
            for binding in value["journal_snapshot"]
            if binding["path"] != selected.terminal_event.path
        ]
        value["journal_snapshot_hash"] = hash_canonical(value["journal_snapshot"])

    selector_without_event = _rewrite_selector(
        result.terminal_selector_path,
        remove_terminal_event,
    )
    with pytest.raises(LF022PostgenReconciliationError, match="absent from its journal snapshot"):
        verify_lf022_postgen_terminal_selector(
            repo_root=tmp_path,
            selector_path=selector_without_event,
        )


def test_reconciliation_rejects_symlinked_content_addressed_destination(tmp_path: Path) -> None:
    _, frozen, result = _completed_selector(tmp_path)
    bad_root = _postgen_root(tmp_path, "-symlink-root")
    bad_root.mkdir()
    digest = result.reconciliation.reconciliation_id.split(":", 1)[1]
    (bad_root / digest).symlink_to(result.reconciliation_path.parent, target_is_directory=True)
    with pytest.raises(LF022PostgenReconciliationError, match="symlink"):
        reconcile_lf022_postgen(
            repo_root=tmp_path,
            manifest_path=frozen.manifest_path,
            output_root=bad_root,
        )


def test_selector_verifier_cli_prints_only_verified_selector_id(tmp_path: Path) -> None:
    _, _, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    cli = CliRunner().invoke(
        app,
        [
            "verify-lf022-postgen-selector",
            "--root",
            str(tmp_path),
            "--selector",
            str(result.terminal_selector_path),
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert cli.output == result.terminal_selector.selector_id + "\n"


def test_selected_only_selector_replays_full_selected_terminal_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None

    def reject_full_loader(**_kwargs):
        raise AssertionError("selected-only replay must not load the full batch")

    monkeypatch.setattr(
        "leanfaith.generation.lf022_postgen_reconcile.load_lf022_public_batch",
        reject_full_loader,
    )
    before = {
        path.relative_to(tmp_path).as_posix(): hash_file(path)
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    verified = verify_lf022_postgen_terminal_selector_selected_only(
        repo_root=tmp_path,
        selector_path=result.terminal_selector_path,
    )
    assert verified.execution_task_ids == tuple(verified.frozen_tasks_by_id)
    after = {
        path.relative_to(tmp_path).as_posix(): hash_file(path)
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert after == before

    task_id = verified.execution_task_ids[0]
    terminal = json.loads(verified.terminal_paths[task_id].read_text(encoding="utf-8"))
    variants_path = tmp_path / terminal["variants_artifact"]
    original_variants = variants_path.read_bytes()
    variants_path.write_bytes(original_variants + b" ")
    with pytest.raises(
        LF022PostgenReconciliationError,
        match="selector executor terminal replay failed",
    ):
        verify_lf022_postgen_terminal_selector_selected_only(
            repo_root=tmp_path,
            selector_path=result.terminal_selector_path,
        )
    variants_path.write_bytes(original_variants)

    attempt_path = tmp_path / terminal["attempt_artifacts"][0]
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    provider_raw_path = tmp_path / attempt["provider_raw_artifact"]
    provider_raw_path.write_bytes(provider_raw_path.read_bytes() + b" ")
    with pytest.raises(
        LF022PostgenReconciliationError,
        match="selector executor terminal replay failed",
    ):
        verify_lf022_postgen_terminal_selector_selected_only(
            repo_root=tmp_path,
            selector_path=result.terminal_selector_path,
        )


def test_selected_only_batch_envelope_does_not_load_unselected_task_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical_admission, historical_task, frozen, _ = _frozen_qwen_batch(tmp_path)

    decoding_value = historical_admission.route.decoding.model_dump(mode="json")
    decoding_value.update(
        contract_id="qwen3_5_proposer_qualification_v2",
        max_tokens=16_384,
    )
    decoding = LF022RCPDecodingContract.model_validate(decoding_value)
    route_value = historical_admission.route.model_dump(mode="json")
    route_value.update(
        execution_scope="public_provisional_g_open",
        decoding=decoding.model_dump(mode="json"),
    )
    route = LF022RCPRouteBinding.model_validate(route_value)
    artifacts_value = historical_admission.artifacts.model_dump(mode="json")
    artifacts_value["proposer_production_eligibility"] = (
        historical_admission.artifacts.reviewed_route_evidence.model_dump(mode="json")
    )
    artifacts = LF022ExecutionArtifacts.model_validate(artifacts_value)
    admission_value = historical_admission.model_dump(mode="json")
    admission_value.update(
        schema_version=2,
        artifacts=artifacts.model_dump(mode="json"),
        route=route.model_dump(mode="json"),
    )
    admission_payload = {
        key: value for key, value in admission_value.items() if key != "admission_id"
    }
    admission_value["admission_id"] = make_id(
        "lf022_execution_admission",
        admission_payload,
    )
    admission = LF022GOpenExecutionAdmission.model_validate(admission_value)

    task_value = historical_task.model_dump(mode="json")
    task_value["execution_admission_id"] = admission.admission_id
    task_payload = {key: value for key, value in task_value.items() if key != "execution_task_id"}
    task_value["execution_task_id"] = make_id("lf022_execution_task", task_payload)
    selected_task = LF022GOpenExecutionTask.model_validate(task_value)

    fake_tasks = [
        {
            "allocation_task_id": make_id("lf022_production_task", {"fake": index}),
            "execution_task_id": make_id("lf022_execution_task", {"fake": index}),
            "task": {
                "path": f"data/absent_tasks/{index:04d}.json",
                "sha256": f"{index + 1:064x}",
            },
        }
        for index in range(256)
    ]
    request = make_lf022_batch_freeze_request(
        batch_directory="data/selected_only/batch",
        executor_output_root=frozen.manifest.executor_output_root,
        routes=(
            LF022BatchRouteFreezeRequest(
                proposer_family_id="qwen3",
                public_pool_audit_id=admission.public_pool_audit_id,
                allocation_plan_id=admission.allocation_plan_id,
                execution_artifacts=admission.artifacts,
                route=admission.route,
                retry_policy=admission.retry_policy,
                code_tree_hash=admission.code_tree_hash,
                allocation_task_ids=tuple(
                    sorted(
                        [
                            selected_task.allocation_task.task_id,
                            *(row["allocation_task_id"] for row in fake_tasks),
                        ]
                    )
                ),
            ),
        ),
    )
    expanded_request_path = tmp_path / "data/selected_only/request.json"
    expanded_request_path.parent.mkdir(parents=True)
    expanded_request_path.write_bytes(canonical_json_bytes(request.model_dump(mode="json")) + b"\n")
    expanded_request_binding = LF022ArtifactBinding(
        path=expanded_request_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(expanded_request_path),
    )

    admission_path = tmp_path / "data/selected_only/admission.json"
    admission_path.write_bytes(canonical_json_bytes(admission.model_dump(mode="json")) + b"\n")
    admission_binding = LF022ArtifactBinding(
        path=admission_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(admission_path),
    )
    task_path = tmp_path / "data/selected_only/selected_task.json"
    task_path.write_bytes(canonical_json_bytes(selected_task.model_dump(mode="json")) + b"\n")
    task_binding = LF022ArtifactBinding(
        path=task_path.relative_to(tmp_path).as_posix(),
        sha256=hash_file(task_path),
    )
    task_digest = selected_task.execution_task_id.split(":", 1)[1]
    adjacent_task_path = (
        tmp_path
        / frozen.manifest.executor_output_root
        / "tasks"
        / task_digest[:2]
        / task_digest
        / "task.json"
    )
    adjacent_task_path.parent.mkdir(parents=True)
    adjacent_task_path.write_bytes(
        canonical_json_bytes(selected_task.model_dump(mode="json")) + b"\n"
    )

    manifest_value = frozen.manifest.model_dump(mode="json")
    manifest_value["freeze_request"] = expanded_request_binding.model_dump(mode="json")
    manifest_value["freeze_request_id"] = request.request_id
    manifest_value["batch_directory"] = request.batch_directory
    selected_binding = {
        "allocation_task_id": selected_task.allocation_task.task_id,
        "execution_task_id": selected_task.execution_task_id,
        "task": task_binding.model_dump(mode="json"),
    }
    manifest_value["routes"][0].update(
        model_id=admission.route.model_id,
        execution_scope="public_provisional_g_open",
        qualification_state="production_live_qualified",
        admission_id=admission.admission_id,
        admission=admission_binding.model_dump(mode="json"),
        qualification_claim=None,
        public_pool_audit_id=admission.public_pool_audit_id,
        allocation_plan_id=admission.allocation_plan_id,
    )
    manifest_value["routes"][0]["tasks"] = sorted(
        [selected_binding, *fake_tasks],
        key=lambda row: row["execution_task_id"],
    )
    manifest_value["total_task_count"] = len(manifest_value["routes"][0]["tasks"])
    manifest_payload = {key: value for key, value in manifest_value.items() if key != "batch_id"}
    manifest_value["batch_id"] = make_id("lf022_public_batch", manifest_payload)
    manifest = LF022PublicBatchManifest.model_validate(manifest_value)
    expanded_manifest_path = tmp_path / "data/selected_only/batch_manifest.json"
    expanded_manifest_path.write_bytes(
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    )

    journal_binding = {
        "path": "data/selected_only/unused_journal_event.json",
        "sha256": "a" * 64,
    }
    selector_value = {
        "schema_version": 2,
        "selector_id": "lf022_postgen_terminal_selector:" + "0" * 64,
        "batch_id": manifest.batch_id,
        "batch_manifest": {
            "path": expanded_manifest_path.relative_to(tmp_path).as_posix(),
            "sha256": hash_file(expanded_manifest_path),
        },
        "journal_snapshot_hash": hash_canonical([journal_binding]),
        "journal_snapshot": [journal_binding],
        "selection_kind": "verified_terminal_snapshot",
        "task_count": 1,
        "routes": [
            {
                "proposer_family_id": "qwen3",
                "model_id": admission.route.model_id,
                "tasks": [
                    {
                        "execution_task_id": selected_task.execution_task_id,
                        "frozen_task": task_binding.model_dump(mode="json"),
                        "terminal_id": "lf022_execution_terminal:" + "b" * 64,
                        "terminal_status": "provider_exhausted",
                        "terminal": {
                            "path": "data/selected_only/unused_terminal.json",
                            "sha256": "c" * 64,
                        },
                        "terminal_event_id": "lf022_batch_event:" + "d" * 64,
                        "terminal_event": journal_binding,
                    }
                ],
            }
        ],
        "public_sources_only": True,
        "private_source_content_forbidden": True,
        "optional_natural_language_forbidden": True,
        "outputs_provisional_only": True,
        "semantic_labels_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    selector_payload = {key: value for key, value in selector_value.items() if key != "selector_id"}
    selector_value["selector_id"] = make_id(
        "lf022_postgen_terminal_selector",
        selector_payload,
    )
    selector = LF022PostgenTerminalSelector.model_validate(selector_value)

    monkeypatch.setattr(
        "leanfaith.generation.lf022_postgen_reconcile.load_lf022_public_batch",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("selected batch envelope must not invoke the full loader")
        ),
    )
    verified = _selected_historical_batch(repo_root=tmp_path, selector=selector)
    assert tuple(verified.frozen_tasks_by_id) == (selected_task.execution_task_id,)
    assert len(verified.task_bindings_by_id) == 257
    assert not any((tmp_path / row["task"]["path"]).exists() for row in fake_tasks)


def test_selected_only_selector_rejects_selected_task_tampering(tmp_path: Path) -> None:
    _, _, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    selected = result.terminal_selector.routes[0].tasks[0]
    selected_task_path = tmp_path / selected.frozen_task.path
    selected_task_path.write_bytes(selected_task_path.read_bytes() + b" ")
    with pytest.raises(LF022PostgenReconciliationError, match=r"frozen task.*hash differs"):
        verify_lf022_postgen_terminal_selector_selected_only(
            repo_root=tmp_path,
            selector_path=result.terminal_selector_path,
        )


def test_selected_only_selector_rejects_qualification_claim_tampering(tmp_path: Path) -> None:
    _, frozen, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    qualification_claim = frozen.manifest.routes[0].qualification_claim
    assert qualification_claim is not None
    claim_path = tmp_path / qualification_claim.path
    claim_path.write_bytes(claim_path.read_bytes() + b" ")
    with pytest.raises(
        LF022PostgenReconciliationError,
        match="qualification claim hash differs",
    ):
        verify_lf022_postgen_terminal_selector_selected_only(
            repo_root=tmp_path,
            selector_path=result.terminal_selector_path,
        )


def test_lean_checker_rejects_selector_with_alternate_input_root(tmp_path: Path) -> None:
    _, frozen, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    alternate_root = tmp_path / "alternate-executor-root"
    alternate_root.mkdir()
    with pytest.raises(LF022LeanCheckError, match="input_root differs"):
        check_lf022_provisional_candidates(
            repo_root=tmp_path,
            input_root=alternate_root,
            output_root=_postgen_root(tmp_path, "-checks"),
            project_dirs={"mathlib": tmp_path},
            workers=1,
            chunk_size=1,
            timeout_seconds=1,
            postgen_selector_path=result.terminal_selector_path,
        )
    assert frozen.manifest.executor_output_root != alternate_root.name


def test_lean_checker_rechecks_selected_terminal_hash_during_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, frozen, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    verified = verify_lf022_postgen_terminal_selector(
        repo_root=tmp_path,
        selector_path=result.terminal_selector_path,
    )
    monkeypatch.setattr(
        "leanfaith.generation.lf022_postgen_reconcile."
        "verify_lf022_postgen_terminal_selector_selected_only",
        lambda **_: verified,
    )
    task_id = verified.execution_task_ids[0]
    terminal_path = verified.terminal_paths[task_id]
    terminal_path.write_bytes(terminal_path.read_bytes() + b" ")
    with pytest.raises(LF022LeanCheckError, match="terminal hash differs"):
        check_lf022_provisional_candidates(
            repo_root=tmp_path,
            input_root=tmp_path / frozen.manifest.executor_output_root,
            output_root=_postgen_root(tmp_path, "-checks"),
            project_dirs={"mathlib": tmp_path},
            workers=1,
            chunk_size=1,
            timeout_seconds=1,
            postgen_selector_path=result.terminal_selector_path,
        )


def test_lean_checker_requires_explicit_expected_selector_id_match(tmp_path: Path) -> None:
    _, frozen, result = _completed_selector(tmp_path)
    assert result.terminal_selector_path is not None
    with pytest.raises(LF022LeanCheckError, match="explicitly expected selector ID"):
        check_lf022_provisional_candidates(
            repo_root=tmp_path,
            input_root=tmp_path / frozen.manifest.executor_output_root,
            output_root=_postgen_root(tmp_path, "-checks"),
            project_dirs={"mathlib": tmp_path},
            workers=1,
            chunk_size=1,
            timeout_seconds=1,
            postgen_selector_path=result.terminal_selector_path,
            expected_postgen_selector_id=("lf022_postgen_terminal_selector:" + "f" * 64),
        )

    with pytest.raises(LF022LeanCheckError, match="requires a postgen selector"):
        check_lf022_provisional_candidates(
            repo_root=tmp_path,
            input_root=tmp_path / frozen.manifest.executor_output_root,
            output_root=_postgen_root(tmp_path, "-checks-no-selector"),
            project_dirs={"mathlib": tmp_path},
            workers=1,
            chunk_size=1,
            timeout_seconds=1,
            expected_postgen_selector_id=result.terminal_selector.selector_id,
        )


def test_lean_checker_rejects_selector_replacement_before_manifest_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, frozen, result = _completed_selector(tmp_path)
    assert result.terminal_selector is not None
    assert result.terminal_selector_path is not None
    project_dir = tmp_path / "mathlib-project"
    project_dir.mkdir()
    monkeypatch.setattr(
        checker,
        "read_git_revision",
        lambda _path: task.source.source_revision,
    )
    FakeBackend.created = []
    FakeBackend.status_scripts = {}
    original_selector = result.terminal_selector_path.read_bytes()

    def tamper_after_verification(_settings) -> None:
        result.terminal_selector_path.write_bytes(original_selector + b" ")

    output_root = _postgen_root(tmp_path, "-race-checks")
    with pytest.raises(LF022LeanCheckError, match="changed after verification"):
        check_lf022_provisional_candidates(
            repo_root=tmp_path,
            input_root=tmp_path / frozen.manifest.executor_output_root,
            output_root=output_root,
            project_dirs={"mathlib": project_dir},
            workers=1,
            chunk_size=1,
            timeout_seconds=1,
            postgen_selector_path=result.terminal_selector_path,
            expected_postgen_selector_id=result.terminal_selector.selector_id,
            backend_factory=FakeBackend,
            prepare_environment=tamper_after_verification,
        )
    assert not (output_root / "manifest.json").exists()
