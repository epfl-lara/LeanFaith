from __future__ import annotations

import datetime
from dataclasses import replace
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.lean.cache import (
    EvidenceCache,
    EvidenceCacheConflictError,
    EvidenceCacheCorruptionError,
)
from leanfaith.representations.goal_v1 import (
    ClosedExprBatchResult,
    ClosedExprInput,
    ClosedExprProvenance,
    ClosedExprRecord,
    ClosedExprSidecar,
    ClosedExprSourceMaterial,
    CompileContext,
    RendererImplementationIdentity,
)
from leanfaith.sft1.wave1_meta_adapter import (
    RenderedWave1Pair,
    Wave1CentralCacheAdapter,
    Wave1MetaAdapterError,
    bind_wave1_central_cache_key,
    make_wave1_audit_evidence,
    persist_wave1_sidecars,
    render_wave1_batch,
    render_wave1_pair,
    runtime_endpoints_from_pair,
)
from leanfaith.sft1.wave1_readiness import Wave1CacheKey
from leanfaith.sft1.wave1_runtime import (
    P01NameOnlyDelta,
    TypedCertificateReceipt,
    compute_p01_binder_aware_fingerprint,
    compute_p01_selected_site_lineage_hash,
    p01_outer_binder_site_path,
)


def _h(value: object) -> str:
    return hash_canonical(value)


def _context() -> CompileContext:
    return CompileContext(
        project_id="mathlib",
        project_revision="d568c8c09630de097a046763c17b9ea99f95f950",
        lean_version="v4.31.0-rc1",
        import_header="import Mathlib",
        command_preamble="set_option Elab.async false",
    )


def _inputs() -> tuple[ClosedExprInput, ClosedExprInput]:
    return (
        ClosedExprInput(
            endpoint_id="case.reference",
            endpoint_role="reference",
            expr_origin="term_elaborated_proposition",
            source_material=ClosedExprSourceMaterial(
                kind="proposition_text", proposition_text="(x : Nat) → x = x"
            ),
        ),
        ClosedExprInput(
            endpoint_id="case.candidate",
            endpoint_role="candidate",
            expr_origin="sft1_transformed_expr",
            source_material=ClosedExprSourceMaterial(
                kind="constructed_expr_no_source_text",
                absence_reason="candidate exists only as the certified live Expr",
            ),
        ),
    )


def _sidecar(
    item: ClosedExprInput, text: str, expr: str, context: CompileContext
) -> ClosedExprSidecar:
    identity = RendererImplementationIdentity(
        renderer_semantic_hash=("0bec5429cc0e539841208be53cd52189a7b80cbdb4649ee2d45b84bd8a5ef1fd"),
        lean_renderer_sha256=("4471262f812746046570c51dde5958ee33db31a450a6974071efce584ba56bc3"),
        injected_helper_sha256=("a6650452eebe683db295df1dfe925d3db8b03fc24e55cbc6793e838b5fe2f272"),
        python_module_sha256=("496237e190c394e9bd3c3036e2bc01c635905116c5084787a42e6cb569f45517"),
        config_file_sha256=("a65d5b29760bbc5eb89405927f946f205eb99856c0538fdf5b57d3f9eceb0db7"),
        implementation_set_hash=(
            "9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff"
        ),
    )
    provenance = ClosedExprProvenance(
        expr_hash=_h(expr),
        expr_hash_algorithm="sha256_canonical_closed_expr_alpha_tree_v1",
        input_level_params=(),
        canonical_level_params=(),
        universe_profile_id="goal_v1_first_occurrence_u_i_v1",
        universe_profile_hash=("d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"),
        render_scope_id="scope",
        render_context_id="goal_v1_render_context_v1",
        render_context_hash=("5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"),
        route_id="closed_expr_in_session",
        expr_origin=item.expr_origin,
    )
    identity_payload = {
        "renderer_version": "goal_v1.0",
        "spec_hash": "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8",
        "goal_v1_source": "closed_prop_expr",
        "goal_v1": text,
        "rendered_goal_hash": sha256_hex(text.encode()),
        "endpoint_id": item.endpoint_id,
        "endpoint_role": item.endpoint_role,
        "source_material_hash": item.source_material.material_hash,
        "compile_context_id": context.compile_context_id,
        "provenance": provenance.to_dict(),
        "implementation_identity": identity.to_dict(),
    }
    record = ClosedExprRecord(
        representation_id=f"repr:{_h(identity_payload)}",
        goal_v1=text,
        goal_v1_source="closed_prop_expr",
        renderer_version="goal_v1.0",
        spec_hash="68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8",
        compile_context_id=context.compile_context_id,
        endpoint_id=item.endpoint_id,
        endpoint_role=item.endpoint_role,
        source_material_hash=item.source_material.material_hash,
        rendered_goal_hash=sha256_hex(text.encode()),
        provenance=provenance,
        implementation_identity=identity,
    )
    return ClosedExprSidecar(
        record=record, source_material=item.source_material, compile_context=context
    )


def _result() -> tuple[
    ClosedExprBatchResult, tuple[ClosedExprInput, ClosedExprInput], CompileContext
]:
    inputs = _inputs()
    context = _context()
    result = ClosedExprBatchResult(
        sidecars=(
            _sidecar(inputs[0], "x : Nat\n⊢ x = x", "source", context),
            _sidecar(inputs[1], "y : Nat\n⊢ y = y", "candidate", context),
        ),
        failures=(),
        request_hash=_h("render-request"),
        elapsed_ms=17,
        raw_response_path=None,
        render_scope_id="scope",
    )
    return result, inputs, context


def _pair(*, raw_response_path: str | None = None) -> RenderedWave1Pair:
    result, _inputs_value, _context_value = _result()
    return RenderedWave1Pair(
        reference=result.sidecars[0],
        candidate=result.sidecars[1],
        request_hash=result.request_hash,
        elapsed_ms=result.elapsed_ms,
        raw_response_path=raw_response_path,
        render_scope_id="scope",
        reference_sidecar_sha256=sha256_hex(
            canonical_json_bytes(result.sidecars[0].to_dict()) + b"\n"
        ),
        candidate_sidecar_sha256=sha256_hex(
            canonical_json_bytes(result.sidecars[1].to_dict()) + b"\n"
        ),
    )


def test_pair_calls_shared_renderer_once_with_both_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, inputs, context = _result()
    calls: list[dict[str, object]] = []

    def fake_renderer(_backend: object, **kwargs: object) -> ClosedExprBatchResult:
        calls.append(kwargs)
        return result

    monkeypatch.setattr(
        "leanfaith.sft1.wave1_meta_adapter.render_closed_expr_in_session", fake_renderer
    )
    pair = render_wave1_pair(
        object(),  # type: ignore[arg-type]
        reference=inputs[0],
        candidate=inputs[1],
        compile_context=context,
        render_scope_id="scope",
        session_body=(
            "run_meta do\n"
            '  LeanFaith.GoalV1.emitClosedProp "case.reference" "scope" '
            '"term_elaborated_proposition" sourceExpr\n'
            '  LeanFaith.GoalV1.emitClosedProp "case.candidate" "scope" '
            '"sft1_transformed_expr" candidateExpr'
        ),
        request_id="request",
        timeout_seconds=2.0,
    )
    assert len(calls) == 1
    assert calls[0]["inputs"] == inputs
    assert pair.model_facing_texts() == ("x : Nat\n⊢ x = x", "y : Nat\n⊢ y = y")
    endpoints = runtime_endpoints_from_pair(pair)
    assert endpoints[0].closed_expr_hash == pair.reference.record.provenance.expr_hash
    assert endpoints[1].complete_sidecar_sha256 == pair.candidate_sidecar_sha256
    assert endpoints[0].render_request_hash == endpoints[1].render_request_hash


@pytest.mark.parametrize(
    ("bad_text", "reason"),
    [
        ("x : Nat\nx = x", "exactly one turnstile"),
        ("x : Nat\n⊢ x = x\n⊢ True", "exactly one turnstile"),
        ("[anonymous] : Nat\n⊢ True", "anonymous_binder_name"),
        ("x : Nat\n⊢ ⋯", "forbidden_rendered_placeholder"),
    ],
)
def test_rendered_residue_fails_closed(
    monkeypatch: pytest.MonkeyPatch, bad_text: str, reason: str
) -> None:
    result, inputs, context = _result()
    bad = ClosedExprBatchResult(
        sidecars=(_sidecar(inputs[0], bad_text, "source", context), result.sidecars[1]),
        failures=(),
        request_hash=result.request_hash,
        elapsed_ms=result.elapsed_ms,
        raw_response_path=None,
        render_scope_id="scope",
    )
    monkeypatch.setattr(
        "leanfaith.sft1.wave1_meta_adapter.render_closed_expr_in_session",
        lambda *_args, **_kwargs: bad,
    )
    with pytest.raises(Wave1MetaAdapterError, match=reason):
        render_wave1_pair(
            object(),  # type: ignore[arg-type]
            reference=inputs[0],
            candidate=inputs[1],
            compile_context=context,
            render_scope_id="scope",
            session_body="run_meta do\n  exact two direct emitter calls are checked below",
            request_id="request",
            timeout_seconds=2.0,
        )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("algorithm", "universe or render-context"),
        ("route", "universe or render-context"),
        ("render_hash", "rendered-goal content hash"),
        ("representation_id", "representation identity"),
        ("implementation", "implementation identity"),
    ],
)
def test_repr_identity_mutations_fail_closed(
    monkeypatch: pytest.MonkeyPatch, mutation: str, reason: str
) -> None:
    result, inputs, context = _result()
    sidecar = result.sidecars[0]
    if mutation == "algorithm":
        provenance = replace(sidecar.record.provenance, expr_hash_algorithm="wrong")
        sidecar = replace(sidecar, record=replace(sidecar.record, provenance=provenance))
    elif mutation == "route":
        provenance = replace(sidecar.record.provenance, route_id="surface")
        sidecar = replace(sidecar, record=replace(sidecar.record, provenance=provenance))
    elif mutation == "render_hash":
        sidecar = replace(
            sidecar, record=replace(sidecar.record, rendered_goal_hash=_h("wrong-render"))
        )
    elif mutation == "representation_id":
        sidecar = replace(
            sidecar, record=replace(sidecar.record, representation_id=f"repr:{_h('x')}")
        )
    else:
        identity = replace(sidecar.record.implementation_identity, implementation_set_hash=_h("x"))
        sidecar = replace(sidecar, record=replace(sidecar.record, implementation_identity=identity))
    bad = replace(result, sidecars=(sidecar, result.sidecars[1]))
    monkeypatch.setattr(
        "leanfaith.sft1.wave1_meta_adapter.render_closed_expr_in_session",
        lambda *_args, **_kwargs: bad,
    )
    with pytest.raises(Wave1MetaAdapterError, match=reason):
        render_wave1_pair(
            object(),  # type: ignore[arg-type]
            reference=inputs[0],
            candidate=inputs[1],
            compile_context=context,
            render_scope_id="scope",
            session_body="run_meta do\n  emitters",
            request_id="request",
            timeout_seconds=2.0,
        )


def test_composed_batch_renders_three_endpoints_in_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, inputs, context = _result()
    third = ClosedExprInput(
        endpoint_id="case.final",
        endpoint_role="candidate",
        expr_origin="sft1_transformed_expr",
        source_material=ClosedExprSourceMaterial(
            kind="constructed_expr_no_source_text",
            absence_reason="final composed candidate",
        ),
    )
    expanded = replace(
        result,
        sidecars=(*result.sidecars, _sidecar(third, "z : Nat\n⊢ z = z", "final", context)),
    )
    calls: list[object] = []

    def fake_renderer(_backend: object, **kwargs: object) -> ClosedExprBatchResult:
        calls.append(kwargs["inputs"])
        return expanded

    monkeypatch.setattr(
        "leanfaith.sft1.wave1_meta_adapter.render_closed_expr_in_session", fake_renderer
    )
    batch = render_wave1_batch(
        object(),  # type: ignore[arg-type]
        inputs=(*inputs, third),
        compile_context=context,
        render_scope_id="scope",
        session_body="run_meta do\n  three explicitly unrolled emitters",
        request_id="request",
        timeout_seconds=2.0,
    )
    assert len(calls) == 1
    assert len(batch.sidecars) == 3


def test_complete_sidecars_persist_content_addressed_and_immutable(tmp_path: Path) -> None:
    pair = _pair()
    persisted = persist_wave1_sidecars(pair, tmp_path)
    assert persisted.reference_path.is_file()
    assert persisted.candidate_path.is_file()
    assert persist_wave1_sidecars(pair, tmp_path) == persisted

    persisted.reference_path.chmod(0o644)
    persisted.reference_path.write_text("conflict", encoding="utf-8")
    with pytest.raises(Wave1MetaAdapterError, match="immutable sidecar conflict"):
        persist_wave1_sidecars(pair, tmp_path)


def _replay_receipt(pair: RenderedWave1Pair) -> TypedCertificateReceipt:
    selected_site_ordinal = 0
    selected_site_path = p01_outer_binder_site_path(selected_site_ordinal)
    old_name = "x"
    new_name = "x_1"
    binder_info = "default"
    return TypedCertificateReceipt(
        operation_id="P01_ALPHA_RENAME_SINGLE_V1",
        source_closed_expr_hash=pair.reference.record.provenance.expr_hash,
        candidate_closed_expr_hash=pair.candidate.record.provenance.expr_hash,
        source_sidecar_sha256=pair.reference_sidecar_sha256,
        candidate_sidecar_sha256=pair.candidate_sidecar_sha256,
        render_request_hash=pair.request_hash,
        replay_request_hash=pair.request_hash,
        selected_site_path=selected_site_path,
        selected_site_path_fingerprint=_h(selected_site_path),
        selected_site_lineage_hash=compute_p01_selected_site_lineage_hash(
            selected_site_path=selected_site_path,
            selected_site_ordinal=selected_site_ordinal,
        ),
        binder_aware_source_fingerprint=compute_p01_binder_aware_fingerprint(
            endpoint_role="source",
            closed_expr_hash=pair.reference.record.provenance.expr_hash,
            sidecar_sha256=pair.reference_sidecar_sha256,
            selected_site_path=selected_site_path,
            selected_site_ordinal=selected_site_ordinal,
            binder_name=old_name,
            binder_info=binder_info,
        ),
        binder_aware_candidate_fingerprint=compute_p01_binder_aware_fingerprint(
            endpoint_role="candidate",
            closed_expr_hash=pair.candidate.record.provenance.expr_hash,
            sidecar_sha256=pair.candidate_sidecar_sha256,
            selected_site_path=selected_site_path,
            selected_site_ordinal=selected_site_ordinal,
            binder_name=new_name,
            binder_info=binder_info,
        ),
        selected_site_uniquely_rediscovered=True,
        replayed_in_persistent_meta=True,
        certificate_replay_passed=True,
        candidate_is_exact_deterministic_replay_result=True,
        p01_name_only_delta=P01NameOnlyDelta(
            old_name=old_name,
            new_name=new_name,
            binder_info=binder_info,
            selected_site_ordinal=selected_site_ordinal,
            selected_site_rediscovery_count=1,
            domains_unchanged=True,
            bodies_unchanged_except_selected_name=True,
            bound_variable_indices_unchanged=True,
            universes_unchanged=True,
            metadata_unchanged=True,
            other_binders_unchanged=True,
            binder_info_unchanged=True,
        ),
    )


def _wave1_key(pair: RenderedWave1Pair) -> Wave1CacheKey:
    replay = _replay_receipt(pair)
    return Wave1CacheKey(
        source_closed_expr_hash=pair.reference.record.provenance.expr_hash,
        candidate_closed_expr_hash=pair.candidate.record.provenance.expr_hash,
        canonical_universe_profile_id="goal_v1_first_occurrence_u_i_v1",
        canonical_universe_profile_hash=(
            "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
        ),
        source_expr_builder_version="source_builder_v1",
        candidate_expr_builder_version="candidate_builder_v1",
        lean_version="v4.31.0-rc1",
        project_id="mathlib",
        project_revision="d568c8c09630de097a046763c17b9ea99f95f950",
        toolchain_revision="v4.31.0-rc1",
        imports_hash=_h("imports"),
        options_hash=_h("options"),
        synthesized_instance_hashes=(),
        operation_id="P01_ALPHA_RENAME_SINGLE_V1",
        operation_registry_entry_hash=(
            "36b680c6c3407d0761de8af1b0c5e685ce0bc89fefed720bc46255c9bc218844"
        ),
        schema_lemma_procedure_hash=(
            "ca485f300ecc818057f10877f0eec5c6b4b963fec2e0a574a6895f9d83357095"
        ),
        evidence_certificate_payload_hash=_h(replay.model_dump(mode="json")),
        bank_resolved_lean_hash=_h("live-resolved-positive-anchor-bundle"),
        transparency="none",
        allowed_axiom_profile="constructive_kernel",
        typed_meta_validator_version="wave1_validator_v0_3_6",
        evidence_replay_version="wave1_replay_v0_3_6",
        evaluation_blocklist_sha256=(
            "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
        ),
        repr_replacement_commit="176a783842c5a73b84413dfa8347670608b615d9",
        render_context_id="goal_v1_render_context_v1",
        render_context_hash=("5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"),
        renderer_api_hash=("c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"),
        repr_spec_hash="68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8",
        environment_fingerprint_hash=_h("environment"),
        policy_config_hash=_h("policy"),
    )


def test_complete_wave1_key_is_bound_into_central_immutable_cache(tmp_path: Path) -> None:
    raw_response_path = tmp_path / "raw-lean-response.json"
    raw_response_path.write_bytes(b'{"status":"ok"}\n')
    pair = _pair(raw_response_path=str(raw_response_path))
    key = _wave1_key(pair)
    replay = _replay_receipt(pair)
    replay_payload = canonical_json_bytes(replay.model_dump(mode="json")) + b"\n"
    replay_artifact_sha256 = sha256_hex(replay_payload)
    replay_path = tmp_path / "typed-replay.json"
    replay_path.write_bytes(replay_payload)
    raw_response_sha256 = sha256_hex(raw_response_path.read_bytes())
    binding = bind_wave1_central_cache_key(
        key,
        pair=pair,
        environment_schema_version=1,
        lean_interact_version="leaninteract-test",
        repl_revision="repl-test",
        timeout_seconds=2.0,
    )
    evidence = make_wave1_audit_evidence(
        binding,
        checks={
            "typed_meta_validation": True,
            "typed_certificate_replay": True,
            "same_request_repr": True,
            "sidecars_persisted": True,
            "candidate_truth_known": None,
        },
        violation_codes=(),
        typed_replay_artifact_path=str(replay_path),
        typed_replay_artifact_sha256=replay_artifact_sha256,
        raw_response_artifact_path=str(raw_response_path),
        raw_response_artifact_sha256=raw_response_sha256,
        created_at=datetime.datetime(2026, 8, 31, tzinfo=datetime.UTC),
    )
    persisted = persist_wave1_sidecars(pair, tmp_path / "sidecars")
    artifacts = {
        str(persisted.reference_path): pair.reference_sidecar_sha256,
        str(persisted.candidate_path): pair.candidate_sidecar_sha256,
        str(replay_path): replay_artifact_sha256,
        str(raw_response_path): raw_response_sha256,
    }
    adapter = Wave1CentralCacheAdapter(EvidenceCache(tmp_path / "cache"))
    entry = adapter.put(
        binding,
        evidence,
        lean_request_hashes=(pair.request_hash,),
        certificate_dependency_hash=key.evidence_certificate_payload_hash,
        artifact_hashes=artifacts,
    )
    assert (
        adapter.get_after_replay(
            binding,
            replay_receipt=replay,
            replay_artifact_sha256=replay_artifact_sha256,
        )
        == entry
    )
    assert binding.wave1_key_hash in binding.central_key.method_version

    conflicting = make_wave1_audit_evidence(
        binding,
        checks={
            "typed_meta_validation": True,
            "typed_certificate_replay": True,
            "same_request_repr": True,
            "sidecars_persisted": True,
            "candidate_truth_known": True,
        },
        violation_codes=("conflicting_evidence",),
        typed_replay_artifact_path=str(replay_path),
        typed_replay_artifact_sha256=replay_artifact_sha256,
        raw_response_artifact_path=str(raw_response_path),
        raw_response_artifact_sha256=raw_response_sha256,
        created_at=datetime.datetime(2026, 8, 31, tzinfo=datetime.UTC),
    )
    with pytest.raises(EvidenceCacheConflictError):
        adapter.put(
            binding,
            conflicting,
            lean_request_hashes=(pair.request_hash,),
            certificate_dependency_hash=key.evidence_certificate_payload_hash,
            artifact_hashes=artifacts,
        )


def test_cache_binding_rejects_sidecar_or_certificate_substitution(tmp_path: Path) -> None:
    raw_response_path = tmp_path / "raw-response.json"
    raw_response_path.write_bytes(b'{"status":"ok"}\n')
    pair = _pair(raw_response_path=str(raw_response_path))
    replay = _replay_receipt(pair)
    replay_payload = canonical_json_bytes(replay.model_dump(mode="json")) + b"\n"
    replay_artifact_sha256 = sha256_hex(replay_payload)
    replay_path = tmp_path / "typed-replay.json"
    replay_path.write_bytes(replay_payload)
    raw_response_sha256 = sha256_hex(raw_response_path.read_bytes())
    wrong_key = _wave1_key(pair).model_copy(
        update={"candidate_closed_expr_hash": _h("wrong-candidate")}
    )
    with pytest.raises(Wave1MetaAdapterError, match="closed-Expr hashes"):
        bind_wave1_central_cache_key(
            wrong_key,
            pair=pair,
            environment_schema_version=1,
            lean_interact_version="leaninteract-test",
            repl_revision="repl-test",
            timeout_seconds=2.0,
        )

    key = _wave1_key(pair)
    binding = bind_wave1_central_cache_key(
        key,
        pair=pair,
        environment_schema_version=1,
        lean_interact_version="leaninteract-test",
        repl_revision="repl-test",
        timeout_seconds=2.0,
    )
    evidence = make_wave1_audit_evidence(
        binding,
        checks=dict.fromkeys(
            (
                "typed_meta_validation",
                "typed_certificate_replay",
                "same_request_repr",
                "sidecars_persisted",
            ),
            True,
        ),
        violation_codes=(),
        typed_replay_artifact_path=str(replay_path),
        typed_replay_artifact_sha256=replay_artifact_sha256,
        raw_response_artifact_path=str(raw_response_path),
        raw_response_artifact_sha256=raw_response_sha256,
        created_at=datetime.datetime(2026, 8, 31, tzinfo=datetime.UTC),
    )
    persisted = persist_wave1_sidecars(pair, tmp_path / "sidecars")
    sidecar_artifacts = {
        str(persisted.reference_path): pair.reference_sidecar_sha256,
        str(persisted.candidate_path): pair.candidate_sidecar_sha256,
    }
    adapter = Wave1CentralCacheAdapter(EvidenceCache(tmp_path))
    with pytest.raises(Wave1MetaAdapterError, match="certificate dependency"):
        adapter.put(
            binding,
            evidence,
            lean_request_hashes=(pair.request_hash,),
            certificate_dependency_hash=_h("wrong"),
            artifact_hashes={
                **sidecar_artifacts,
                str(replay_path): replay_artifact_sha256,
                str(raw_response_path): raw_response_sha256,
            },
        )
    with pytest.raises(Wave1MetaAdapterError, match="typed replay artifact binding"):
        adapter.put(
            binding,
            evidence,
            lean_request_hashes=(pair.request_hash,),
            certificate_dependency_hash=key.evidence_certificate_payload_hash,
            artifact_hashes={
                **sidecar_artifacts,
                str(replay_path): _h("wrong-typed-replay"),
                str(raw_response_path): raw_response_sha256,
            },
        )
    with pytest.raises(Wave1MetaAdapterError, match=r"typed replay receipt|replay artifact"):
        adapter.get_after_replay(
            binding,
            replay_receipt=replay,
            replay_artifact_sha256=_h("unrelated-replay-artifact"),
        )
    forged = replay.model_copy(update={"operation_id": "P15_SWAP_IFF_SIDES_V1"})
    with pytest.raises(Wave1MetaAdapterError, match=r"typed replay|replay artifact"):
        adapter.get_after_replay(
            binding,
            replay_receipt=forged,
            replay_artifact_sha256=replay_artifact_sha256,
        )


@pytest.mark.parametrize(
    "expr_hash_field",
    ("source_closed_expr_hash", "candidate_closed_expr_hash"),
)
def test_cache_replay_binds_certificate_expr_hashes_to_both_endpoints(
    tmp_path: Path, expr_hash_field: str
) -> None:
    pair = _pair()
    forged = _replay_receipt(pair).model_copy(
        update={expr_hash_field: _h(f"different-{expr_hash_field}")}
    )
    forged_payload = canonical_json_bytes(forged.model_dump(mode="json")) + b"\n"
    forged_hash = sha256_hex(forged_payload)
    key = _wave1_key(pair).model_copy(
        update={"evidence_certificate_payload_hash": hash_canonical(forged.model_dump(mode="json"))}
    )
    binding = bind_wave1_central_cache_key(
        key,
        pair=pair,
        environment_schema_version=1,
        lean_interact_version="leaninteract-test",
        repl_revision="repl-test",
        timeout_seconds=2.0,
    )
    adapter = Wave1CentralCacheAdapter(EvidenceCache(tmp_path / "cache"))
    with pytest.raises(Wave1MetaAdapterError, match=r"typed replay receipt|replay artifact"):
        adapter.get_after_replay(
            binding,
            replay_receipt=forged,
            replay_artifact_sha256=forged_hash,
        )


def test_live_evidence_requires_renderer_raw_response_path(tmp_path: Path) -> None:
    pair = _pair()
    replay = _replay_receipt(pair)
    binding = bind_wave1_central_cache_key(
        _wave1_key(pair),
        pair=pair,
        environment_schema_version=1,
        lean_interact_version="leaninteract-test",
        repl_revision="repl-test",
        timeout_seconds=2.0,
    )
    with pytest.raises(Wave1MetaAdapterError, match="renderer raw response path"):
        make_wave1_audit_evidence(
            binding,
            checks=dict.fromkeys(
                (
                    "typed_meta_validation",
                    "typed_certificate_replay",
                    "same_request_repr",
                    "sidecars_persisted",
                ),
                True,
            ),
            violation_codes=(),
            typed_replay_artifact_path=str(tmp_path / "typed-replay.json"),
            typed_replay_artifact_sha256=sha256_hex(
                canonical_json_bytes(replay.model_dump(mode="json")) + b"\n"
            ),
            raw_response_artifact_path=str(tmp_path / "raw-response.json"),
            raw_response_artifact_sha256=_h("raw-response"),
            created_at=datetime.datetime(2026, 8, 31, tzinfo=datetime.UTC),
        )


@pytest.mark.parametrize(
    ("poison", "reason"),
    (
        ("checks", "mandatory replay checks"),
        ("certificate", "certificate dependency"),
        ("request", "REPR/Meta request"),
        ("sidecars", "complete sidecars"),
        ("sidecar_content", "does not match"),
        ("duplicate_sidecar", "exactly one bound artifact path"),
        ("typed_hash", "typed replay artifact binding"),
        ("raw_hash", "raw response artifact binding"),
    ),
)
def test_directly_inserted_poisoned_central_cache_entry_fails_closed(
    tmp_path: Path, poison: str, reason: str
) -> None:
    raw_response_path = tmp_path / "raw-response.json"
    raw_response_path.write_bytes(b'{"status":"ok"}\n')
    pair = _pair(raw_response_path=str(raw_response_path))
    key = _wave1_key(pair)
    replay = _replay_receipt(pair)
    replay_payload = canonical_json_bytes(replay.model_dump(mode="json")) + b"\n"
    replay_sha256 = sha256_hex(replay_payload)
    replay_path = tmp_path / "typed-replay.json"
    replay_path.write_bytes(replay_payload)
    raw_response_sha256 = sha256_hex(raw_response_path.read_bytes())
    binding = bind_wave1_central_cache_key(
        key,
        pair=pair,
        environment_schema_version=1,
        lean_interact_version="leaninteract-test",
        repl_revision="repl-test",
        timeout_seconds=2.0,
    )
    checks = dict.fromkeys(
        (
            "typed_meta_validation",
            "typed_certificate_replay",
            "same_request_repr",
            "sidecars_persisted",
        ),
        True,
    )
    if poison == "checks":
        checks["typed_certificate_replay"] = False
    evidence = make_wave1_audit_evidence(
        binding,
        checks=checks,
        violation_codes=("direct_cache_poison",),
        typed_replay_artifact_path=str(replay_path),
        typed_replay_artifact_sha256=replay_sha256,
        raw_response_artifact_path=str(raw_response_path),
        raw_response_artifact_sha256=raw_response_sha256,
        created_at=datetime.datetime(2026, 8, 31, tzinfo=datetime.UTC),
    )
    metadata = dict(evidence.metadata)
    if poison == "typed_hash":
        metadata["typed_replay_artifact_sha256"] = _h("wrong-typed-replay")
        evidence = evidence.model_copy(update={"metadata": metadata})
    elif poison == "raw_hash":
        metadata["raw_artifact_sha256"] = _h("wrong-raw-response")
        evidence = evidence.model_copy(update={"metadata": metadata})

    persisted = persist_wave1_sidecars(pair, tmp_path / "sidecars")
    artifact_hashes = {
        str(persisted.reference_path): pair.reference_sidecar_sha256,
        str(persisted.candidate_path): pair.candidate_sidecar_sha256,
        str(replay_path): replay_sha256,
        str(raw_response_path): raw_response_sha256,
    }
    if poison == "sidecars":
        artifact_hashes = {
            str(replay_path): replay_sha256,
            str(raw_response_path): raw_response_sha256,
        }
    elif poison == "duplicate_sidecar":
        duplicate = tmp_path / "duplicate-reference-sidecar.json"
        duplicate.write_bytes(persisted.reference_path.read_bytes())
        artifact_hashes[str(duplicate)] = persisted.reference_sha256
    lean_request_hashes = () if poison == "request" else (pair.request_hash,)
    certificate_dependency_hash = (
        None if poison == "certificate" else key.evidence_certificate_payload_hash
    )
    cache = EvidenceCache(tmp_path / "cache")
    cache.put(
        binding.central_key,
        evidence,
        lean_request_hashes=lean_request_hashes,
        certificate_dependency_hash=certificate_dependency_hash,
        artifact_hashes=artifact_hashes,
    )
    if poison == "sidecar_content":
        persisted.reference_path.chmod(0o644)
        persisted.reference_path.write_bytes(b'{"corrupt":true}\n')
    expected_error = (
        EvidenceCacheCorruptionError if poison == "sidecar_content" else Wave1MetaAdapterError
    )
    with pytest.raises(expected_error, match=reason):
        Wave1CentralCacheAdapter(cache).get_after_replay(
            binding,
            replay_receipt=replay,
            replay_artifact_sha256=replay_sha256,
        )
