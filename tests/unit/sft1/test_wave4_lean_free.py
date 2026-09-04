from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.loading import ConfigError
from leanfaith.sft1.sprint import square as square_module
from leanfaith.sft1.sprint.orbit import (
    EDGE_ROLES,
    CertifiedNegativeEdge,
    CertifiedPair,
    ClosureGroup,
    OperationSpec,
    OrbitError,
    OrbitPolicy,
    PreservingChain,
    PreservingHop,
    SiteLineage,
    materialize_closure_groups,
    policy_from_config,
    select_closure_groups,
    validate_chain,
    validate_closure_group,
)
from leanfaith.sft1.sprint.square import (
    WAVE4_ROW_KINDS,
    Wave4Runner,
    _wave4_negative_certificate,
    combine_wave4_selected_payload,
    load_wave4_config,
    materialize_wave4_records,
    preselect_wave4_variant_descriptors,
    select_wave4_release_groups,
    validate_wave4_root_payload,
    wave4_process_body,
    wave4_render_body,
)


def _hash(value: object) -> str:
    return hash_canonical(value)


SPECS = (
    OperationSpec("P_A", "PA", "class_a", "inverse_a"),
    OperationSpec("P_B", "PB", "class_b", "inverse_b"),
    OperationSpec("P_C", "PC", "class_c", "inverse_c"),
    OperationSpec("P_B_SAME_CLASS", "PB2", "class_a", "inverse_b2"),
    OperationSpec("P_B_SAME_INVERSE", "PB3", "class_b3", "inverse_a"),
    OperationSpec("P_B_SAME_MECHANISM", "PA", "class_b4", "inverse_b4"),
)


def _policy(*, maximum_depth: int = 3, maximum_variants: int = 5) -> OrbitPolicy:
    return OrbitPolicy(
        policy_id="wave4-test-v1",
        selection_salt="wave4-test-selection-v1",
        operations=SPECS,
        maximum_depth=maximum_depth,
        maximum_variants_per_root=maximum_variants,
    )


def _site(
    tag: str,
    input_expr_hash: str,
    *,
    origin_path: tuple[int, ...],
    transported_from: tuple[str, ...] = (),
) -> SiteLineage:
    return SiteLineage(
        kind="expr",
        path=origin_path,
        origin_path=origin_path,
        occurrence=0,
        input_expr_hash=input_expr_hash,
        focus_expr_hash=_hash([tag, "focus"]),
        footprint_hash=_hash([tag, "footprint"]),
        binder_context_hash=_hash([tag, "binders"]),
        transported_from=transported_from,
        transport_certificate_hash=_hash([tag, "transport"]) if transported_from else None,
        detail=tag,
    )


def _hop(
    spec: OperationSpec,
    start: str,
    end: str,
    *,
    path: tuple[int, ...],
    tag: str,
    transported_from: tuple[str, ...] = (),
) -> PreservingHop:
    return PreservingHop(
        operation_id=spec.operation_id,
        mechanism=spec.mechanism,
        superclass=spec.superclass,
        inverse_token=spec.inverse_token,
        site=_site(tag, start, origin_path=path, transported_from=transported_from),
        input_expr_hash=start,
        output_expr_hash=end,
        input_render_hash=_hash([start, "render"]),
        output_render_hash=_hash([end, "render"]),
        certificate_hash=_hash([tag, "certificate"]),
    )


def _chain(root: str, prefix: str, depth: int, *, variant: int = 0) -> PreservingChain:
    hashes = [_hash([root, prefix, "root"])] + [
        _hash([root, prefix, variant, index]) for index in range(1, depth + 1)
    ]
    hops = tuple(
        _hop(
            SPECS[index],
            hashes[index],
            hashes[index + 1],
            path=(index,),
            tag=f"{prefix}-{variant}-{index}",
        )
        for index in range(depth)
    )
    return PreservingChain(root, hops)


def _pair(
    root: str,
    reference_hash: str,
    candidate_hash: str,
    label: bool,
    *,
    tag: str,
    reference_text: str | None = None,
    candidate_text: str | None = None,
) -> CertifiedPair:
    return CertifiedPair(
        root_id=root,
        reference=reference_text or f"⊢ {reference_hash[:12]}",
        candidate=candidate_text or f"⊢ {candidate_hash[:12]}",
        label=label,
        reference_expr_hash=reference_hash,
        candidate_expr_hash=candidate_hash,
        operation_chain_hash=_hash([tag, "operations"]),
        selected_site_lineage_hash=_hash([tag, "sites"]),
        evidence_hash=_hash([tag, "evidence"]),
    )


def _group(root: str = "anc:test", *, depth: int = 2, variant: int = 0) -> ClosureGroup:
    reference_chain = _chain(root, "p", depth, variant=variant)
    candidate_chain = _chain(root, "c", depth, variant=variant)
    base_pair = _pair(
        root,
        candidate_chain.start_expr_hash,
        reference_chain.start_expr_hash,
        False,
        tag="shared-base",
    )
    base = CertifiedNegativeEdge(
        operation_id="N31_DROP_REQUIRED_GUARD_PROOF_V1",
        mechanism="N31",
        site_lineage_hash=_hash([root, "negative-site"]),
        certificate_hash=_hash([root, "negative-certificate"]),
        proved_expr_hash=reference_chain.start_expr_hash,
        refuted_expr_hash=candidate_chain.start_expr_hash,
        pair=base_pair,
    )
    terminal_pair = _pair(
        root,
        reference_chain.end_expr_hash,
        candidate_chain.end_expr_hash,
        False,
        tag=f"terminal-{variant}",
    )
    terminal = CertifiedNegativeEdge(
        operation_id=base.operation_id,
        mechanism=base.mechanism,
        site_lineage_hash=_hash([root, variant, "terminal-site"]),
        certificate_hash=_hash([root, variant, "terminal-certificate"]),
        proved_expr_hash=reference_chain.end_expr_hash,
        refuted_expr_hash=candidate_chain.end_expr_hash,
        pair=terminal_pair,
    )
    return ClosureGroup(
        root_id=root,
        base_negative=base,
        terminal_negative=terminal,
        reference_chain=reference_chain,
        candidate_chain=candidate_chain,
        preserving_reference=_pair(
            root,
            reference_chain.end_expr_hash,
            reference_chain.start_expr_hash,
            True,
            tag=f"reference-{variant}",
        ),
        preserving_candidate=_pair(
            root,
            candidate_chain.start_expr_hash,
            candidate_chain.end_expr_hash,
            True,
            tag=f"candidate-{variant}",
        ),
        closure_certificate_hash=_hash([root, variant, "closure-certificate"]),
    )


def test_site_lineage_hash_normalizes_transport_order() -> None:
    start = _hash("start")
    first = _hash("first")
    second = _hash("second")
    left = _site("x", start, origin_path=(0,), transported_from=(first, second))
    right = _site("x", start, origin_path=(0,), transported_from=(second, first))
    assert left.lineage_hash == right.lineage_hash
    assert left.transported_from == tuple(sorted((first, second)))


def test_site_rejects_unattached_transport_certificate() -> None:
    with pytest.raises(OrbitError, match="requires at least one predecessor"):
        replace(
            _site("x", _hash("start"), origin_path=(0,)),
            transport_certificate_hash=_hash("orphan-transport"),
        )


@pytest.mark.parametrize("maximum_depth", [0, 4])
def test_policy_rejects_depth_outside_one_to_three(maximum_depth: int) -> None:
    with pytest.raises(OrbitError, match="maximum_depth"):
        _policy(maximum_depth=maximum_depth)


@pytest.mark.parametrize("maximum_variants", [0, 6])
def test_policy_rejects_more_than_five_variants(maximum_variants: int) -> None:
    with pytest.raises(OrbitError, match="maximum_variants_per_root"):
        _policy(maximum_variants=maximum_variants)


def test_valid_three_hop_chain_has_stable_content_identity() -> None:
    chain = _chain("anc:test", "p", 3)
    validate_chain(chain, _policy())
    assert chain.chain_hash == _chain("anc:test", "p", 3).chain_hash
    assert chain.operation_ids == ("P_A", "P_B", "P_C")


def test_chain_enforces_configured_depth_even_below_hard_maximum() -> None:
    with pytest.raises(OrbitError, match="exceeds the configured depth"):
        validate_chain(_chain("anc:test", "p", 3), _policy(maximum_depth=2))


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (SPECS[3], "superclass"),
        (SPECS[4], "inverse token"),
        (SPECS[5], "repeats a mechanism"),
    ],
)
def test_chain_rejects_repeated_mechanism_classes(replacement: OperationSpec, message: str) -> None:
    start = _hash("start")
    middle = _hash("middle")
    end = _hash("end")
    chain = PreservingChain(
        "anc:test",
        (
            _hop(SPECS[0], start, middle, path=(0,), tag="first"),
            _hop(replacement, middle, end, path=(1,), tag="second"),
        ),
    )
    with pytest.raises(OrbitError, match=message):
        validate_chain(chain, _policy())


def test_overlapping_site_requires_checked_transport() -> None:
    start = _hash("start")
    middle = _hash("middle")
    end = _hash("end")
    first = _hop(SPECS[0], start, middle, path=(0,), tag="first")
    missing = _hop(SPECS[1], middle, end, path=(0, 1), tag="second")
    with pytest.raises(OrbitError, match="lack checked transport"):
        validate_chain(PreservingChain("anc:test", (first, missing)), _policy())

    transported = _hop(
        SPECS[1],
        middle,
        end,
        path=(0, 1),
        tag="second",
        transported_from=(first.site.lineage_hash,),
    )
    validate_chain(PreservingChain("anc:test", (first, transported)), _policy())


def test_chain_rejects_broken_expression_link() -> None:
    first = _hop(SPECS[0], _hash("a"), _hash("b"), path=(0,), tag="first")
    second = _hop(SPECS[1], _hash("not-b"), _hash("c"), path=(1,), tag="second")
    with pytest.raises(OrbitError, match="do not link"):
        validate_chain(PreservingChain("anc:test", (first, second)), _policy())


def test_chain_rejects_expression_and_render_cycles() -> None:
    first = _hop(SPECS[0], _hash("a"), _hash("b"), path=(0,), tag="first")
    second = _hop(SPECS[1], _hash("b"), _hash("c"), path=(1,), tag="second")
    expression_cycle = replace(second, output_expr_hash=first.input_expr_hash)
    with pytest.raises(OrbitError, match="repeats a checked expression"):
        validate_chain(PreservingChain("anc:test", (first, expression_cycle)), _policy())
    render_cycle = replace(second, output_render_hash=first.input_render_hash)
    with pytest.raises(OrbitError, match="repeats a goal_v1 render"):
        validate_chain(PreservingChain("anc:test", (first, render_cycle)), _policy())


def test_closure_requires_negative_last_alignment() -> None:
    group = _group()
    validate_closure_group(group, _policy())
    wrong_terminal = replace(
        group.terminal_negative,
        proved_expr_hash=_hash("wrong"),
        pair=_pair(
            group.root_id,
            _hash("wrong"),
            group.candidate_chain.end_expr_hash,
            False,
            tag="wrong-terminal",
        ),
    )
    with pytest.raises(OrbitError, match="negative-last proved endpoint"):
        replace(group, terminal_negative=wrong_terminal)


def test_selection_is_order_invariant_and_bounded_to_five() -> None:
    groups = tuple(_group(variant=index) for index in range(8))
    forward = select_closure_groups(groups, _policy(maximum_variants=5))
    backward = select_closure_groups(tuple(reversed(groups)), _policy(maximum_variants=5))
    assert len(forward) == 5
    assert [group.group_id for group in forward] == [group.group_id for group in backward]
    assert len({group.selection_hash for group in forward}) == 5


def test_selection_collapses_an_exact_duplicate_group() -> None:
    group = _group()
    assert select_closure_groups((group, group), _policy()) == (group,)


def test_selection_fails_on_conflicting_exact_operation_site_result() -> None:
    group = _group()
    conflict = replace(group, closure_certificate_hash=_hash("different-certificate"))
    assert group.selection_hash == conflict.selection_hash
    assert group.group_id != conflict.group_id
    with pytest.raises(OrbitError, match="conflicting groups"):
        select_closure_groups((group, conflict), _policy())


def test_selection_identity_treats_changed_negative_evidence_as_a_conflict() -> None:
    group = _group()
    changed_base = replace(group.base_negative, certificate_hash=_hash("changed-negative-proof"))
    conflict = replace(group, base_negative=changed_base)
    assert group.selection_hash == conflict.selection_hash
    with pytest.raises(OrbitError, match="conflicting groups"):
        select_closure_groups((group, conflict), _policy())


def test_shared_base_edge_materializes_once_without_partial_groups() -> None:
    first = _group(variant=0)
    second = _group(variant=1)
    assert first.base_negative.pair.pair_id == second.base_negative.pair.pair_id
    materialized = materialize_closure_groups((second, first), _policy())

    assert len(materialized.groups) == 2
    assert len(materialized.pairs) == 7  # four logical edges each, one shared physical edge
    pair_ids = [pair.pair_id for pair in materialized.pairs]
    assert len(pair_ids) == len(set(pair_ids))
    assert len({pair.unordered_pair_key for pair in materialized.pairs}) == len(pair_ids)
    physical = set(pair_ids)
    for group in materialized.groups:
        assert tuple(role for role, _ in group.logical_pair_ids) == (
            "preserving_reference",
            "preserving_candidate",
            "negative_base",
            "negative_last",
        )
        assert len(group.logical_pair_ids) == 4
        assert {pair_id for _, pair_id in group.logical_pair_ids}.issubset(physical)
    shared = dict(materialized.pair_group_ids)[first.base_negative.pair.pair_id]
    assert len(shared) == 2


def test_materialized_model_rows_keep_exact_three_field_contract() -> None:
    materialized = materialize_closure_groups((_group(),), _policy())
    assert all(set(row) == {"reference", "candidate", "label"} for row in materialized.model_rows())
    assert all("closure_group_ids" in sidecar for sidecar in materialized.sidecars())
    assert all(
        set(cast(dict[str, str], record["logical_pair_ids"])) == set(EDGE_ROLES)
        for record in materialized.group_records()
    )


def test_materializer_requires_preselected_per_root_bound() -> None:
    groups = tuple(_group(variant=index) for index in range(6))
    with pytest.raises(OrbitError, match="selected-variant bound"):
        materialize_closure_groups(groups, _policy(maximum_variants=5))


def test_materializer_rejects_sharing_a_nonbase_pair() -> None:
    first = _group(variant=0)
    second = _group(variant=1)
    # Make the chain endpoints agree with the shared pair while retaining a distinct site chain.
    second_reference_chain = replace(
        second.reference_chain,
        hops=tuple(
            replace(
                hop,
                input_expr_hash=first.reference_chain.hops[index].input_expr_hash,
                output_expr_hash=first.reference_chain.hops[index].output_expr_hash,
                input_render_hash=first.reference_chain.hops[index].input_render_hash,
                output_render_hash=first.reference_chain.hops[index].output_render_hash,
                site=replace(
                    hop.site,
                    input_expr_hash=first.reference_chain.hops[index].input_expr_hash,
                ),
            )
            for index, hop in enumerate(second.reference_chain.hops)
        ),
    )
    second_terminal_pair = _pair(
        second.root_id,
        second_reference_chain.end_expr_hash,
        second.candidate_chain.end_expr_hash,
        False,
        tag="second-terminal-relinked",
    )
    second = replace(
        second,
        reference_chain=second_reference_chain,
        preserving_reference=first.preserving_reference,
        terminal_negative=replace(
            second.terminal_negative,
            proved_expr_hash=second_reference_chain.end_expr_hash,
            pair=second_terminal_pair,
        ),
    )
    with pytest.raises(OrbitError, match="only one identical base-negative"):
        materialize_closure_groups((first, second), _policy())


def test_pair_identity_binds_chain_site_and_evidence() -> None:
    pair = _group().preserving_reference
    assert replace(pair, operation_chain_hash=_hash("other-chain")).pair_id != pair.pair_id
    assert replace(pair, selected_site_lineage_hash=_hash("other-site")).pair_id != pair.pair_id
    assert replace(pair, evidence_hash=_hash("other-evidence")).pair_id != pair.pair_id
    assert replace(pair, reference=pair.reference + " ").pair_id != pair.pair_id


def test_wave4_config_records_hard_caps_and_immutable_release_prefix() -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert config["release"]["prefix"] == "wave4/composed_core_v1"
    assert config["release"]["private"] is True
    assert config["composition"]["maximum_depth"] == 3
    assert config["selection"]["maximum_preserving_variants_per_root"] == 5
    assert config["selection"]["maximum_negative_last_variants_per_root"] == 5
    assert config["selection"]["descriptor_preselection_before_full_validation_and_frozen_render"]
    assert config["composition"]["exact_negative_last_operation_replay_required"]
    assert config["closure_storage"]["negative_last_replay_required"]
    assert config["execution"]["lean_descriptor_then_selected_certificate_api_required"]
    assert config["negative_families"]["forbidden"] == ["N19_WHOLE_CLAIM_NEGATION_V1"]
    assert (
        config["negative_families"]["maximum_released_share"]["N25_TOGGLE_EQ_NE_PROOF_V1"] == 0.25
    )
    policy = policy_from_config(config)
    assert policy.maximum_depth == 3
    assert policy.maximum_variants_per_root == 5
    assert len(policy.operations) == 14
    assert policy.policy_hash == policy_from_config(config).policy_hash


@pytest.mark.parametrize(
    ("filename", "project_id", "project_revision", "staging_suffix"),
    (
        (
            "wave4_physlib_v1.yaml",
            "physlib",
            "f5242c99d796b59a390d26cd7d1a8057e04c46b5",
            "/wave4/physlib",
        ),
        (
            "wave4_cslib_v1.yaml",
            "cslib",
            "2f677bfc8ef76fa7a27feafc597c1e4a7eda3e42",
            "/wave4/cslib",
        ),
    ),
)
def test_wave4_project_overlays_reuse_exact_policy_with_distinct_runtime(
    filename: str, project_id: str, project_revision: str, staging_suffix: str
) -> None:
    root = Path(__file__).resolve().parents[3]
    config_dir = root / "configs/transformations/sft1_value_first_v1"
    base = load_wave4_config(root, config_dir / "wave4_v1.yaml")
    loaded = load_wave4_config(root, config_dir / filename)
    assert loaded.policy.policy_hash == base.policy.policy_hash
    assert loaded.raw["release"] == base.raw["release"]
    assert loaded.runtime.config.project.project_id == project_id
    assert loaded.runtime.config.project.project_revision == project_revision
    assert str(loaded.runtime.config.output.staging_root).endswith(staging_suffix)
    assert loaded.runtime.config_hash != base.runtime.config_hash


def test_wave4_runtime_overlay_rejects_policy_fields(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    source = root / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"
    (tmp_path / "policy.yaml").write_bytes(source.read_bytes())
    overlay = {
        "schema_version": 1,
        "wave_id": "sft1_wave4_orbit_composition_v1",
        "policy_config": "policy.yaml",
        "runtime": yaml.safe_load(source.read_text(encoding="utf-8"))["runtime"],
        "selection": {"maximum_preserving_variants_per_root": 99},
    }
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported fields"):
        load_wave4_config(root, path)


def test_wave4_config_loader_fails_closed_if_negative_is_not_last() -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["composition"]["negative_operation_position"] = "anywhere"
    with pytest.raises(OrbitError, match="negative operation last"):
        policy_from_config(config)


def test_wave4_executable_config_requires_negative_last_replay(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]
    source = root / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"
    config = yaml.safe_load(source.read_text(encoding="utf-8"))
    config["composition"]["exact_negative_last_operation_replay_required"] = False
    path = tmp_path / "wave4-invalid.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ConfigError, match="negative-last operation replay"):
        load_wave4_config(root, path)


def _checked(tag: int) -> dict[str, object]:
    return {
        "meta_checked": True,
        "kernel_checked": True,
        "kernel_level_instantiation": "none",
        "proof_expr_hash_u64": str(tag),
    }


def _lean_site(index: int) -> dict[str, object]:
    return {
        "kind": "binder_pair",
        "index": index,
        "detail": "test",
        "guard_variable_index": 0,
        "bound_variable_index": None,
        "literal": 0,
        "path": [3, index],
    }


def _wave4_payload(count: int = 8) -> dict[str, object]:
    root = "Test.root"
    negative = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    common = {
        "source_proof": {
            "kind": "loaded_environment_constant",
            "constant": root,
            "value_expr_hash_u64": "101",
        },
        "source_proof_check": _checked(102),
        "base_candidate_refutation": {
            "kind": "boundary_counterexample:test",
            "check": _checked(103),
            "grounding": {
                "assignment": [],
                "binder_count": 0,
                "tactic_calls": 0,
                "universe_instantiation": "none",
            },
            "boundary": 0,
            "separator": {"kind": "source_guard_false:test", "check": _checked(104)},
            "witnesses": [],
            "witness_checks": [],
            "enumeration": None,
        },
        "not_iff_c_p": _checked(105),
    }
    variants = []
    for index in range(count):
        p_prime = 1000 + index * 2
        c_prime = p_prime + 1
        variants.append(
            {
                "index": index,
                "depth": 1,
                "p_alpha_hash": "1",
                "c_alpha_hash": "2",
                "p_prime_alpha_hash": str(p_prime),
                "c_prime_alpha_hash": str(c_prime),
                "negative_site": _lean_site(0),
                "goals": {
                    "p": "⊢ True",
                    "c": "⊢ False",
                    "p_prime": f"⊢ {p_prime} = {p_prime}",
                    "c_prime": f"⊢ {c_prime} = {p_prime}",
                },
                "evidence": {
                    "negative_operation": negative,
                    "direction": "guard",
                    "hops": [
                        {
                            "p_operation": "P_A",
                            "c_operation": "P_A",
                            "mechanism": "PA",
                            "superclass": "class_a",
                            "inverse_token": "inverse_a",
                            "p_site": _lean_site(index),
                            "c_site": _lean_site(index),
                            "p_input_alpha_hash": "1",
                            "c_input_alpha_hash": "2",
                            "p_output_alpha_hash": str(p_prime),
                            "c_output_alpha_hash": str(c_prime),
                            "p_direct_iff": _checked(2000 + index),
                            "c_direct_iff": _checked(3000 + index),
                            "site_transport": "disjoint_root_coordinates",
                        }
                    ],
                    **common,
                    "p_composite_iff": _checked(4000 + index),
                    "c_composite_iff": _checked(5000 + index),
                    "p_prime_transported_proof": _checked(6000 + index),
                    "c_prime_refutation": _checked(7000 + index),
                    "not_iff_p_prime_c_prime": _checked(8000 + index),
                    "negative_last_replay": {
                        "operation_id": negative,
                        "reference_alpha_hash": str(p_prime),
                        "candidate_alpha_hash": str(c_prime),
                        "reference_expr_equal": True,
                        "candidate_expr_equal": True,
                        "reference_replay_exact": True,
                        "candidate_replay_exact": True,
                        "site": _lean_site(index),
                        "refutation": _checked(9000 + index),
                        "certificate": common["base_candidate_refutation"],
                    },
                    "closure": {
                        "exact_typed": True,
                        "site_policy": "disjoint_only_no_transport_inference",
                        "depth": 1,
                    },
                },
            }
        )
    return {
        "kind": "wave4_root",
        "status": "retained",
        "operation_id": "ORBIT_WAVE4_N31_V1",
        "negative_operation": negative,
        "root": root,
        "enumerated_variant_count": count,
        "variants": variants,
    }


def test_production_preselection_is_bounded_before_full_certificate_validation() -> None:
    payload = _wave4_payload()
    descriptors = preselect_wave4_variant_descriptors(
        payload,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=_policy(maximum_variants=5),
        maximum_depth=3,
        expected_root="Test.root",
    )
    assert len(descriptors) == 5
    selected = validate_wave4_root_payload(
        payload,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=_policy(maximum_variants=5),
        maximum_depth=3,
        expected_root="Test.root",
        selected_descriptors=descriptors,
    )
    assert len(selected.variants) == 5
    assert {variant.index for variant in selected.variants} == {
        descriptor.index for descriptor in descriptors
    }


def test_production_preselection_identity_binds_project_qualified_root_id() -> None:
    payload = _wave4_payload(1)
    first = preselect_wave4_variant_descriptors(
        payload,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=_policy(),
        maximum_depth=3,
        selection_root_id="root:project-a",
    )
    second = preselect_wave4_variant_descriptors(
        payload,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=_policy(),
        maximum_depth=3,
        selection_root_id="root:project-b",
    )
    assert first[0].selection_hash != second[0].selection_hash


def _split_wave4_payloads(count: int = 3) -> tuple[dict[str, Any], dict[str, Any]]:
    complete = _wave4_payload(count)
    variants = cast(list[dict[str, Any]], complete["variants"])
    descriptors = [
        {
            key: variant[key]
            for key in (
                "index",
                "depth",
                "p_alpha_hash",
                "c_alpha_hash",
                "p_prime_alpha_hash",
                "c_prime_alpha_hash",
                "negative_site",
            )
        }
        | {"hops": json.loads(json.dumps(cast(dict[str, Any], variant["evidence"])["hops"]))}
        for variant in variants
    ]
    common = {
        "schema_version": 1,
        "operation_id": complete["operation_id"],
        "negative_operation": complete["negative_operation"],
        "engine_semantic_version": "test-v1",
        "root": complete["root"],
        "module": "Test",
        "level_params": [],
        "certificate_phase": "selected_only",
    }
    descriptor_payload = {
        **common,
        "kind": "wave4_descriptor_root",
        "status": "described",
        "reason": "",
        "descriptors": descriptors,
        "enumerated_descriptor_count": len(descriptors),
    }
    selected_payload = {
        **common,
        "kind": "wave4_selected_root",
        "status": "retained",
        "reason": "",
        "selected_descriptor_indices": [0],
        "selected_variant_count": 1,
        "variants": [variants[0]],
    }
    return descriptor_payload, selected_payload


def test_split_protocol_selects_before_certification_and_binds_both_phases() -> None:
    descriptor_payload, selected_payload = _split_wave4_payloads()
    descriptors = preselect_wave4_variant_descriptors(
        descriptor_payload,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=_policy(maximum_variants=1),
        maximum_depth=3,
        expected_root="Test.root",
    )
    selected_index = descriptors[0].index
    complete = _wave4_payload(3)
    variants = cast(list[dict[str, Any]], complete["variants"])
    selected_payload["selected_descriptor_indices"] = [selected_index]
    selected_payload["variants"] = [variants[selected_index]]
    combined = combine_wave4_selected_payload(
        descriptor_payload,
        selected_payload,
        expected_indices=[selected_index],
    )
    validated = validate_wave4_root_payload(
        combined,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=_policy(maximum_variants=1),
        maximum_depth=3,
        expected_root="Test.root",
        selected_descriptors=descriptors,
    )
    assert [variant.index for variant in validated.variants] == [selected_index]
    assert len(cast(list[object], combined["descriptors"])) == 3
    assert len(cast(list[object], combined["variants"])) == 1


def test_split_protocol_rejects_selected_structure_drift() -> None:
    descriptor_payload, selected_payload = _split_wave4_payloads(1)
    descriptors = preselect_wave4_variant_descriptors(
        descriptor_payload,
        operation_id="ORBIT_WAVE4_N31_V1",
        policy=_policy(maximum_variants=1),
        maximum_depth=3,
    )
    variant = cast(dict[str, Any], cast(list[object], selected_payload["variants"])[0])
    cast(dict[str, Any], cast(list[object], cast(dict[str, Any], variant["evidence"])["hops"])[0])[
        "p_output_alpha_hash"
    ] = "999"
    variant["p_prime_alpha_hash"] = "999"
    combined = combine_wave4_selected_payload(
        descriptor_payload, selected_payload, expected_indices=[0]
    )
    with pytest.raises(OrbitError, match="descriptor changed"):
        validate_wave4_root_payload(
            combined,
            operation_id="ORBIT_WAVE4_N31_V1",
            policy=_policy(maximum_variants=1),
            maximum_depth=3,
            selected_descriptors=descriptors,
        )


def test_wave4_request_bodies_use_only_the_split_contract() -> None:
    process = wave4_process_body(["Test.root"], "ORBIT_WAVE4_N31_V1", 3)
    render = wave4_render_body("Test.root", [7, 2], "scope", "ORBIT_WAVE4_N31_V1", 3)
    assert "processWave4DescriptorRoots" in process
    assert "processWave4Roots" not in process
    assert "rebuildSelectedWave4Orbits" in render
    assert "emitSelectedWave4Report" in render
    assert "rebuildWave4Orbits" not in render
    assert "orbits[0]?" in render and "orbits[1]?" in render


def test_negative_certificate_rejects_dropped_family_evidence() -> None:
    certificate = dict(
        _wave4_payload(1)["variants"][0]["evidence"]["base_candidate_refutation"]  # type: ignore[index]
    )
    certificate.pop("separator")
    with pytest.raises(OrbitError, match="drops family-specific evidence"):
        _wave4_negative_certificate(
            certificate,
            negative_operation="N31_DROP_REQUIRED_GUARD_PROOF_V1",
            field="negative",
        )


def test_production_row_evidence_keeps_negative_family_and_replay_records() -> None:
    evidence = cast(
        dict[str, Any],
        cast(list[dict[str, Any]], _wave4_payload(1)["variants"])[0]["evidence"],
    )
    base, _base_check = Wave4Runner._row_evidence("negative_base", evidence, "selection")
    terminal, _terminal_check = Wave4Runner._row_evidence("negative_last", evidence, "selection")
    assert base["negative_family_evidence"] == evidence["base_candidate_refutation"]
    assert terminal["negative_family_evidence"] == evidence["base_candidate_refutation"]
    assert terminal["negative_last_replay"] == evidence["negative_last_replay"]


def test_shared_base_render_endpoint_discards_variant_slot() -> None:
    render = {
        "p": {
            "record": {"endpoint_id": "4.p", "goal_v1": "⊢ True"},
            "source_material": {"kind": "raw_statement"},
        }
    }
    base = Wave4Runner._render_endpoint(render, "p", shared_base=True)
    variant = Wave4Runner._render_endpoint(render, "p", shared_base=False)
    assert cast(Mapping[str, Any], base["record"])["endpoint_id"] == "base.p"
    assert cast(Mapping[str, Any], variant["record"])["endpoint_id"] == "4.p"


def test_shared_base_render_endpoint_rebinds_exact_representation_identity() -> None:
    def endpoint(slot: int) -> dict[str, object]:
        record: dict[str, object] = {
            "renderer_version": "goal_v1.0",
            "spec_hash": "a" * 64,
            "goal_v1_source": "closed_prop_expr",
            "goal_v1": "⊢ True",
            "rendered_goal_hash": "b" * 64,
            "endpoint_id": f"{slot}.p",
            "endpoint_role": "reference",
            "source_material_hash": "c" * 64,
            "compile_context_id": "ctx:" + "d" * 64,
            "provenance": {"expr_hash": "e" * 64},
            "implementation_identity": {"renderer_semantic_hash": "f" * 64},
        }
        record["representation_id"] = "repr:" + hash_canonical(record)
        return {"record": record, "source_material": {"kind": "raw_statement"}}

    first = Wave4Runner._render_endpoint({"p": endpoint(0)}, "p", shared_base=True)
    second = Wave4Runner._render_endpoint({"p": endpoint(1)}, "p", shared_base=True)
    first_record = cast(dict[str, Any], first["record"])
    second_record = cast(dict[str, Any], second["record"])
    assert first_record == second_record
    identity_fields = (
        "renderer_version",
        "spec_hash",
        "goal_v1_source",
        "goal_v1",
        "rendered_goal_hash",
        "endpoint_id",
        "endpoint_role",
        "source_material_hash",
        "compile_context_id",
        "provenance",
        "implementation_identity",
    )
    assert first_record["representation_id"] == "repr:" + hash_canonical(
        {field: first_record[field] for field in identity_fields}
    )


def test_shared_base_render_endpoint_fails_closed_on_partial_identity() -> None:
    render = {
        "p": {
            "record": {
                "endpoint_id": "0.p",
                "representation_id": "repr:" + "a" * 64,
                "goal_v1": "⊢ True",
            },
            "source_material": {"kind": "raw_statement"},
        }
    }
    with pytest.raises(OrbitError, match="cannot replay its representation identity"):
        Wave4Runner._render_endpoint(render, "p", shared_base=True)


def test_square_inspection_supports_wave4_shared_base_materialization() -> None:
    negative = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    rows, _groups = _production_materialization((negative, negative))
    for row in rows:
        sidecar = cast(dict[str, Any], row["sidecar"])
        sidecar.update(
            {
                "root_name": "Test.wave4",
                "module": "Test",
                "statement": "theorem Test.wave4 : True := by trivial",
            }
        )
    lines = square_module.square_inspection_lines(rows)
    rendered = "\n".join(lines)
    assert "- rows: 7" in rendered
    assert "- closure groups: 2" in rendered
    assert rendered.count("### preserving_reference (label True)") == 2
    assert rendered.count("### preserving_candidate (label True)") == 2
    assert rendered.count("### negative_base (label False)") == 1
    assert rendered.count("### negative_last (label False)") == 2


def test_square_cli_dispatches_wave4_operation_to_wave4_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    roots = tmp_path / "roots.json"
    roots.write_text(json.dumps({"roots": ["Test.root"]}), encoding="utf-8")
    retained = tmp_path / "retained.jsonl"
    retained.write_text("", encoding="utf-8")
    called: dict[str, object] = {}

    class FakeWave4Runner:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            called.update(kwargs)
            self.paths = SimpleNamespace(retained=retained, run_dir=tmp_path)

        def run(self, *, require_zero_lean: bool = False) -> dict[str, object]:
            called["require_zero_lean"] = require_zero_lean
            return {"lean_requests": 0, "roots_considered": 1}

    monkeypatch.setattr(square_module, "Wave4Runner", FakeWave4Runner)
    result = square_module.main(
        [
            "run",
            "--repo-root",
            str(repo_root),
            "--config",
            str(repo_root / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"),
            "--operation",
            "ORBIT_WAVE4_N31_V1",
            "--roots-file",
            str(roots),
            "--run-id",
            "test-wave4-dispatch",
        ]
    )
    assert result == 0
    assert called["operation_id"] == "ORBIT_WAVE4_N31_V1"
    assert called["require_zero_lean"] is False


def test_square_cli_dispatches_wave4_build_to_closure_compactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    called: dict[str, object] = {}

    def fake_build(*_args: object, **kwargs: object) -> dict[str, object]:
        called.update(kwargs)
        return {"passed": True, "shortcut": {"screens": []}}

    def reject_legacy(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("legacy square compactor was selected")

    monkeypatch.setattr(square_module, "build_wave4_view", fake_build)
    monkeypatch.setattr(square_module, "build_square_view", reject_legacy)
    result = square_module.main(
        [
            "build",
            "--repo-root",
            str(repo_root),
            "--config",
            str(repo_root / "configs/transformations/sft1_value_first_v1/wave4_v1.yaml"),
            "--operation",
            "ORBIT_WAVE4_N31_V1",
            "--run-ids",
            "wave4-n31,wave4-n26",
            "--label",
            "wave4-test",
        ]
    )
    assert result == 0
    assert called["run_ids"] == ["wave4-n31", "wave4-n26"]
    assert called["label"] == "wave4-test"


def _production_materialization(
    operations: tuple[str, ...], *, root_prefix: str = "anc"
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows: dict[str, dict[str, object]] = {}
    groups: list[dict[str, object]] = []
    memberships: dict[str, set[str]] = {}
    for index, negative_operation in enumerate(operations):
        root_id = f"{root_prefix}:{negative_operation}:{index // 5}"
        operation_id = f"ORBIT_WAVE4_{negative_operation.split('_', 1)[0]}_V1"
        group_id = f"{root_prefix}:variant:{index:04d}"
        base_family_evidence = {"root_id": root_id, "negative_operation": negative_operation}
        negative_last_replay = {"group_id": group_id, "exact": True}
        logical: dict[str, str] = {}
        for role, label, _reference_endpoint, _candidate_endpoint, _evidence in WAVE4_ROW_KINDS:
            pair_id = (
                f"pair:{root_id}:base"
                if role == "negative_base"
                else f"pair:{root_id}:{index}:{role}"
            )
            logical[role] = pair_id
            memberships.setdefault(pair_id, set()).add(group_id)
            if pair_id in rows:
                continue
            row = {
                "reference": f"⊢ reference {pair_id}",
                "candidate": f"⊢ candidate {pair_id}",
                "label": label,
            }
            sidecar: dict[str, object] = {
                "pair_id": pair_id,
                "root_id": root_id,
                "operation_id": operation_id,
                "negative_operation": negative_operation,
                "mechanism": negative_operation.split("_", 1)[0],
                "row_kind": role,
                "label": label,
                "evidence_hash": _hash([pair_id, "evidence"]),
                "evidence": (
                    {
                        "negative_family_evidence": base_family_evidence,
                        **(
                            {"negative_last_replay": negative_last_replay}
                            if role == "negative_last"
                            else {}
                        ),
                    }
                    if role in {"negative_base", "negative_last"}
                    else {"equivalence_proof": {"check": _checked(index + 1)}}
                ),
                "closure_group_ids": [],
            }
            rows[pair_id] = {
                "row": row,
                "sidecar": sidecar,
                "unordered_pair_key": _hash([pair_id, "unordered"]),
            }
        groups.append(
            {
                "schema_version": 1,
                "group_id": group_id,
                "root_id": root_id,
                "operation_id": operation_id,
                "negative_operation": negative_operation,
                "negative_mechanism": negative_operation.split("_", 1)[0],
                "selection_hash": _hash([group_id, "selection"]),
                "content_hash": _hash([group_id, "content"]),
                "depth": 1,
                "reference_chain_hash": _hash([group_id, "reference-chain"]),
                "candidate_chain_hash": _hash([group_id, "candidate-chain"]),
                "reference_site_hash": _hash([group_id, "reference-site"]),
                "candidate_site_hash": _hash([group_id, "candidate-site"]),
                "reference_operation_chain": ["P14"],
                "candidate_operation_chain": ["P14"],
                "preserving_mechanism_chain": ["P14"],
                "preserving_superclass_chain": ["binder_permutation"],
                "base_negative_evidence_hash": _hash(base_family_evidence),
                "negative_last_replay_hash": _hash(negative_last_replay),
                "logical_pair_ids": logical,
                "closure_certificate_hash": _hash([group_id, "closure"]),
            }
        )
    for pair_id, record in rows.items():
        sidecar = cast(dict[str, object], record["sidecar"])
        sidecar["closure_group_ids"] = sorted(memberships[pair_id])
        record["row_hash"] = _hash(
            {
                "kind": "sft1_wave4_retained_row_v1",
                "row": record["row"],
                "pair_id": pair_id,
                "evidence_hash": sidecar["evidence_hash"],
                "closure_group_ids": sidecar["closure_group_ids"],
            }
        )
    return list(rows.values()), groups


def test_production_materialization_keeps_complete_groups_with_one_shared_base() -> None:
    negative = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    rows, groups = _production_materialization((negative, negative))
    materialized = materialize_wave4_records(rows, groups)
    assert len(materialized.groups) == 2
    assert materialized.logical_row_count == 8
    assert len(materialized.rows) == 7
    shared = [
        row
        for row in materialized.rows
        if cast(dict[str, Any], row["sidecar"])["row_kind"] == "negative_base"
    ]
    assert len(shared) == 1
    shared_sidecar = cast(dict[str, Any], shared[0]["sidecar"])
    assert len(shared_sidecar["closure_group_ids"]) == 2


def test_production_materialization_rehydrates_canonical_json_role_order() -> None:
    rows, groups = _production_materialization(("N31_DROP_REQUIRED_GUARD_PROOF_V1",))
    # Production artifacts use canonical JSON, which sorts mapping keys.  Reloading
    # one must preserve the fixed semantic edge order without treating key order as
    # evidence or weakening the exact-role-set check.
    reloaded_groups = json.loads(json.dumps(groups, sort_keys=True))
    materialized = materialize_wave4_records(rows, reloaded_groups)
    assert tuple(materialized.groups[0].record["logical_pair_ids"]) == EDGE_ROLES
    assert materialized.groups[0].row_ids == tuple(
        materialized.groups[0].record["logical_pair_ids"][role] for role in EDGE_ROLES
    )


def test_production_materialization_rejects_a_partial_group() -> None:
    rows, groups = _production_materialization(("N31_DROP_REQUIRED_GUARD_PROOF_V1",))
    logical = cast(dict[str, str], groups[0]["logical_pair_ids"])
    del logical["negative_last"]
    with pytest.raises(OrbitError, match="partial or noncanonical"):
        materialize_wave4_records(rows, groups)


def test_production_materialization_rejects_lost_negative_family_evidence() -> None:
    rows, groups = _production_materialization(("N31_DROP_REQUIRED_GUARD_PROOF_V1",))
    negative = next(
        row for row in rows if cast(dict[str, Any], row["sidecar"])["row_kind"] == "negative_base"
    )
    evidence = cast(dict[str, Any], cast(dict[str, Any], negative["sidecar"])["evidence"])
    evidence["negative_family_evidence"] = {"changed": True}
    with pytest.raises(OrbitError, match="drops its negative-family evidence"):
        materialize_wave4_records(rows, groups)


def test_release_selection_caps_physical_rows_without_partial_groups() -> None:
    operations = ("N31_DROP_REQUIRED_GUARD_PROOF_V1",) * 5
    rows, groups = _production_materialization(operations)
    materialized = materialize_wave4_records(rows, groups)
    selected = select_wave4_release_groups(
        materialized,
        maximum_rows=7,
        n25_maximum_share=0.25,
        selection_salt="test",
    ).materialized
    assert len(selected.groups) == 2
    assert len(selected.rows) == 7
    assert selected.logical_row_count == 8


def test_release_selection_enforces_n25_cap_on_unique_physical_rows() -> None:
    n31 = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    n25 = "N25_TOGGLE_EQ_NE_PROOF_V1"
    rows, groups = _production_materialization((n31, n31, n31, n31, n25, n25))
    materialized = materialize_wave4_records(rows, groups)
    selected = select_wave4_release_groups(
        materialized,
        maximum_rows=None,
        n25_maximum_share=0.25,
        selection_salt="test",
    )
    report = selected.negative_share_report
    selected_rows = cast(int, report["operation_selected_row_count"])
    maximum_rows = cast(int, report["maximum_operation_row_count"])
    assert selected_rows <= maximum_rows
    assert all(len(group.row_ids) == 4 for group in selected.materialized.groups)


def _retarget_materialization_cells(
    rows: list[dict[str, object]], *, positive_relation_changed: bool
) -> None:
    for record in rows:
        model_row = cast(dict[str, object], record["row"])
        sidecar = cast(dict[str, object], record["sidecar"])
        relation_changed = (
            positive_relation_changed if bool(model_row["label"]) else not positive_relation_changed
        )
        model_row["reference"] = f"⊢ lhs_{sidecar['pair_id']} = rhs"
        model_row["candidate"] = (
            f"⊢ lhs_{sidecar['pair_id']} < rhs"
            if relation_changed
            else f"⊢ rhs = lhs_{sidecar['pair_id']}"
        )
        record["row_hash"] = _hash(
            {
                "kind": "sft1_wave4_retained_row_v1",
                "row": model_row,
                "pair_id": sidecar["pair_id"],
                "evidence_hash": sidecar["evidence_hash"],
                "closure_group_ids": sidecar["closure_group_ids"],
            }
        )


def test_release_selection_balances_pair_delta_by_complete_ancestry_units() -> None:
    operation = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    left_rows, left_groups = _production_materialization((operation,), root_prefix="left")
    right_rows, right_groups = _production_materialization((operation,), root_prefix="right")
    zero_rows, zero_groups = _production_materialization((operation,), root_prefix="zero")
    _retarget_materialization_cells(left_rows, positive_relation_changed=True)
    _retarget_materialization_cells(right_rows, positive_relation_changed=False)
    for record in zero_rows:
        model_row = cast(dict[str, object], record["row"])
        sidecar = cast(dict[str, object], record["sidecar"])
        model_row["reference"] = f"⊢ lhs_{sidecar['pair_id']} = rhs"
        model_row["candidate"] = f"⊢ rhs = lhs_{sidecar['pair_id']}"
        record["row_hash"] = _hash(
            {
                "kind": "sft1_wave4_retained_row_v1",
                "row": model_row,
                "pair_id": sidecar["pair_id"],
                "evidence_hash": sidecar["evidence_hash"],
                "closure_group_ids": sidecar["closure_group_ids"],
            }
        )
    materialized = materialize_wave4_records(
        [*left_rows, *right_rows, *zero_rows],
        [*left_groups, *right_groups, *zero_groups],
    )
    selected = select_wave4_release_groups(
        materialized,
        maximum_rows=None,
        n25_maximum_share=0.25,
        selection_salt="pair-delta-test",
        enforce_pair_delta_balance=True,
    )
    assert len(selected.materialized.groups) == 3
    report = selected.pair_delta_balance_report
    assert report["passed"] is True
    assert report["quarantined_group_ids"] == []
    assert all(
        counts["positive"] == counts["negative"]
        for counts in cast(dict[str, dict[str, int]], report["cell_counts_after"]).values()
    )


def test_release_selection_quarantines_only_unmatched_pair_delta_units() -> None:
    operation = "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    leaking_rows, leaking_groups = _production_materialization((operation,), root_prefix="leaking")
    balanced_rows, balanced_groups = _production_materialization(
        (operation,), root_prefix="balanced"
    )
    _retarget_materialization_cells(leaking_rows, positive_relation_changed=True)
    materialized = materialize_wave4_records(
        [*leaking_rows, *balanced_rows], [*leaking_groups, *balanced_groups]
    )
    selected = select_wave4_release_groups(
        materialized,
        maximum_rows=None,
        n25_maximum_share=0.25,
        selection_salt="pair-delta-quarantine-test",
        enforce_pair_delta_balance=True,
    )
    assert {group.root_id for group in selected.materialized.groups} == {
        balanced_groups[0]["root_id"]
    }
    report = selected.pair_delta_balance_report
    assert report["passed"] is True
    assert report["quarantined_group_ids"] == [leaking_groups[0]["group_id"]]
    assert report["quarantined_cells"]


def test_release_capacity_selection_interleaves_negative_operation_strata() -> None:
    rows: list[dict[str, object]] = []
    groups: list[dict[str, object]] = []
    for root_prefix, operation in (
        ("n26-a", "N26_INCREMENT_BOUND_PROOF_V1"),
        ("n26-b", "N26_INCREMENT_BOUND_PROOF_V1"),
        ("n31-a", "N31_DROP_REQUIRED_GUARD_PROOF_V1"),
        ("n31-b", "N31_DROP_REQUIRED_GUARD_PROOF_V1"),
    ):
        root_rows, root_groups = _production_materialization((operation,), root_prefix=root_prefix)
        rows.extend(root_rows)
        groups.extend(root_groups)
    selected = select_wave4_release_groups(
        materialize_wave4_records(rows, groups),
        maximum_rows=8,
        n25_maximum_share=0.25,
        selection_salt="operation-interleave-test",
    )
    assert {group.operation_id for group in selected.materialized.groups} == {
        "N26_INCREMENT_BOUND_PROOF_V1",
        "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    }


def test_pair_delta_quarantine_reapplies_n25_cap_to_fixed_point() -> None:
    rows: list[dict[str, object]] = []
    groups: list[dict[str, object]] = []
    for index in range(3):
        root_rows, root_groups = _production_materialization(
            ("N31_DROP_REQUIRED_GUARD_PROOF_V1",), root_prefix=f"leaking-{index}"
        )
        _retarget_materialization_cells(root_rows, positive_relation_changed=True)
        rows.extend(root_rows)
        groups.extend(root_groups)
    n25_rows, n25_groups = _production_materialization(
        ("N25_TOGGLE_EQ_NE_PROOF_V1",), root_prefix="n25-balanced"
    )
    rows.extend(n25_rows)
    groups.extend(n25_groups)
    selected = select_wave4_release_groups(
        materialize_wave4_records(rows, groups),
        maximum_rows=None,
        n25_maximum_share=0.25,
        selection_salt="joint-fixed-point-test",
        enforce_pair_delta_balance=True,
    )
    report = selected.negative_share_report
    assert cast(int, report["operation_selected_row_count"]) <= cast(
        int, report["maximum_operation_row_count"]
    )
    assert report["joint_selection_iterations"] == 3
    assert not selected.materialized.rows


def test_wave4_cli_routes_repeated_explicit_run_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_build(
        repo_root: Path,
        loaded: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured.update({"repo_root": repo_root, "loaded": loaded, **kwargs})
        return {
            "passed": True,
            "shortcut": {"screens": []},
        }

    monkeypatch.setattr(square_module, "build_wave4_release", fake_build)
    run_a = tmp_path / "run-a"
    run_b = tmp_path / "run-b"
    output = tmp_path / "release"
    gate = tmp_path / "gate.json"
    assert (
        square_module.main(
            [
                "build",
                "--repo-root",
                str(Path(__file__).resolve().parents[3]),
                "--run-dir",
                str(run_a),
                "--run-dir",
                str(run_b),
                "--output-dir",
                str(output),
                "--composition-gate-report",
                str(gate),
            ]
        )
        == 0
    )
    assert captured["run_dirs"] == [run_a, run_b]
    assert captured["output_dir"] == output
    assert captured["composition_gate_report"] == gate
    assert captured["label"] == "wave4/composed_core_v1"
