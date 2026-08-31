"""Lean-free invariants for the additive Wave 1 census implementation."""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import load_config
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.cache import EvidenceCacheKey, make_evidence_cache_entry
from leanfaith.schemas.enums import EvidenceExecutionStatus, EvidenceKind, EvidenceTargetKind
from leanfaith.schemas.evidence import AuditValue, EvidenceRecord
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    PAIR_PREFIX,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    make_id,
)
from leanfaith.sft1 import wave1_census_v0_3_6 as census
from leanfaith.sft1 import wave1_runtime as runtime

_REAL_BIND_CLEAN_RUNTIME_COMMIT = census._bind_clean_runtime_commit
_REAL_VERIFY_RECORDED_RUNTIME_COMMIT = census._verify_recorded_runtime_commit


def _fake_runtime_binding(
    *,
    n31_admitted: bool = True,
    n31_activation_authorized: bool = True,
) -> census.FinalizedRuntimeSmokeBinding:
    identity = census.N31AdmittedBankIdentity(
        project_id="cslib",
        bank_id="n31-cslib-admitted-v1",
        resolved_lean_hash=sha256_hex(b"n31-resolved-lean"),
        resolution_receipt_hash=sha256_hex(b"n31-resolution-receipt"),
    )
    return census.FinalizedRuntimeSmokeBinding(
        runtime_config_path=census.RUNTIME_CONFIG_PATH.as_posix(),
        runtime_config_file_sha256=sha256_hex(b"runtime-config-file"),
        runtime_config_semantic_hash=sha256_hex(b"runtime-config-semantic"),
        runtime_loader_path=census.RUNTIME_LOADER_PATH.as_posix(),
        runtime_loader_file_sha256=sha256_hex(b"runtime-loader"),
        runtime_helper_path="LeanFaith/Meta/SFT1/Wave1Runtime.lean",
        runtime_helper_file_sha256=sha256_hex(b"runtime-helper"),
        assembled_preamble_sha256=sha256_hex(b"runtime-preamble"),
        operations=tuple(
            census.RuntimeOperationSmokeBinding(
                operation_id=cast(census.PrimaryOperation, operation),
                registry_entry_hash=sha256_hex(f"registry:{operation}".encode()),
                anchor_hash=sha256_hex(f"anchor:{operation}".encode()),
                operation_bank_entry_hash=sha256_hex(f"bank:{operation}".encode()),
                runtime_fixture_bundle_hash=sha256_hex(f"fixture:{operation}".encode()),
                dispatch_symbol="LeanFaith.SFT1.Wave1.dispatchAt",
                checker_symbol="LeanFaith.SFT1.Wave1.replayCertificate",
                runtime_status=(
                    "n31_admitted_for_smoke"
                    if operation == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1" and n31_admitted
                    else (
                        "n31_resolution_proposal_only_not_admitted"
                        if operation == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
                        else "positive_implementation_authorized_pending_live_receipt"
                    )
                ),
            )
            for operation in census.PRIMARY_OPERATIONS
        ),
        compile_contexts=tuple(
            census.RuntimeCompileContextBinding(
                source_id=cast(census.SourceId, source_id),
                compile_context_identity=f"ctx:{sha256_hex(f'context:{source_id}'.encode())}",
                compile_context_fingerprint=sha256_hex(f"context:{source_id}".encode()),
            )
            for source_id in census.EXPECTED_SOURCE_IDS
        ),
        n31_activation_authorized=n31_activation_authorized,
        n31_admitted_identities=(identity,) if n31_admitted else (),
    )


@pytest.fixture(autouse=True)
def _bind_fake_finalized_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _fake_runtime_binding()
    monkeypatch.setattr(census, "_load_finalized_runtime_binding", lambda _loaded: binding)
    runtime_commit = census._current_runtime_commit(find_repo_root())
    monkeypatch.setattr(
        census,
        "_bind_clean_runtime_commit",
        lambda _loaded, **_kwargs: runtime_commit,
    )
    monkeypatch.setattr(census, "_verify_recorded_runtime_commit", lambda *_args: None)


def _root(
    loaded: census.LoadedCensusConfig,
    *,
    source_id: str,
    locator: str,
    operations: tuple[str, ...],
    evidence_level: str | None = None,
    typed_operations: tuple[str, ...] = (),
    current_meta_receipt_hash: str | None = None,
    blocked: bool = False,
    private: bool = False,
    golden_exclusion: str | None = None,
    proof_status: str = "unknown",
    proof_hash: str | None = None,
    cluster_tag: str | None = None,
) -> census.RootRecord:
    source = next(item for item in loaded.config.sources if item.source_id == source_id)
    level = evidence_level or (
        "preexisting_typed" if source_id == "compiler_data" else "typed_pending"
    )
    text_hash = sha256_hex(f"text:{source_id}:{locator}".encode())
    upstream_hash = (
        None
        if level == "surface_prefilter"
        else sha256_hex(f"upstream:{source_id}:{locator}".encode())
    )
    tag = cluster_tag or locator
    needs_context = level in ("typed_pending", "typed")
    if level == "typed" and current_meta_receipt_hash is None:
        raise AssertionError("typed test root requires its exact Meta receipt hash")
    exclusion_reason = (
        "private_declaration"
        if private
        else ("proof_placeholder" if blocked else (golden_exclusion or "none"))
    )
    return census.RootRecord(
        root_id=census.make_root_id(source_id, source.revision, locator, text_hash),
        source_id=source_id,
        source_revision=source.revision,
        source_locator=locator,
        source_text_hash=text_hash,
        signature_text_hash=sha256_hex(f"signature:{tag}".encode()),
        surface_identity_hash=sha256_hex(f"exact:{tag}".encode()),
        near_identity_hash=sha256_hex(f"alpha:{tag}".encode()),
        structure_identity_hash=sha256_hex(f"structure:{tag}".encode()),
        evidence_level=cast(census.EvidenceLevel, level),
        upstream_evidence_kind=(
            "none"
            if level == "surface_prefilter"
            else (
                "compiler_data_validation"
                if source_id == "compiler_data"
                else "git_source_declaration"
            )
        ),
        upstream_typed_evidence_hash=upstream_hash,
        compile_context_available=needs_context,
        closed_expr_route_available=needs_context,
        current_meta_receipt_hash=current_meta_receipt_hash,
        blocklist_screened=True,
        blocklist_file_sha256=census.GOLDEN_BLOCKLIST_SHA256,
        blocklist_procedure_id=census.GOLDEN_BLOCKLIST_PROCEDURE_ID,
        golden_near_dup_hash=sha256_hex(f"golden:{tag}".encode()),
        private_declaration=private,
        proof_placeholder_detected=blocked,
        golden_blocklist_hit=golden_exclusion is not None,
        exclusion_reason=cast(Any, exclusion_reason),
        root_blocklisted=exclusion_reason != "none",
        internal_gate_eligible=True,
        operation_candidates=cast(tuple[census.PrimaryOperation, ...], operations),
        typed_applicable_operations=cast(tuple[census.PrimaryOperation, ...], typed_operations),
        n31_proof_status=cast(Any, proof_status),
        n31_proof_payload_hash=proof_hash,
    )


def _typed_entry(
    tmp_path: Path,
    base: census.RootRecord,
    operation: str,
    *,
    suffix: str,
) -> tuple[census.RootRecord, census.SmokeManifestEntry]:
    typed_operations = (operation,)
    runtime_binding = _fake_runtime_binding()
    runtime_operation = next(
        item for item in runtime_binding.operations if item.operation_id == operation
    )
    context = next(
        item for item in runtime_binding.compile_contexts if item.source_id == base.source_id
    )
    receipt_path = tmp_path / f"meta-{suffix}.json"
    source_expr_hash = sha256_hex(f"expr-source:{suffix}".encode())
    candidate_expr_hash = sha256_hex(f"expr-candidate:{suffix}".encode())
    source_sidecar_sha256 = sha256_hex(f"sidecar-source:{suffix}".encode())
    candidate_sidecar_sha256 = sha256_hex(f"sidecar-candidate:{suffix}".encode())
    render_request_hash = sha256_hex(f"request:{suffix}".encode())
    selected_site_path = (
        runtime.p01_outer_binder_site_path(0) if operation == "P01_ALPHA_RENAME_SINGLE_V1" else "/"
    )
    p01_delta = (
        runtime.P01NameOnlyDelta(
            old_name="x",
            new_name="x_alpha",
            binder_info="default",
            selected_site_ordinal=0,
            selected_site_rediscovery_count=1,
            domains_unchanged=True,
            bodies_unchanged_except_selected_name=True,
            bound_variable_indices_unchanged=True,
            universes_unchanged=True,
            metadata_unchanged=True,
            other_binders_unchanged=True,
            binder_info_unchanged=True,
        )
        if operation == "P01_ALPHA_RENAME_SINGLE_V1"
        else None
    )
    source_fingerprint = (
        runtime.compute_p01_binder_aware_fingerprint(
            endpoint_role="source",
            closed_expr_hash=source_expr_hash,
            sidecar_sha256=source_sidecar_sha256,
            selected_site_path=selected_site_path,
            selected_site_ordinal=0,
            binder_name="x",
            binder_info="default",
        )
        if p01_delta is not None
        else sha256_hex(f"fingerprint-source:{suffix}".encode())
    )
    candidate_fingerprint = (
        runtime.compute_p01_binder_aware_fingerprint(
            endpoint_role="candidate",
            closed_expr_hash=candidate_expr_hash,
            sidecar_sha256=candidate_sidecar_sha256,
            selected_site_path=selected_site_path,
            selected_site_ordinal=0,
            binder_name="x_alpha",
            binder_info="default",
        )
        if p01_delta is not None
        else sha256_hex(f"fingerprint-candidate:{suffix}".encode())
    )
    replay = runtime.TypedCertificateReceipt(
        operation_id=cast(Any, operation),
        source_closed_expr_hash=source_expr_hash,
        candidate_closed_expr_hash=candidate_expr_hash,
        source_sidecar_sha256=source_sidecar_sha256,
        candidate_sidecar_sha256=candidate_sidecar_sha256,
        render_request_hash=render_request_hash,
        replay_request_hash=render_request_hash,
        selected_site_path=selected_site_path,
        selected_site_path_fingerprint=hash_canonical(selected_site_path),
        selected_site_lineage_hash=(
            runtime.compute_p01_selected_site_lineage_hash(
                selected_site_path=selected_site_path,
                selected_site_ordinal=0,
            )
            if p01_delta is not None
            else sha256_hex(f"lineage:{suffix}".encode())
        ),
        binder_aware_source_fingerprint=source_fingerprint,
        binder_aware_candidate_fingerprint=candidate_fingerprint,
        selected_site_uniquely_rediscovered=True,
        replayed_in_persistent_meta=True,
        certificate_replay_passed=True,
        candidate_is_exact_deterministic_replay_result=True,
        p01_name_only_delta=p01_delta,
    )
    replay_path = tmp_path / f"{suffix}-replay.json"
    replay_path.write_bytes(canonical_json_bytes(replay.model_dump(mode="json")) + b"\n")
    replay_sha256 = hash_file(replay_path)
    replay_payload_hash = hash_canonical(replay.model_dump(mode="json"))

    raw_path = tmp_path / f"{suffix}-raw.json"
    raw_payload: dict[str, object] = {
        "request": {
            "request_id": f"request-{suffix}",
            "context_id": context.compile_context_identity,
            "code": (
                "run_meta do\n"
                "  LeanFaith.GoalV1.emitClosedProp referenceExpr\n"
                "  LeanFaith.GoalV1.emitClosedProp candidateExpr\n"
            ),
            "file_path": None,
            "declarations": False,
            "root_goals": False,
            "infotree": "none",
            "allow_sorry": False,
            "timeout_seconds": 30.0,
        },
        "transport_isolation": None,
        "request_hash": render_request_hash,
        "method_version": "sft1-wave1-test-backend-v1",
        "response": {"messages": []},
        "error": None,
    }
    raw_path.write_bytes(
        json.dumps(raw_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    raw_sha256 = hash_file(raw_path)

    pair_id = make_id(PAIR_PREFIX, {"suffix": suffix})
    theorem_a_id = make_id(THEOREM_PREFIX, {"suffix": suffix, "endpoint": "source"})
    theorem_b_id = make_id(THEOREM_PREFIX, {"suffix": suffix, "endpoint": "candidate"})
    representation_a_id = make_id(REPRESENTATION_PREFIX, {"suffix": suffix, "endpoint": "source"})
    representation_b_id = make_id(
        REPRESENTATION_PREFIX, {"suffix": suffix, "endpoint": "candidate"}
    )
    cache_key = EvidenceCacheKey(
        pair_id=pair_id,
        theorem_a_id=theorem_a_id,
        theorem_b_id=theorem_b_id,
        theorem_a_statement_hash=source_expr_hash,
        theorem_b_statement_hash=candidate_expr_hash,
        representation_a_id=representation_a_id,
        representation_b_id=representation_b_id,
        representation_a_content_hash=source_sidecar_sha256,
        representation_b_content_hash=candidate_sidecar_sha256,
        representation_version="goal_v1.0",
        context_id=context.compile_context_identity,
        context_fingerprint=context.compile_context_fingerprint,
        environment_schema_version=1,
        environment_hash=sha256_hex(f"environment:{suffix}".encode()),
        evidence_kind=EvidenceKind.TRANSFORMATION_AUDIT,
        evidence_direction="none",
        method_version=f"sft1_wave1_readiness_v0_3_6:{suffix}",
        timeout_seconds=30.0,
        config_hash=runtime_binding.runtime_config_semantic_hash,
        semantic_policy_version="sft1_revision_0_3_6",
        semantic_policy_hash=runtime_operation.registry_entry_hash,
        lean_version="v4.31.0-rc1",
        lean_interact_version="test",
        repl_revision="test",
        project_revision=base.source_revision,
    )
    checks: dict[str, bool | None] = {
        "typed_meta_validation": True,
        "typed_certificate_replay": True,
        "same_request_repr": True,
        "sidecars_persisted": True,
    }
    evidence = EvidenceRecord(
        evidence_id=make_id(EVIDENCE_PREFIX, {"suffix": suffix}),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=pair_id,
        kind=EvidenceKind.TRANSFORMATION_AUDIT,
        status=EvidenceExecutionStatus.SUCCESS,
        value=AuditValue(
            checks=checks,
            violation_codes=(),
            detail_artifact=str(replay_path.resolve()),
        ),
        method_version=cache_key.method_version,
        config_hash=cache_key.config_hash,
        raw_artifact=str(raw_path.resolve()),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata={
            "wave1_cache_key_hash": sha256_hex(f"wave-key:{suffix}".encode()),
            "typed_replay_artifact_sha256": replay_sha256,
            "raw_artifact_sha256": raw_sha256,
        },
    )
    cache_entry = make_evidence_cache_entry(
        cache_key,
        evidence,
        lean_request_hashes=(render_request_hash,),
        certificate_dependency_hash=replay_payload_hash,
        artifact_hashes={
            str(raw_path.resolve()): raw_sha256,
            str(replay_path.resolve()): replay_sha256,
            f"{suffix}-source-sidecar.json": source_sidecar_sha256,
            f"{suffix}-candidate-sidecar.json": candidate_sidecar_sha256,
        },
    )
    cache_path = tmp_path / f"{suffix}-cache.json"
    cache_path.write_bytes(canonical_json_bytes(cache_entry.model_dump(mode="json")) + b"\n")
    cache_sha256 = hash_file(cache_path)
    artifact_bindings = {
        "raw": {
            "path": raw_path.name,
            "file_sha256": raw_sha256,
            "byte_count": raw_path.stat().st_size,
        },
        "replay": {
            "path": replay_path.name,
            "file_sha256": replay_sha256,
            "byte_count": replay_path.stat().st_size,
        },
        "cache": {
            "path": cache_path.name,
            "file_sha256": cache_sha256,
            "byte_count": cache_path.stat().st_size,
        },
    }
    n31_identity = (
        runtime_binding.n31_admitted_identities[0]
        if operation == "N31_DROP_REQUIRED_GUARD_RUBRIC_V1"
        else None
    )
    meta_payload = {
        "schema_version": 1,
        "receipt_kind": "sft1_wave1_typed_applicability_v1",
        "root_id": base.root_id,
        "source_id": base.source_id,
        "source_revision": base.source_revision,
        "source_locator": base.source_locator,
        "source_text_hash": base.source_text_hash,
        "signature_text_hash": base.signature_text_hash,
        "selected_operation_id": operation,
        "typed_applicable_operations": list(typed_operations),
        "closed_expr_hash": source_expr_hash,
        "candidate_closed_expr_hash": candidate_expr_hash,
        "source_sidecar_sha256": source_sidecar_sha256,
        "candidate_sidecar_sha256": candidate_sidecar_sha256,
        "render_request_hash": render_request_hash,
        "compile_context_identity": context.compile_context_identity,
        "compile_context_fingerprint": context.compile_context_fingerprint,
        "meta_request_hash": render_request_hash,
        "typed_certificate_payload_hash": replay_payload_hash,
        "central_cache_key_hash": cache_entry.cache_key_hash,
        "runtime_config_path": runtime_binding.runtime_config_path,
        "runtime_config_file_sha256": runtime_binding.runtime_config_file_sha256,
        "runtime_config_semantic_hash": runtime_binding.runtime_config_semantic_hash,
        "runtime_loader_path": runtime_binding.runtime_loader_path,
        "runtime_loader_file_sha256": runtime_binding.runtime_loader_file_sha256,
        "runtime_helper_path": runtime_binding.runtime_helper_path,
        "runtime_helper_file_sha256": runtime_binding.runtime_helper_file_sha256,
        "assembled_preamble_sha256": runtime_binding.assembled_preamble_sha256,
        "operation_registry_entry_hash": runtime_operation.registry_entry_hash,
        "operation_anchor_hash": runtime_operation.anchor_hash,
        "operation_bank_entry_hash": runtime_operation.operation_bank_entry_hash,
        "runtime_fixture_bundle_hash": runtime_operation.runtime_fixture_bundle_hash,
        "dispatch_symbol": runtime_operation.dispatch_symbol,
        "checker_symbol": runtime_operation.checker_symbol,
        "n31_admitted_bank_identity": (
            n31_identity.model_dump(mode="json") if n31_identity is not None else None
        ),
        "raw_lean_response_artifact": artifact_bindings["raw"],
        "typed_replay_artifact": artifact_bindings["replay"],
        "central_cache_artifact": artifact_bindings["cache"],
        "persistent_same_request": True,
        "certificate_replay_passed": True,
        "typed_applicability_passed": True,
        "lean_invoked": True,
    }
    receipt_path.write_bytes(canonical_json_bytes(meta_payload) + b"\n")
    receipt_hash = hash_file(receipt_path)
    typed = census.RootRecord.model_validate(
        {
            **base.model_dump(mode="json"),
            "evidence_level": "typed",
            "compile_context_available": True,
            "closed_expr_route_available": True,
            "current_meta_receipt_hash": receipt_hash,
            "typed_applicable_operations": list(typed_operations),
        }
    )
    entry = census.SmokeManifestEntry(
        schema_version=1,
        selection_operation_id=cast(Any, operation),
        root=typed,
        typed_meta_receipt_path=receipt_path.name,
        typed_meta_receipt_sha256=receipt_hash,
    )
    return typed, entry


def _write_manifest(path: Path, entries: tuple[census.SmokeManifestEntry, ...]) -> None:
    path.write_bytes(
        b"".join(canonical_json_bytes(entry.model_dump(mode="json")) + b"\n" for entry in entries)
    )


def _rebind_smoke_artifact(
    entry: census.SmokeManifestEntry,
    *,
    artifact_root: Path,
    artifact_field: str,
    payload: dict[str, object],
    backend_json: bool = False,
) -> census.SmokeManifestEntry:
    """Rewrite one adversarial artifact while coherently rebinding every outer hash."""

    receipt_path = artifact_root / entry.typed_meta_receipt_path
    meta = json.loads(receipt_path.read_text(encoding="utf-8"))
    binding = cast(dict[str, object], meta[artifact_field])
    artifact_path = receipt_path.parent / cast(str, binding["path"])
    artifact_bytes = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if backend_json
        else canonical_json_bytes(payload) + b"\n"
    )
    artifact_path.write_bytes(artifact_bytes)
    binding["file_sha256"] = hash_file(artifact_path)
    binding["byte_count"] = len(artifact_bytes)
    receipt_path.write_bytes(canonical_json_bytes(meta) + b"\n")
    receipt_sha256 = hash_file(receipt_path)
    root = census.RootRecord.model_validate(
        {
            **entry.root.model_dump(mode="json"),
            "current_meta_receipt_hash": receipt_sha256,
        }
    )
    return census.SmokeManifestEntry(
        schema_version=1,
        selection_operation_id=entry.selection_operation_id,
        root=root,
        typed_meta_receipt_path=entry.typed_meta_receipt_path,
        typed_meta_receipt_sha256=receipt_sha256,
    )


def _patch_source_replay(
    monkeypatch: pytest.MonkeyPatch, expected: dict[str, census.RootRecord]
) -> None:
    def replay(
        _loaded: census.LoadedCensusConfig,
        root: census.RootRecord,
        **_kwargs: object,
    ) -> census.RootRecord:
        return expected[root.root_id]

    monkeypatch.setattr(census, "_recompute_smoke_source_root", replay)


def _finalize_state_journal(
    loaded: census.LoadedCensusConfig,
    tier: census.Tier,
    state: census.CensusState,
    journal: Path,
) -> str:
    state.mark_route_complete()
    state_hash = census._state_evidence_hash(state.connection)
    writer = census.JournalWriter(journal)
    writer.append({"event": "start", "tier": tier, "state_route_id": state.binding.route_id})
    return writer.append(
        {
            "event": "census_state_finalized",
            "tier": tier,
            "state_route_id": state.binding.route_id,
            "state_evidence_hash": state_hash,
            "config_file_sha256": loaded.config_file_sha256,
            "config_semantic_hash": loaded.config_hash,
            "implementation_source_sha256": state.binding.implementation_source_sha256,
            "runtime_git_commit": state.binding.runtime_git_commit,
            "evaluation_blocklist_file_sha256": state.binding.evaluation_blocklist_file_sha256,
            "evaluation_blocklist_procedure_id": state.binding.evaluation_blocklist_procedure_id,
        }
    )


def _payload() -> dict[str, Any]:
    return copy.deepcopy(census.load_wave1_census_config().config.model_dump(mode="json"))


def test_checked_in_config_is_exact_zero_lean_and_hash_bound() -> None:
    loaded = census.load_wave1_census_config()
    config = loaded.config
    assert config.authorization.invokes_lean is False
    assert config.authorization.executes_transforms is False
    assert config.authorization.emits_model_facing_rows is False
    assert config.authorization.ten_k_authorized is False
    assert config.authorization.scale_authorized is False
    assert config.tiers.selected_wave.input_route == "deterministic_bounded_sampling_frame_scan"
    assert config.tiers.selected_wave.target_per_primary_operation == 125
    assert config.tiers.selected_wave.source_scan_root_budget == 25_000
    assert config.tiers.full_cross_source.source_scan_root_budget is None
    assert loaded.config_file_sha256 == hash_file(loaded.path)
    implementation = loaded.repo_root / config.implementation_binding.implementation_source_path
    assert hash_file(implementation) == config.implementation_binding.implementation_source_sha256
    blocklist = config.evaluation_blocklist_binding
    assert blocklist.file_sha256 == census.GOLDEN_BLOCKLIST_SHA256
    assert blocklist.procedure_id == census.GOLDEN_BLOCKLIST_PROCEDURE_ID
    assert hash_file(loaded.repo_root / blocklist.path) == blocklist.file_sha256
    assert loaded.golden_blocklist.near_dup_hashes
    assert config.tiers.selected_wave.completion_claim == (
        "bounded_route_slice_complete_not_source_complete"
    )
    assert config.durability.receipt_write == "immutable_create_or_identical_v2"


@pytest.mark.parametrize(
    ("tier", "field", "value"),
    (
        ("smoke", "minimum_gate_roots_per_primary_operation", 0),
        ("smoke", "source_scan_root_budget", 1),
        ("smoke", "blocks_approximately_100_root_gate", True),
        ("smoke", "blocks_ten_k_and_scale", True),
        ("selected_wave", "blocks_two_row_smoke", True),
        ("selected_wave", "blocks_ten_k_and_scale", True),
        ("full_cross_source", "minimum_gate_roots_per_primary_operation", 0),
        ("full_cross_source", "source_scan_root_budget", 1),
        ("full_cross_source", "blocks_two_row_smoke", True),
        ("full_cross_source", "blocks_approximately_100_root_gate", True),
    ),
)
def test_progression_flags_and_null_states_are_literal(
    tier: str, field: str, value: object
) -> None:
    payload = _payload()
    tiers = cast(dict[str, dict[str, object]], payload["tiers"])
    tiers[tier][field] = value
    with pytest.raises(ValidationError):
        census.Wave1CensusConfig.model_validate(payload)


def test_effective_readiness_hash_is_a_literal_dependency() -> None:
    payload = _payload()
    policy = cast(dict[str, object], payload["policy_binding"])
    assert policy["effective_readiness_sha256"] == census.EXPECTED_EFFECTIVE_READINESS_SHA256
    policy["effective_readiness_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        census.Wave1CensusConfig.model_validate(payload)


def test_blocklist_binding_drift_or_unknown_loader_fails_closed() -> None:
    payload = _payload()
    blocklist = cast(dict[str, Any], payload["evaluation_blocklist_binding"])
    blocklist["file_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        census.Wave1CensusConfig.model_validate(payload)
    payload = _payload()
    blocklist = cast(dict[str, Any], payload["evaluation_blocklist_binding"])
    blocklist["loader"] = "unknown.Loader"
    with pytest.raises(ValidationError):
        census.Wave1CensusConfig.model_validate(payload)


def test_malformed_golden_blocklist_loader_result_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed(_path: Path) -> census.GoldenBlocklist:
        raise ValueError("malformed fixture")

    monkeypatch.setattr(census.GoldenBlocklist, "load", malformed)
    with pytest.raises(census.CensusError, match="blocklist is malformed"):
        census.load_wave1_census_config()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("authorization", "invokes_lean", True),
        ("authorization", "emits_model_facing_rows", True),
        ("authorization", "publication_authorized", True),
        ("operation_contract", "n31_proof_activation_authorized", True),
        ("durability", "no_per_row_process_spawn", False),
        ("cluster_contract", "selected_wave_clusters_must_remain_intact", False),
    ),
)
def test_authorization_and_contract_drift_fail_closed(
    section: str, field: str, value: bool
) -> None:
    payload = _payload()
    cast(dict[str, Any], payload[section])[field] = value
    with pytest.raises(ValidationError):
        census.Wave1CensusConfig.model_validate(payload)


def test_every_source_path_revision_glob_toolchain_and_route_is_exactly_frozen() -> None:
    base = _payload()
    fields = (
        "repository",
        "revision",
        "checkout_or_artifact_path",
        "repo_binding_path",
        "globs",
        "expected_toolchain",
        "root_module",
        "closed_expr_route",
    )
    for source_index in range(4):
        for field in fields:
            payload = copy.deepcopy(base)
            source = cast(list[dict[str, Any]], payload["sources"])[source_index]
            source[field] = ["Drift/**/*.lean"] if field == "globs" else "drift"
            with pytest.raises(ValidationError):
                census.Wave1CensusConfig.model_validate(payload)


def test_unknown_yaml_field_fails_closed(tmp_path: Path) -> None:
    payload = _payload()
    payload["surprise"] = "not allowed"
    path = tmp_path / "bad.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_config(path, census.Wave1CensusConfig)


def test_state_routes_bind_implementation_commit_journal_and_distinct_scan_routes(
    tmp_path: Path,
) -> None:
    loaded = census.load_wave1_census_config()
    selected = census.make_state_binding(loaded, "selected_wave", None, tmp_path / "a.jsonl")
    full = census.make_state_binding(loaded, "full_cross_source", None, tmp_path / "a.jsonl")
    moved = census.make_state_binding(loaded, "selected_wave", None, tmp_path / "b.jsonl")
    smoke = census.make_state_binding(loaded, "smoke", "a" * 64, tmp_path / "a.jsonl")
    assert selected.route_kind == "bounded_sampling_frame_scan"
    assert full.route_kind == "complete_streaming_source_scan"
    assert selected != full
    assert selected != moved
    assert selected != smoke
    assert selected.implementation_source_sha256 == (
        loaded.config.implementation_binding.implementation_source_sha256
    )
    assert len(selected.runtime_git_commit) == 40
    state_path = tmp_path / "state.sqlite"
    census.CensusState(state_path, selected).close()
    with pytest.raises(census.CensusError, match="differs from config or census route"):
        census.CensusState(state_path, full)


def test_clean_build_binding_and_historical_commit_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        census,
        "_verify_recorded_runtime_commit",
        _REAL_VERIFY_RECORDED_RUNTIME_COMMIT,
    )
    loaded = census.load_wave1_census_config()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "census-test@example.invalid"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Census Test"], cwd=repo, check=True)
    runtime_paths = tuple(census._runtime_commit_file_bindings(loaded))
    for relative in runtime_paths:
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((loaded.repo_root / relative).read_bytes())
    subprocess.run(["git", "add", "-f", "--", *runtime_paths], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "runtime A"], cwd=repo, check=True)
    commit_a = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config_relative = loaded.path.relative_to(loaded.repo_root)
    fixture_loaded = replace(
        loaded,
        repo_root=repo,
        path=repo / config_relative,
    )
    assert _REAL_BIND_CLEAN_RUNTIME_COMMIT(fixture_loaded) == commit_a

    note = repo / "note.txt"
    note.write_text("later commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "note.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "runtime B"], cwd=repo, check=True)
    commit_b = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert commit_b != commit_a
    _REAL_VERIFY_RECORDED_RUNTIME_COMMIT(fixture_loaded, commit_a)
    historical = census.make_state_binding(
        fixture_loaded,
        "selected_wave",
        None,
        tmp_path / "historical.jsonl",
        runtime_git_commit=commit_a,
    )
    assert historical.runtime_git_commit == commit_a

    note.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(census.CensusError, match="clean committed checkout"):
        _REAL_BIND_CLEAN_RUNTIME_COMMIT(fixture_loaded)


def test_root_evidence_and_typed_applicability_fail_closed() -> None:
    loaded = census.load_wave1_census_config()
    valid = _root(
        loaded,
        source_id="mathlib",
        locator="Mathlib/A.lean#decl=0:x",
        operations=("P01_ALPHA_RENAME_SINGLE_V1",),
    )
    payload = valid.model_dump(mode="json")
    for update, match in (
        ({"root_id": "0" * 64}, "root_id does not replay"),
        ({"upstream_typed_evidence_hash": None}, "upstream typed evidence"),
        (
            {"typed_applicable_operations": ["P01_ALPHA_RENAME_SINGLE_V1"]},
            "non-typed root cannot claim",
        ),
    ):
        with pytest.raises(ValidationError, match=match):
            census.RootRecord.model_validate({**payload, **update})


def test_sqlite_exclusion_axes_fail_closed_on_non_boolean_tamper(tmp_path: Path) -> None:
    loaded = census.load_wave1_census_config()
    state = census.CensusState(
        tmp_path / "state.sqlite",
        census.make_state_binding(
            loaded,
            "selected_wave",
            None,
            tmp_path / "journal.jsonl",
        ),
    )
    try:
        root = _root(
            loaded,
            source_id="mathlib",
            locator="Mathlib/A.lean#decl=0:x",
            operations=("P01_ALPHA_RENAME_SINGLE_V1",),
        )
        state.add(root)
        state.connection.execute(
            "UPDATE roots SET private_declaration=2 WHERE root_id=?", (root.root_id,)
        )
        with pytest.raises(census.CensusError, match="non-boolean evidence axis"):
            census._validate_state_rows(loaded, state)
    finally:
        state.close()


def test_n31_proof_is_optional_nested_evidence_only() -> None:
    loaded = census.load_wave1_census_config()
    proof = sha256_hex(b"proof")
    root = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/A.lean#decl=0:n31",
        operations=("N31_DROP_REQUIRED_GUARD_RUBRIC_V1",),
        proof_status="available",
        proof_hash=proof,
    )
    assert census.OPTIONAL_PROOF_OPERATION not in root.operation_candidates
    with pytest.raises(ValidationError, match="nested under its parent"):
        census.RootRecord.model_validate(
            {**root.model_dump(mode="json"), "operation_candidates": []}
        )


def test_bounded_parser_is_attributed_source_faithful_and_excludes_later_material() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "cslib")
    text = """
@[simp]
theorem first (x : Nat) : x = x := by
  rfl
-- trailing comment naming theorem fake : False
@[simp] theorem «second name» (y : Nat) : y = y := by rfl
def later := "theorem hidden : False := by sorry"
private theorem skipped : True := by trivial
"""
    roots = tuple(census._records_from_lean_text(loaded, source, "Cslib/Test.lean", text))
    assert [root.source_locator.rsplit(":", 1)[-1] for root in roots] == [
        "first",
        "«second name»",
        "skipped",
    ]
    first_block = text[text.index("@[simp]") : text.index("@[simp] theorem")]
    assert roots[0].source_text_hash != sha256_hex(first_block.encode())
    spans = census._bounded_declaration_spans(text)
    extracted = text[spans[0].start : spans[0].end]
    assert "trailing comment" not in extracted
    assert "second name" not in extracted
    assert "later" not in extracted
    assert roots[0].root_blocklisted is False
    assert roots[2].private_declaration is True
    assert roots[2].exclusion_reason == "private_declaration"


def test_nested_set_option_does_not_truncate_placeholder_or_declaration_bytes() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "cslib")
    text = """
theorem unsafe : True := by
  set_option pp.universes true in
    sorry
notation "local-token" => True
theorem later : True := by trivial
"""
    spans = census._bounded_declaration_spans(text)
    first_block = text[spans[0].start : spans[0].end]
    assert "set_option" in first_block
    assert "sorry" in first_block
    assert "notation" not in first_block
    unsafe, later = tuple(
        census._records_from_lean_text(loaded, source, "Cslib/NestedOption.lean", text)
    )
    assert unsafe.proof_placeholder_detected is True
    assert unsafe.exclusion_reason == "proof_placeholder"
    assert unsafe.source_text_hash == sha256_hex(first_block.encode("utf-8"))
    assert later.proof_placeholder_detected is False


def test_inline_attributed_declaration_does_not_leak_into_following_command() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "cslib")
    text = "@[simp] theorem first : True := by trivial\ntheorem second : True := by trivial\n"
    spans = census._bounded_declaration_spans(text)
    blocks = [text[span.start : span.end] for span in spans]
    assert len(blocks) == 2
    assert "second" not in blocks[0]
    assert "first" not in blocks[1]
    attributed = next(census._records_from_lean_text(loaded, source, "Cslib/A.lean", text))
    plain = next(
        census._records_from_lean_text(
            loaded, source, "Cslib/B.lean", "theorem renamed : True := by trivial\n"
        )
    )
    assert attributed.surface_identity_hash == plain.surface_identity_hash


def test_signature_clusters_ignore_comments_and_alpha_names_but_keep_exact_delta() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "cslib")
    first = next(
        census._records_from_lean_text(
            loaded,
            source,
            "Cslib/A.lean",
            "theorem a (x : Nat) : x = x := by rfl\n-- later comment\n",
        )
    )
    second = next(
        census._records_from_lean_text(
            loaded,
            source,
            "Cslib/B.lean",
            "theorem b (y : Nat) : y = y := by rfl\n",
        )
    )
    assert first.surface_identity_hash != second.surface_identity_hash
    assert first.near_identity_hash == second.near_identity_hash
    assert first.structure_identity_hash == second.structure_identity_hash


def test_later_commands_do_not_change_first_declaration_evidence() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "cslib")
    prefix = "theorem first (x : Nat) : x = x := by rfl\n"
    a = next(
        census._records_from_lean_text(
            loaded,
            source,
            "Cslib/A.lean",
            prefix + "theorem tail : True := by trivial\n",
        )
    )
    b = next(
        census._records_from_lean_text(
            loaded,
            source,
            "Cslib/A.lean",
            prefix + "theorem tail : False := by contradiction\n",
        )
    )
    assert a.source_text_hash == b.source_text_hash
    assert a.upstream_typed_evidence_hash == b.upstream_typed_evidence_hash


def test_surface_prefilter_broadly_flags_beta_and_guard_families_without_typed_claims() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "mathlib")
    text = """
theorem beta : ((fun x : Nat => x) 1) = 1 := by rfl
theorem guarded (p : Prop) : p → p := by intro h; exact h
theorem member_guard (x : Nat) (s : Set Nat) : x ∈ s → x ∈ s := by intro h; exact h
"""
    beta, guarded, member = tuple(
        census._records_from_lean_text(loaded, source, "Mathlib/Test.lean", text)
    )
    assert "P21_BETA_REDUCE_V1" in beta.operation_candidates
    assert "N31_DROP_REQUIRED_GUARD_RUBRIC_V1" in guarded.operation_candidates
    assert "N31_DROP_REQUIRED_GUARD_RUBRIC_V1" in member.operation_candidates
    assert all(not root.typed_applicable_operations for root in (beta, guarded, member))


def test_placeholders_in_comments_and_strings_are_not_blocklisted() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "cslib")
    safe = next(
        census._records_from_lean_text(
            loaded,
            source,
            "Cslib/A.lean",
            'theorem safe : "sorry" = "sorry" := by rfl -- admit\n',
        )
    )
    unsafe = next(
        census._records_from_lean_text(
            loaded,
            source,
            "Cslib/B.lean",
            "theorem unsafe : True := by sorry\n",
        )
    )
    assert safe.root_blocklisted is False
    assert unsafe.root_blocklisted is True
    assert unsafe.exclusion_reason == "proof_placeholder"


def test_placeholder_matching_is_token_aware_and_reason_coded() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "cslib")
    text = """
theorem sorryful_name : True := by exact sorryful
theorem admitValue_name : True := by exact admitValue
theorem actual_admit : True := by admit
theorem actual_by_question : True := by?
"""
    roots = tuple(census._records_from_lean_text(loaded, source, "Cslib/Tokens.lean", text))
    assert [root.proof_placeholder_detected for root in roots] == [False, False, True, True]
    assert [root.exclusion_reason for root in roots] == [
        "none",
        "none",
        "proof_placeholder",
        "proof_placeholder",
    ]


def test_golden_blocklist_screen_uses_headless_near_duplicate_hash() -> None:
    loaded = census.load_wave1_census_config()
    source = next(item for item in loaded.config.sources if item.source_id == "cslib")
    declaration = "theorem exact_name (x : Nat) : x = x := by rfl\n"
    signature = census._bounded_signature(declaration)
    headless = census.normalize_headless(signature)
    assert headless is not None
    near_hash = census.signature_near_dup_hash(headless)
    screened = replace(
        loaded,
        golden_blocklist=census.GoldenBlocklist(
            near_dup_hashes=frozenset({near_hash}),
            group_keys=frozenset(),
            problem_names=frozenset(),
        ),
    )
    root = next(census._records_from_lean_text(screened, source, "Cslib/Golden.lean", declaration))
    assert root.golden_near_dup_hash == near_hash
    assert root.golden_blocklist_hit is True
    assert root.exclusion_reason == "golden_blocklist_near_duplicate"
    assert root.root_blocklisted is True


def test_smoke_entry_authenticates_exact_source_and_typed_meta_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/A.lean#decl=0:p",
        operations=("P01_ALPHA_RENAME_SINGLE_V1",),
    )
    _typed, entry = _typed_entry(tmp_path, base, "P01_ALPHA_RENAME_SINGLE_V1", suffix="p01")
    _patch_source_replay(monkeypatch, {base.root_id: base})
    receipt = census._authenticate_smoke_entry(
        loaded,
        tmp_path / "manifest.jsonl",
        entry,
        verified_git={},
        verified_artifacts=set(),
    )
    assert receipt.root_id == base.root_id
    (tmp_path / "meta-p01.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(census.CensusError, match="receipt path/hash drift"):
        census._authenticate_smoke_entry(
            loaded,
            tmp_path / "manifest.jsonl",
            entry,
            verified_git={},
            verified_artifacts=set(),
        )


def test_smoke_receipt_binds_final_runtime_artifacts_and_exact_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/Runtime.lean#decl=0:p",
        operations=("P01_ALPHA_RENAME_SINGLE_V1",),
    )
    _typed, entry = _typed_entry(
        tmp_path, base, "P01_ALPHA_RENAME_SINGLE_V1", suffix="runtime-bind"
    )
    _patch_source_replay(monkeypatch, {base.root_id: base})
    drifted = _fake_runtime_binding().model_copy(update={"runtime_config_semantic_hash": "f" * 64})
    monkeypatch.setattr(census, "_load_finalized_runtime_binding", lambda _loaded: drifted)
    with pytest.raises(census.CensusError, match="finalized runtime drift"):
        census._authenticate_smoke_entry(
            loaded,
            tmp_path / "manifest.jsonl",
            entry,
            verified_git={},
            verified_artifacts=set(),
        )

    monkeypatch.setattr(
        census, "_load_finalized_runtime_binding", lambda _loaded: _fake_runtime_binding()
    )
    (tmp_path / "runtime-bind-cache.json").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(census.CensusError, match="central cache artifact byte/hash drift"):
        census._authenticate_smoke_entry(
            loaded,
            tmp_path / "manifest.jsonl",
            entry,
            verified_git={},
            verified_artifacts=set(),
        )


@pytest.mark.parametrize(
    ("artifact_field", "payload_mutation", "backend_json", "message"),
    (
        (
            "raw_lean_response_artifact",
            lambda payload: payload.update({"request_hash": "0" * 64}),
            True,
            "same-request Meta route",
        ),
        (
            "raw_lean_response_artifact",
            lambda payload: payload.update({"unexpected": True}),
            True,
            "schema validation",
        ),
        (
            "typed_replay_artifact",
            lambda payload: payload.update({"candidate_closed_expr_hash": "0" * 64}),
            False,
            "does not cross-link",
        ),
        (
            "central_cache_artifact",
            lambda payload: payload.update({"lean_request_hashes": []}),
            False,
            "does not replay",
        ),
    ),
)
def test_smoke_artifacts_are_strictly_typed_and_cross_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_field: str,
    payload_mutation: Callable[[dict[str, object]], None],
    backend_json: bool,
    message: str,
) -> None:
    loaded = census.load_wave1_census_config()
    base = _root(
        loaded,
        source_id="cslib",
        locator=f"Cslib/X/{artifact_field}.lean#decl=0:n",
        operations=("N31_DROP_REQUIRED_GUARD_RUBRIC_V1",),
    )
    _typed, entry = _typed_entry(
        tmp_path,
        base,
        "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        suffix=artifact_field,
    )
    meta_path = tmp_path / entry.typed_meta_receipt_path
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    artifact_binding = cast(dict[str, object], meta[artifact_field])
    artifact_path = tmp_path / cast(str, artifact_binding["path"])
    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload_mutation(artifact_payload)
    rebound = _rebind_smoke_artifact(
        entry,
        artifact_root=tmp_path,
        artifact_field=artifact_field,
        payload=artifact_payload,
        backend_json=backend_json,
    )
    _patch_source_replay(monkeypatch, {base.root_id: base})
    with pytest.raises(census.CensusError, match=message):
        census._authenticate_smoke_entry(
            loaded,
            tmp_path / "manifest.jsonl",
            rebound,
            verified_git={},
            verified_artifacts=set(),
        )


def test_smoke_artifact_json_rejects_duplicate_keys_and_nonfinite_values(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"request_hash":"a","request_hash":"b"}', encoding="utf-8")
    with pytest.raises(census.CensusError, match="invalid typed replay artifact JSON"):
        census._read_strict_json_object(duplicate, "typed replay")
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"elapsed":NaN}', encoding="utf-8")
    with pytest.raises(census.CensusError, match="invalid raw Lean response artifact JSON"):
        census._read_strict_json_object(nonfinite, "raw Lean response")


def test_typed_receipt_rejects_multiple_operations_and_proposal_only_n31(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    n31_base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/N31.lean#decl=0:n",
        operations=("N31_DROP_REQUIRED_GUARD_RUBRIC_V1",),
    )
    _typed, entry = _typed_entry(
        tmp_path,
        n31_base,
        "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        suffix="n31-admission",
    )
    payload = json.loads((tmp_path / "meta-n31-admission.json").read_text(encoding="utf-8"))
    payload["typed_applicable_operations"] = [
        "P01_ALPHA_RENAME_SINGLE_V1",
        "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
    ]
    with pytest.raises(ValidationError, match="exactly its selected operation"):
        census.TypedMetaReceipt.model_validate(payload)

    _patch_source_replay(monkeypatch, {n31_base.root_id: n31_base})
    proposal_only = _fake_runtime_binding(
        n31_admitted=False,
        n31_activation_authorized=False,
    )
    monkeypatch.setattr(census, "_load_finalized_runtime_binding", lambda _loaded: proposal_only)
    with pytest.raises(census.CensusError, match="proposal-only or lacks exact user admission"):
        census._authenticate_smoke_entry(
            loaded,
            tmp_path / "manifest.jsonl",
            entry,
            verified_git={},
            verified_artifacts=set(),
        )


def test_smoke_glob_matching_uses_zero_or_more_directory_semantics() -> None:
    assert census._matches_frozen_glob(census.PurePosixPath("Cslib/A.lean"), "Cslib/**/*.lean")
    assert census._matches_frozen_glob(census.PurePosixPath("Cslib/X/A.lean"), "Cslib/**/*.lean")
    assert not census._matches_frozen_glob(census.PurePosixPath("Other/A.lean"), "Cslib/**/*.lean")


def test_smoke_manifest_rejects_pending_roots_and_duplicate_operation_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/A.lean#decl=0:p",
        operations=("P01_ALPHA_RENAME_SINGLE_V1",),
    )
    with pytest.raises(ValidationError, match="smoke root must be current-environment typed"):
        census.SmokeManifestEntry(
            schema_version=1,
            selection_operation_id="P01_ALPHA_RENAME_SINGLE_V1",
            root=base,
            typed_meta_receipt_path="meta.json",
            typed_meta_receipt_sha256="a" * 64,
        )
    _typed, entry = _typed_entry(tmp_path, base, "P01_ALPHA_RENAME_SINGLE_V1", suffix="p01")
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, (entry, entry))
    _patch_source_replay(monkeypatch, {base.root_id: base})
    with pytest.raises(census.CensusError, match="exactly one ordered P01 and one N31"):
        tuple(census.iter_authenticated_smoke_manifest(loaded, manifest))


def test_smoke_manifest_requires_exact_p01_n31_inventory_without_extras(
    tmp_path: Path,
) -> None:
    loaded = census.load_wave1_census_config()
    p01_base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/Inventory.lean#decl=0:p",
        operations=("P01_ALPHA_RENAME_SINGLE_V1",),
    )
    n31_base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/Inventory.lean#decl=1:n",
        operations=("N31_DROP_REQUIRED_GUARD_RUBRIC_V1",),
    )
    _typed_p01, p01 = _typed_entry(
        tmp_path, p01_base, "P01_ALPHA_RENAME_SINGLE_V1", suffix="inventory-p01"
    )
    _typed_n31, n31 = _typed_entry(
        tmp_path,
        n31_base,
        "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        suffix="inventory-n31",
    )
    bad_inventories = ((p01,), (n31,), (n31, p01), (p01, n31, p01), (p01, n31, n31))
    for index, entries in enumerate(bad_inventories):
        manifest = tmp_path / f"bad-inventory-{index}.jsonl"
        _write_manifest(manifest, entries)
        with pytest.raises(census.CensusError, match="exactly one ordered P01 and one N31"):
            tuple(census.iter_authenticated_smoke_manifest(loaded, manifest))


def test_smoke_build_marks_only_present_sources_and_replays_all_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    p01 = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/A.lean#decl=0:p",
        operations=("P01_ALPHA_RENAME_SINGLE_V1",),
    )
    n31 = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/A.lean#decl=1:n",
        operations=("N31_DROP_REQUIRED_GUARD_RUBRIC_V1",),
    )
    _typed_p01, p01_entry = _typed_entry(tmp_path, p01, "P01_ALPHA_RENAME_SINGLE_V1", suffix="p01")
    _typed_n31, n31_entry = _typed_entry(
        tmp_path, n31, "N31_DROP_REQUIRED_GUARD_RUBRIC_V1", suffix="n31"
    )
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, (p01_entry, n31_entry))
    _patch_source_replay(monkeypatch, {p01.root_id: p01, n31.root_id: n31})
    output = tmp_path / "receipt.json"
    journal = tmp_path / "journal.jsonl"
    marker = tmp_path / "terminal.json"
    state = tmp_path / "state.sqlite"
    receipt = census.run_build(
        loaded,
        "smoke",
        output=output,
        journal=journal,
        terminal_marker=marker,
        state_db=state,
        input_manifest=manifest,
    )
    assert receipt.complete is True
    assert receipt.sampling_frame_sufficient is True
    assert receipt.evaluation_blocklist_file_sha256 == census.GOLDEN_BLOCKLIST_SHA256
    assert receipt.evaluation_blocklist_procedure_id == census.GOLDEN_BLOCKLIST_PROCEDURE_ID
    completion = {item.source_id: item.scan_complete for item in receipt.source_results}
    assert completion == {
        "compiler_data": False,
        "cslib": True,
        "mathlib": False,
        "physlib": False,
    }
    assert receipt.selected_root_ids["P01_ALPHA_RENAME_SINGLE_V1"] == (p01.root_id,)
    assert receipt.selected_root_ids["N31_DROP_REQUIRED_GUARD_RUBRIC_V1"] == (n31.root_id,)
    assert census.verify_receipt(loaded, output, marker, state, journal) == receipt
    evidence = census._read_journal(journal)
    assert evidence.final_event == "census_state_finalized"
    assert evidence.final_chain_hash == receipt.journal_final_chain_hash


def test_smoke_state_cannot_mark_absent_source_or_complete_without_exact_operations(
    tmp_path: Path,
) -> None:
    loaded = census.load_wave1_census_config()
    journal = tmp_path / "journal.jsonl"
    binding = census.make_state_binding(loaded, "smoke", "a" * 64, journal)
    state = census.CensusState(tmp_path / "state.sqlite", binding)
    try:
        with pytest.raises(census.CensusError, match="absent source"):
            state.mark_complete("mathlib")
        with pytest.raises(census.CensusError, match="exact authenticated P01 and N31"):
            state.mark_route_complete()
    finally:
        state.close()


def test_journal_tamper_reorder_and_path_substitution_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    p01_base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/A.lean#decl=0:p01",
        operations=("P01_ALPHA_RENAME_SINGLE_V1",),
    )
    n31_base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/A.lean#decl=1:n31",
        operations=("N31_DROP_REQUIRED_GUARD_RUBRIC_V1",),
    )
    _typed_p01, p01 = _typed_entry(
        tmp_path,
        p01_base,
        "P01_ALPHA_RENAME_SINGLE_V1",
        suffix="journal-p01",
    )
    _typed_n31, n31 = _typed_entry(
        tmp_path,
        n31_base,
        "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        suffix="journal-n31",
    )
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, (p01, n31))
    _patch_source_replay(
        monkeypatch,
        {p01_base.root_id: p01_base, n31_base.root_id: n31_base},
    )
    receipt = tmp_path / "receipt.json"
    marker = tmp_path / "marker.json"
    state = tmp_path / "state.sqlite"
    journal = tmp_path / "journal.jsonl"
    census.run_build(
        loaded,
        "smoke",
        output=receipt,
        journal=journal,
        terminal_marker=marker,
        state_db=state,
        input_manifest=manifest,
    )
    copied = tmp_path / "copied.jsonl"
    shutil.copy2(journal, copied)
    with pytest.raises(census.CensusError, match="journal path binding drift"):
        census.verify_receipt(loaded, receipt, marker, state, copied)
    lines = journal.read_text(encoding="utf-8").splitlines()
    journal.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    with pytest.raises(census.CensusError, match="journal chain metadata drift"):
        census.verify_receipt(loaded, receipt, marker, state, journal)


def test_journal_rejects_symlinks_and_stale_concurrent_writers(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("", encoding="utf-8")
    symlink = tmp_path / "journal-link.jsonl"
    symlink.symlink_to(target)
    with pytest.raises(census.CensusError, match="symlink"):
        census.JournalWriter(symlink)

    loaded = census.load_wave1_census_config()
    state_target = tmp_path / "state-target.sqlite"
    state_target.touch()
    state_symlink = tmp_path / "state-link.sqlite"
    state_symlink.symlink_to(state_target)
    binding = census.make_state_binding(loaded, "selected_wave", None, tmp_path / "state.jsonl")
    with pytest.raises(census.CensusError, match="symlink"):
        census.CensusState(state_symlink, binding)

    journal = tmp_path / "journal.jsonl"
    first = census.JournalWriter(journal)
    second = census.JournalWriter(journal)
    first.append({"event": "first"})
    with pytest.raises(census.CensusError, match="changed after writer initialization"):
        second.append({"event": "stale"})


@pytest.mark.parametrize("symlink_field", ("output", "journal", "terminal_marker", "state_db"))
def test_build_cli_rejects_final_path_symlinks(tmp_path: Path, symlink_field: str) -> None:
    loaded = census.load_wave1_census_config()
    arguments = {
        "output": tmp_path / "receipt.json",
        "journal": tmp_path / "journal.jsonl",
        "terminal_marker": tmp_path / "marker.json",
        "state_db": tmp_path / "state.sqlite",
    }
    target = tmp_path / f"{symlink_field}-target"
    target.write_text("target\n", encoding="utf-8")
    link = tmp_path / f"{symlink_field}-link"
    link.symlink_to(target)
    arguments[symlink_field] = link
    with pytest.raises(census.CensusError, match="symlink"):
        census.run_build(
            loaded,
            "selected_wave",
            input_manifest=None,
            **arguments,
        )


def test_cli_rejects_symlink_ancestors_aliases_and_verify_symlink(tmp_path: Path) -> None:
    loaded = census.load_wave1_census_config()
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(census.CensusError, match="symlink"):
        census.run_build(
            loaded,
            "selected_wave",
            output=alias / "receipt.json",
            journal=tmp_path / "journal.jsonl",
            terminal_marker=tmp_path / "marker.json",
            state_db=tmp_path / "state.sqlite",
            input_manifest=None,
        )
    shared = tmp_path / "shared.json"
    with pytest.raises(census.CensusError, match="aliases"):
        census.run_build(
            loaded,
            "selected_wave",
            output=shared,
            journal=tmp_path / "journal-2.jsonl",
            terminal_marker=shared,
            state_db=tmp_path / "state-2.sqlite",
            input_manifest=None,
        )
    state_path = tmp_path / "reserved-state.sqlite"
    with pytest.raises(census.CensusError, match="reserved SQLite sidecar"):
        census.run_build(
            loaded,
            "selected_wave",
            output=tmp_path / "reserved-receipt.json",
            journal=state_path.with_name(f"{state_path.name}-wal"),
            terminal_marker=tmp_path / "reserved-marker.json",
            state_db=state_path,
            input_manifest=None,
        )

    receipt_target = tmp_path / "receipt-target.json"
    receipt_target.write_text("{}\n", encoding="utf-8")
    receipt_link = tmp_path / "receipt-link.json"
    receipt_link.symlink_to(receipt_target)
    for name in ("verify-marker.json", "verify-state.sqlite", "verify-journal.jsonl"):
        (tmp_path / name).write_text("{}\n", encoding="utf-8")
    with pytest.raises(census.CensusError, match="symlink"):
        census.verify_receipt(
            loaded,
            receipt_link,
            tmp_path / "verify-marker.json",
            tmp_path / "verify-state.sqlite",
            tmp_path / "verify-journal.jsonl",
        )


def test_success_json_is_immutable_and_failure_never_overwrites_completion(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "receipt.json"
    first_hash = census._write_immutable_json(
        receipt,
        {"status": "complete", "value": 1},
        purpose="successful census receipt",
    )
    assert first_hash == census._write_immutable_json(
        receipt,
        {"status": "complete", "value": 1},
        purpose="successful census receipt",
    )
    with pytest.raises(census.CensusError, match="immutable"):
        census._write_immutable_json(
            receipt,
            {"status": "complete", "value": 2},
            purpose="successful census receipt",
        )

    marker = tmp_path / "marker.json"
    census._write_immutable_json(
        marker,
        {"status": "complete", "receipt_sha256": first_hash},
        purpose="successful terminal marker",
    )
    before = marker.read_bytes()
    route = {
        "schema_version": 1,
        "tier": "selected_wave",
        "config_file_sha256": "a" * 64,
        "config_semantic_hash": "b" * 64,
        "implementation_source_sha256": "c" * 64,
        "runtime_git_commit": "d" * 40,
        "state_db_path": str(tmp_path / "state.sqlite"),
        "state_route_id": "e" * 64,
        "journal_path": str(tmp_path / "journal.jsonl"),
        "evaluation_blocklist_file_sha256": census.GOLDEN_BLOCKLIST_SHA256,
        "evaluation_blocklist_procedure_id": census.GOLDEN_BLOCKLIST_PROCEDURE_ID,
        "lean_invoked": False,
    }
    with pytest.raises(census.CensusError, match="completed terminal marker is immutable"):
        census._write_failure_marker(marker, {**route, "status": "failed"})
    assert marker.read_bytes() == before

    resumed_marker = tmp_path / "resumed-marker.json"
    census._write_failure_marker(
        resumed_marker,
        {
            **route,
            "status": "failed",
            "failure_class": "InterruptedError",
            "journal_final_chain_hash": "f" * 64,
        },
    )
    complete_payload = {
        **route,
        "status": "complete",
        "receipt_path": str(tmp_path / "complete-receipt.json"),
        "receipt_sha256": "1" * 64,
        "state_evidence_hash": "2" * 64,
        "journal_final_chain_hash": "3" * 64,
    }
    census._write_success_terminal_marker(resumed_marker, complete_payload)
    assert json.loads(resumed_marker.read_text(encoding="utf-8")) == complete_payload

    foreign_marker = tmp_path / "foreign-marker.json"
    census._write_failure_marker(
        foreign_marker,
        {
            **route,
            "state_route_id": "4" * 64,
            "status": "failed",
            "failure_class": "InterruptedError",
            "journal_final_chain_hash": "5" * 64,
        },
    )
    with pytest.raises(census.CensusError, match="another census route"):
        census._write_success_terminal_marker(foreign_marker, complete_payload)


def test_finalized_state_recovers_receipt_and_marker_without_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    small_spec = loaded.config.tiers.selected_wave.model_copy(update={"source_scan_root_budget": 1})
    small_tiers = loaded.config.tiers.model_copy(update={"selected_wave": small_spec})
    small_config = loaded.config.model_copy(update={"tiers": small_tiers})
    loaded = replace(
        loaded,
        config=small_config,
        config_hash=hash_canonical(small_config.model_dump(mode="json")),
    )
    runtime_commit = census._current_runtime_commit(loaded.repo_root)
    monkeypatch.setattr(
        census,
        "_bind_clean_runtime_commit",
        lambda _loaded, **_kwargs: runtime_commit,
    )
    monkeypatch.setattr(census, "_verify_recorded_runtime_commit", lambda *_args: None)
    scans = dict.fromkeys(census.EXPECTED_SOURCE_IDS, 0)

    def records(_loaded: census.LoadedCensusConfig, source: census.SourceSpec) -> Any:
        scans[source.source_id] += 1
        yield _root(
            loaded,
            source_id=source.source_id,
            locator=f"{source.source_id}/recovery/0",
            operations=census.PRIMARY_OPERATIONS,
        )

    monkeypatch.setattr(census, "iter_git_source", records)
    monkeypatch.setattr(census, "iter_parquet_source", records)
    output = tmp_path / "receipt.json"
    journal = tmp_path / "journal.jsonl"
    marker = tmp_path / "marker.json"
    state = tmp_path / "state.sqlite"
    real_persist = census._persist_success_artifacts

    def fail_after_finalization(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash after journal finalization")

    monkeypatch.setattr(census, "_persist_success_artifacts", fail_after_finalization)
    with pytest.raises(OSError, match="simulated crash"):
        census.run_build(
            loaded,
            "selected_wave",
            output=output,
            journal=journal,
            terminal_marker=marker,
            state_db=state,
            input_manifest=None,
        )
    journal_before_recovery = journal.read_bytes()
    assert census._read_journal(journal).final_event == "census_state_finalized"
    assert not output.exists()
    assert not marker.exists()
    assert scans == dict.fromkeys(census.EXPECTED_SOURCE_IDS, 1)

    def forbid_rescan(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("finalization recovery must not rescan a source")

    monkeypatch.setattr(census, "iter_git_source", forbid_rescan)
    monkeypatch.setattr(census, "iter_parquet_source", forbid_rescan)
    monkeypatch.setattr(census, "_persist_success_artifacts", real_persist)
    recovered = census.run_build(
        loaded,
        "selected_wave",
        output=output,
        journal=journal,
        terminal_marker=marker,
        state_db=state,
        input_manifest=None,
    )
    assert recovered.complete is True
    assert output.is_file()
    assert marker.is_file()
    assert journal.read_bytes() == journal_before_recovery


def test_receipt_manifest_state_and_marker_tampering_fail_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    p01_base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/A.lean#decl=0:p01",
        operations=("P01_ALPHA_RENAME_SINGLE_V1",),
    )
    n31_base = _root(
        loaded,
        source_id="cslib",
        locator="Cslib/X/A.lean#decl=1:n31",
        operations=("N31_DROP_REQUIRED_GUARD_RUBRIC_V1",),
    )
    _typed_p01, p01 = _typed_entry(
        tmp_path,
        p01_base,
        "P01_ALPHA_RENAME_SINGLE_V1",
        suffix="tamper-p01",
    )
    _typed_n31, n31 = _typed_entry(
        tmp_path,
        n31_base,
        "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
        suffix="tamper-n31",
    )
    manifest = tmp_path / "manifest.jsonl"
    _write_manifest(manifest, (p01, n31))
    _patch_source_replay(
        monkeypatch,
        {p01_base.root_id: p01_base, n31_base.root_id: n31_base},
    )
    receipt = tmp_path / "receipt.json"
    marker = tmp_path / "marker.json"
    state = tmp_path / "state.sqlite"
    journal = tmp_path / "journal.jsonl"
    census.run_build(
        loaded,
        "smoke",
        output=receipt,
        journal=journal,
        terminal_marker=marker,
        state_db=state,
        input_manifest=manifest,
    )
    connection = sqlite3.connect(state)
    try:
        connection.execute("UPDATE roots SET blocklisted=1")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(census.CensusError, match="authenticated manifest"):
        census.verify_receipt(loaded, receipt, marker, state, journal)


def test_selected_wave_clusters_are_intact_and_oversized_clusters_are_skipped(
    tmp_path: Path,
) -> None:
    loaded = census.load_wave1_census_config()
    journal = tmp_path / "journal.jsonl"
    binding = census.make_state_binding(loaded, "selected_wave", None, journal)
    state = census.CensusState(tmp_path / "state.sqlite", binding)
    try:
        roots = [
            _root(
                loaded,
                source_id="mathlib",
                locator=f"Mathlib/A.lean#decl={index}:x",
                operations=("P01_ALPHA_RENAME_SINGLE_V1",),
                cluster_tag=f"exact-{index}",
            )
            for index in range(3)
        ]
        shared_alpha = sha256_hex(b"shared-alpha")
        roots = [
            census.RootRecord.model_validate(
                {**root.model_dump(mode="json"), "near_identity_hash": shared_alpha}
            )
            for root in roots
        ]
        for root in roots:
            state.add(root)
        spec = loaded.config.tiers.selected_wave.model_copy(
            update={"target_per_primary_operation": 2}
        )
        selected, _sources, clusters = census._select_roots(state.connection, spec)
        assert selected["P01_ALPHA_RENAME_SINGLE_V1"] == ()
        assert clusters["P01_ALPHA_RENAME_SINGLE_V1"] == ()
        spec = spec.model_copy(update={"target_per_primary_operation": 3})
        selected, _sources, clusters = census._select_roots(state.connection, spec)
        assert set(selected["P01_ALPHA_RENAME_SINGLE_V1"]) == {root.root_id for root in roots}
        assert len(clusters["P01_ALPHA_RENAME_SINGLE_V1"]) == 1
    finally:
        state.close()


def test_selected_wave_cluster_integrity_is_cross_source_within_the_bound_route(
    tmp_path: Path,
) -> None:
    loaded = census.load_wave1_census_config()
    journal = tmp_path / "journal.jsonl"
    binding = census.make_state_binding(loaded, "selected_wave", None, journal)
    state = census.CensusState(tmp_path / "state.sqlite", binding)
    try:
        roots = [
            _root(
                loaded,
                source_id=source_id,
                locator=f"{source_id}/CrossSource.lean#decl={index}:x",
                operations=("P01_ALPHA_RENAME_SINGLE_V1",),
                cluster_tag=f"cross-source-exact-{index}",
            )
            for index, source_id in enumerate(("cslib", "mathlib", "physlib"))
        ]
        shared_structure = sha256_hex(b"cross-source-structure-component")
        roots = [
            census.RootRecord.model_validate(
                {**root.model_dump(mode="json"), "structure_identity_hash": shared_structure}
            )
            for root in roots
        ]
        for root in roots:
            state.add(root)
        too_small = loaded.config.tiers.selected_wave.model_copy(
            update={"target_per_primary_operation": 2}
        )
        selected, _sources, clusters = census._select_roots(state.connection, too_small)
        assert selected["P01_ALPHA_RENAME_SINGLE_V1"] == ()
        assert clusters["P01_ALPHA_RENAME_SINGLE_V1"] == ()
        exact_fit = too_small.model_copy(update={"target_per_primary_operation": 3})
        selected, sources, clusters = census._select_roots(state.connection, exact_fit)
        assert set(selected["P01_ALPHA_RENAME_SINGLE_V1"]) == {root.root_id for root in roots}
        assert len(clusters["P01_ALPHA_RENAME_SINGLE_V1"]) == 1
        assert sources["P01_ALPHA_RENAME_SINGLE_V1"].model_dump() == {
            "compiler_data": 0,
            "cslib": 1,
            "mathlib": 1,
            "physlib": 1,
        }
    finally:
        state.close()


def test_operation_pool_hash_binds_exact_alpha_and_structure_membership(tmp_path: Path) -> None:
    loaded = census.load_wave1_census_config()
    journal = tmp_path / "journal.jsonl"
    state = census.CensusState(
        tmp_path / "state.sqlite",
        census.make_state_binding(loaded, "selected_wave", None, journal),
    )
    try:
        root = _root(
            loaded,
            source_id="mathlib",
            locator="Mathlib/A.lean#decl=0:x",
            operations=("P01_ALPHA_RENAME_SINGLE_V1",),
        )
        state.add(root)
        before = census._operation_pool_hashes(state.connection)
        state.connection.execute(
            "UPDATE roots SET structure_hash=? WHERE root_id=?", ("f" * 64, root.root_id)
        )
        after = census._operation_pool_hashes(state.connection)
    finally:
        state.close()
    assert before.P01_ALPHA_RENAME_SINGLE_V1 != after.P01_ALPHA_RENAME_SINGLE_V1


def test_selected_wave_build_uses_bounded_sampling_frames_not_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    small_spec = loaded.config.tiers.selected_wave.model_copy(update={"source_scan_root_budget": 2})
    small_tiers = loaded.config.tiers.model_copy(update={"selected_wave": small_spec})
    small_config = loaded.config.model_copy(update={"tiers": small_tiers})
    loaded = replace(
        loaded,
        config=small_config,
        config_hash=hash_canonical(small_config.model_dump(mode="json")),
    )
    consumed = dict.fromkeys(census.EXPECTED_SOURCE_IDS, 0)

    def records(_loaded: census.LoadedCensusConfig, source: census.SourceSpec) -> Any:
        for index in range(3):
            consumed[source.source_id] += 1
            yield _root(
                loaded,
                source_id=source.source_id,
                locator=f"{source.source_id}/frame/{index}",
                operations=census.PRIMARY_OPERATIONS,
            )

    monkeypatch.setattr(census, "iter_git_source", records)
    monkeypatch.setattr(census, "iter_parquet_source", records)
    receipt = census.run_build(
        loaded,
        "selected_wave",
        output=tmp_path / "receipt.json",
        journal=tmp_path / "journal.jsonl",
        terminal_marker=tmp_path / "marker.json",
        state_db=tmp_path / "state.sqlite",
        input_manifest=None,
    )
    assert receipt.complete is True
    assert receipt.total_root_count == 8
    assert consumed == dict.fromkeys(census.EXPECTED_SOURCE_IDS, 2)
    assert all(item.root_count == 2 for item in receipt.source_results)
    assert all(
        item.completion_scope == "bounded_sampling_frame_route_slice"
        for item in receipt.source_results
    )
    assert all(not item.source_inventory_complete for item in receipt.source_results)


def test_selected_wave_reports_raw_eligible_and_reason_coded_route_slice_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = census.load_wave1_census_config()
    small_spec = loaded.config.tiers.selected_wave.model_copy(update={"source_scan_root_budget": 3})
    small_tiers = loaded.config.tiers.model_copy(update={"selected_wave": small_spec})
    small_config = loaded.config.model_copy(update={"tiers": small_tiers})
    loaded = replace(
        loaded,
        config=small_config,
        config_hash=hash_canonical(small_config.model_dump(mode="json")),
    )

    def records(_loaded: census.LoadedCensusConfig, source: census.SourceSpec) -> Any:
        yield _root(
            loaded,
            source_id=source.source_id,
            locator=f"{source.source_id}/raw/eligible",
            operations=("P01_ALPHA_RENAME_SINGLE_V1",),
        )
        yield _root(
            loaded,
            source_id=source.source_id,
            locator=f"{source.source_id}/raw/private",
            operations=("P01_ALPHA_RENAME_SINGLE_V1",),
            private=True,
        )
        yield _root(
            loaded,
            source_id=source.source_id,
            locator=f"{source.source_id}/raw/placeholder",
            operations=("P01_ALPHA_RENAME_SINGLE_V1",),
            blocked=True,
        )

    monkeypatch.setattr(census, "iter_git_source", records)
    monkeypatch.setattr(census, "iter_parquet_source", records)
    journal = tmp_path / "journal.jsonl"
    receipt = census.run_build(
        loaded,
        "selected_wave",
        output=tmp_path / "receipt.json",
        journal=journal,
        terminal_marker=tmp_path / "marker.json",
        state_db=tmp_path / "state.sqlite",
        input_manifest=None,
    )
    assert receipt.total_raw_declaration_count == 12
    assert receipt.total_root_count == 12
    assert receipt.total_eligible_root_count == 4
    assert receipt.total_excluded_declaration_count == 8
    for result in receipt.source_results:
        assert result.completion_scope == "bounded_sampling_frame_route_slice"
        assert result.scan_complete is True
        assert result.source_inventory_complete is False
        assert result.raw_declaration_count == 3
        assert result.eligible_root_count == 1
        assert result.excluded_declaration_count == 2
        assert result.exclusion_counts.private_declaration == 1
        assert result.exclusion_counts.proof_placeholder == 1
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    source_events = [item for item in events if item["event"] == "source_route_slice_complete"]
    assert len(source_events) == 4
    assert all(item["source_inventory_complete"] is False for item in source_events)
    assert all(
        item["completion_scope"] == "bounded_sampling_frame_route_slice" for item in source_events
    )


def test_direct_receipt_reports_n31_optional_status_and_pool_hashes(tmp_path: Path) -> None:
    loaded = census.load_wave1_census_config()
    journal = tmp_path / "journal.jsonl"
    state = census.CensusState(
        tmp_path / "state.sqlite",
        census.make_state_binding(loaded, "full_cross_source", None, journal),
    )
    try:
        state.add(
            _root(
                loaded,
                source_id="mathlib",
                locator="Mathlib/A.lean#decl=0:n",
                operations=("N31_DROP_REQUIRED_GUARD_RUBRIC_V1",),
                proof_status="available",
                proof_hash=sha256_hex(b"proof"),
            )
        )
        for source_id in census.EXPECTED_SOURCE_IDS:
            state.mark_complete(source_id)
        final_hash = _finalize_state_journal(loaded, "full_cross_source", state, journal)
        receipt = census.build_receipt(
            loaded,
            "full_cross_source",
            state,
            input_manifest_path=None,
            input_manifest_sha256=None,
            journal_path=str(journal),
            journal_final_chain_hash=final_hash,
        )
    finally:
        state.close()
    assert receipt.n31_proof_route_coverage.parent_root_count == 1
    assert receipt.n31_proof_route_coverage.available == 1
    assert receipt.n31_proof_route_coverage.activation_authorized is False
    assert len(receipt.operation_pool_hashes.N31_DROP_REQUIRED_GUARD_RUBRIC_V1) == 64


def test_failed_build_writes_hash_chained_terminal_evidence(tmp_path: Path) -> None:
    loaded = census.load_wave1_census_config()
    manifest = tmp_path / "bad.jsonl"
    manifest.write_text("{}\n", encoding="utf-8")
    marker = tmp_path / "marker.json"
    journal = tmp_path / "journal.jsonl"
    with pytest.raises(census.CensusError, match="invalid smoke manifest entry"):
        census.run_build(
            loaded,
            "smoke",
            output=tmp_path / "receipt.json",
            journal=journal,
            terminal_marker=marker,
            state_db=tmp_path / "state.sqlite",
            input_manifest=manifest,
        )
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    evidence = census._read_journal(journal)
    assert marker_payload["status"] == "failed"
    assert marker_payload["journal_final_chain_hash"] == evidence.final_chain_hash
    assert evidence.final_event == "failed"


def test_build_route_rejects_missing_or_misplaced_manifest(tmp_path: Path) -> None:
    loaded = census.load_wave1_census_config()
    arguments = {
        "output": tmp_path / "receipt.json",
        "journal": tmp_path / "journal.jsonl",
        "terminal_marker": tmp_path / "marker.json",
        "state_db": tmp_path / "state.sqlite",
    }
    with pytest.raises(census.CensusError, match="requires --input-manifest"):
        census.run_build(loaded, "smoke", input_manifest=None, **arguments)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")
    with pytest.raises(census.CensusError, match="cannot use --input-manifest"):
        census.run_build(loaded, "selected_wave", input_manifest=manifest, **arguments)


def test_module_has_no_lean_backend_dependency() -> None:
    import inspect

    source = inspect.getsource(census)
    assert "from leanfaith.lean" not in source
    assert "import leanfaith.lean" not in source


def test_default_config_path_is_repo_relative() -> None:
    expected = Path("configs/transformations/sft1_value_first_v1/wave1_census_v0_3_6.yaml")
    assert expected == census.DEFAULT_CONFIG_PATH
