"""Audit-only deterministic provisional-pair combination is fail closed."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.deterministic_provisional_combine import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.representations import NORMALIZATION_VERSION, TheoremForRepresentation
from leanfaith.schemas.theorem import RepresentationRecord
from leanfaith.transforms.protocol import build_transformation_attempt
from leanfaith.transforms.provisional_pair_combine import (
    MaterializationRootBinding,
    ProvisionalPairCombinationManifest,
    ProvisionalPairCombineError,
    ProvisionalPairObservation,
    UniqueProvisionalPair,
    _unique_pairs,
    combine_provisional_pair_roots,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_e0_materializer import (
    V2E0MaterializationResult,
    _build_result,
)
from leanfaith.transforms.v2_e0_runtime import build_v2_e0_runtime
from leanfaith.transforms.v2_e0_scale_run import V2E0ScaleRunManifest, run_v2_e0_scale
from tests.unit.test_deterministic_v2_e0_scale import _BatchBackend, _source


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _write_inputs(
    root: Path, *, compact_canonical: bool = True
) -> tuple[Path, Path, RepresentationRecord]:
    source = _source("combineSource")
    theorem_path = root / "theorems.jsonl"
    representation_path = root / "representations.jsonl"
    theorem_payload = source.theorem.model_dump(mode="json")
    representation_payload = source.representation.model_dump(mode="json")
    if compact_canonical:
        theorem_path.write_bytes(_canonical_line(theorem_payload))
        representation_path.write_bytes(_canonical_line(representation_payload))
    else:
        theorem_path.write_text(
            json.dumps(theorem_payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        representation_path.write_text(
            json.dumps(representation_payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return theorem_path, representation_path, source.representation


def _install_candidate_representation(
    monkeypatch: pytest.MonkeyPatch,
    source_representation: RepresentationRecord,
) -> None:
    import leanfaith.transforms.v2_e0_scale as module

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
                    "representation_id": "repr:" + item.theorem_id.removeprefix("thm:"),
                    "theorem_id": item.theorem_id,
                    "normalization_version": NORMALIZATION_VERSION,
                    "raw_proof_stripped": item.proof_stripped,
                    "content_hash": "0" * 64,
                }
            )
            output.append(
                candidate.model_copy(
                    update={"content_hash": _representation_payload_hash(candidate)}
                )
            )
        return output

    monkeypatch.setattr(module, "build_representations", fake_build)


def _make_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    compact_canonical_inputs: bool = True,
) -> tuple[Path, Path]:
    theorem_path, representation_path, source_representation = _write_inputs(
        tmp_path,
        compact_canonical=compact_canonical_inputs,
    )
    _install_candidate_representation(monkeypatch, source_representation)
    roots = (tmp_path / "run-a", tmp_path / "run-b")
    for index, root in enumerate(roots):
        backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,))
        run_v2_e0_scale(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_e0_runtime(),
            theorem_path=theorem_path,
            representation_path=representation_path,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
            output_dir=root,
            batch_size=2,
            base_seed=17 + index,
        )
    return roots


def test_combiner_accepts_exactly_bound_historical_json_formatting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(
        monkeypatch,
        tmp_path,
        compact_canonical_inputs=False,
    )

    artifacts = combine_provisional_pair_roots(
        materialization_roots=(roots[0],),
        output_dir=tmp_path / "combined-historical-format",
    )

    assert artifacts.gross_count == 1
    assert artifacts.unique_count == 1


def _load_manifest(path: Path) -> ProvisionalPairCombinationManifest:
    return ProvisionalPairCombinationManifest.model_validate_json(path.read_text())


def _replace_first_result_with_infrastructure_error(root: Path) -> None:
    results = [
        V2E0MaterializationResult.model_validate_json(line)
        for line in (root / "results.jsonl").read_text().splitlines()
    ]
    original = results[0]
    attempt = build_transformation_attempt(
        family_id=original.attempt.family_id,
        rule_id=original.attempt.rule_id,
        rule_version=original.attempt.rule_version,
        source_theorem_ids=original.attempt.source_theorem_ids,
        source_representation_ids=original.attempt.source_representation_ids,
        context_id=original.attempt.context_id,
        registry_hash=original.attempt.registry_hash,
        generation_config_hash=original.attempt.generation_config_hash,
        seed=original.attempt.seed,
        applicability=original.attempt.applicability,
        terminal_outcome="infrastructure_error",
        failure_codes=("lean_crash",),
        metadata=original.attempt.metadata,
    )
    results[0] = _build_result(
        profile_id=original.profile_id,
        profile_config_hash=original.profile_config_hash,
        rule_id=original.rule_id,
        terminal_status="candidate_invalid",
        attempt=attempt,
        failure_codes=("lean_crash",),
        resolved_label_count=0,
        promoted_item_count=0,
        training_eligible=False,
    )
    payload = b"".join(_canonical_line(item.model_dump(mode="json")) for item in results)
    journal = root / "journal" / "batch_000000.jsonl"
    journal.write_bytes(payload)
    (root / "results.jsonl").write_bytes(payload)
    statuses = Counter(item.terminal_status for item in results)
    family_statuses = Counter(f"{item.rule_id}:{item.terminal_status}" for item in results)
    manifest = V2E0ScaleRunManifest(
        run_spec_sha256=hash_file(root / "run_spec.json"),
        batch_count=1,
        result_count=len(results),
        terminal_status_counts=dict(sorted(statuses.items())),
        family_status_counts=dict(sorted(family_statuses.items())),
        journal_tree_hash=hash_canonical([(journal.name, hash_file(journal))]),
        results_sha256=hash_file(root / "results.jsonl"),
    )
    (root / "manifest.json").write_bytes(_canonical_line(manifest.model_dump(mode="json")))


def test_combiner_deduplicates_exact_pairs_and_replays_immutably(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(monkeypatch, tmp_path)
    output_dir = tmp_path / "combined"

    first = combine_provisional_pair_roots(
        materialization_roots=roots,
        output_dir=output_dir,
    )
    second = combine_provisional_pair_roots(
        materialization_roots=tuple(reversed(roots)),
        output_dir=output_dir,
    )

    assert first.replayed is False
    assert second.replayed is True
    assert first.combination_hash == second.combination_hash
    assert first.gross_count == 2
    assert first.unique_count == 1
    manifest = _load_manifest(first.manifest_path)
    assert manifest.audit_only is True
    assert manifest.provisional_intentions_only is True
    assert manifest.semantic_labels_created is False
    assert manifest.resolved_label_count == 0
    assert manifest.promoted_item_count == 0
    assert manifest.training_eligible is False
    assert manifest.duplicate_group_count == 1
    assert manifest.duplicate_excess_count == 1
    assert manifest.gross_counts_by_family == {"p11_bounded_quantifiers": 2}
    assert manifest.unique_counts_by_family == {"p11_bounded_quantifiers": 1}
    unique = [
        UniqueProvisionalPair.model_validate(json.loads(line))
        for line in first.unique_path.read_text().splitlines()
    ]
    assert len(unique) == 1
    assert unique[0].provenance_count == 2
    assert len(unique[0].observation_ids) == 2
    assert unique[0].semantic_label_id is None


def test_combiner_identity_is_stable_under_root_relocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(monkeypatch, tmp_path)
    first = combine_provisional_pair_roots(
        materialization_roots=(roots[0],),
        output_dir=tmp_path / "combined-original",
    )
    relocated_root = tmp_path / "relocated" / roots[0].name
    shutil.copytree(roots[0], relocated_root)
    second = combine_provisional_pair_roots(
        materialization_roots=(relocated_root,),
        output_dir=tmp_path / "combined-relocated",
    )

    assert first.combination_hash == second.combination_hash
    assert first.gross_path.read_bytes() == second.gross_path.read_bytes()
    assert first.unique_path.read_bytes() == second.unique_path.read_bytes()
    first_manifest = _load_manifest(first.manifest_path)
    second_manifest = _load_manifest(second.manifest_path)
    assert first_manifest.root_bindings[0].root_path != second_manifest.root_bindings[0].root_path
    assert (
        first_manifest.root_bindings[0].root_binding_id
        == second_manifest.root_bindings[0].root_binding_id
    )

    binding_payload = first_manifest.root_bindings[0].model_dump(mode="json")
    binding_payload.update(
        {
            "root_path": "/relocated/root",
            "theorem_partition_path": "/relocated/theorems.jsonl",
            "representation_partition_path": "/relocated/representations.jsonl",
        }
    )
    relocated_binding = MaterializationRootBinding.model_validate(binding_payload)
    manifest_payload = first_manifest.model_dump(mode="json")
    manifest_payload["root_bindings"] = [relocated_binding.model_dump(mode="json")]
    relocated_manifest = ProvisionalPairCombinationManifest.model_validate(manifest_payload)
    assert relocated_manifest.combination_hash == first_manifest.combination_hash


def test_combiner_reports_concurrent_exact_output_as_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(monkeypatch, tmp_path)
    expected = combine_provisional_pair_roots(
        materialization_roots=(roots[0],),
        output_dir=tmp_path / "expected",
    )
    raced_output = tmp_path / "raced-output"

    def lose_exact_race(source: Path, destination: Path) -> None:
        del source
        shutil.copytree(expected.output_dir, destination)
        raise FileExistsError(destination)

    monkeypatch.setattr(
        "leanfaith.transforms.provisional_pair_combine.os.rename",
        lose_exact_race,
    )
    replay = combine_provisional_pair_roots(
        materialization_roots=(roots[0],),
        output_dir=raced_output,
    )

    assert replay.replayed is True
    assert replay.combination_hash == expected.combination_hash


def test_unique_pair_flags_relation_and_polarity_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(monkeypatch, tmp_path)
    artifacts = combine_provisional_pair_roots(
        materialization_roots=(roots[0],),
        output_dir=tmp_path / "combined",
    )
    original = ProvisionalPairObservation.model_validate_json(
        artifacts.gross_path.read_text().strip()
    )

    def changed_observation(**updates: object) -> ProvisionalPairObservation:
        payload = original.model_dump(mode="json")
        payload.update(updates)
        payload.pop("observation_id")
        return ProvisionalPairObservation.model_validate(
            {
                "observation_id": f"detprov_observation:{hash_canonical(payload)}",
                **payload,
            }
        )

    polarity_conflict = changed_observation(
        result_id="result:polarity-conflict",
        polarity_metadata="negative",
    )
    relation_conflict = changed_observation(
        result_id="result:relation-conflict",
        intended_relation="near_miss",
    )

    by_polarity = _unique_pairs((original, polarity_conflict))
    by_relation = _unique_pairs((original, relation_conflict))
    assert by_polarity[0].conflicting_intentions is True
    assert by_polarity[0].polarity_metadata == ("negative", "positive")
    assert by_relation[0].conflicting_intentions is True
    assert by_relation[0].intended_relations == ("equivalent", "near_miss")


def test_combiner_rejects_abort_marker_and_incomplete_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(monkeypatch, tmp_path)
    (roots[0] / "ABORTED.md").write_text("diagnostic-only\n", encoding="utf-8")

    with pytest.raises(ProvisionalPairCombineError, match="abort/incomplete marker"):
        combine_provisional_pair_roots(
            materialization_roots=(roots[0],),
            output_dir=tmp_path / "combined-aborted",
        )

    (roots[1] / "manifest.json").unlink()
    with pytest.raises(ProvisionalPairCombineError, match="incomplete"):
        combine_provisional_pair_roots(
            materialization_roots=(roots[1],),
            output_dir=tmp_path / "combined-incomplete",
        )


def test_combiner_rejects_infrastructure_error_even_in_consistent_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(monkeypatch, tmp_path)
    _replace_first_result_with_infrastructure_error(roots[0])

    with pytest.raises(ProvisionalPairCombineError, match="infrastructure-error result"):
        combine_provisional_pair_roots(
            materialization_roots=(roots[0],),
            output_dir=tmp_path / "combined-infrastructure-error",
        )


def test_combiner_rejects_changed_results_and_output_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(monkeypatch, tmp_path)
    output_dir = tmp_path / "combined"
    artifacts = combine_provisional_pair_roots(
        materialization_roots=(roots[0],),
        output_dir=output_dir,
    )
    artifacts.unique_path.write_bytes(artifacts.unique_path.read_bytes() + b"\n")
    with pytest.raises(ProvisionalPairCombineError, match="existing output differs"):
        combine_provisional_pair_roots(
            materialization_roots=(roots[0],),
            output_dir=output_dir,
        )

    results_path = roots[1] / "results.jsonl"
    results_path.write_bytes(results_path.read_bytes() + b"{}\n")
    with pytest.raises(ProvisionalPairCombineError, match=r"results\.jsonl"):
        combine_provisional_pair_roots(
            materialization_roots=(roots[1],),
            output_dir=tmp_path / "combined-tampered",
        )


def test_standalone_cli_reports_audit_only_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = _make_roots(monkeypatch, tmp_path)
    output_dir = tmp_path / "cli-output"

    result = CliRunner().invoke(
        app,
        [
            "combine",
            "--materialization-root",
            str(roots[0]),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "combined"
    assert payload["gross_observation_count"] == 1
    assert payload["unique_pair_count"] == 1
    assert payload["semantic_labels_created"] is False
    assert payload["training_eligible"] is False
