"""Focused fixture tests for the receipt-bound composition export."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import DeterministicCompositionChainManifest
from leanfaith.transforms.composition_full_launcher import (
    CompositionFullLaunchSpec,
    CompositionFullReceipt,
    FullFamilyPlan,
    FullRootReceipt,
)
from leanfaith.transforms.composition_receipt_export import (
    _EXPECTED_SEQUENCES,
    CompositionReceiptExportArtifacts,
    CompositionReceiptExportError,
    DeterministicCompositionExportRecord,
    DeterministicCompositionReceiptExportManifest,
    _export_record,
    _FullBinding,
    _join_source,
    _load_source_inventory,
    _report,
    _require_disjoint_write_roots,
    _sequence_counts,
    _validate_family_coverage,
    _write_or_replay,
)
from leanfaith.transforms.composition_smoke_launcher import FAMILY_DEFINITIONS
from leanfaith.transforms.composition_unique_pairs import (
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
)
from tests.unit.record_factories import representation_record, theorem_record


def _pair(
    *,
    alpha_novel: bool = True,
    kinds: tuple[str, ...] = ("P_to_P",),
    sequences: tuple[str, ...] = ("p14_independent_binder_permutation->p15_root_iff_reversal",),
) -> DeterministicCompositionUniquePairRecord:
    return DeterministicCompositionUniquePairRecord.model_construct(
        unique_pair_id="detcomp_unique_pair:" + "1" * 64,
        original_source_theorem_id="thm:" + "2" * 64,
        original_source_representation_id="repr:" + "3" * 64,
        source_statement_content_hash="4" * 64,
        source_alpha_identity_fingerprint="5" * 64,
        final_theorem_ids=("thm:" + "6" * 64,),
        final_representation_ids=("repr:" + "7" * 64,),
        final_candidate_code_hash="8" * 64,
        final_alpha_identity_fingerprint="9" * 64 if alpha_novel else "5" * 64,
        chain_ids=tuple(f"detcomp_chain:{index:064x}" for index in range(1, len(kinds) + 1)),
        chain_sequences=sequences,
        chain_kinds=kinds,
        alpha_novel=alpha_novel,
        source_alpha_return=not alpha_novel,
    )


def _record(pair: DeterministicCompositionUniquePairRecord) -> DeterministicCompositionExportRecord:
    return _export_record(
        pair,
        source_lean="theorem source : True",
        source_dataset="mathlib",
        private_source_content=False,
        redistribution_allowed=True,
        external_transmission_allowed=True,
        release_eligible=True,
        final_lean="theorem candidate : True",
    )


def test_alpha_novel_export_cycles_and_mixed_intentions_are_partitioned() -> None:
    novel = _record(_pair())
    cycle = _record(_pair(alpha_novel=False))
    mixed = _record(
        _pair(
            kinds=("P_to_N", "P_to_P"),
            sequences=(
                "p14_independent_binder_permutation->n11_bound_variable_substitution",
                "p14_independent_binder_permutation->p15_root_iff_reversal",
            ),
        )
    )

    assert novel.disposition == "provisional_inventory"
    assert novel.mechanical_intention == "equivalent_candidate"
    assert cycle.disposition == "cycle_audit"
    assert mixed.disposition == "mixed_intention_quarantine"
    assert mixed.mechanical_intention == "conflicting_intentions"
    assert mixed.training_eligible is False
    assert mixed.semantic_label_id is None


def test_private_record_cannot_be_marked_transmittable() -> None:
    with pytest.raises(ValueError, match="source privacy flags"):
        _export_record(
            _pair(),
            source_lean="theorem private_source : True",
            source_dataset="sft_classic",
            private_source_content=True,
            redistribution_allowed=False,
            external_transmission_allowed=True,
            release_eligible=False,
            final_lean="theorem candidate : True",
        )


def _manifest_with_privacy(
    *,
    source_datasets: tuple[str, ...],
    contains_private: bool,
    contains_mixed: bool,
    redistribution: bool,
    external: bool,
    release: bool,
) -> DeterministicCompositionReceiptExportManifest:
    zero_counts = dict.fromkeys(_EXPECTED_SEQUENCES, 0)
    data: dict[str, object] = {
        "export_set_id": "detcomp_export_set:" + "0" * 64,
        "full_receipt_id": "detcomp_full_receipt:" + "1" * 64,
        "full_receipt_sha256": "2" * 64,
        "full_launch_id": "detcomp_full_launch:" + "2" * 64,
        "full_launch_spec_sha256": "3" * 64,
        "input_seed_set_id": "detcomp_seed_set:" + "4" * 64,
        "input_seed_manifest_sha256": "5" * 64,
        "input_chain_set_id": "detcomp_chain_set:" + "6" * 64,
        "input_chain_manifest_sha256": "7" * 64,
        "input_unique_pair_set_id": "detcomp_unique_pair_set:" + "8" * 64,
        "input_unique_pair_manifest_sha256": "9" * 64,
        "source_theorem_partition_sha256s": ("a" * 64,),
        "source_representation_partition_sha256s": ("b" * 64,),
        "source_datasets": source_datasets,
        "contains_private_source_content": contains_private,
        "contains_mixed_source_privacy": contains_mixed,
        "redistribution_allowed": redistribution,
        "external_transmission_allowed": external,
        "release_eligible": release,
        "required_families": tuple(sorted(item.key for item in FAMILY_DEFINITIONS)),
        "gross_chain_count": 0,
        "unique_pair_count": 0,
        "provisional_inventory_count": 0,
        "cycle_audit_count": 0,
        "mixed_intention_quarantine_count": 0,
        "sequence_counts": zero_counts,
        "sequence_inventory_counts": zero_counts,
        "sequence_cycle_counts": zero_counts,
        "sequence_quarantine_counts": zero_counts,
        "inventory_sha256": "c" * 64,
        "cycles_sha256": "d" * 64,
        "quarantine_sha256": "e" * 64,
        "report_sha256": "f" * 64,
    }
    placeholder = DeterministicCompositionReceiptExportManifest.model_construct(
        _fields_set=None, **data
    )
    identity = placeholder.model_dump(mode="json")
    identity.pop("export_set_id")
    data["export_set_id"] = "detcomp_export_set:" + hash_canonical(identity)
    return DeterministicCompositionReceiptExportManifest.model_validate(data)


def test_manifest_privacy_must_match_registered_source_datasets() -> None:
    manifest = _manifest_with_privacy(
        source_datasets=("mathlib", "sft_classic"),
        contains_private=True,
        contains_mixed=True,
        redistribution=False,
        external=False,
        release=False,
    )
    assert manifest.contains_mixed_source_privacy is True

    with pytest.raises(ValueError, match="manifest privacy flags"):
        _manifest_with_privacy(
            source_datasets=("sft_classic",),
            contains_private=False,
            contains_mixed=False,
            redistribution=True,
            external=True,
            release=True,
        )


def test_source_join_binds_text_identity_and_private_policy() -> None:
    pair = _pair()
    theorem = TheoremRecord.model_construct(
        theorem_id=pair.original_source_theorem_id,
        source="sft_classic",
        context_id="ctx:" + "a" * 64,
        root_ancestry_ids=("anc:" + "b" * 64,),
        statement_content_hash=pair.source_statement_content_hash,
        proof_stripped_declaration="theorem private_source : True",
    )
    representation = RepresentationRecord.model_construct(
        representation_id=pair.original_source_representation_id,
        theorem_id=theorem.theorem_id,
        context_id=theorem.context_id,
        raw_proof_stripped=theorem.proof_stripped_declaration,
        alpha_identity_fingerprint=pair.source_alpha_identity_fingerprint,
    )
    pair = pair.model_copy(
        update={"context_id": theorem.context_id, "root_ancestry_ids": theorem.root_ancestry_ids}
    )

    joined = _join_source(
        pair,
        {theorem.theorem_id: theorem},
        {representation.representation_id: representation},
    )
    assert joined == (
        theorem.proof_stripped_declaration,
        "sft_classic",
        True,
        False,
        False,
        False,
    )

    drifted = theorem.model_copy(update={"statement_content_hash": "f" * 64})
    with pytest.raises(CompositionReceiptExportError, match="source Lean join"):
        _join_source(
            pair,
            {drifted.theorem_id: drifted},
            {representation.representation_id: representation},
        )


def test_existing_wrapper_and_direct_source_partitions_load(tmp_path: Path) -> None:
    theorem = theorem_record(source="mathlib")
    representation = representation_record()
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    theorem_path.write_bytes(
        canonical_json_bytes(
            {
                "theorem": theorem.model_dump(mode="json"),
                "representation": representation.model_dump(mode="json"),
            }
        )
        + b"\n"
    )
    representation_path.write_bytes(
        canonical_json_bytes(representation.model_dump(mode="json")) + b"\n"
    )

    theorems, representations = _load_source_inventory((theorem_path,), (representation_path,))
    assert theorems == {theorem.theorem_id: theorem}
    assert representations == {representation.representation_id: representation}

    duplicate_path = tmp_path / "duplicate-theorems.jsonl"
    duplicate_path.write_bytes(canonical_json_bytes(theorem.model_dump(mode="json")) + b"\n")
    with pytest.raises(CompositionReceiptExportError, match="duplicate identities"):
        _load_source_inventory(
            (theorem_path, duplicate_path),
            (representation_path,),
        )


def test_source_partition_symlinks_are_rejected(tmp_path: Path) -> None:
    theorem = theorem_record(source="mathlib")
    representation = representation_record()
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    theorem_path.write_bytes(canonical_json_bytes(theorem.model_dump(mode="json")) + b"\n")
    representation_path.write_bytes(
        canonical_json_bytes(representation.model_dump(mode="json")) + b"\n"
    )
    theorem_alias = tmp_path / "theorem-alias.jsonl"
    theorem_alias.symlink_to(theorem_path)

    with pytest.raises(CompositionReceiptExportError, match="contains a symlink"):
        _load_source_inventory((theorem_alias,), (representation_path,))


def _full_models() -> tuple[CompositionFullLaunchSpec, CompositionFullReceipt]:
    plans = tuple(
        FullFamilyPlan.model_construct(
            family=item.key,
            run_kind=item.run_kind,
            profile_id=item.profile_id,
        )
        for item in FAMILY_DEFINITIONS
    )
    roots = tuple(
        FullRootReceipt.model_construct(
            family=item.key,
            run_kind=item.run_kind,
            profile_id=item.profile_id,
        )
        for item in FAMILY_DEFINITIONS
    )
    return (
        CompositionFullLaunchSpec.model_construct(families=plans),
        CompositionFullReceipt.model_construct(
            receipt_id="detcomp_full_receipt:" + "c" * 64,
            roots=roots,
        ),
    )


def test_family_coverage_requires_exactly_all_thirteen_roots() -> None:
    spec, receipt = _full_models()
    _validate_family_coverage(spec, receipt)
    missing = receipt.model_copy(update={"roots": receipt.roots[:-1]})
    with pytest.raises(CompositionReceiptExportError, match="exactly P14-P18"):
        _validate_family_coverage(spec, missing)


def test_report_contains_exactly_65_sequence_rows_and_privacy_warning() -> None:
    spec, receipt = _full_models()
    record = _record(_pair())
    counts = _sequence_counts((record,))
    assert len(_EXPECTED_SEQUENCES) == 65
    assert len(counts) == 65
    report = _report(
        binding=_FullBinding(spec, receipt, Path("spec"), Path("receipt"), ()),
        chain_manifest=DeterministicCompositionChainManifest.model_construct(
            chain_set_id="detcomp_chain_set:" + "d" * 64,
            chain_count=1,
        ),
        unique_manifest=DeterministicCompositionUniquePairManifest.model_construct(
            unique_pair_set_id="detcomp_unique_pair_set:" + "e" * 64,
            unique_pair_count=1,
        ),
        gross_sequence_counts=counts,
        inventory=(record,),
        cycles=(),
        quarantine=(),
    ).decode()
    rows = [line for line in report.splitlines() if line.startswith("| `p")]
    assert len(rows) == 65
    assert "NOT TRAINING READY" in report
    assert "private `sft_classic`" in report


def test_report_marks_sequences_with_exports_and_quarantine_as_conflicted() -> None:
    spec, receipt = _full_models()
    record = _record(_pair())
    mixed = _record(
        _pair(
            kinds=("P_to_N", "P_to_P"),
            sequences=(
                "p14_independent_binder_permutation->n11_bound_variable_substitution",
                "p14_independent_binder_permutation->p15_root_iff_reversal",
            ),
        )
    )
    report = _report(
        binding=_FullBinding(spec, receipt, Path("spec"), Path("receipt"), ()),
        chain_manifest=DeterministicCompositionChainManifest.model_construct(
            chain_set_id="detcomp_chain_set:" + "d" * 64,
            chain_count=2,
        ),
        unique_manifest=DeterministicCompositionUniquePairManifest.model_construct(
            unique_pair_set_id="detcomp_unique_pair_set:" + "e" * 64,
            unique_pair_count=2,
        ),
        gross_sequence_counts=_sequence_counts((record, mixed)),
        inventory=(record,),
        cycles=(),
        quarantine=(mixed,),
    ).decode()
    sequence_row = next(
        line
        for line in report.splitlines()
        if line.startswith("| `p14_independent_binder_permutation` | `p15_root_iff_reversal`")
    )
    assert "provisional + conflict" in sequence_row


def test_export_directory_is_immutable_and_exactly_replayable(tmp_path: Path) -> None:
    payloads = {
        "inventory.jsonl": b"",
        "cycles.jsonl": b"",
        "quarantine.jsonl": b"",
        "manifest.json": b"{}\n",
        "report.md": b"report\n",
    }
    output = tmp_path / "export"
    assert _write_or_replay(output, payloads) is False
    assert _write_or_replay(output, payloads) is True
    (output / "report.md").write_bytes(b"changed\n")
    with pytest.raises(CompositionReceiptExportError, match="differs"):
        _write_or_replay(output, payloads)


def test_export_directory_rejects_direct_and_parent_symlinks(tmp_path: Path) -> None:
    payloads = {
        "inventory.jsonl": b"",
        "cycles.jsonl": b"",
        "quarantine.jsonl": b"",
        "manifest.json": b"{}\n",
        "report.md": b"report\n",
    }
    target = tmp_path / "target"
    assert _write_or_replay(target, payloads) is False
    direct_alias = tmp_path / "direct-alias"
    direct_alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(CompositionReceiptExportError, match="contains a symlink"):
        _write_or_replay(direct_alias, payloads)

    parent_target = tmp_path / "real-parent"
    parent_target.mkdir()
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(parent_target, target_is_directory=True)
    with pytest.raises(CompositionReceiptExportError, match="contains a symlink"):
        _write_or_replay(parent_alias / "export", payloads)


def test_composition_write_roots_are_pairwise_and_input_disjoint(tmp_path: Path) -> None:
    receipt_root = tmp_path / "full"
    seed_root = tmp_path / "seeds"
    source_partition = tmp_path / "sources/theorems.jsonl"
    for directory in (receipt_root, seed_root, source_partition.parent):
        directory.mkdir(parents=True, exist_ok=True)
    source_partition.write_text("", encoding="utf-8")

    with pytest.raises(CompositionReceiptExportError, match="write roots must be disjoint"):
        _require_disjoint_write_roots(
            chain_dir=tmp_path / "scratch",
            unique_pair_dir=tmp_path / "scratch/unique",
            output_dir=tmp_path / "export",
            protected_inputs=(receipt_root, seed_root, source_partition),
        )

    with pytest.raises(CompositionReceiptExportError, match="overlaps a bound input"):
        _require_disjoint_write_roots(
            chain_dir=receipt_root / "chains",
            unique_pair_dir=tmp_path / "unique",
            output_dir=tmp_path / "export",
            protected_inputs=(receipt_root, seed_root, source_partition),
        )

    with pytest.raises(CompositionReceiptExportError, match="overlaps a bound input"):
        _require_disjoint_write_roots(
            chain_dir=tmp_path / "scratch",
            unique_pair_dir=tmp_path / "unique",
            output_dir=source_partition.parent,
            protected_inputs=(receipt_root, seed_root, source_partition),
        )


def test_cli_accepts_multiple_public_private_source_partitions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import leanfaith.transforms.composition_receipt_export as export_module

    observed: dict[str, object] = {}

    def fake_export(**kwargs: object) -> CompositionReceiptExportArtifacts:
        observed.update(kwargs)
        return CompositionReceiptExportArtifacts(
            output_dir=tmp_path / "out",
            manifest_path=tmp_path / "out/manifest.json",
            inventory_path=tmp_path / "out/inventory.jsonl",
            cycles_path=tmp_path / "out/cycles.jsonl",
            quarantine_path=tmp_path / "out/quarantine.jsonl",
            report_path=tmp_path / "out/report.md",
            export_set_id="detcomp_export_set:" + "f" * 64,
            provisional_inventory_count=7,
            cycle_audit_count=2,
            mixed_intention_quarantine_count=1,
            replayed=False,
        )

    monkeypatch.setattr(export_module, "export_deterministic_v2_composition_receipt", fake_export)
    result = CliRunner().invoke(
        app,
        [
            "export-deterministic-composition-receipt",
            "--full-run-root",
            "full",
            "--seed-dir",
            "seeds",
            "--source-theorems",
            "mathlib-theorems.jsonl",
            "--source-theorems",
            "sft-theorems.jsonl",
            "--source-representations",
            "mathlib-representations.jsonl",
            "--source-representations",
            "sft-representations.jsonl",
            "--chain-dir",
            "chains",
            "--unique-pair-dir",
            "unique",
            "--output-dir",
            "out",
            "--root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert observed["source_theorems"] == [
        tmp_path / "mathlib-theorems.jsonl",
        tmp_path / "sft-theorems.jsonl",
    ]
    assert "provisional_inventory=7" in result.stdout
    assert "training_eligible=false" in result.stdout
