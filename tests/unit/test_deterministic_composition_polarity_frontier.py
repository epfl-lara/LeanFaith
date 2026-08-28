"""Fail-closed tests for polarity-preserving third-hop frontier preparation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.transforms.composition_chain import audit_deterministic_v2_composition_chains
from leanfaith.transforms.composition_polarity_frontier import (
    CompositionPolarityFrontierError,
    DeterministicCompositionPolarityFrontierManifest,
    DeterministicCompositionPolarityFrontierRecord,
    prepare_deterministic_v2_polarity_frontier,
)
from leanfaith.transforms.composition_unique_pairs import (
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
    postprocess_deterministic_v2_composition_unique_pairs,
)
from tests.unit.test_deterministic_composition_chain import _run_second_hop, _seed_set


def _inputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    seeds = _seed_set(monkeypatch, tmp_path)
    positive = _run_second_hop(
        monkeypatch,
        tmp_path,
        kind="e2",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    negative = _run_second_hop(
        monkeypatch,
        tmp_path,
        kind="d0",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    chains = audit_deterministic_v2_composition_chains(
        seed_dir=seeds.output_dir,
        second_hop_roots=(negative, positive),
        output_dir=tmp_path / "chains",
    )
    unique = postprocess_deterministic_v2_composition_unique_pairs(
        seed_dir=seeds.output_dir,
        chain_dir=chains.output_dir,
        output_dir=tmp_path / "unique",
    )
    return seeds, positive, negative, chains, unique


def _records(path: Path) -> tuple[DeterministicCompositionPolarityFrontierRecord, ...]:
    return tuple(
        DeterministicCompositionPolarityFrontierRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def test_frontier_preserves_polarity_excludes_cycles_and_replays_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, positive, negative, chains, unique = _inputs(monkeypatch, tmp_path)
    output = tmp_path / "frontier"
    first = prepare_deterministic_v2_polarity_frontier(
        chain_dir=chains.output_dir,
        unique_pair_dir=unique.output_dir,
        second_hop_roots=(positive, negative),
        output_dir=output,
    )
    second = prepare_deterministic_v2_polarity_frontier(
        chain_dir=chains.output_dir,
        unique_pair_dir=unique.output_dir,
        second_hop_roots=(negative, positive),
        output_dir=output,
    )
    assert first.replayed is False
    assert second.replayed is True
    assert second.frontier_set_id == first.frontier_set_id

    records = _records(first.frontier_path)
    assert records
    assert all(item.permitted_next_hop == "E2_positive_only" for item in records)
    assert all(item.second_negative_hop_authorized is False for item in records)
    for item in records:
        if item.parent_chain_kind == "P_to_P":
            assert item.preserved_intention == "equivalent_candidate"
            assert item.semantic_negative_hop_count == 0
        else:
            assert item.preserved_intention == "near_miss_candidate"
            assert item.semantic_negative_hop_count == 1

    manifest = DeterministicCompositionPolarityFrontierManifest.model_validate_json(
        first.manifest_path.read_text(encoding="utf-8")
    )
    assert manifest.frontier_count == len(records)
    assert manifest.negative_after_negative_authorized is False
    assert manifest.maximum_semantic_negative_hops == 1
    assert manifest.frontier_output_sha256 == hash_file(first.frontier_path)
    assert manifest.theorem_output_sha256 == hash_file(first.theorem_path)
    assert manifest.representation_output_sha256 == hash_file(first.representation_path)
    assert manifest.training_eligible is False


def test_frontier_rejects_missing_or_duplicate_bound_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, positive, _negative, chains, unique = _inputs(monkeypatch, tmp_path)
    with pytest.raises(CompositionPolarityFrontierError, match="root count"):
        prepare_deterministic_v2_polarity_frontier(
            chain_dir=chains.output_dir,
            unique_pair_dir=unique.output_dir,
            second_hop_roots=(positive,),
            output_dir=tmp_path / "missing",
        )
    with pytest.raises(CompositionPolarityFrontierError, match="root binding"):
        prepare_deterministic_v2_polarity_frontier(
            chain_dir=chains.output_dir,
            unique_pair_dir=unique.output_dir,
            second_hop_roots=(positive, positive),
            output_dir=tmp_path / "duplicate",
        )


def test_frontier_rederives_polarity_from_exact_chains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, positive, negative, chains, unique = _inputs(monkeypatch, tmp_path)
    records = [
        DeterministicCompositionUniquePairRecord.model_validate_json(line)
        for line in unique.unique_pairs_path.read_bytes().splitlines()
    ]
    target = next(item for item in records if item.chain_kinds == ("P_to_N",))
    forged_payload = target.model_dump(mode="json")
    forged_payload["chain_kinds"] = ["P_to_P"]
    forged = DeterministicCompositionUniquePairRecord.model_validate(forged_payload)
    records = [forged if item.unique_pair_id == target.unique_pair_id else item for item in records]

    forged_dir = tmp_path / "forged-unique"
    forged_dir.mkdir()
    unique_payload = b"".join(_canonical_line(item) for item in records)
    (forged_dir / "unique_pairs.jsonl").write_bytes(unique_payload)
    original_manifest = DeterministicCompositionUniquePairManifest.model_validate_json(
        unique.manifest_path.read_bytes()
    )
    manifest_data = original_manifest.model_dump(mode="json")
    manifest_data["unique_output_sha256"] = hashlib.sha256(unique_payload).hexdigest()
    manifest_data["unique_pair_set_id"] = "detcomp_unique_pair_set:" + "0" * 64
    placeholder = DeterministicCompositionUniquePairManifest.model_construct(
        _fields_set=None, **manifest_data
    )
    identity = placeholder.model_dump(mode="json")
    identity.pop("unique_pair_set_id")
    manifest_data["unique_pair_set_id"] = "detcomp_unique_pair_set:" + hash_canonical(identity)
    forged_manifest = DeterministicCompositionUniquePairManifest.model_validate(manifest_data)
    (forged_dir / "manifest.json").write_bytes(_canonical_line(forged_manifest))

    with pytest.raises(CompositionPolarityFrontierError, match="polarity or theorem lineage"):
        prepare_deterministic_v2_polarity_frontier(
            chain_dir=chains.output_dir,
            unique_pair_dir=forged_dir,
            second_hop_roots=(positive, negative),
            output_dir=tmp_path / "forged-output",
        )


def test_frontier_rejects_input_and_output_symlink_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, positive, negative, chains, unique = _inputs(monkeypatch, tmp_path)
    chain_alias = tmp_path / "chain-alias"
    chain_alias.symlink_to(chains.output_dir, target_is_directory=True)
    with pytest.raises(CompositionPolarityFrontierError, match="symlink"):
        prepare_deterministic_v2_polarity_frontier(
            chain_dir=chain_alias,
            unique_pair_dir=unique.output_dir,
            second_hop_roots=(positive, negative),
            output_dir=tmp_path / "input-alias-output",
        )

    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(positive, target_is_directory=True)
    with pytest.raises(CompositionPolarityFrontierError, match="symlink"):
        prepare_deterministic_v2_polarity_frontier(
            chain_dir=chains.output_dir,
            unique_pair_dir=unique.output_dir,
            second_hop_roots=(root_alias, negative),
            output_dir=tmp_path / "root-alias-output",
        )

    prepared = prepare_deterministic_v2_polarity_frontier(
        chain_dir=chains.output_dir,
        unique_pair_dir=unique.output_dir,
        second_hop_roots=(positive, negative),
        output_dir=tmp_path / "real-output",
    )
    output_alias = tmp_path / "output-alias"
    output_alias.symlink_to(prepared.output_dir, target_is_directory=True)
    with pytest.raises(CompositionPolarityFrontierError, match="symlink"):
        prepare_deterministic_v2_polarity_frontier(
            chain_dir=chains.output_dir,
            unique_pair_dir=unique.output_dir,
            second_hop_roots=(positive, negative),
            output_dir=output_alias,
        )


def test_frontier_new_writer_rejects_negative_followup_authorization() -> None:
    with pytest.raises(ValueError):
        DeterministicCompositionPolarityFrontierRecord.model_validate(
            {
                "frontier_id": "detcomp_frontier:" + "0" * 64,
                "input_unique_pair_id": "detcomp_unique_pair:" + "1" * 64,
                "input_chain_set_id": "detcomp_chain_set:" + "2" * 64,
                "context_id": "ctx:" + "3" * 64,
                "root_ancestry_ids": ("ancestry:" + "4" * 64,),
                "original_source_theorem_id": "thm:" + "5" * 64,
                "original_source_representation_id": "repr:" + "6" * 64,
                "depth_two_theorem_ids": ("thm:" + "7" * 64,),
                "depth_two_representation_ids": ("repr:" + "8" * 64,),
                "selected_frontier_theorem_id": "thm:" + "7" * 64,
                "selected_frontier_representation_id": "repr:" + "8" * 64,
                "final_candidate_code_hash": "9" * 64,
                "final_alpha_identity_fingerprint": "a" * 64,
                "parent_chain_ids": ("detcomp_chain:" + "b" * 64,),
                "parent_chain_sequences": ("p18->n18",),
                "parent_chain_kind": "P_to_N",
                "preserved_intention": "near_miss_candidate",
                "semantic_negative_hop_count": 1,
                "second_negative_hop_authorized": True,
            }
        )


def test_frontier_cli_prepares_and_replays(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, positive, negative, chains, unique = _inputs(monkeypatch, tmp_path)
    output = tmp_path / "cli-frontier"
    arguments = [
        "prepare-deterministic-composition-polarity-frontier",
        "--chain-dir",
        str(chains.output_dir),
        "--unique-pair-dir",
        str(unique.output_dir),
        "--second-hop-root",
        str(positive),
        "--second-hop-root",
        str(negative),
        "--output-dir",
        str(output),
    ]
    first = CliRunner().invoke(app, arguments)
    second = CliRunner().invoke(app, arguments)

    assert first.exit_code == 0, first.output
    assert "status=prepared" in first.output
    assert "next_hop=E2_positive_only" in first.output
    assert second.exit_code == 0, second.output
    assert "status=replayed" in second.output
