"""Typed compiler-root contract and opt-in live pinned-row smoke."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import CLOSED_EXPR_MARKER
from leanfaith.sft1.sprint import compiler_certificate_gate as certificate_gate_module
from leanfaith.sft1.sprint import compiler_replay as compiler_replay_module
from leanfaith.sft1.sprint.compiler_certificate_gate import (
    CompilerTypedCertificateExecution,
    CompilerTypedCertificateGateRunner,
    CompilerTypedCertificateRootOutcome,
    typed_certificate_gate_complete_path,
    verify_typed_certificate_gate,
)
from leanfaith.sft1.sprint.compiler_inventory import (
    InputShard,
    build_compiler_record,
    extract_theorem_signature,
    iter_inventory_records,
    load_pinned_input_shards,
    reconstruct_source,
)
from leanfaith.sft1.sprint.compiler_replay import (
    DOWNSTREAM_MODE,
    CompilerAuditSource,
    CompilerReplayError,
    CompilerTypedHookSpec,
    CompilerTypedWave4Selection,
    _backend_context_fingerprint,
    _backend_settings,
    build_typed_descriptor_batch_request,
    build_typed_descriptor_request,
    build_typed_wave4_selected_batch_request,
    build_typed_wave4_selected_request,
    load_compiler_audit_config,
    parse_typed_descriptor_batch_payloads,
    parse_typed_descriptor_payloads,
    resolve_audit_sources,
    typed_wave4_endpoint_id,
    validate_typed_wave4_selected_batch_result,
    validate_typed_wave4_selected_result,
)
from leanfaith.sft1.sprint.engine import (
    EVIDENCE_MARKER as SPRINT_EVIDENCE_MARKER,
)
from leanfaith.sft1.sprint.engine import (
    engine_semantic_version,
)
from leanfaith.sft1.sprint.square import (
    WAVE4_ROW_KINDS,
    load_wave4_config,
    preselect_wave4_variant_descriptors,
)
from leanfaith.sft1.sprint.store import write_atomic
from tests.unit.sft1.test_wave5_lean_free import _tiny_audit_settings

ROOT = find_repo_root(Path(__file__))
WAVE5_CONFIG = ROOT / "configs/transformations/sft1_value_first_v1/wave5_v1.yaml"
WAVE4_CONFIG = ROOT / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"
LIVE_ROW_INDEX = 7_138


def test_downstream_mode_names_the_proof_certified_typed_hook() -> None:
    assert DOWNSTREAM_MODE == "proof_certified_typed_sft1_hook_v1"


def test_lean_compiler_hook_is_additive_and_reuses_shared_certifiers() -> None:
    source = (ROOT / "LeanFaith/Meta/SFT1/Sprint.lean").read_text(encoding="utf-8")
    imported_start = source.index("def loadRoot (name : Name)")
    imported_end = source.index("def loadCompilerRootChecked", imported_start)
    imported_loader = source[imported_start:imported_end]
    assert 'throwNA "root_not_imported"' in imported_loader
    assert "loadCompilerRootChecked" not in imported_loader

    compiler_start = source.index("def loadCompilerRootChecked")
    compiler_end = source.index("structure BinderReport", compiler_start)
    compiler_loader = source[compiler_start:compiler_end]
    assert 'throwNA "compiler_root_not_local"' in compiler_loader
    assert 'checkedProof "compiler_source"' in compiler_loader
    assert "proofValue" in compiler_loader

    process_start = source.index("def processCompilerRoot")
    process_end = source.index("def rebuildCompilerPairs", process_start)
    process = source[process_start:process_end]
    assert "runOp root op" in process
    assert "compilerWave4DescriptorPayload" in process

    selected_start = source.index("def rebuildSelectedCompilerWave4Orbits")
    selected_end = source.index("def emitSelectedCompilerWave4Report", selected_start)
    selected = source[selected_start:selected_end]
    assert "buildWave4Descriptors" in selected
    assert "certifyWave4Descriptors" in selected
    assert '("negative_site", siteJson orbit.negativeSite)' in source


def _live_source() -> tuple[Any, CompilerAuditSource]:
    settings = load_compiler_audit_config(WAVE5_CONFIG)
    manifest = json.loads(settings.inventory.manifest_path.read_text(encoding="utf-8"))
    pair = cast(dict[str, Any], manifest["release"]["shards"][0])
    receipt = cast(dict[str, Any], pair["train"])
    path = settings.inventory.release_root / str(receipt["file"])
    assert hash_file(path) == receipt["sha256"]
    shard = InputShard(
        part=int(pair["part"]),
        total_parts=int(pair["total_parts"]),
        split="train",
        file=str(receipt["file"]),
        path=path,
        sha256=str(receipt["sha256"]),
        rows=int(receipt["rows"]),
        valid_rows=int(receipt["labels"]["true"]),
    )
    table = pq.read_table(path, columns=["theorem", "body", "label"])
    theorem = table.column("theorem")[LIVE_ROW_INDEX].as_py()
    body = table.column("body")[LIVE_ROW_INDEX].as_py()
    label = table.column("label")[LIVE_ROW_INDEX].as_py()
    assert isinstance(theorem, str) and isinstance(body, str) and label is True
    draft = build_compiler_record(
        theorem=theorem,
        body=body,
        row_index=LIVE_ROW_INDEX,
        shard=shard,
        pin=settings.inventory.pin,
        project=settings.inventory.project,
    )
    record = dict(draft.record)
    record["dedup"] = {
        "winner_exact_proof_count": 1,
        "normalized_exact_group_count": 1,
        "normalized_proof_count": 1,
    }
    record["inventory_record_sha256"] = hash_canonical(record)
    signature = extract_theorem_signature(theorem)
    full_source = reconstruct_source(theorem, body)
    qualified = cast(dict[str, Any], record["declaration"])["qualified_name_candidate"]
    source = CompilerAuditSource(
        inventory_record=record,
        shard=shard,
        theorem=theorem,
        body=body,
        full_source=full_source,
        context_prefix=signature.context_prefix,
        declaration_source=theorem[signature.declaration_offset :] + "by" + body,
        qualified_name=cast(str, qualified),
    )
    return settings, source


def _tiny_sources(
    tmp_path: Path,
) -> tuple[Any, dict[str, CompilerAuditSource], tuple[InputShard, ...]]:
    settings = _tiny_audit_settings(tmp_path)
    records = list(iter_inventory_records(settings.inventory.output_root))
    by_name = {str(record["declaration"]["name"]): record for record in records}
    shards = load_pinned_input_shards(settings.inventory)
    wanted = [by_name[name] for name in ("Alpha", "StringWide")]
    sources = resolve_audit_sources(settings, wanted, shards=shards)
    return settings, {str(source.qualified_name): source for source in sources}, shards


def _typed_spec() -> CompilerTypedHookSpec:
    return CompilerTypedHookSpec(
        operations=("P18_SYMMETRIZE_EQUALITY_V1", "N25_TOGGLE_EQ_NE_PROOF_V1"),
        orbit_operations=("ORBIT_WAVE4_N25_V1",),
        maximum_depth=3,
        maximum_variants_per_orbit=5,
        selection_salt="typed-hook-batch-test-v1",
    )


def _certificate_gate_spec() -> CompilerTypedHookSpec:
    return CompilerTypedHookSpec(
        operations=("P18_SYMMETRIZE_EQUALITY_V1",),
        orbit_operations=(),
        maximum_depth=3,
        maximum_variants_per_orbit=5,
        selection_salt="typed-certificate-gate-test-v1",
    )


def _two_root_audit_settings(tmp_path: Path) -> Any:
    settings = _tiny_audit_settings(tmp_path)
    rows = list(compiler_replay_module.load_audit_sample(settings))[:2]
    narrowed = replace(settings, expected_rows=2)
    sample_data = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    write_atomic(narrowed.sample_path, sample_data)

    receipt = json.loads(settings.sample_receipt_path.read_text(encoding="utf-8"))
    receipt["file"] = narrowed.sample_path.name
    receipt["rows"] = len(rows)
    receipt["bytes"] = len(sample_data)
    receipt["sha256"] = hash_file(narrowed.sample_path)
    receipt["policy"] = dict(receipt["policy"], size=len(rows))
    receipt["feature_counts"] = {"equality": len(rows)}
    receipt["namespace_status_counts"] = {"simple_namespace_stack_v1": len(rows)}
    receipt["context_complexity_counts"] = {"no_preceding_declarations": len(rows)}
    receipt["context_frequency_counts"] = {"rare_2_4": len(rows)}
    write_atomic(settings.sample_receipt_path, canonical_json_bytes(receipt) + b"\n")
    manifest = json.loads(settings.inventory_manifest_path.read_text(encoding="utf-8"))
    manifest["audit_sample"] = receipt
    write_atomic(settings.inventory_manifest_path, canonical_json_bytes(manifest) + b"\n")
    assert len(compiler_replay_module.load_audit_sample(narrowed)) == 2
    return narrowed


class FakeTypedCertificateExecutor:
    def __init__(self, settings: Any, *, emit_positive: bool = True) -> None:
        self.settings = settings
        self.emit_positive = emit_positive
        self.execute_calls: list[tuple[str, ...]] = []
        self.close_calls = 0

    def execute(self, sources: Any, *, run_id: str) -> CompilerTypedCertificateExecution:
        exact_sources = tuple(sources)
        self.execute_calls.append(tuple(source.root_id for source in exact_sources))
        request_hash = hash_canonical(
            ["fake-typed-certificate-batch", run_id, self.execute_calls[-1]]
        )
        source_proof_check = {
            "meta_checked": True,
            "kernel_checked": True,
            "kernel_level_instantiation": "none",
            "proof_expr_hash_u64": "17",
        }
        outcomes: list[CompilerTypedCertificateRootOutcome] = []
        for source_index, source in enumerate(exact_sources):
            binding = _json_binding(
                compiler_replay_module._typed_source_binding(source, self.settings)
            )
            terminal: dict[str, Any] = {
                "operation_id": "P18_SYMMETRIZE_EQUALITY_V1",
                "status": "not_applicable",
            }
            reference_goal = f"x : Nat\n⊢ x = {source_index}"
            if self.emit_positive:
                terminal = {
                    "operation_id": "P18_SYMMETRIZE_EQUALITY_V1",
                    "status": "retained",
                    "label": True,
                    "candidate_goal": f"x : Nat\n⊢ {source_index} = x",
                    "candidate_alpha_hash": str(23 + source_index),
                    "evidence": {
                        "candidate_truth": "proved_equivalent_to_reference",
                        "equivalence_proof": {"check": source_proof_check},
                    },
                }
            outcomes.append(
                CompilerTypedCertificateRootOutcome(
                    root_id=source.root_id,
                    status="passed",
                    taxonomy="fake_typed_certificates_checked",
                    descriptor_root={
                        "kind": "compiler_root",
                        "root": source.qualified_name,
                        "root_status": "ok",
                        "reference_goal": reference_goal,
                        "reference_alpha_hash": str(19 + source_index),
                        "engine_semantic_version": engine_semantic_version(ROOT),
                        "source_proof_check": source_proof_check,
                        "compiler_source_binding": binding,
                        "terminals": [terminal],
                    },
                    descriptor_payloads={},
                    request_hashes=(request_hash,),
                    raw_response_paths=(f"fake://{source.root_id}",),
                )
            )
        return CompilerTypedCertificateExecution(
            outcomes=tuple(outcomes),
            lean_requests=1,
            lean_elapsed_ms=11,
            backend_wall_seconds=0.02,
            batch_attempts=1,
        )

    def close(self) -> None:
        self.close_calls += 1


def _json_binding(binding: Mapping[str, str]) -> dict[str, str]:
    names = {
        "rootId": "root_id",
        "sourceRowId": "source_row_id",
        "inventoryRecordSha256": "inventory_record_sha256",
        "theoremSha256": "theorem_sha256",
        "proofSourceSha256": "proof_source_sha256",
        "typeSourceSha256": "type_source_sha256",
        "fullSourceSha256": "full_source_sha256",
        "declarationSourceSha256": "declaration_source_sha256",
        "contextSha256": "context_sha256",
        "contextFingerprint": "context_fingerprint",
        "qualifiedName": "qualified_name",
        "sourceRevision": "source_revision",
        "projectRevision": "project_revision",
        "checkerVersion": "checker_version",
    }
    return {json_name: binding[lean_name] for lean_name, json_name in names.items()}


def test_descriptor_batch_uses_one_exact_context_and_one_action_per_root(
    tmp_path: Path,
) -> None:
    settings, sources, _shards = _tiny_sources(tmp_path)
    ordered = (sources["Alpha"], sources["StringWide"])
    prepared = build_typed_descriptor_batch_request(
        ordered,
        settings=settings,
        spec=_typed_spec(),
        context_id="ctx:" + "1" * 64,
        timeout_seconds=30,
        run_id="2" * 64,
    )
    code = cast(str, prepared.request.code)
    first_action = code.index("LeanFaith.SFT1.Sprint.processCompilerRoot")
    assert all(code.index(source.declaration_source) < first_action for source in ordered)
    assert code.count("LeanFaith.SFT1.Sprint.processCompilerRoot") == 2
    assert code.count("def processCompilerRoot") == 1
    assert json.loads(prepared.request.metadata["typed_hook_root_ids"]) == [
        source.root_id for source in ordered
    ]
    assert prepared.sources == ordered
    assert prepared.phase == "descriptor"

    reversed_request = build_typed_descriptor_batch_request(
        tuple(reversed(ordered)),
        settings=settings,
        spec=_typed_spec(),
        context_id="ctx:" + "1" * 64,
        timeout_seconds=30,
        run_id="2" * 64,
    )
    assert reversed_request.request.request_id != prepared.request.request_id


def test_typed_batches_fail_closed_on_context_name_and_source_drift(tmp_path: Path) -> None:
    settings, sources, _shards = _tiny_sources(tmp_path)
    alpha = sources["Alpha"]
    string_wide = sources["StringWide"]
    mixed_context = replace(
        string_wide,
        context_prefix=string_wide.context_prefix + "open Nat\n",
    )
    with pytest.raises(CompilerReplayError, match="byte-identical context"):
        build_typed_descriptor_batch_request(
            (alpha, mixed_context),
            settings=settings,
            spec=_typed_spec(),
            context_id="ctx:" + "1" * 64,
            timeout_seconds=30,
            run_id="2" * 64,
        )

    duplicate_name = replace(string_wide, qualified_name=alpha.qualified_name)
    with pytest.raises(CompilerReplayError, match="unique qualified theorem names"):
        build_typed_descriptor_batch_request(
            (alpha, duplicate_name),
            settings=settings,
            spec=_typed_spec(),
            context_id="ctx:" + "1" * 64,
            timeout_seconds=30,
            run_id="2" * 64,
        )

    unsafe_record = json.loads(json.dumps(alpha.inventory_record))
    unsafe_record["context"]["option_commands"] = ["set_option Elab.async true"]
    unsafe_source = replace(alpha, inventory_record=unsafe_record)
    with pytest.raises(CompilerReplayError, match="source_reenables_async_elaboration"):
        build_typed_descriptor_batch_request(
            (unsafe_source,),
            settings=settings,
            spec=_typed_spec(),
            context_id="ctx:" + "1" * 64,
            timeout_seconds=30,
            run_id="2" * 64,
        )


def test_selected_batch_has_root_scoped_endpoints_and_ordered_identity(tmp_path: Path) -> None:
    settings, sources, _shards = _tiny_sources(tmp_path)
    ordered = (sources["Alpha"], sources["StringWide"])
    selections = tuple(
        CompilerTypedWave4Selection(
            source=source,
            operation_id="ORBIT_WAVE4_N25_V1",
            selected_indices=(1, 4),
        )
        for source in ordered
    )
    prepared = build_typed_wave4_selected_batch_request(
        selections,
        settings=settings,
        spec=_typed_spec(),
        render_scope_id="sft1-wave5-batch-test",
        context_id="ctx:" + "1" * 64,
        timeout_seconds=30,
        run_id="2" * 64,
    )
    code = cast(str, prepared.request.code)
    first_action = code.index("LeanFaith.SFT1.Sprint.rebuildSelectedCompilerWave4Orbits")
    assert all(code.index(source.declaration_source) < first_action for source in ordered)
    assert code.count("LeanFaith.SFT1.Sprint.rebuildSelectedCompilerWave4Orbits") == 2
    assert code[first_action:].count("LeanFaith.GoalV1.emitClosedProp") == 16
    for source in ordered:
        for slot in range(2):
            for endpoint in ("p", "c", "p_prime", "c_prime"):
                assert typed_wave4_endpoint_id(source.root_id, slot, endpoint) in code

    reversed_request = build_typed_wave4_selected_batch_request(
        tuple(reversed(selections)),
        settings=settings,
        spec=_typed_spec(),
        render_scope_id="sft1-wave5-batch-test",
        context_id="ctx:" + "1" * 64,
        timeout_seconds=30,
        run_id="2" * 64,
    )
    assert reversed_request.request.request_id != prepared.request.request_id


def test_descriptor_batch_parser_requires_every_exact_binding(tmp_path: Path) -> None:
    settings, sources, _shards = _tiny_sources(tmp_path)
    ordered = (sources["Alpha"], sources["StringWide"])
    prepared = build_typed_descriptor_batch_request(
        ordered,
        settings=settings,
        spec=_typed_spec(),
        context_id="ctx:" + "1" * 64,
        timeout_seconds=30,
        run_id="2" * 64,
    )
    payloads: list[dict[str, Any]] = []
    source_proof_check = {
        "meta_checked": True,
        "kernel_checked": True,
        "kernel_level_instantiation": "none",
        "proof_expr_hash_u64": "17",
    }
    for source, binding in zip(ordered, prepared.source_bindings, strict=True):
        evidence_binding = _json_binding(binding)
        payloads.extend(
            [
                {
                    "kind": "compiler_root",
                    "root": source.qualified_name,
                    "root_status": "ok",
                    "engine_semantic_version": "typed-test-v1",
                    "source_proof_check": source_proof_check,
                    "compiler_source_binding": evidence_binding,
                },
                {
                    "kind": "wave4_descriptor_root",
                    "root": source.qualified_name,
                    "operation_id": "ORBIT_WAVE4_N25_V1",
                    "status": "not_applicable",
                    "engine_semantic_version": "typed-test-v1",
                    "source_proof_check": source_proof_check,
                    "compiler_source_binding": evidence_binding,
                },
            ]
        )
    messages = [
        {
            "severity": "info",
            "data": "\n".join(SPRINT_EVIDENCE_MARKER + json.dumps(payload) for payload in payloads),
        }
    ]
    parsed = parse_typed_descriptor_batch_payloads(
        ordered, settings=settings, spec=_typed_spec(), messages=messages
    )
    assert list(parsed) == [source.root_id for source in ordered]
    assert all(set(descriptors) == {"ORBIT_WAVE4_N25_V1"} for _root, descriptors in parsed.values())

    payloads[0]["compiler_source_binding"] = dict(
        cast(dict[str, str], payloads[0]["compiler_source_binding"]), root_id="0" * 64
    )
    tampered_messages = [
        {
            "severity": "info",
            "data": "\n".join(SPRINT_EVIDENCE_MARKER + json.dumps(payload) for payload in payloads),
        }
    ]
    with pytest.raises(CompilerReplayError, match="exact compiler source binding"):
        parse_typed_descriptor_batch_payloads(
            ordered,
            settings=settings,
            spec=_typed_spec(),
            messages=tampered_messages,
        )


def test_resolver_reuses_prevalidated_shards_without_rehashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, sources, shards = _tiny_sources(tmp_path)
    alpha_record = sources["Alpha"].inventory_record

    def explode(_settings: Any) -> tuple[InputShard, ...]:
        raise AssertionError("cached source resolution must not reload or rehash input shards")

    monkeypatch.setattr(compiler_replay_module, "load_pinned_input_shards", explode)
    resolved = resolve_audit_sources(settings, (alpha_record,), shards=shards)
    assert resolved[0].root_id == sources["Alpha"].root_id


def test_selected_batch_validator_demultiplexes_exact_root_scoped_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, sources, _shards = _tiny_sources(tmp_path)
    ordered = (sources["Alpha"], sources["StringWide"])
    selections = tuple(
        CompilerTypedWave4Selection(
            source=source,
            operation_id="ORBIT_WAVE4_N25_V1",
            selected_indices=(0,),
        )
        for source in ordered
    )
    endpoint_ids = {
        typed_wave4_endpoint_id(source.root_id, 0, endpoint)
        for source in ordered
        for endpoint in ("p", "c", "p_prime", "c_prime")
    }
    report_lines = [
        SPRINT_EVIDENCE_MARKER
        + json.dumps(
            {
                "kind": "wave4_selected_root",
                "root": selection.source.qualified_name,
                "operation_id": selection.operation_id,
            }
        )
        for selection in selections
    ]
    endpoint_lines = [
        CLOSED_EXPR_MARKER + json.dumps({"endpoint_id": endpoint_id})
        for endpoint_id in sorted(endpoint_ids)
    ]
    result = LeanResult(
        request_id="typed-batch-test",
        request_hash="1" * 64,
        context_id="ctx:" + "2" * 64,
        context_fingerprint="2" * 64,
        status=LeanStatus.VALID,
        messages=({"severity": "info", "data": "\n".join((*report_lines, *endpoint_lines))},),
    )
    calls: list[str] = []

    def fake_root_validator(source: CompilerAuditSource, **kwargs: Any) -> Any:
        calls.append(source.root_id)
        assert kwargs["batch_expected_endpoint_ids"] == endpoint_ids
        assert set(kwargs["_parsed_endpoint_payloads"]) == endpoint_ids
        return ({"root_id": source.root_id}, object(), ())

    monkeypatch.setattr(
        compiler_replay_module,
        "validate_typed_wave4_selected_result",
        fake_root_validator,
    )
    materialized = validate_typed_wave4_selected_batch_result(
        selections,
        settings=settings,
        spec=_typed_spec(),
        descriptor_payloads={source.root_id: {} for source in ordered},
        selected_descriptors={
            source.root_id: cast(Any, (SimpleNamespace(index=0),)) for source in ordered
        },
        render_scope_id="sft1-wave5-batch-test",
        policy=cast(Any, object()),
        result=result,
    )
    assert list(materialized) == [source.root_id for source in ordered]
    assert calls == [source.root_id for source in ordered]

    spoofed = replace(
        result,
        messages=(
            {
                "severity": "info",
                "data": result.messages[0]["data"]
                + "\n"
                + CLOSED_EXPR_MARKER
                + json.dumps({"endpoint_id": "f" * 64 + ".0.p"}),
            },
        ),
    )
    with pytest.raises(CompilerReplayError, match="batch frozen render failed"):
        validate_typed_wave4_selected_batch_result(
            selections,
            settings=settings,
            spec=_typed_spec(),
            descriptor_payloads={source.root_id: {} for source in ordered},
            selected_descriptors={
                source.root_id: cast(Any, (SimpleNamespace(index=0),)) for source in ordered
            },
            render_scope_id="sft1-wave5-batch-test",
            policy=cast(Any, object()),
            result=spoofed,
        )


def test_typed_certificate_gate_batches_resumes_and_verifies_without_lean(
    tmp_path: Path,
) -> None:
    settings = _two_root_audit_settings(tmp_path)
    spec = _certificate_gate_spec()
    policy = load_wave4_config(ROOT, WAVE4_CONFIG).policy
    executor = FakeTypedCertificateExecutor(settings)
    runner = CompilerTypedCertificateGateRunner(
        settings,
        spec,
        policy,
        executor_factory=lambda: executor,
        manage_resources=False,
        verify_project=False,
        required_sample_rows=2,
    )
    first = runner.run()
    assert first.status == "passed"
    assert first.roots == first.passed_roots == 2
    assert first.failed_roots == 0
    assert first.lean_requests == 1
    assert len(executor.execute_calls) == 1
    assert len(executor.execute_calls[0]) == 2
    assert executor.close_calls == 1
    assert first.complete_path == typed_certificate_gate_complete_path(settings)

    complete = json.loads(first.complete_path.read_text(encoding="utf-8"))
    assert complete["status"] == "passed"
    assert complete["roots"] == 2
    assert complete["checks"]["all_source_proofs_checked"] is True
    assert complete["checks"]["requested_positive_output_nonzero"] is True
    assert complete["checks"]["n19_forbidden"] is True
    assert complete["integrity"]["n25_retained_share"] == 0.0
    assert complete["execution_totals"]["lean_requests"] == 1
    assert (
        complete["automated_validation_verdict"]
        == "exhaustive_structural_and_certificate_validation_passed"
    )
    assert complete["manual_inspection_verdict"] == "not_recorded"
    assert complete["proof_contract"]["manual_review_required_by_typed_gate"] is False
    replay = runner.replay()
    assert replay["forced_resume"] is True
    assert replay["cache_hits"] == 2
    assert replay["lean_requests"] == 0
    assert replay["backend_constructed"] is False
    assert replay["resource_claimed"] is False

    def explode() -> Any:
        raise AssertionError("a completed typed gate must not construct an executor")

    resumed = CompilerTypedCertificateGateRunner(
        settings,
        spec,
        policy,
        executor_factory=explode,
        manage_resources=False,
        verify_project=False,
        required_sample_rows=2,
    ).run()
    assert resumed.status == "passed"
    assert resumed.cache_hits == 2
    assert resumed.lean_requests == 0
    verified = verify_typed_certificate_gate(
        settings,
        spec,
        policy,
        required_sample_rows=2,
    )
    assert verified["passed"] is True
    assert verified["terminal_sha256"] == hash_file(first.complete_path)
    assert verified["audit_sample_sha256"] == hash_file(settings.sample_path)
    assert verified["replay"]["forced_resume"] is True
    assert verified["replay"]["lean_requests"] == 0


def test_typed_certificate_gate_cannot_pass_with_empty_requested_yield(
    tmp_path: Path,
) -> None:
    settings = _two_root_audit_settings(tmp_path)
    spec = _certificate_gate_spec()
    policy = load_wave4_config(ROOT, WAVE4_CONFIG).policy
    executor = FakeTypedCertificateExecutor(settings, emit_positive=False)
    runner = CompilerTypedCertificateGateRunner(
        settings,
        spec,
        policy,
        executor_factory=lambda: executor,
        manage_resources=False,
        verify_project=False,
        required_sample_rows=2,
    )

    result = runner.run()

    assert result.status == "failed"
    assert not runner.complete_path.exists()
    terminal = json.loads(runner.status_path.read_text(encoding="utf-8"))
    assert terminal["checks"]["requested_positive_output_nonzero"] is False
    assert terminal["integrity"]["retained_label_counts"] == {"false": 0, "true": 0}
    assert (
        terminal["automated_validation_verdict"]
        == "exhaustive_structural_and_certificate_validation_failed"
    )
    assert terminal["manual_inspection_verdict"] == "not_recorded"


def test_typed_certificate_gate_rejects_cache_and_replay_tampering(tmp_path: Path) -> None:
    settings = _two_root_audit_settings(tmp_path / "cache")
    spec = _certificate_gate_spec()
    policy = load_wave4_config(ROOT, WAVE4_CONFIG).policy
    executor = FakeTypedCertificateExecutor(settings)
    runner = CompilerTypedCertificateGateRunner(
        settings,
        spec,
        policy,
        executor_factory=lambda: executor,
        manage_resources=False,
        verify_project=False,
        required_sample_rows=2,
    )
    runner.run()
    complete = json.loads(runner.complete_path.read_text(encoding="utf-8"))
    cache_path = Path(complete["cache_receipts"][0]["cache_path"])
    cache_record = json.loads(cache_path.read_text(encoding="utf-8"))
    cache_record["taxonomy"] = "tampered"
    write_atomic(cache_path, canonical_json_bytes(cache_record) + b"\n")
    with pytest.raises(CompilerReplayError, match="cache content hash differs"):
        runner.replay()

    replay_settings = _two_root_audit_settings(tmp_path / "replay")
    replay_executor = FakeTypedCertificateExecutor(replay_settings)
    replay_runner = CompilerTypedCertificateGateRunner(
        replay_settings,
        spec,
        policy,
        executor_factory=lambda: replay_executor,
        manage_resources=False,
        verify_project=False,
        required_sample_rows=2,
    )
    replay_runner.run()
    replay_complete = json.loads(replay_runner.complete_path.read_text(encoding="utf-8"))
    replay_path = Path(replay_complete["forced_resume_replay_path"])
    replay_record = json.loads(replay_path.read_text(encoding="utf-8"))
    replay_record["lean_requests"] = 1
    write_atomic(replay_path, canonical_json_bytes(replay_record) + b"\n")
    with pytest.raises(CompilerReplayError, match="forced-resume receipt differs"):
        replay_runner.replay()


def test_compiler_replay_cli_exposes_typed_gate_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = SimpleNamespace(output_root=tmp_path)
    complete_path = tmp_path / "typed_certificate_gate" / "complete.json"
    write_atomic(complete_path, b"{}\n")
    monkeypatch.setattr(
        certificate_gate_module,
        "load_typed_certificate_gate_config",
        lambda _path: (settings, object(), object()),
    )
    monkeypatch.setattr(
        certificate_gate_module,
        "verify_typed_certificate_gate",
        lambda _settings, _spec, _policy, **_kwargs: {
            "passed": True,
            "terminal_sha256": "a" * 64,
        },
    )
    result = compiler_replay_module.main(
        ["--config", str(tmp_path / "wave5.yaml"), "typed-gate-status"]
    )
    assert result == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True


def test_typed_certificate_gate_forbids_n19_and_caps_n25_whole_groups(
    tmp_path: Path,
) -> None:
    settings = _two_root_audit_settings(tmp_path)
    policy = load_wave4_config(ROOT, WAVE4_CONFIG).policy
    forbidden_spec = _certificate_gate_spec()
    object.__setattr__(
        forbidden_spec,
        "operations",
        ("N19_WHOLE_CLAIM_NEGATION_V1",),
    )
    with pytest.raises(CompilerReplayError, match="forbids N19"):
        CompilerTypedCertificateGateRunner(
            settings,
            forbidden_spec,
            policy,
            manage_resources=False,
            verify_project=False,
            required_sample_rows=2,
        )

    sample = compiler_replay_module.load_audit_sample(settings)
    sources = resolve_audit_sources(settings, sample)
    cap_spec = CompilerTypedHookSpec(
        operations=("P18_SYMMETRIZE_EQUALITY_V1", "N25_TOGGLE_EQ_NE_PROOF_V1"),
        orbit_operations=(),
        selection_salt="typed-gate-n25-cap-test-v1",
    )
    logical_pairs: list[dict[str, Any]] = []
    for index in range(3):
        logical_pairs.append(
            certificate_gate_module._typed_gate_pair(
                source=sources[0],
                operation_id="P18_SYMMETRIZE_EQUALITY_V1",
                negative_operation=None,
                row_kind="wave3_operation",
                label=True,
                reference="⊢ True",
                candidate=f"⊢ {index} = {index}",
                group_id=hash_canonical(["positive", index]),
                group_size=1,
            )
        )
    for index in range(4):
        logical_pairs.append(
            certificate_gate_module._typed_gate_pair(
                source=sources[0],
                operation_id="N25_TOGGLE_EQ_NE_PROOF_V1",
                negative_operation="N25_TOGGLE_EQ_NE_PROOF_V1",
                row_kind="wave3_operation",
                label=False,
                reference=f"⊢ {index} = {index}",
                candidate=f"⊢ {index} ≠ {index}",
                group_id=hash_canonical(["negative", index]),
                group_size=1,
            )
        )
    integrity = certificate_gate_module._typed_gate_integrity(
        sources,
        (
            {"root_id": sources[0].root_id, "status": "passed", "pairs": logical_pairs},
            {"root_id": sources[1].root_id, "status": "passed", "pairs": []},
        ),
        spec=cap_spec,
        policy=policy,
    )
    assert integrity["n25_retained_rows"] == 1
    assert integrity["retained_rows_after_n25_cap"] == 4
    assert integrity["n25_retained_share"] == 0.25
    assert integrity["n25_share_cap"]["operation_dropped_group_count"] == 3
    assert integrity["checks"]["n25_retained_share_at_most_one_quarter"] is True


def test_typed_certificate_integrity_models_only_exact_cross_group_shared_rows(
    tmp_path: Path,
) -> None:
    settings = _two_root_audit_settings(tmp_path)
    sources = resolve_audit_sources(settings, compiler_replay_module.load_audit_sample(settings))
    spec = _certificate_gate_spec()
    policy = load_wave4_config(ROOT, WAVE4_CONFIG).policy
    first = certificate_gate_module._typed_gate_pair(
        source=sources[0],
        operation_id="P18_SYMMETRIZE_EQUALITY_V1",
        negative_operation=None,
        row_kind="wave3_operation",
        label=True,
        reference="x : Nat\n⊢ x = 0",
        candidate="x : Nat\n⊢ 0 = x",
        group_id=hash_canonical(["shared", 1]),
        group_size=1,
    )
    shared = certificate_gate_module._typed_gate_pair(
        source=sources[0],
        operation_id="P18_SYMMETRIZE_EQUALITY_V1",
        negative_operation=None,
        row_kind="wave3_operation",
        label=True,
        reference="x : Nat\n⊢ x = 0",
        candidate="x : Nat\n⊢ 0 = x",
        group_id=hash_canonical(["shared", 2]),
        group_size=1,
    )
    records = (
        {"root_id": sources[0].root_id, "status": "passed", "pairs": [first, shared]},
        {"root_id": sources[1].root_id, "status": "passed", "pairs": []},
    )

    integrity = certificate_gate_module._typed_gate_integrity(
        sources, records, spec=spec, policy=policy
    )

    assert integrity["duplicate_stable_ids"] == 1
    assert integrity["duplicate_stable_id_classes"] == 1
    assert integrity["duplicate_pair_classes"] == 1
    assert integrity["duplicate_same_label_pair_classes"] == 1
    assert integrity["modeled_cross_group_stable_id_references"] == 1
    assert integrity["modeled_cross_group_pair_class_references"] == 1
    assert integrity["unmodeled_duplicate_stable_ids"] == 0
    assert integrity["unmodeled_duplicate_pair_classes"] == 0
    assert all(integrity["checks"].values())

    repeated = dict(shared, group_id=first["group_id"])
    with pytest.raises(CompilerReplayError, match="repeats a physical row within a group"):
        certificate_gate_module._typed_gate_integrity(
            sources,
            (
                {
                    "root_id": sources[0].root_id,
                    "status": "passed",
                    "pairs": [first, repeated],
                },
                {"root_id": sources[1].root_id, "status": "passed", "pairs": []},
            ),
            spec=spec,
            policy=policy,
        )

    drifted = certificate_gate_module._typed_gate_pair(
        source=sources[0],
        operation_id="P18_SYMMETRIZE_EQUALITY_V1",
        negative_operation=None,
        row_kind="wave3_operation",
        label=True,
        reference="x : Nat\n⊢ 0 = x",
        candidate="x : Nat\n⊢ x = 0",
        group_id=hash_canonical(["shared", 3]),
        group_size=1,
    )
    with pytest.raises(CompilerReplayError, match="identity/content drift"):
        certificate_gate_module._typed_gate_integrity(
            sources,
            (
                {
                    "root_id": sources[0].root_id,
                    "status": "passed",
                    "pairs": [first, drifted],
                },
                {"root_id": sources[1].root_id, "status": "passed", "pairs": []},
            ),
            spec=spec,
            policy=policy,
        )


def test_typed_certificate_integrity_requires_useful_new_negative_families(
    tmp_path: Path,
) -> None:
    settings = _two_root_audit_settings(tmp_path)
    sources = resolve_audit_sources(settings, compiler_replay_module.load_audit_sample(settings))
    operations = (
        "N26_INCREMENT_BOUND_PROOF_V1",
        "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
        "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
        "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    )
    spec = CompilerTypedHookSpec(operations=operations, orbit_operations=())
    policy = load_wave4_config(ROOT, WAVE4_CONFIG).policy

    def negative_pair(operation: str, index: int) -> dict[str, Any]:
        return certificate_gate_module._typed_gate_pair(
            source=sources[0],
            operation_id=operation,
            negative_operation=operation,
            row_kind="wave3_operation",
            label=False,
            reference=f"x : Nat\n⊢ x = {index}",
            candidate=f"x : Nat\n⊢ x ≠ {index}",
            group_id=hash_canonical(["useful-negative", operation]),
            group_size=1,
        )

    pairs = [negative_pair(operation, index) for index, operation in enumerate(operations)]

    below_minimum = certificate_gate_module._typed_gate_integrity(
        sources,
        (
            {"root_id": sources[0].root_id, "status": "passed", "pairs": pairs[:2]},
            {"root_id": sources[1].root_id, "status": "passed", "pairs": []},
        ),
        spec=spec,
        policy=policy,
    )
    requirements = below_minimum["requested_output_requirements"]
    assert requirements["required_useful_new_wave3_negative_families"] == 3
    assert len(requirements["useful_new_wave3_negative_families"]) == 2
    assert below_minimum["checks"]["requested_negative_output_nonzero"] is True
    assert below_minimum["checks"]["requested_new_wave3_negative_family_minimum_met"] is False

    at_minimum = certificate_gate_module._typed_gate_integrity(
        sources,
        (
            {"root_id": sources[0].root_id, "status": "passed", "pairs": pairs[:3]},
            {"root_id": sources[1].root_id, "status": "passed", "pairs": []},
        ),
        spec=spec,
        policy=policy,
    )
    assert at_minimum["checks"]["requested_new_wave3_negative_family_minimum_met"] is True


def test_typed_certificate_integrity_requires_a_complete_requested_wave4_closure(
    tmp_path: Path,
) -> None:
    settings = _two_root_audit_settings(tmp_path)
    sources = resolve_audit_sources(settings, compiler_replay_module.load_audit_sample(settings))
    negative = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    orbit = "ORBIT_WAVE4_N31_V1"
    spec = CompilerTypedHookSpec(operations=(negative,), orbit_operations=(orbit,))
    policy = load_wave4_config(ROOT, WAVE4_CONFIG).policy
    direct = certificate_gate_module._typed_gate_pair(
        source=sources[0],
        operation_id=negative,
        negative_operation=negative,
        row_kind="wave3_operation",
        label=False,
        reference="x : Nat\n⊢ x = 0",
        candidate="x : Nat\n⊢ x ≠ 0",
        group_id=hash_canonical(["wave3", negative]),
        group_size=1,
    )

    missing = certificate_gate_module._typed_gate_integrity(
        sources,
        (
            {"root_id": sources[0].root_id, "status": "passed", "pairs": [direct]},
            {"root_id": sources[1].root_id, "status": "passed", "pairs": []},
        ),
        spec=spec,
        policy=policy,
    )
    assert missing["checks"]["requested_new_wave3_negative_family_minimum_met"] is True
    assert missing["checks"]["requested_wave4_closure_output_nonzero"] is False

    closure_group = hash_canonical(["wave4", orbit])
    closure = [
        certificate_gate_module._typed_gate_pair(
            source=sources[0],
            operation_id=orbit,
            negative_operation=negative,
            row_kind=row_kind,
            label=label,
            reference=f"x : Nat\n⊢ x = {10 + index}",
            candidate=f"x : Nat\n⊢ x = {20 + index}",
            group_id=closure_group,
            group_size=len(WAVE4_ROW_KINDS),
        )
        for index, (row_kind, label, *_rest) in enumerate(WAVE4_ROW_KINDS)
    ]
    complete = certificate_gate_module._typed_gate_integrity(
        sources,
        (
            {
                "root_id": sources[0].root_id,
                "status": "passed",
                "pairs": [direct, *closure],
            },
            {"root_id": sources[1].root_id, "status": "passed", "pairs": []},
        ),
        spec=spec,
        policy=policy,
    )
    assert complete["complete_wave4_closure_groups"] == 1
    assert complete["checks"]["requested_wave4_closure_output_nonzero"] is True


@pytest.mark.skipif(
    os.environ.get("LEANFAITH_RUN_LIVE_WAVE5_TYPED") != "1",
    reason="requires the pinned CPT2 release and one claimed persistent Lean worker",
)
def test_one_real_pinned_cpt2_row_runs_wave3_and_selected_wave4() -> None:
    base_settings, source = _live_source()
    output_root = Path(
        os.environ.get(
            "LEANFAITH_WAVE5_TYPED_SMOKE_ROOT",
            "/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/"
            "wave5/compiler_typed_hook_smoke_v1",
        )
    )
    settings = replace(
        base_settings,
        output_root=output_root,
        lean_workers=1,
        lean_rss_claim_gib=24,
    )
    spec = CompilerTypedHookSpec(
        operations=("P18_SYMMETRIZE_EQUALITY_V1", "N25_TOGGLE_EQ_NE_PROOF_V1"),
        orbit_operations=("ORBIT_WAVE4_N25_V1",),
        maximum_depth=3,
        maximum_variants_per_orbit=5,
        selection_salt="sft1-wave5-live-pinned-row-v1",
    )
    context_id = "ctx:" + _backend_context_fingerprint(settings)
    run_id = hash_canonical(
        {
            "kind": "sft1_wave5_typed_live_v1",
            "root_id": source.root_id,
            "spec": spec.semantic_payload(),
        }
    )
    descriptor = build_typed_descriptor_request(
        source,
        settings=settings,
        spec=spec,
        context_id=context_id,
        timeout_seconds=900,
        run_id=run_id,
    )
    backend = LeanInteractBackend(_backend_settings(settings))
    try:
        descriptor_result = backend.run(descriptor.request)
        assert descriptor_result.status == LeanStatus.VALID
        assert not descriptor_result.sorries
        root_payload, descriptor_payloads = parse_typed_descriptor_payloads(
            source, spec, descriptor_result.messages
        )
        assert root_payload["root_status"] == "ok"
        assert root_payload["source_proof_check"]["meta_checked"] is True
        assert root_payload["source_proof_check"]["kernel_checked"] is True
        terminals = {item["operation_id"]: item for item in root_payload["terminals"]}
        assert terminals["P18_SYMMETRIZE_EQUALITY_V1"]["status"] == "retained"
        assert terminals["N25_TOGGLE_EQ_NE_PROOF_V1"]["status"] == "retained"

        operation_id = "ORBIT_WAVE4_N25_V1"
        descriptor_payload = descriptor_payloads[operation_id]
        assert descriptor_payload["status"] == "described"
        policy = load_wave4_config(ROOT, WAVE4_CONFIG).policy
        chosen = preselect_wave4_variant_descriptors(
            descriptor_payload,
            operation_id=operation_id,
            policy=policy,
            maximum_depth=spec.maximum_depth,
            expected_root=source.qualified_name,
            selection_root_id=source.root_id,
        )
        assert 1 <= len(chosen) <= 5
        selected = build_typed_wave4_selected_request(
            source,
            settings=settings,
            spec=spec,
            operation_id=operation_id,
            selected_indices=[item.index for item in chosen],
            render_scope_id=f"sft1-wave5-live:{source.root_id}",
            context_id=context_id,
            timeout_seconds=900,
            run_id=run_id,
        )
        selected_result = backend.run(selected.request)
        assert selected_result.status == LeanStatus.VALID
        assert not selected_result.sorries
        combined, validated, selected_records = validate_typed_wave4_selected_result(
            source,
            settings=settings,
            spec=spec,
            operation_id=operation_id,
            descriptor_payload=descriptor_payload,
            selected_descriptors=chosen,
            render_scope_id=f"sft1-wave5-live:{source.root_id}",
            policy=policy,
            result=selected_result,
        )
        assert len(validated.variants) == len(chosen)
        assert len(selected_records) == len(chosen)
        assert combined["compiler_source_binding"]["root_id"] == source.root_id
    finally:
        backend.close()

    receipt = {
        "artifact_kind": "sft1_wave5_compiler_typed_hook_live_smoke",
        "schema_version": 1,
        "run_id": run_id,
        "root_id": source.root_id,
        "source_row_id": source.inventory_record["source_row_id"],
        "inventory_record_sha256": source.inventory_record_sha256,
        "source_locator": dict(cast(dict[str, Any], source.inventory_record["source"])),
        "source_hashes": dict(cast(dict[str, Any], source.inventory_record["hashes"])),
        "context": {
            "context_sha256": source.inventory_record["context"]["context_sha256"],
            "context_fingerprint": source.context_fingerprint,
        },
        "engine_source_sha256": hash_file(settings.engine_path),
        "engine_semantic_version": root_payload["engine_semantic_version"],
        "wave3_statuses": {
            operation: terminal["status"] for operation, terminal in sorted(terminals.items())
        },
        "wave4": {
            "operation_id": operation_id,
            "enumerated_descriptors": descriptor_payload["enumerated_descriptor_count"],
            "selected_indices": [item.index for item in chosen],
            "selected_variants": len(validated.variants),
        },
        "lean": {
            "requests": 2,
            "descriptor_elapsed_ms": descriptor_result.elapsed_ms,
            "selected_elapsed_ms": selected_result.elapsed_ms,
            "descriptor_request_hash": descriptor_result.request_hash,
            "selected_request_hash": selected_result.request_hash,
            "descriptor_raw_response_path": descriptor_result.raw_response_path,
            "selected_raw_response_path": selected_result.raw_response_path,
        },
    }
    write_atomic(output_root / "receipt.json", canonical_json_bytes(receipt) + b"\n")
