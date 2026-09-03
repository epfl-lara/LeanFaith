"""Lean-free contracts for the additive SFT1 Wave 3 engine and release config.

These tests deliberately inspect semantic registries and small declaration blocks
rather than pinning the complete Lean source text.  Live elaboration and retained
fixtures belong to the bounded Wave 3 gates, not to this unit-test module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
import yaml

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.sprint import engine
from leanfaith.sft1.sprint.engine import (
    ALL_OPERATIONS_MASK,
    NEGATIVE_OPERATIONS,
    OPERATION_BITS,
    OPERATIONS,
    POSITIVE_OPERATIONS,
    operation_mask,
    operations_in_mask,
)
from leanfaith.sft1.sprint.orbit import cap_negative_operation_share
from leanfaith.sft1.sprint.provenance import CACHE_SCHEMA_CURRENT
from leanfaith.sft1.sprint.runner import (
    SprintConfig,
    SprintRunner,
    SprintRunnerError,
    load_sprint_config,
    release_certificate_issues,
    target_family_matches,
    target_family_priority,
    write_certified_targets,
)

ROOT = find_repo_root(Path(__file__))
CONFIG_DIR = ROOT / "configs/transformations/sft1_value_first_v1"
WAVE3_CONFIG = CONFIG_DIR / "wave3_v1.yaml"
LEAN_ENGINE = ROOT / engine.ENGINE_RELATIVE_PATH

HISTORICAL_OPERATIONS = (
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P23_CURRY_PROP_PAIR_V1",
    "N25_TOGGLE_EQ_NE_PROOF_V1",
    "N32_SWAP_ROLE_ORDER_PROOF_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    "P_NE_SYMMETRIZE_V1",
    "P_DROP_REDUNDANT_GUARD_PROOF_V1",
    "P21_BETA_REDUCE_V1",
    "P21_ZETA_REDUCE_V1",
    "P32_ADD_ASSOC_LOCAL_V1",
    "P32_ADD_COMM_LOCAL_V1",
    "P35_SET_INTER_MEMBERSHIP_V1",
    "N26_INCREMENT_BOUND_PROOF_V1",
)
WAVE3_OPERATIONS = (
    "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
    "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
    "P24_SWAP_INDEPENDENT_PROP_BINDERS_V1",
    "P16_REASSOCIATE_AND_V1",
    "P28_IFF_TO_IMPLICATION_PAIR_V1",
    "P_ORDER_COMPLEMENT_V1",
)
WAVE3_NEGATIVES = frozenset(
    {
        "N26_INCREMENT_BOUND_PROOF_V1",
        "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "N32_SWAP_ROLE_ORDER_PROOF_V1",
        "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
        "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
    }
)
WAVE3_POSITIVES = frozenset(WAVE3_OPERATIONS).difference(WAVE3_NEGATIVES)
N25 = "N25_TOGGLE_EQ_NE_PROOF_V1"


def _lean_declaration(source: str, name: str) -> str:
    """Return one top-level Lean declaration without depending on line numbers."""

    declaration = re.compile(
        rf"^(?:(?:private|protected)\s+)?(?:partial\s+)?"
        rf"(?:def|structure|inductive)\s+{re.escape(name)}(?=\s|:)",
        re.MULTILINE,
    )
    match = declaration.search(source)
    if match is None:
        raise AssertionError(f"Lean declaration {name!r} is missing")
    next_declaration = re.compile(
        r"^(?:(?:private|protected)\s+)?(?:partial\s+)?"
        r"(?:def|structure|inductive)\s+[A-Za-z_]",
        re.MULTILINE,
    ).search(source, match.end())
    end = next_declaration.start() if next_declaration is not None else len(source)
    return source[match.start() : end]


def _lean_case_map(block: str, value_pattern: str) -> dict[str, str]:
    return dict(re.findall(rf"\|\s+\.(\w+)\s*=>\s*{value_pattern}", block))


def _mapping(value: object, field: str) -> Mapping[str, object]:
    assert isinstance(value, Mapping), f"{field} must be a mapping"
    assert all(isinstance(key, str) for key in value), f"{field} keys must be strings"
    return cast(Mapping[str, object], value)


def test_wave3_registry_is_append_only_and_lean_python_bits_agree() -> None:
    assert OPERATIONS[: len(HISTORICAL_OPERATIONS)] == HISTORICAL_OPERATIONS
    assert OPERATIONS[len(HISTORICAL_OPERATIONS) :] == WAVE3_OPERATIONS
    assert engine.ENGINE_OPERATION_SET_VERSION == 4
    assert "def engineOperationSetVersion : Nat := 4" in LEAN_ENGINE.read_text(encoding="utf-8")
    assert {operation: index for index, operation in enumerate(OPERATIONS)} == OPERATION_BITS
    assert (1 << len(OPERATIONS)) - 1 == ALL_OPERATIONS_MASK
    assert operations_in_mask(operation_mask(OPERATIONS)) == OPERATIONS

    source = LEAN_ENGINE.read_text(encoding="utf-8")
    constructor_to_id = _lean_case_map(_lean_declaration(source, "Op.id"), r'"([^"]+)"')
    constructor_to_bit = {
        constructor: int(bit)
        for constructor, bit in _lean_case_map(
            _lean_declaration(source, "Op.bit"), r"(\d+)"
        ).items()
    }
    assert len(constructor_to_id) == len(OPERATIONS)
    assert set(constructor_to_id) == set(constructor_to_bit)
    assert (
        tuple(
            constructor_to_id[constructor]
            for constructor, _ in sorted(constructor_to_bit.items(), key=lambda item: item[1])
        )
        == OPERATIONS
    )


def test_wave3_registry_has_exact_polarity_and_no_n19() -> None:
    assert WAVE3_NEGATIVES <= NEGATIVE_OPERATIONS
    assert WAVE3_POSITIVES <= POSITIVE_OPERATIONS
    assert not WAVE3_NEGATIVES.intersection(POSITIVE_OPERATIONS)
    assert not WAVE3_POSITIVES.intersection(NEGATIVE_OPERATIONS)
    assert all("N19" not in operation for operation in OPERATIONS)


def test_wave3_config_is_additive_pinned_and_within_shared_host_budget() -> None:
    loaded = load_sprint_config(ROOT, WAVE3_CONFIG)
    config = loaded.config

    assert config.sprint_id == "sft1_wave3_natural_core_v1"
    configured = tuple(config.engine.operations)
    assert configured == tuple(operation for operation in OPERATIONS if operation in configured)
    assert set(WAVE3_OPERATIONS) <= set(configured)
    assert set(HISTORICAL_OPERATIONS).difference({"P21_BETA_REDUCE_V1"}) <= set(configured)
    assert "P21_BETA_REDUCE_V1" not in configured
    assert config.project.project_revision
    assert config.project.lean_version
    assert config.project.lean_interact_version
    assert config.project.repl_revision
    assert config.project.options.get("Elab.async") is False
    assert config.execution.lean_workers <= 2
    assert config.execution.lean_rss_claim_gib <= 40
    assert config.execution.memory_hard_limit_mb == 24_576
    assert "/wave3/" in config.output.staging_root
    assert not config.output.staging_root.endswith(
        ("wave2/core_v1", "core_v5_combined_square", "aux_n19_square_curriculum")
    )
    assert all("N19" not in operation for operation in config.engine.operations)


def test_wave3_negative_json_requires_source_check_refutation_and_typed_details() -> None:
    source = LEAN_ENGINE.read_text(encoding="utf-8")
    certification = _lean_declaration(source, "certifyNegative")
    fields = set(re.findall(r'\("([a-z_]+)"\s*,', certification))

    assert {
        "label",
        "source_proof",
        "source_proof_check",
        "refutation",
        "goal",
        "kind",
        "check",
        "grounding",
        "boundary",
        "separator",
        "witnesses",
        "witness_checks",
        "enumeration",
        "candidate_truth",
    } <= fields
    assert 'checkedProof "source"' in certification
    for constructor, certifier in {
        "n26": "n26Refute",
        "n31": "n31Refute",
        "n30": "n30Refute",
        "n29": "n29Refute",
    }.items():
        assert re.search(
            rf"\|\s+\.{constructor}\s*=>[\s\S]{{0,160}}?\b{certifier}\b", certification
        )
    assert re.search(r'\("candidate_truth"\s*,\s*Json\.str\s+"refuted"\)', certification)


def test_wave3_boundary_and_finite_certifiers_are_kernel_checked_and_fail_closed() -> None:
    source = LEAN_ENGINE.read_text(encoding="utf-8")
    n26 = _lean_declaration(source, "n26Refute")
    n31 = _lean_declaration(source, "n31Refute")
    n30 = _lean_declaration(source, "n30Refute")
    n29 = _lean_declaration(source, "n29Refute")

    for boundary_certifier in (n26, n31):
        assert "checkedGuardSeparator" in boundary_certifier
        assert 'checkedProof "refutation"' in boundary_certifier
        assert (
            "no_boundary_refutation" in boundary_certifier
            or "exact_boundary_refutation" in boundary_certifier
        )

    assert "finiteEnumerableDomain?" in n30
    assert "domain.values.size < 2" in n30
    assert 'checkedProof "n30_first_witness"' in n30
    assert 'checkedProof "n30_second_witness"' in n30
    assert 'checkedProof "n30_distinct_witnesses"' in n30

    assert n29.count("finiteEnumerableDomain?") >= 2
    assert "complete_finite_matrix" in n29
    assert 'checkedProof "n29_matrix_counterexample"' in n29
    assert 'checkedProof "refutation"' in n29


def test_complete_finite_enumeration_is_separate_from_grounding_samples() -> None:
    source = LEAN_ENGINE.read_text(encoding="utf-8")
    finite = _lean_declaration(source, "finiteDomain?")
    samples = _lean_declaration(source, "dataValues")

    for type_name in ("Bool", "Unit", "Fin", "Option", "Prod"):
        assert f"``{type_name}" in finite
    assert "checkedDataValue?" in finite
    assert "complete" in finite
    assert "if d.isConstOf ``Nat" not in finite
    assert "if d.isConstOf ``Int" not in finite
    assert "if d.isConstOf ``Nat" in samples
    assert "if d.isConstOf ``Int" in samples
    assert "typeClassChoice?" in samples


def test_generic_fintype_enumeration_uses_a_complete_equivalence() -> None:
    source = LEAN_ENGINE.read_text(encoding="utf-8")
    finite = _lean_declaration(source, "finiteEnumerableDomain?")
    assert "synthInstanceSafe?" in finite
    assert "if ← isProp d then return none" in finite
    assert "Fintype.equivFin" in finite
    assert "Equiv.toFun" in finite
    assert "equivFin_complete" in finite
    assert "bound > 32" in finite


def test_grounding_synthesis_failures_are_fail_closed() -> None:
    source = LEAN_ENGINE.read_text(encoding="utf-8")
    safe = _lean_declaration(source, "synthInstanceSafe?")
    choices = _lean_declaration(source, "typeClassChoice?")
    binders = _lean_declaration(source, "binderCandidates")

    assert "tryCatchRuntimeEx" in safe
    assert "if ex.isInterrupt then throw ex" in safe
    assert "return none" in safe
    assert choices.count("synthInstanceSafe?") == 2
    assert "synthInstanceSafe? d" in binders


def test_n32_deterministic_heartbeat_limit_is_a_typed_rejection() -> None:
    source = LEAN_ENGINE.read_text(encoding="utf-8")
    certification = _lean_declaration(source, "certifyNegative")
    assert "isDeterministicHeartbeatTimeout" in certification
    assert 'throwRej "n32_certificate_search_heartbeat_limit"' in certification


def test_finite_negative_search_heartbeat_limits_are_typed_rejections() -> None:
    source = LEAN_ENGINE.read_text(encoding="utf-8")
    certification = _lean_declaration(source, "certifyNegative")
    assert 'failClosedOnHeartbeat "n30_certificate_search_heartbeat_limit"' in certification
    assert 'failClosedOnHeartbeat "n29_certificate_search_heartbeat_limit"' in certification


def test_wave3_new_positives_receive_direct_checked_iff_certificates() -> None:
    source = LEAN_ENGINE.read_text(encoding="utf-8")
    preserving = _lean_declaration(source, "preservingIffProof")
    certification = _lean_declaration(source, "certifyPositive")
    for constructor in ("p24", "p16", "p28", "pOrderComplement"):
        assert re.search(rf"\|\s+\.{constructor}\s*=>", preserving)
    assert "preservingIffProof op applied.site ref cand" in certification
    assert "mkConst ``Iff" in certification
    assert 'checkedProof "equivalence"' in certification
    assert re.search(
        r'\("candidate_truth"\s*,\s*Json\.str\s+"proved_equivalent_to_reference"\)',
        certification,
    )


@dataclass(frozen=True)
class _CapGroup:
    group_id: str
    operation_id: str
    mechanism: str
    row_ids: tuple[str, ...]


def test_n25_release_cap_is_declared_group_preserving_and_deterministic() -> None:
    """The executable cap counts unique physical rows and retains whole groups."""

    wave4 = cast(
        Mapping[str, object],
        yaml.safe_load((CONFIG_DIR / "wave4_v1.yaml").read_text(encoding="utf-8")),
    )
    negative_families = _mapping(wave4.get("negative_families"), "negative_families")
    maximum_shares = _mapping(
        negative_families.get("maximum_released_share"), "maximum_released_share"
    )
    assert maximum_shares.get(N25) == 0.25

    shared_n25_row = "pair:n25-shared-base"
    n25_groups = tuple(
        _CapGroup(
            group_id=f"group:n25-{index}",
            operation_id=N25,
            mechanism="N25",
            row_ids=(
                shared_n25_row,
                *(f"pair:n25-{index}-{row}" for row in range(3)),
            ),
        )
        for index in range(4)
    )
    other_groups = tuple(
        _CapGroup(
            group_id=f"group:other-{index}",
            operation_id="N31_DROP_REQUIRED_GUARD_PROOF_V1",
            mechanism="N31",
            row_ids=tuple(f"pair:other-{index}-{row}" for row in range(6)),
        )
        for index in range(2)
    )
    groups = n25_groups + other_groups
    forward = cap_negative_operation_share(
        groups, N25, 0.25, selection_salt="wave3-n25-cap-test-v1"
    )
    backward = cap_negative_operation_share(
        tuple(reversed(groups)), N25, 0.25, selection_salt="wave3-n25-cap-test-v1"
    )

    selected_ids = tuple(group.group_id for group in forward.selected_groups)
    assert selected_ids == tuple(group.group_id for group in backward.selected_groups)
    assert {group.group_id for group in other_groups} <= set(selected_ids)
    selected_n25 = [group for group in forward.selected_groups if group.operation_id == N25]
    assert len(selected_n25) == 1
    assert selected_n25[0].row_ids in {group.row_ids for group in n25_groups}

    report = forward.report
    assert report.input_group_count == 6
    assert report.selected_group_count == 3
    assert report.dropped_group_count == 3
    assert report.operation_input_group_count == 4
    assert report.operation_selected_group_count == 1
    assert report.operation_dropped_group_count == 3
    assert report.input_row_count == 25
    assert report.selected_row_count == 16
    assert report.dropped_row_count == 9
    assert report.operation_input_row_count == 13
    assert report.operation_selected_row_count == 4
    assert report.operation_dropped_row_count == 9
    assert report.maximum_operation_row_count == 4
    assert report.operation_selected_row_count <= int(
        report.maximum_share * report.selected_row_count
    )
    assert len(report.dropped_group_ids) == 3


def test_wave3_targeting_is_shape_only_and_covers_required_negative_forms() -> None:
    assert target_family_matches("theorem x (n : Nat) : ∀ i, i ∈ Finset.range n → P i", "N26")
    assert target_family_matches("theorem x (n : Nat) : ∀ i < n, P i", "N26")
    assert not target_family_matches("theorem x (h : ∀ i < n, P i) : Q", "N26")
    assert not target_family_matches("theorem x : ∀ n, 0 < n → P n", "N26")
    assert target_family_matches("theorem x : ∃ b : Bool, p b", "N30")
    assert target_family_matches(
        "theorem exists_pair_ne (α : Type*) [Nontrivial α] : ∃ x y : α, x ≠ y",
        "N30",
    )
    assert not target_family_matches("theorem x : P ↔ ∃ b : Bool, p b", "N30")
    assert target_family_matches("theorem x : ∀ b : Bool, ∃ c : Bool, r b c", "N29")
    assert target_family_matches("theorem exists_ne [Nontrivial α] (x : α) : ∃ y, y ≠ x", "N29")
    assert not target_family_matches("theorem x : ∃ y, Q y", "N29")
    assert target_family_matches("theorem x (a b : Int) : a < b", "N32")
    assert target_family_matches("theorem x (n : Nat) : n ≠ 0 → p n", "N31")
    assert not target_family_matches("theorem x : True", "N30")


def test_wave3_targeting_prioritizes_finite_multiwitness_and_dependency_shapes() -> None:
    assert (
        target_family_priority(
            "theorem exists_pair_ne (α : Type*) [Nontrivial α] : ∃ x y : α, x ≠ y",
            "N30",
        )
        == 0
    )
    assert (
        target_family_priority("theorem exists_ne [Nontrivial α] (x : α) : ∃ y, y ≠ x", "N29") == 0
    )


def test_certified_target_extraction_checks_hashes_and_exact_negative_evidence(
    tmp_path: Path,
) -> None:
    compacted = tmp_path / "source"
    shard = compacted / "shard-0001"
    shard.mkdir(parents=True)
    checked = {"meta_checked": True, "kernel_checked": True}
    sidecar = {
        "root_name": "Nat.certified",
        "operation_id": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "label": False,
        "evidence": {
            "source_proof_check": checked,
            "refutation": {"check": checked},
        },
    }
    sidecars_path = shard / "sidecars.jsonl"
    sidecars_path.write_bytes(canonical_json_bytes(sidecar) + b"\n")
    manifest = {
        "run_id": "prior",
        "shards": [
            {
                "shard": 1,
                "complete": True,
                "sidecars_sha256": hash_file(sidecars_path),
            }
        ],
    }
    (compacted / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    out = tmp_path / "targets.json"
    report = write_certified_targets(
        compacted_dir=compacted,
        operation_id="N31_DROP_REQUIRED_GUARD_PROOF_V1",
        out=out,
        selection_salt="test",
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert report["eligible_count"] == 1
    assert payload["roots"] == ["Nat.certified"]
    assert payload["source_manifest_sha256"] == hash_file(compacted / "manifest.json")

    sidecars_path.write_bytes(canonical_json_bytes({**sidecar, "label": True}) + b"\n")
    with pytest.raises(Exception, match="hash mismatch"):
        write_certified_targets(
            compacted_dir=compacted,
            operation_id="N31_DROP_REQUIRED_GUARD_PROOF_V1",
            out=out,
            selection_salt="test",
        )


def _temporary_loaded_config(tmp_path: Path) -> LoadedConfig[SprintConfig]:
    loaded = load_sprint_config(ROOT, WAVE3_CONFIG)
    output = loaded.config.output.model_copy(update={"staging_root": str(tmp_path)})
    config = loaded.config.model_copy(update={"output": output})
    return LoadedConfig(
        config=config,
        path=loaded.path,
        raw=loaded.raw,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )


def test_resume_manifest_rejects_engine_identity_drift(tmp_path: Path) -> None:
    loaded = _temporary_loaded_config(tmp_path)
    runner = SprintRunner(
        ROOT,
        loaded,
        run_id="identity",
        explicit_roots=["Nat.factorial_lt"],
        operations=["N31_DROP_REQUIRED_GUARD_PROOF_V1"],
    )
    runner.write_run_manifest(order_size=1)
    runner.identity = replace(runner.identity, semantic_version="drifted-engine")
    with pytest.raises(SprintRunnerError, match="engine changed"):
        runner.write_run_manifest(order_size=1)


def test_fixture_roots_are_forbidden_outside_fixture_mode(tmp_path: Path) -> None:
    loaded = _temporary_loaded_config(tmp_path)
    root = "LeanFaith.SFT1.Sprint.Fixtures.n31Retained"
    runner = SprintRunner(
        ROOT,
        loaded,
        run_id="release",
        explicit_roots=[root],
    )
    with pytest.raises(SprintRunnerError, match="fixture-only roots"):
        runner.root_order()
    fixture_runner = SprintRunner(
        ROOT,
        loaded,
        run_id="fixture",
        explicit_roots=[root],
        allow_fixture_roots=True,
    )
    assert fixture_runner.root_order() == [(root, "explicit")]


def test_wave3_release_certificate_validator_requires_family_specific_evidence() -> None:
    checked = {"meta_checked": True, "kernel_checked": True}
    base = {
        "operation_id": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "label": False,
        "engine": {"semantic_version": "sft1_wave3_engine_v1"},
        "evidence": {
            "candidate_truth": "refuted",
            "source_proof_check": checked,
            "refutation": {"check": checked, "separator": {"check": checked}},
        },
    }
    assert release_certificate_issues({"sidecar": base, "label": False}) == []
    missing_separator = {
        **base,
        "evidence": {
            **cast(dict[str, object], base["evidence"]),
            "refutation": {"check": checked, "separator": None},
        },
    }
    assert release_certificate_issues({"sidecar": missing_separator, "label": False}) == [
        "boundary_separator_unchecked"
    ]

    n30 = {
        **base,
        "operation_id": "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
        "evidence": {
            **cast(dict[str, object], base["evidence"]),
            "refutation": {
                "check": checked,
                "witnesses": ["false", "true"],
                "witness_checks": [checked, checked, checked],
                "enumeration": "Bool:complete",
            },
        },
    }
    assert release_certificate_issues({"sidecar": n30, "label": False}) == []


def test_wave3_semantic_cache_identity_binds_exact_engine_source() -> None:
    from leanfaith.sft1.sprint.store import SemanticCache

    common = {
        "project_revision": "project",
        "lean_version": "lean",
        "import_options_fingerprint": "imports",
        "engine_semantic_version": "semantic",
        "name": "Nat.root",
    }
    assert CACHE_SCHEMA_CURRENT == 3
    legacy_named = SemanticCache.root_key(**common)
    first = SemanticCache.root_key(**common, engine_source_sha256="a" * 64)
    second = SemanticCache.root_key(**common, engine_source_sha256="b" * 64)
    assert len({legacy_named, first, second}) == 3

    op_common = {
        **common,
        "reference_alpha_hash": "alpha",
        "operation_id": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    }
    assert SemanticCache.op_key(**op_common, engine_source_sha256="a" * 64) != (
        SemanticCache.op_key(**op_common, engine_source_sha256="b" * 64)
    )


def test_fixture_run_identity_binds_runner_source() -> None:
    source = Path(engine.__file__).with_name("runner.py").read_text(encoding="utf-8")
    fixtures = re.search(r"def run_fixtures\(.*?^def _parser\(", source, re.MULTILINE | re.DOTALL)
    assert fixtures is not None
    assert "hash_file(Path(__file__))" in fixtures.group(0)
    assert "owner_session=owner_session" in fixtures.group(0)
    assert "run_fixtures(repo_root, loaded, owner_session=args.owner_session)" in source
    assert 'write_atomic(self.paths.retained, b"")' in source
