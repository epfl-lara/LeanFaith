"""Fail-closed tests for immutable deterministic-v2 composition seed preparation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app as central_app
from leanfaith.cli.deterministic_composition_seed import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.representations import (
    NORMALIZATION_VERSION,
    TheoremForRepresentation,
    alpha_identity_fingerprint,
)
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas.theorem import RepresentationRecord
from leanfaith.transforms.composition_seed import (
    CompositionSeedError,
    CompositionSeedManifest,
    CompositionSeedRecord,
    _admit_e2_observation,
    _load_bound_roots,
    _load_combination,
    prepare_deterministic_v2_composition_seeds,
)
from leanfaith.transforms.provisional_pair_combine import combine_provisional_pair_roots
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_e2_materializer import build_v2_e2_result
from leanfaith.transforms.v2_e2_p18_runtime import build_v2_e2_p18_runtime
from leanfaith.transforms.v2_e2_scale_run import run_v2_e2_scale
from tests.unit.test_deterministic_v2_n11_scale import _BatchBackend
from tests.unit.test_deterministic_v2_p18 import _records, _root
from tests.unit.test_provisional_pair_combine import _make_roots

_SOURCE = "theorem p18_seed (x y : Nat) : x = y := by sorry"


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _make_e2_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    root.mkdir(parents=True)
    theorem, representation = _records(_SOURCE, "composition-seed", _root())
    theorem = theorem.model_copy(
        update={
            "declaration_name": "p18_seed",
            "declaration_full_name": "p18_seed",
            "inline_elaboration_source": "import LeanFaithFixtures\n" + _SOURCE,
        }
    )
    theorem_path = root / "theorems.jsonl"
    representation_path = root / "representations.jsonl"
    theorem_path.write_bytes(_canonical_line(theorem.model_dump(mode="json")))
    representation_path.write_bytes(_canonical_line(representation.model_dump(mode="json")))

    import leanfaith.transforms.v2_e2_scale as scale_module

    def fake_build(
        backend: object,
        inputs: list[TheoremForRepresentation],
        **kwargs: object,
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        output: list[RepresentationRecord] = []
        for item in inputs:
            candidate = representation.model_copy(
                update={
                    "representation_id": "repr:" + item.theorem_id.removeprefix("thm:"),
                    "theorem_id": item.theorem_id,
                    "normalization_version": NORMALIZATION_VERSION,
                    "raw_proof_stripped": item.proof_stripped,
                    "headless": "(x y : Nat) : y = x",
                    "signature_pp": "(x y : Nat) : y = x",
                    "signature_explicit": "∀ (x y : Nat), Eq y x",
                    "semantic_atoms": semantic_atoms(_root(swapped=True)),
                    "operator_tree": operator_tree(_root(swapped=True)),
                    "alpha_identity_fingerprint": alpha_identity_fingerprint(_root(swapped=True)),
                    "content_hash": "0" * 64,
                }
            )
            output.append(
                candidate.model_copy(
                    update={"content_hash": _representation_payload_hash(candidate)}
                )
            )
        return output

    monkeypatch.setattr(scale_module, "build_representations", fake_build)
    output = root / "e2-run"
    backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,), workers=1)
    run_v2_e2_scale(
        backend=cast(LeanInteractBackend, backend),
        runtime=build_v2_e2_p18_runtime(),
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=root,
        import_header="import LeanFaithFixtures",
        output_dir=output,
        batch_size=1,
        base_seed=18,
        workers=1,
    )
    return output


def _make_combination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    include_d0: bool = False,
) -> tuple[Path, tuple[Path, ...]]:
    roots: list[Path] = [_make_e2_root(monkeypatch, tmp_path / "e2")]
    if include_d0:
        d0_root = tmp_path / "d0"
        d0_root.mkdir()
        roots.extend(_make_roots(monkeypatch, d0_root)[:1])
    combination = combine_provisional_pair_roots(
        materialization_roots=roots,
        output_dir=tmp_path / "combined",
    )
    return combination.output_dir, tuple(roots)


def test_composition_seed_preparation_is_exact_label_free_and_replayable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    combination, roots = _make_combination(monkeypatch, tmp_path, include_d0=True)
    output = tmp_path / "seeds"

    first = prepare_deterministic_v2_composition_seeds(
        combination_dir=combination,
        materialization_roots=roots,
        output_dir=output,
    )
    second = prepare_deterministic_v2_composition_seeds(
        combination_dir=combination,
        materialization_roots=tuple(reversed(roots)),
        output_dir=output,
    )

    assert first.replayed is False
    assert second.replayed is True
    assert first.seed_set_id == second.seed_set_id
    assert first.seed_count == 1
    seed = CompositionSeedRecord.model_validate_json(first.seeds_path.read_text().strip())
    assert seed.first_hop_rule_id == "p18_root_equality_symmetry"
    assert seed.certificate_kind == "root_equality_symmetry_certificate"
    assert seed.seed_evidence_class == "E2"
    assert seed.chain_depth == 1
    assert seed.semantic_label_id is None
    assert seed.resolved_label_count == 0
    assert seed.promoted_item_count == 0
    assert seed.training_eligible is False
    assert seed.evaluation_eligible is False
    manifest = CompositionSeedManifest.model_validate_json(first.manifest_path.read_text().strip())
    assert manifest.excluded_observation_counts == {"non_e2_e0": 1}
    assert manifest.negative_source_admitted is False
    assert manifest.semantic_labels_created is False
    assert manifest.training_eligible is False
    assert hash_file(first.seeds_path) == manifest.seed_output_sha256
    assert hash_file(first.theorem_path) == manifest.theorem_output_sha256
    assert hash_file(first.representation_path) == manifest.representation_output_sha256


def test_composition_seed_requires_every_exact_bound_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    combination, roots = _make_combination(monkeypatch, tmp_path, include_d0=True)

    with pytest.raises(CompositionSeedError, match="roots are incomplete"):
        prepare_deterministic_v2_composition_seeds(
            combination_dir=combination,
            materialization_roots=roots[:1],
            output_dir=tmp_path / "missing-root",
        )


def test_composition_seed_rejects_existing_output_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    combination, roots = _make_combination(monkeypatch, tmp_path)
    output = tmp_path / "seeds"
    artifacts = prepare_deterministic_v2_composition_seeds(
        combination_dir=combination,
        materialization_roots=roots,
        output_dir=output,
    )
    artifacts.seeds_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(CompositionSeedError, match="output differs"):
        prepare_deterministic_v2_composition_seeds(
            combination_dir=combination,
            materialization_roots=roots,
            output_dir=output,
        )


def test_composition_seed_admission_rejects_missing_certificate_and_derived_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    combination, roots = _make_combination(monkeypatch, tmp_path)
    combination_manifest, (observation,), _ = _load_combination(combination)
    (root,) = _load_bound_roots(
        materialization_roots=roots,
        manifest=combination_manifest,
    ).values()
    result = root.results_by_line[observation.result_line_number]
    assert result.audit is not None
    bad_metadata = dict(result.audit.metadata)
    bad_metadata.pop("root_equality_symmetry_certificate")
    bad_audit = result.audit.model_copy(update={"metadata": bad_metadata})
    result_payload = {
        field_name: getattr(result, field_name)
        for field_name in type(result).model_fields
        if field_name != "result_id"
    }
    result_payload["audit"] = bad_audit
    bad_result = build_v2_e2_result(**result_payload)
    bad_observation = observation.model_copy(update={"result_id": bad_result.result_id})
    bad_root = replace(
        root,
        results_by_line={observation.result_line_number: bad_result},
    )
    with pytest.raises(CompositionSeedError, match="lacks its family certificate"):
        _admit_e2_observation(bad_observation, bad_root)

    source = next(iter(root.source_theorems.values()))
    derived_source = source.model_copy(
        update={
            "source": "deterministic_transform",
            "parent_theorem_ids": ("thm:" + "f" * 64,),
        }
    )
    derived_root = replace(
        root,
        source_theorems={source.theorem_id: derived_source},
    )
    with pytest.raises(CompositionSeedError, match="already derived"):
        _admit_e2_observation(observation, derived_root)


def test_composition_seed_cli_reports_machine_readable_success_and_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    combination, roots = _make_combination(monkeypatch, tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "prepare",
            "--combination-dir",
            str(combination),
            "--materialization-root",
            str(roots[0]),
            "--output-dir",
            str(tmp_path / "cli-seeds"),
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["seed_count"] == 1

    failure = runner.invoke(
        app,
        [
            "prepare",
            "--combination-dir",
            str(combination),
            "--materialization-root",
            str(tmp_path / "missing-root"),
            "--output-dir",
            str(tmp_path / "cli-failure"),
        ],
    )
    assert failure.exit_code == 1
    assert json.loads(failure.stderr)["status"] == "error"


def test_composition_seed_is_available_from_central_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    combination, roots = _make_combination(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        central_app,
        [
            "prepare-deterministic-composition-seeds",
            "--combination-dir",
            str(combination),
            "--materialization-root",
            str(roots[0]),
            "--output-dir",
            str(tmp_path / "central-cli-seeds"),
            "--root",
            str(Path.cwd()),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "seeds=1" in result.stdout
    assert "semantic_labels_created=0" in result.stdout
    assert "training_eligible=false" in result.stdout
