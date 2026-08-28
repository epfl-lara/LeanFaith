"""Fail-closed tests for immutable deterministic two-hop chain auditing."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.representations import (
    NORMALIZATION_VERSION,
    TheoremForRepresentation,
    alpha_identity_fingerprint,
)
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import (
    CompositionChainError,
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
    _build_chain,
    _load_seed_inventory,
    audit_deterministic_v2_composition_chains,
)
from leanfaith.transforms.composition_seed import (
    CompositionSeedManifest,
    prepare_deterministic_v2_composition_seeds,
)
from leanfaith.transforms.protocol import expected_transformation_audit_id
from leanfaith.transforms.provisional_pair_combine import (
    _iter_jsonl_objects,
    _load_root,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_d0_n18_runtime import build_v2_d0_n18_runtime
from leanfaith.transforms.v2_d0_scale_run import run_v2_d0_scale
from leanfaith.transforms.v2_e2_materializer import (
    V2E2MaterializationResult,
    build_v2_e2_result,
)
from leanfaith.transforms.v2_e2_p18_runtime import build_v2_e2_p18_runtime
from leanfaith.transforms.v2_e2_scale_run import run_v2_e2_scale
from tests.unit.test_deterministic_composition_seed import _make_combination
from tests.unit.test_deterministic_v2_n11_scale import _BatchBackend
from tests.unit.test_deterministic_v2_p18 import _root as p18_root


def _line(record: object) -> bytes:
    if hasattr(record, "model_dump"):
        record = record.model_dump(mode="json")  # type: ignore[union-attr]
    return canonical_json_bytes(record) + b"\n"


def _seed_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    combination, roots = _make_combination(monkeypatch, tmp_path / "first-hop")
    return prepare_deterministic_v2_composition_seeds(
        combination_dir=combination,
        materialization_roots=roots,
        output_dir=tmp_path / "seeds",
    )


def _negative_swapped_root() -> dict[str, object]:
    root = copy.deepcopy(p18_root(swapped=True))

    def replace(value: object) -> None:
        if isinstance(value, dict):
            if value.get("k") == "const" and value.get("n") == "Eq":
                value["n"] = "Ne"
            for child in value.values():
                replace(child)
        elif isinstance(value, list):
            for child in value:
                replace(child)

    replace(root)
    return root


def _run_second_hop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    kind: str,
    theorem_path: Path,
    representation_path: Path,
    status: LeanStatus = LeanStatus.VALID_WITH_SORRY,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_representation = RepresentationRecord.model_validate_json(
        representation_path.read_text(encoding="utf-8").strip()
    )
    candidate_root = p18_root() if kind == "e2" else _negative_swapped_root()

    def fake_build(
        backend: object,
        inputs: list[TheoremForRepresentation],
        **kwargs: object,
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        output: list[RepresentationRecord] = []
        for item in inputs:
            candidate = source_representation.model_copy(
                update={
                    "representation_id": make_id(
                        "repr", {"composition_second_hop": kind, "theorem": item.theorem_id}
                    ),
                    "theorem_id": item.theorem_id,
                    "normalization_version": NORMALIZATION_VERSION,
                    "raw_proof_stripped": item.proof_stripped,
                    "headless": "(x y : Nat) : x = y" if kind == "e2" else "(x y : Nat) : y ≠ x",
                    "signature_pp": "∀ (x y : Nat), x = y"
                    if kind == "e2"
                    else "∀ (x y : Nat), y ≠ x",
                    "signature_explicit": "∀ (x y : Nat), Eq x y"
                    if kind == "e2"
                    else "∀ (x y : Nat), Ne y x",
                    "semantic_atoms": semantic_atoms(candidate_root),
                    "operator_tree": operator_tree(candidate_root),
                    "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
                    "content_hash": "0" * 64,
                }
            )
            output.append(
                candidate.model_copy(
                    update={"content_hash": _representation_payload_hash(candidate)}
                )
            )
        return output

    backend = _BatchBackend((status,), workers=1)
    output = tmp_path / f"second-{kind}-{status.value}"
    if kind == "e2":
        import leanfaith.transforms.v2_e2_scale as scale_module

        monkeypatch.setattr(scale_module, "build_representations", fake_build)
        run_v2_e2_scale(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_e2_p18_runtime(),
            theorem_path=theorem_path,
            representation_path=representation_path,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
            output_dir=output,
            batch_size=1,
            base_seed=118,
            workers=1,
        )
    else:
        import leanfaith.transforms.v2_d0_scale as scale_module

        monkeypatch.setattr(scale_module, "build_representations", fake_build)
        run_v2_d0_scale(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_d0_n18_runtime(),
            theorem_path=theorem_path,
            representation_path=representation_path,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
            output_dir=output,
            batch_size=1,
            base_seed=218,
            workers=1,
        )
    return output


def test_p_to_p_and_p_to_n_are_label_free_and_exactly_replayable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seeds = _seed_set(monkeypatch, tmp_path)
    p_root = _run_second_hop(
        monkeypatch,
        tmp_path,
        kind="e2",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    n_root = _run_second_hop(
        monkeypatch,
        tmp_path,
        kind="d0",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    output = tmp_path / "chains"
    first = audit_deterministic_v2_composition_chains(
        seed_dir=seeds.output_dir,
        second_hop_roots=(n_root, p_root),
        output_dir=output,
    )
    second = audit_deterministic_v2_composition_chains(
        seed_dir=seeds.output_dir,
        second_hop_roots=(p_root, n_root),
        output_dir=output,
    )
    assert first.replayed is False
    assert second.replayed is True
    assert first.chain_set_id == second.chain_set_id
    records = tuple(
        DeterministicCompositionChainRecord.model_validate_json(line)
        for line in first.chains_path.read_text(encoding="utf-8").splitlines()
    )
    assert {item.chain_kind for item in records} == {"P_to_P", "P_to_N"}
    assert all(item.chain_depth == 2 for item in records)
    assert all(item.semantic_label_id is None for item in records)
    assert all(item.training_eligible is False for item in records)
    positive = next(item for item in records if item.chain_kind == "P_to_P")
    negative = next(item for item in records if item.chain_kind == "P_to_N")
    assert positive.second_hop_certificate_kind == "root_equality_symmetry_certificate"
    assert positive.second_hop_certificate_sha256 is not None
    assert negative.second_hop_certificate_kind is None
    assert negative.second_hop_certificate_sha256 is None
    manifest = DeterministicCompositionChainManifest.model_validate_json(
        first.manifest_path.read_text(encoding="utf-8")
    )
    assert manifest.chain_kind_counts == {"P_to_N": 1, "P_to_P": 1}
    assert manifest.negative_source_admitted is False
    assert manifest.semantic_labels_created is False
    assert manifest.training_eligible is False
    assert hash_file(first.chains_path) == manifest.chain_output_sha256


def _replace_e2_audit(
    result: V2E2MaterializationResult,
    **updates: object,
) -> V2E2MaterializationResult:
    assert result.audit is not None
    audit = result.audit.model_copy(update=updates)
    audit = audit.model_copy(update={"audit_id": expected_transformation_audit_id(audit)})
    data = {
        field_name: getattr(result, field_name)
        for field_name in type(result).model_fields
        if field_name != "result_id"
    }
    data["audit"] = audit
    return build_v2_e2_result(**data)


def test_p_to_p_requires_exact_family_certificate_and_true_mechanical_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seeds = _seed_set(monkeypatch, tmp_path)
    root = _run_second_hop(
        monkeypatch,
        tmp_path,
        kind="e2",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    inventory = _load_seed_inventory(seeds.output_dir)
    loaded = _load_root(root)
    line_number, raw, _ = next(_iter_jsonl_objects(root / "results.jsonl"))
    result = V2E2MaterializationResult.model_validate(raw)
    assert result.audit is not None
    seed, theorem, representation = inventory.by_theorem_id[result.attempt.source_theorem_ids[0]]
    observation = loaded.observations[0]

    metadata = dict(result.audit.metadata)
    metadata.pop("root_equality_symmetry_certificate")
    missing = _replace_e2_audit(result, metadata=metadata)
    with pytest.raises(CompositionChainError, match="lacks its exact family certificate"):
        _build_chain(
            seed_set_id=inventory.manifest.seed_set_id,
            seed=seed,
            source_theorem=theorem,
            source_representation=representation,
            root_binding=loaded.binding,
            line_number=line_number,
            observation=observation.model_copy(update={"result_id": missing.result_id}),
            result=missing,
        )

    failed = _replace_e2_audit(result, structural_diff_ok=False)
    with pytest.raises(CompositionChainError, match="structural certificate failed"):
        _build_chain(
            seed_set_id=inventory.manifest.seed_set_id,
            seed=seed,
            source_theorem=theorem,
            source_representation=representation,
            root_binding=loaded.binding,
            line_number=line_number,
            observation=observation.model_copy(update={"result_id": failed.result_id}),
            result=failed,
        )


def test_root_mutation_between_validation_and_reread_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.composition_chain as chain_module

    seeds = _seed_set(monkeypatch, tmp_path)
    root = _run_second_hop(
        monkeypatch,
        tmp_path,
        kind="e2",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    original = chain_module._load_run_models
    call_count = 0

    def mutate_after_initial_validation(path: Path):
        nonlocal call_count
        result = original(path)
        call_count += 1
        # ``_load_root`` has already completed the initial audit before this
        # composition-local reread begins.
        if call_count == 1:
            manifest_path = path / "manifest.json"
            manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(chain_module, "_load_run_models", mutate_after_initial_validation)
    with pytest.raises(CompositionChainError, match="changed after validation"):
        audit_deterministic_v2_composition_chains(
            seed_dir=seeds.output_dir,
            second_hop_roots=(root,),
            output_dir=tmp_path / "mutated-root-chains",
        )


def test_seed_manifest_symlink_and_post_parse_seed_mutation_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.composition_chain as chain_module

    symlinked = _seed_set(monkeypatch, tmp_path / "symlink")
    manifest_path = symlinked.manifest_path
    target = tmp_path / "seed-manifest-target.json"
    target.write_bytes(manifest_path.read_bytes())
    manifest_path.unlink()
    manifest_path.symlink_to(target)
    with pytest.raises(CompositionChainError, match="not a regular file"):
        audit_deterministic_v2_composition_chains(
            seed_dir=symlinked.output_dir,
            second_hop_roots=(),
            output_dir=tmp_path / "symlink-chains",
        )

    seeds = _seed_set(monkeypatch, tmp_path / "mutation")
    root = _run_second_hop(
        monkeypatch,
        tmp_path / "mutation",
        kind="e2",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    original = chain_module._audit_root

    def mutate_seed_after_root_audit(path: Path, inventory):
        result = original(path, inventory)
        seeds.seeds_path.write_bytes(seeds.seeds_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(chain_module, "_audit_root", mutate_seed_after_root_audit)
    with pytest.raises(CompositionChainError, match="seed files changed"):
        audit_deterministic_v2_composition_chains(
            seed_dir=seeds.output_dir,
            second_hop_roots=(root,),
            output_dir=tmp_path / "mutated-seed-chains",
        )


def _rewrite_seed_theorem(
    seed_dir: Path,
    update: dict[str, object],
) -> None:
    theorem_path = seed_dir / "theorems.jsonl"
    theorem = TheoremRecord.model_validate_json(theorem_path.read_text(encoding="utf-8"))
    theorem = theorem.model_copy(update=update)
    theorem_path.write_bytes(_line(theorem))
    manifest_path = seed_dir / "manifest.json"
    manifest = CompositionSeedManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    values = manifest.model_dump(mode="json")
    values["theorem_output_sha256"] = hash_file(theorem_path)
    values["seed_set_id"] = "detcomp_seed_set:" + hash_canonical(
        {key: value for key, value in values.items() if key != "seed_set_id"}
    )
    rewritten = CompositionSeedManifest.model_validate(values)
    manifest_path.write_bytes(_line(rewritten))


def test_n_derived_and_depth_three_seed_sources_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    n_seeds = _seed_set(monkeypatch, tmp_path / "negative")
    theorem = TheoremRecord.model_validate_json(n_seeds.theorem_path.read_text(encoding="utf-8"))
    n_metadata = dict(theorem.metadata)
    n_metadata.update(
        {
            "rule_id": "n18_root_equality_polarity",
            "family_id": "n18_root_equality_polarity",
        }
    )
    _rewrite_seed_theorem(n_seeds.output_dir, {"metadata": n_metadata})
    with pytest.raises(CompositionChainError, match="N-derived"):
        audit_deterministic_v2_composition_chains(
            seed_dir=n_seeds.output_dir,
            second_hop_roots=(),
            output_dir=tmp_path / "negative-chains",
        )

    deep_seeds = _seed_set(monkeypatch, tmp_path / "depth")
    deep_theorem = TheoremRecord.model_validate_json(
        deep_seeds.theorem_path.read_text(encoding="utf-8")
    )
    _rewrite_seed_theorem(
        deep_seeds.output_dir,
        {"parent_theorem_ids": ("thm:" + "f" * 64, deep_theorem.parent_theorem_ids[0])},
    )
    with pytest.raises(CompositionChainError, match="depth one"):
        audit_deterministic_v2_composition_chains(
            seed_dir=deep_seeds.output_dir,
            second_hop_roots=(),
            output_dir=tmp_path / "depth-chains",
        )


def test_foreign_seed_missing_root_and_infrastructure_failure_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seeds = _seed_set(monkeypatch, tmp_path)
    with pytest.raises(CompositionChainError, match="at least one completed"):
        audit_deterministic_v2_composition_chains(
            seed_dir=seeds.output_dir,
            second_hop_roots=(),
            output_dir=tmp_path / "missing",
        )

    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    theorem = TheoremRecord.model_validate_json(seeds.theorem_path.read_text(encoding="utf-8"))
    representation = RepresentationRecord.model_validate_json(
        seeds.representation_path.read_text(encoding="utf-8")
    )
    foreign_theorem_id = make_id("thm", {"foreign_seed": theorem.theorem_id})
    foreign_theorem = theorem.model_copy(update={"theorem_id": foreign_theorem_id})
    foreign_representation = representation.model_copy(
        update={
            "representation_id": make_id("repr", {"foreign_seed": theorem.theorem_id}),
            "theorem_id": foreign_theorem_id,
            "content_hash": "0" * 64,
        }
    )
    foreign_representation = foreign_representation.model_copy(
        update={"content_hash": _representation_payload_hash(foreign_representation)}
    )
    foreign_theorem_path = foreign_dir / "theorems.jsonl"
    foreign_representation_path = foreign_dir / "representations.jsonl"
    foreign_theorem_path.write_bytes(_line(foreign_theorem))
    foreign_representation_path.write_bytes(_line(foreign_representation))
    foreign_root = _run_second_hop(
        monkeypatch,
        foreign_dir,
        kind="e2",
        theorem_path=foreign_theorem_path,
        representation_path=foreign_representation_path,
    )
    with pytest.raises(CompositionChainError, match="source partitions differ"):
        audit_deterministic_v2_composition_chains(
            seed_dir=seeds.output_dir,
            second_hop_roots=(foreign_root,),
            output_dir=tmp_path / "foreign-chains",
        )

    failed_root = _run_second_hop(
        monkeypatch,
        tmp_path / "failure",
        kind="e2",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
        status=LeanStatus.CRASH,
    )
    with pytest.raises(CompositionChainError, match="infrastructure-error"):
        audit_deterministic_v2_composition_chains(
            seed_dir=seeds.output_dir,
            second_hop_roots=(failed_root,),
            output_dir=tmp_path / "failed-chains",
        )


def test_central_cli_is_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seeds = _seed_set(monkeypatch, tmp_path)
    root = _run_second_hop(
        monkeypatch,
        tmp_path,
        kind="e2",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    result = CliRunner().invoke(
        app,
        [
            "audit-deterministic-composition-chains",
            "--seed-dir",
            str(seeds.output_dir),
            "--second-hop-root",
            str(root),
            "--output-dir",
            str(tmp_path / "cli-chains"),
            "--root",
            str(Path.cwd()),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "chains=1" in result.stdout
    assert "semantic_labels_created=0" in result.stdout
    assert "training_eligible=false" in result.stdout
