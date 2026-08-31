from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft1.wave1_runtime import (
    DedupCandidate,
    P01CapObservation,
    P01NameOnlyDelta,
    RuntimeChain,
    RuntimeEdge,
    RuntimeEndpoint,
    RuntimeRetentionBatch,
    TypedCertificateReceipt,
    Wave1RuntimeError,
    assert_post_orientation_unique,
    compute_p01_binder_aware_fingerprint,
    compute_p01_selected_site_lineage_hash,
    deduplicate_unordered_pairs,
    load_and_validate_p01_runtime_binding,
    make_runtime_chain,
    validate_p01_caps,
    validate_retention_batch,
    validate_runtime_chain,
)


def _h(value: object) -> str:
    return hash_canonical(value)


def _retention_batch(
    chains: tuple[RuntimeChain, ...],
    tmp_path: Path,
    *,
    scope_id: str = "complete-test-scope",
) -> RuntimeRetentionBatch:
    evidence_root = (tmp_path / f"{scope_id}.evidence").resolve()
    artifact_root = evidence_root / "artifacts"
    artifact_root.mkdir(parents=True)
    journal_relative_path = Path("artifacts") / f"{scope_id}.journal.jsonl"
    manifest_relative_path = Path("artifacts") / f"{scope_id}.manifest.json"
    journal_path = evidence_root / journal_relative_path
    manifest_path = evidence_root / manifest_relative_path
    previous = "0" * 64
    journal_records: list[dict[str, object]] = []
    for sequence, chain in enumerate(chains):
        core: dict[str, object] = {
            "schema_version": 1,
            "sequence": sequence,
            "previous_chain_hash": previous,
            "retention_scope_id": scope_id,
            "event": "prospective_chain_bound",
            "stable_row_hash": chain.stable_row_hash,
        }
        record = {**core, "chain_hash": hash_canonical(core)}
        journal_records.append(record)
        previous = str(record["chain_hash"])
    journal_path.write_bytes(
        b"".join(canonical_json_bytes(record) + b"\n" for record in journal_records)
    )
    manifest_core: dict[str, object] = {
        "schema_version": 1,
        "manifest_kind": "durable_complete_prospective_retention_manifest_v1",
        "scope_purpose": "bounded_readiness_contract_fixture_scope_v1",
        "retention_scope_id": scope_id,
        "evidence_root_path": str(evidence_root),
        "scope_manifest_relative_path": manifest_relative_path.as_posix(),
        "scope_journal_relative_path": journal_relative_path.as_posix(),
        "scope_journal_file_sha256": hash_file(journal_path),
        "complete_scope": True,
        "scope_record_count": len(chains),
        "scope_journal_final_chain_hash": previous,
        "chains": [chain.model_dump(mode="json") for chain in chains],
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
        "production_admission_changed": False,
    }
    manifest_hash = hash_canonical(manifest_core)
    manifest = {**manifest_core, "manifest_hash": manifest_hash}
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    return RuntimeRetentionBatch(
        retention_scope_id=scope_id,
        evidence_root_path=str(evidence_root),
        scope_manifest_relative_path=manifest_relative_path.as_posix(),
        scope_manifest_path=str(manifest_path),
        scope_manifest_file_sha256=hash_file(manifest_path),
        scope_manifest_hash=manifest_hash,
        scope_journal_relative_path=journal_relative_path.as_posix(),
        scope_journal_path=str(journal_path),
        scope_journal_file_sha256=hash_file(journal_path),
        scope_journal_final_chain_hash=previous,
    )


def _endpoint(index: int, *, expr: str | None = None) -> RuntimeEndpoint:
    return RuntimeEndpoint(
        closed_expr_hash=_h(expr or f"expr-{index}"),
        render_hash=_h(f"render-{index}"),
        core_text_sha256=_h(f"text-{index}"),
        complete_sidecar_sha256=_h(f"sidecar-{index}"),
        render_request_hash=_h("shared-render-request"),
        render_scope_id="wave1-test-scope",
        repr_spec_hash="68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8",
        renderer_api_hash="c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d",
        universe_profile_id="goal_v1_first_occurrence_u_i_v1",
        universe_profile_hash=("d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"),
        render_context_id="goal_v1_render_context_v1",
        render_context_hash=("5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"),
    )


def _certificate(
    operation_id: str,
    source: RuntimeEndpoint,
    candidate: RuntimeEndpoint,
    *,
    path: str,
    lineage: str | None = None,
) -> TypedCertificateReceipt:
    p01_delta = (
        P01NameOnlyDelta(
            old_name="x",
            new_name="x_1",
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
        if operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
        else None
    )
    source_fingerprint = _h(f"source:{operation_id}:{path}")
    candidate_fingerprint = _h(f"candidate:{operation_id}:{path}")
    if p01_delta is not None:
        source_fingerprint = compute_p01_binder_aware_fingerprint(
            endpoint_role="source",
            closed_expr_hash=source.closed_expr_hash,
            sidecar_sha256=source.complete_sidecar_sha256,
            selected_site_path=path,
            selected_site_ordinal=p01_delta.selected_site_ordinal,
            binder_name=p01_delta.old_name,
            binder_info=p01_delta.binder_info,
        )
        candidate_fingerprint = compute_p01_binder_aware_fingerprint(
            endpoint_role="candidate",
            closed_expr_hash=candidate.closed_expr_hash,
            sidecar_sha256=candidate.complete_sidecar_sha256,
            selected_site_path=path,
            selected_site_ordinal=p01_delta.selected_site_ordinal,
            binder_name=p01_delta.new_name,
            binder_info=p01_delta.binder_info,
        )
    selected_lineage = lineage or _h(f"lineage:{operation_id}:{path}")
    if p01_delta is not None and lineage is None:
        selected_lineage = compute_p01_selected_site_lineage_hash(
            selected_site_path=path,
            selected_site_ordinal=p01_delta.selected_site_ordinal,
        )
    return TypedCertificateReceipt(
        operation_id=operation_id,
        source_closed_expr_hash=source.closed_expr_hash,
        candidate_closed_expr_hash=candidate.closed_expr_hash,
        source_sidecar_sha256=source.complete_sidecar_sha256,
        candidate_sidecar_sha256=candidate.complete_sidecar_sha256,
        render_request_hash=source.render_request_hash,
        replay_request_hash=source.render_request_hash,
        selected_site_path=path,
        selected_site_path_fingerprint=_h(path),
        selected_site_lineage_hash=selected_lineage,
        binder_aware_source_fingerprint=source_fingerprint,
        binder_aware_candidate_fingerprint=candidate_fingerprint,
        selected_site_uniquely_rediscovered=True,
        replayed_in_persistent_meta=True,
        certificate_replay_passed=True,
        candidate_is_exact_deterministic_replay_result=True,
        p01_name_only_delta=p01_delta,
    )


def _edge(
    operation_id: str,
    source: RuntimeEndpoint,
    candidate: RuntimeEndpoint,
    *,
    path: str,
    lineage: str | None = None,
) -> RuntimeEdge:
    binding = load_and_validate_p01_runtime_binding()
    operation = next(item for item in binding.operations if item.operation_id == operation_id)
    certificate = _certificate(
        operation_id,
        source,
        candidate,
        path=path,
        lineage=lineage,
    )
    return RuntimeEdge(
        operation_id=operation_id,
        mechanism_superclass=operation.mechanism_superclass,
        inverse_token=operation.inverse_token,
        registry_entry_hash=operation.registry_entry_hash,
        anchor_hash=operation.anchor_hash,
        operation_bank_entry_hash=operation.operation_bank_entry_hash,
        certificate_payload_hash=_h(certificate.model_dump(mode="json")),
        certificate=certificate,
    )


def _chain(
    endpoints: tuple[RuntimeEndpoint, ...],
    edges: tuple[RuntimeEdge, ...],
    *,
    root: str = "root-1",
    source_identity: str = "source-1",
    polarity: str = "positive",
    label: int = 1,
) -> RuntimeChain:
    return make_runtime_chain(
        root_ancestry_id=root,
        source_identity_hash=_h(source_identity),
        polarity=polarity,  # type: ignore[arg-type]
        label=label,  # type: ignore[arg-type]
        endpoints=endpoints,
        edges=edges,
    )


def _valid_chain() -> RuntimeChain:
    reference = _endpoint(0, expr="alpha-canonical")
    candidate = _endpoint(1, expr="alpha-canonical")
    return _chain(
        (reference, candidate),
        (
            _edge(
                "P01_ALPHA_RENAME_SINGLE_V1",
                reference,
                candidate,
                path="/",
            ),
        ),
    )


def test_runtime_loads_exact_policy_and_bundle_hashes() -> None:
    binding = load_and_validate_p01_runtime_binding()
    assert binding.required_policy_semantic_hash == (
        "a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd"
    )
    assert binding.corrected_envelope_semantic_hash == (
        "dcdd6c07a83aa84faf81b448e2732121027b5a93fc89512caa38035b9c4cdbe4"
    )
    assert tuple(
        (item.operation_id, item.mechanism_superclass, item.inverse_token)
        for item in binding.operations
    ) == (
        ("P01_ALPHA_RENAME_SINGLE_V1", "presentation_alpha", "P01_ALPHA_RENAME"),
        ("P15_SWAP_IFF_SIDES_V1", "logical_symmetry", "P15_IFF_SWAP"),
        ("P18_SYMMETRIZE_EQUALITY_V1", "relation_symmetry", "P18_EQUALITY_SYMMETRY"),
        ("P21_BETA_REDUCE_V1", "definitional_beta", "P21_BETA_INTRO_REDUCE"),
        (
            "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
            "required_guard_mutation",
            "N31_DROP_REQUIRED_GUARD",
        ),
    )
    assert tuple(item.runtime_activated for item in binding.operations) == (
        True,
        True,
        True,
        True,
        False,
    )


def test_n31_cannot_enter_runtime_before_exact_user_admission() -> None:
    source = _endpoint(0)
    candidate = _endpoint(1)
    chain = _chain(
        (source, candidate),
        (
            _edge(
                "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
                source,
                candidate,
                path="/1",
            ),
        ),
        root="root",
        polarity="negative",
        label=0,
    )
    with pytest.raises(Wave1RuntimeError, match="operation_not_runtime_admitted"):
        validate_runtime_chain(chain)


def test_runtime_binding_cannot_be_injected_to_activate_n31() -> None:
    chain = _valid_chain()
    binding = load_and_validate_p01_runtime_binding()
    with pytest.raises(TypeError):
        validate_runtime_chain(chain, binding=binding)  # type: ignore[call-arg]


def test_exact_adjacent_p01_repeat_passes_only_after_certificate_replay() -> None:
    result = validate_runtime_chain(_valid_chain())
    assert result.p01_present
    assert result.exception_used


def test_p01_without_alpha_hash_repeat_fails_closed() -> None:
    source = _endpoint(0)
    candidate = _endpoint(1)
    chain = _chain(
        (source, candidate),
        (_edge("P01_ALPHA_RENAME_SINGLE_V1", source, candidate, path="/"),),
    )
    with pytest.raises(Wave1RuntimeError, match="p01_alpha_invariant_closed_expr_hash_mismatch"):
        validate_runtime_chain(chain)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("mechanism_superclass", "fake", "operation_binding_mismatch"),
        ("inverse_token", "fake", "operation_binding_mismatch"),
        ("registry_entry_hash", _h("fake"), "operation_binding_mismatch"),
        ("anchor_hash", _h("fake"), "operation_binding_mismatch"),
        ("operation_bank_entry_hash", _h("fake"), "operation_binding_mismatch"),
        ("certificate_payload_hash", _h("fake"), "invalid_runtime_chain_receipt"),
    ],
)
def test_edge_bindings_cannot_be_forged(field: str, value: str, reason: str) -> None:
    chain = _valid_chain()
    forged_edge = chain.edges[0].model_copy(update={field: value})
    forged_chain = (
        chain.model_copy(update={"edges": (forged_edge,)})
        if field == "certificate_payload_hash"
        else _chain(chain.endpoints, (forged_edge,))
    )
    with pytest.raises(Wave1RuntimeError, match=reason):
        validate_runtime_chain(forged_chain)


@pytest.mark.parametrize(
    "field",
    [
        "selected_site_uniquely_rediscovered",
        "replayed_in_persistent_meta",
        "certificate_replay_passed",
        "candidate_is_exact_deterministic_replay_result",
    ],
)
def test_missing_or_failed_certificate_axes_fail_after_unchecked_copy(field: str) -> None:
    chain = _valid_chain()
    certificate = chain.edges[0].certificate.model_copy(update={field: False})
    edge = chain.edges[0].model_copy(
        update={
            "certificate": certificate,
            "certificate_payload_hash": _h(certificate.model_dump(mode="json")),
        }
    )
    with pytest.raises(Wave1RuntimeError, match="invalid_runtime_chain_receipt"):
        validate_runtime_chain(chain.model_copy(update={"edges": (edge,)}))


@pytest.mark.parametrize(
    "field",
    [
        "domains_unchanged",
        "bodies_unchanged_except_selected_name",
        "bound_variable_indices_unchanged",
        "universes_unchanged",
        "metadata_unchanged",
        "other_binders_unchanged",
        "binder_info_unchanged",
    ],
)
def test_p01_name_only_delta_axes_fail_after_unchecked_copy(field: str) -> None:
    chain = _valid_chain()
    delta = chain.edges[0].certificate.p01_name_only_delta
    assert delta is not None
    bad_delta = delta.model_copy(update={field: False})
    certificate = chain.edges[0].certificate.model_copy(update={"p01_name_only_delta": bad_delta})
    edge = chain.edges[0].model_copy(
        update={
            "certificate": certificate,
            "certificate_payload_hash": _h(certificate.model_dump(mode="json")),
        }
    )
    with pytest.raises(Wave1RuntimeError, match="invalid_runtime_chain_receipt"):
        validate_runtime_chain(chain.model_copy(update={"edges": (edge,)}))


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("ordinal_path", "ordinal and selected-site path disagree"),
        ("old_name", "binder-aware endpoint fingerprint mismatch"),
        ("new_name", "binder-aware endpoint fingerprint mismatch"),
        ("lineage", "selected-site lineage mismatch"),
        ("source_fingerprint", "binder-aware endpoint fingerprint mismatch"),
        ("candidate_fingerprint", "binder-aware endpoint fingerprint mismatch"),
    ],
)
def test_p01_identity_fields_are_cross_bound(mutation: str, reason: str) -> None:
    chain = _valid_chain()
    certificate = chain.edges[0].certificate
    delta = certificate.p01_name_only_delta
    assert delta is not None
    if mutation == "ordinal_path":
        certificate = certificate.model_copy(
            update={
                "selected_site_path": "/1",
                "selected_site_path_fingerprint": _h("/1"),
            }
        )
    elif mutation in {"old_name", "new_name"}:
        delta = delta.model_copy(
            update={mutation: "forged_old" if mutation == "old_name" else "forged_new"}
        )
        certificate = certificate.model_copy(update={"p01_name_only_delta": delta})
    elif mutation == "lineage":
        certificate = certificate.model_copy(update={"selected_site_lineage_hash": _h("forged")})
    else:
        certificate = certificate.model_copy(update={f"binder_aware_{mutation}": _h("forged")})
    edge = chain.edges[0].model_copy(
        update={
            "certificate": certificate,
            "certificate_payload_hash": _h(certificate.model_dump(mode="json")),
        }
    )
    with pytest.raises(Wave1RuntimeError, match=reason):
        validate_runtime_chain(chain.model_copy(update={"edges": (edge,)}))


def test_endpoint_sidecar_and_same_request_bindings_are_mandatory() -> None:
    chain = _valid_chain()
    candidate = chain.endpoints[1].model_copy(update={"render_request_hash": _h("other")})
    with pytest.raises(Wave1RuntimeError, match="same_request_and_scope"):
        validate_runtime_chain(
            chain.model_copy(update={"endpoints": (chain.endpoints[0], candidate)})
        )

    certificate = chain.edges[0].certificate.model_copy(
        update={"replay_request_hash": _h("other-request")}
    )
    edge = chain.edges[0].model_copy(
        update={
            "certificate": certificate,
            "certificate_payload_hash": _h(certificate.model_dump(mode="json")),
        }
    )
    with pytest.raises(Wave1RuntimeError, match="share one Meta request"):
        validate_runtime_chain(chain.model_copy(update={"edges": (edge,)}))

    candidate = chain.endpoints[1].model_copy(update={"complete_sidecar_sha256": _h("other")})
    with pytest.raises(Wave1RuntimeError, match="certificate_endpoint_or_same_request"):
        validate_runtime_chain(
            chain.model_copy(update={"endpoints": (chain.endpoints[0], candidate)})
        )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("equal_render", "render_hash_cycle"),
        ("equal_text", "model_facing_text_cycle"),
        ("wrong_edge", "closed_expr_hash_cycle_without_p01"),
        ("nonadjacent", "nonadjacent_or_wrong_operation_edge_repeat"),
        ("third", "third_closed_expr_hash_occurrence"),
        ("multiple_p01", "multiple_p01_hops"),
    ],
)
def test_named_runtime_rejections(mutation: str, reason: str) -> None:
    chain = _valid_chain()
    source, renamed = chain.endpoints
    if mutation == "equal_render":
        renamed = renamed.model_copy(update={"render_hash": source.render_hash})
        chain = chain.model_copy(update={"endpoints": (source, renamed)})
    elif mutation == "equal_text":
        renamed = renamed.model_copy(update={"core_text_sha256": source.core_text_sha256})
        chain = chain.model_copy(update={"endpoints": (source, renamed)})
    elif mutation == "wrong_edge":
        chain = _chain(
            (source, renamed),
            (_edge("P15_SWAP_IFF_SIDES_V1", source, renamed, path="/0"),),
        )
    elif mutation in {"nonadjacent", "third"}:
        middle = _endpoint(2)
        final = _endpoint(3, expr="alpha-canonical")
        if mutation == "third":
            middle = middle.model_copy(update={"closed_expr_hash": source.closed_expr_hash})
        edges = (
            _edge("P01_ALPHA_RENAME_SINGLE_V1", source, middle, path="/"),
            _edge("P15_SWAP_IFF_SIDES_V1", middle, final, path="/1"),
        )
        chain = _chain((source, middle, final), edges, root="root")
    else:
        final = _endpoint(3)
        edges = (
            chain.edges[0],
            _edge("P01_ALPHA_RENAME_SINGLE_V1", renamed, final, path="/"),
        )
        chain = _chain((source, renamed, final), edges)
    with pytest.raises(Wave1RuntimeError, match=reason):
        validate_runtime_chain(chain)


def test_selected_sites_must_be_pairwise_disjoint_and_lineage_unique() -> None:
    first = _endpoint(0)
    second = _endpoint(1)
    third = _endpoint(2)
    first_edge = _edge("P15_SWAP_IFF_SIDES_V1", first, second, path="/0")
    overlap = _edge("P21_BETA_REDUCE_V1", second, third, path="/0/1")
    chain = _chain((first, second, third), (first_edge, overlap), root="root")
    with pytest.raises(Wave1RuntimeError, match="selected_site_path_overlap"):
        validate_runtime_chain(chain)

    same_lineage = _edge(
        "P21_BETA_REDUCE_V1",
        second,
        third,
        path="/1",
        lineage=first_edge.certificate.selected_site_lineage_hash,
    )
    chain = _chain((first, second, third), (first_edge, same_lineage), root="root")
    with pytest.raises(Wave1RuntimeError, match="selected_site_lineage_cycle"):
        validate_runtime_chain(chain)


def test_p01_binder_name_slot_rejects_whole_expr_site_at_same_node() -> None:
    source = _endpoint(0, expr="alpha-canonical")
    renamed = _endpoint(1, expr="alpha-canonical")
    rewritten = _endpoint(2, expr="rewritten")
    p01 = _edge("P01_ALPHA_RENAME_SINGLE_V1", source, renamed, path="/")
    p15 = _edge("P15_SWAP_IFF_SIDES_V1", renamed, rewritten, path="/")
    with pytest.raises(Wave1RuntimeError, match="selected_site_path_overlap"):
        validate_runtime_chain(_chain((source, renamed, rewritten), (p01, p15)))


def test_p01_binder_name_slot_composes_with_strict_descendant_expr_site() -> None:
    source = _endpoint(0, expr="alpha-canonical")
    renamed = _endpoint(1, expr="alpha-canonical")
    rewritten = _endpoint(2, expr="rewritten")
    p01 = _edge("P01_ALPHA_RENAME_SINGLE_V1", source, renamed, path="/")
    p15 = _edge("P15_SWAP_IFF_SIDES_V1", renamed, rewritten, path="/1")
    result = validate_runtime_chain(_chain((source, renamed, rewritten), (p01, p15)))
    assert result.p01_present is True
    assert result.exception_used is True


def test_caps_cover_both_polarities_and_all_compositions_at_procedure_limit() -> None:
    validate_p01_caps(
        P01CapObservation(
            retained_semantic_pair_count=2000,
            p01_pair_count=5,
            p01_procedure_pair_count=5,
            p01_pairs_by_root={f"root-{index}": 1 for index in range(5)},
            positive_p01_pair_count=3,
            negative_p01_pair_count=2,
            direct_p01_pair_count=2,
            composed_p01_pair_count=3,
        )
    )


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"p01_pairs_by_root": {"root": 2}}, "p01_per_root_cap_exceeded"),
        ({"p01_procedure_pair_count": 4}, "p01_procedure_accounting_mismatch"),
        ({"positive_p01_pair_count": 5}, "p01_polarity_accounting_mismatch"),
        ({"direct_p01_pair_count": 5}, "p01_composition_accounting_mismatch"),
        ({"p01_pair_count": 6}, "p01_root_accounting_mismatch"),
        ({"retained_semantic_pair_count": 1999}, "p01_procedure_share_cap_exceeded"),
    ],
)
def test_cap_adversaries_fail_closed(update: dict[str, object], reason: str) -> None:
    base = P01CapObservation(
        retained_semantic_pair_count=2000,
        p01_pair_count=5,
        p01_procedure_pair_count=5,
        p01_pairs_by_root={f"root-{index}": 1 for index in range(5)},
        positive_p01_pair_count=3,
        negative_p01_pair_count=2,
        direct_p01_pair_count=2,
        composed_p01_pair_count=3,
    )
    with pytest.raises(Wave1RuntimeError, match=reason):
        validate_p01_caps(base.model_copy(update=update))


def test_negative_per_root_cap_count_is_rejected_by_schema() -> None:
    with pytest.raises(ValueError):
        P01CapObservation(
            retained_semantic_pair_count=400,
            p01_pair_count=0,
            p01_procedure_pair_count=0,
            p01_pairs_by_root={"root": -1},
            positive_p01_pair_count=0,
            negative_p01_pair_count=0,
            direct_p01_pair_count=0,
            composed_p01_pair_count=0,
        )


def test_canonical_unordered_dedup_keeps_minimum_and_rejects_conflicts() -> None:
    a, b, c = _h("a"), _h("b"), _h("c")
    rows = (
        DedupCandidate(
            stable_row_hash=_h("row-2"), reference_render_hash=a, candidate_render_hash=b, label=1
        ),
        DedupCandidate(
            stable_row_hash=_h("row-1"), reference_render_hash=b, candidate_render_hash=a, label=1
        ),
        DedupCandidate(
            stable_row_hash=_h("row-3"), reference_render_hash=a, candidate_render_hash=c, label=1
        ),
        DedupCandidate(
            stable_row_hash=_h("row-4"), reference_render_hash=c, candidate_render_hash=a, label=0
        ),
    )
    result = deduplicate_unordered_pairs(rows)
    assert min(_h("row-1"), _h("row-2")) in result.retained_stable_row_hashes
    assert len(result.rejected_conflict_class_keys) == 1
    assert len(result.suppressed_duplicate_stable_row_hashes) == 3


def test_post_orientation_duplicate_or_conflict_fails_shard() -> None:
    a, b = _h("a"), _h("b")
    first = DedupCandidate(
        stable_row_hash=_h("one"), reference_render_hash=a, candidate_render_hash=b, label=1
    )
    duplicate = first.model_copy(
        update={
            "stable_row_hash": _h("two"),
            "reference_render_hash": b,
            "candidate_render_hash": a,
        }
    )
    with pytest.raises(Wave1RuntimeError, match="post_orientation_duplicate_class"):
        assert_post_orientation_unique((first, duplicate))
    conflict = duplicate.model_copy(update={"label": 0})
    with pytest.raises(Wave1RuntimeError, match="post_orientation_conflicting_label_class"):
        assert_post_orientation_unique((first, conflict))


def test_stable_row_hash_is_recomputed_at_runtime_boundary() -> None:
    chain = _valid_chain().model_copy(update={"stable_row_hash": _h("forged")})
    with pytest.raises(Wave1RuntimeError, match="stable row hash mismatch"):
        validate_runtime_chain(chain)


def test_integrated_retention_derives_dedup_then_p01_caps(tmp_path: Path) -> None:
    source = _endpoint(0, expr="alpha-canonical")
    renamed = _endpoint(1, expr="alpha-canonical")
    rewritten = _endpoint(2, expr="rewritten")
    composed = _chain(
        (source, renamed, rewritten),
        (
            _edge("P01_ALPHA_RENAME_SINGLE_V1", source, renamed, path="/"),
            _edge("P15_SWAP_IFF_SIDES_V1", renamed, rewritten, path="/1"),
        ),
    )
    chains: list[RuntimeChain] = [composed]
    for index in range(1, 400):
        source = _endpoint(index * 2 + 10)
        candidate = _endpoint(index * 2 + 11)
        chains.append(
            _chain(
                (source, candidate),
                (_edge("P15_SWAP_IFF_SIDES_V1", source, candidate, path="/1"),),
                root=f"root-{index}",
                source_identity=f"source-{index}",
            )
        )
    result = validate_retention_batch(_retention_batch(tuple(chains), tmp_path))
    assert len(result.retained_stable_row_hashes) == 400
    assert result.p01_cap_observation.p01_pair_count == 1
    assert result.p01_cap_observation.p01_pairs_by_root == {"root-1": 1}
    assert result.p01_cap_observation.direct_p01_pair_count == 0
    assert result.p01_cap_observation.composed_p01_pair_count == 1


def test_integrated_retention_rejects_conflicts_before_caps(tmp_path: Path) -> None:
    first = _valid_chain()
    source = _endpoint(90, expr="other-source")
    candidate = _endpoint(91, expr="other-candidate")
    source = source.model_copy(update={"render_hash": first.endpoints[-1].render_hash})
    candidate = candidate.model_copy(update={"render_hash": first.endpoints[0].render_hash})
    conflict = _chain(
        (source, candidate),
        (_edge("N31_DROP_REQUIRED_GUARD_RUBRIC_V1", source, candidate, path="/1"),),
        root="negative-root",
        source_identity="negative-source",
        polarity="negative",
        label=0,
    )
    binding = load_and_validate_p01_runtime_binding()
    assert binding.operations[-1].runtime_activated is False
    # The inactive operation is rejected even before a conflicting-label class
    # could be retained.
    with pytest.raises(Wave1RuntimeError, match="operation_not_runtime_admitted"):
        validate_retention_batch(_retention_batch((first, conflict), tmp_path))


def test_retention_scope_rejects_a_tampered_durable_journal(tmp_path: Path) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    journal_path = Path(batch.scope_journal_path)
    journal_path.write_bytes(journal_path.read_bytes() + b"{}\n")
    with pytest.raises(Wave1RuntimeError, match="journal file hash mismatch"):
        validate_retention_batch(batch)


@pytest.mark.parametrize("artifact", ["manifest", "journal"])
def test_retention_scope_rejects_final_artifact_symlinks(tmp_path: Path, artifact: str) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    artifact_path = Path(getattr(batch, f"scope_{artifact}_path"))
    backing_path = artifact_path.with_name(f"{artifact_path.name}.backing")
    artifact_path.rename(backing_path)
    artifact_path.symlink_to(backing_path.name)
    with pytest.raises(Wave1RuntimeError, match=f"{artifact} path contains a symlink"):
        validate_retention_batch(batch)


def test_retention_scope_rejects_an_artifact_parent_symlink(tmp_path: Path) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    artifact_root = Path(batch.scope_manifest_path).parent
    backing_root = artifact_root.with_name("artifact-backing")
    artifact_root.rename(backing_root)
    artifact_root.symlink_to(backing_root.name, target_is_directory=True)
    with pytest.raises(Wave1RuntimeError, match="manifest path contains a symlink"):
        validate_retention_batch(batch)


def test_retention_scope_rejects_swapped_manifest_and_journal(tmp_path: Path) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    swapped = batch.model_copy(
        update={
            "scope_manifest_relative_path": batch.scope_journal_relative_path,
            "scope_manifest_path": batch.scope_journal_path,
            "scope_manifest_file_sha256": batch.scope_journal_file_sha256,
            "scope_journal_relative_path": batch.scope_manifest_relative_path,
            "scope_journal_path": batch.scope_manifest_path,
            "scope_journal_file_sha256": batch.scope_manifest_file_sha256,
        }
    )
    with pytest.raises(Wave1RuntimeError, match="invalid durable retention scope manifest"):
        validate_retention_batch(swapped)


def test_retention_scope_rejects_a_containment_escape(tmp_path: Path) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    escaped_path = tmp_path / "escaped.manifest.json"
    escaped_path.write_bytes(Path(batch.scope_manifest_path).read_bytes())
    escaped = batch.model_copy(
        update={
            "scope_manifest_relative_path": "../escaped.manifest.json",
            "scope_manifest_path": str(escaped_path),
            "scope_manifest_file_sha256": hash_file(escaped_path),
        }
    )
    with pytest.raises(Wave1RuntimeError, match="manifest relative path is unsafe"):
        validate_retention_batch(escaped)


def test_retention_scope_rejects_an_absolute_path_outside_the_root(tmp_path: Path) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    escaped_path = tmp_path / "escaped.manifest.json"
    escaped_path.write_bytes(Path(batch.scope_manifest_path).read_bytes())
    escaped = batch.model_copy(
        update={
            "scope_manifest_relative_path": "escaped.manifest.json",
            "scope_manifest_path": str(escaped_path),
            "scope_manifest_file_sha256": hash_file(escaped_path),
        }
    )
    with pytest.raises(Wave1RuntimeError, match="artifact path/root identity mismatch"):
        validate_retention_batch(escaped)


def test_retention_scope_rejects_equal_artifact_paths(tmp_path: Path) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    same_path = batch.model_copy(
        update={
            "scope_journal_relative_path": batch.scope_manifest_relative_path,
            "scope_journal_path": batch.scope_manifest_path,
            "scope_journal_file_sha256": batch.scope_manifest_file_sha256,
        }
    )
    with pytest.raises(Wave1RuntimeError, match="must be distinct files"):
        validate_retention_batch(same_path)


def test_retention_scope_rejects_a_shared_artifact_inode(tmp_path: Path) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    manifest_path = Path(batch.scope_manifest_path)
    journal_path = Path(batch.scope_journal_path)
    journal_path.unlink()
    journal_path.hardlink_to(manifest_path)
    shared_inode = batch.model_copy(update={"scope_journal_file_sha256": hash_file(journal_path)})
    with pytest.raises(Wave1RuntimeError, match="share a file identity"):
        validate_retention_batch(shared_inode)


def test_retention_scope_rejects_handle_manifest_identity_drift(tmp_path: Path) -> None:
    batch = _retention_batch((_valid_chain(),), tmp_path)
    manifest_path = Path(batch.scope_manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence_root_path"] = str(tmp_path / "different-evidence-root")
    manifest.pop("manifest_hash")
    manifest_hash = hash_canonical(manifest)
    manifest["manifest_hash"] = manifest_hash
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    drifted = batch.model_copy(
        update={
            "scope_manifest_file_sha256": hash_file(manifest_path),
            "scope_manifest_hash": manifest_hash,
        }
    )
    with pytest.raises(Wave1RuntimeError, match="handle/manifest identity mismatch"):
        validate_retention_batch(drifted)
