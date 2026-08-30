"""Offline contracts for the fixed-sample full-skeleton N21/N22 pilot."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

import leanfaith.corpus2.s1_public_negative_skeleton_pilot as v1
import leanfaith.corpus2.s1_public_negative_skeleton_pilot_v2 as pilot
from leanfaith.config.hashing import sha256_hex
from leanfaith.corpus2.build_v1 import FinalRow
from leanfaith.corpus2.s1_public_negative_skeleton_pilot_v2 import (
    EngineAuditV2,
    EngineCandidateV2,
    PilotV2Config,
    choose_candidates,
    render_audit_driver,
    render_primary_driver,
    select_sources,
)
from leanfaith.train2.trainer import TrainingRecord


def _inputs(path: Path) -> dict[str, v1.FrozenInput]:
    return {name: v1.FrozenInput(path=path, sha256="0" * 64) for name in pilot._INPUT_NAMES}


def _config(tmp_path: Path) -> PilotV2Config:
    inputs = _inputs(tmp_path / "placeholder")
    inputs["negative_engine"] = v1.FrozenInput(
        path=Path(pilot.__file__).resolve().parents[3]
        / "LeanFaith"
        / "Meta"
        / "NegativeSkeletonEngineV2.lean",
        sha256="0" * 64,
    )
    return PilotV2Config(
        output_root=tmp_path / "output",
        mathlib_root=tmp_path / "mathlib",
        tokenizer_root=tmp_path / "tokenizer",
        inputs=inputs,
        enforce_storage_root=False,
    )


def _source(index: int, split: str) -> v1.SourceRow:
    trainer = TrainingRecord(
        record_id=f"source-{index}",
        reference_headless=f"P_{index} \u2194 Q_{index} \u2227 R_{index}",
        candidate_headless=f"Q_{index} \u2227 R_{index} \u2194 P_{index}",
        label=True,
        group_key=f"component-{index}",
        family="P20",
        source="meta_engine_slice2",
        weight=1.0,
    )
    split_name = cast(Literal["train", "validation", "test"], split)
    final = FinalRow(trainer=trainer, provenance={}, split=split_name)
    return v1.SourceRow(
        declaration=f"Fixture.theorem_{index}",
        ancestry_id=f"mathlib-declaration:{index:064x}",
        split=split_name,
        trainer=trainer,
        final_row=final,
    )


def _candidate(
    source: v1.SourceRow,
    family: str = "N22",
    operation_kind: str = "andToOr",
    site_path: str = "/root-body/right",
) -> EngineCandidateV2:
    reference = source.trainer.reference_headless
    candidate = f"P_{source.trainer.record_id} \u2194 Q_{source.trainer.record_id} \u2228 R"
    operation = f"{operation_kind}:{site_path}"
    return EngineCandidateV2.model_validate(
        {
            "schemaVersion": 2,
            "kind": "candidate",
            "recordKind": "candidate",
            "status": "ok",
            "declaration": source.declaration,
            "family": family,
            "operation": operation,
            "operationKind": operation_kind,
            "sitePath": site_path,
            "source": reference,
            "candidate": candidate,
            "sourceTypeHash": sha256_hex(reference.encode()),
            "candidateTypeHash": sha256_hex(candidate.encode()),
            "evidenceClass": "N-SEP",
            "evidence": {
                "relation": "schemaInequivalence",
                "exactBooleanSkeleton": True,
                "deduplicatedAtoms": True,
                "fullTruthTableEnumerated": True,
                "rootInfluence": True,
                "separatorVerified": True,
                "contractScope": "abstract-propositional-schema",
            },
            "witness": {
                "sourceSkeleton": "(A0 \u2194 (A1 \u2227 A2))",
                "candidateSkeleton": "(A0 \u2194 (A1 \u2228 A2))",
                "atomHashes": ["1" * 64, "2" * 64, "3" * 64],
                "atomCount": 3,
                "valuationSpaceSize": 8,
                "valuation": [False, False, True],
                "sourceValue": True,
                "candidateValue": False,
                "outerBinderCount": 0,
            },
            "candidateElaborates": True,
            "wholeTypeDefEq": False,
            "axioms": "none",
        }
    )


def _audit(candidate: EngineCandidateV2) -> EngineAuditV2:
    return EngineAuditV2.model_validate(
        {
            "schemaVersion": 2,
            "kind": "audit",
            "recordKind": "audit",
            "declaration": candidate.declaration,
            "family": candidate.family,
            "operation": candidate.operation,
            "expectedCandidateTypeHash": candidate.candidate_type_hash,
            "actualCandidateTypeHash": candidate.candidate_type_hash,
            "verified": True,
            "status": "verified",
            "reason": "verified",
            "auditMode": "independent-full-skeleton-reconstruction",
        }
    )


def _canary(validation: float, test: float, *, target_met: bool = False) -> dict[str, Any]:
    return {
        "diagnostics": {
            "validation": {"balanced_accuracy": validation},
            "test": {"balanced_accuracy": test},
        },
        "target_met": target_met,
    }


def test_config_rejects_changed_preregistered_quotas(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="split quotas differ"):
        PilotV2Config(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            tokenizer_root=tmp_path / "tokenizer",
            inputs=_inputs(tmp_path / "placeholder"),
            selection_quotas={"train": 73, "validation": 11, "test": 12},
            enforce_storage_root=False,
        )


def test_selection_remains_exactly_split_stratified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    eligible = {
        source.ancestry_id: source
        for split, count, offset in (("train", 90, 0), ("validation", 20, 100), ("test", 20, 200))
        for source in (_source(offset + index, split) for index in range(count))
    }
    monkeypatch.setattr(v1, "_load_base_rows", lambda _config: ([], eligible))

    selected = select_sources(config)

    assert len(selected) == 96
    assert Counter(source.split for source in selected) == {
        "train": 72,
        "validation": 12,
        "test": 12,
    }


def test_candidate_requires_full_truth_table_inventory() -> None:
    payload = _candidate(_source(0, "train")).model_dump(mode="json", by_alias=True)
    payload["witness"]["valuationSpaceSize"] = 4

    with pytest.raises(ValidationError, match="valuation inventory differs"):
        EngineCandidateV2.model_validate(payload)


def test_candidate_requires_operation_path_binding() -> None:
    payload = _candidate(_source(0, "train")).model_dump(mode="json", by_alias=True)
    payload["operation"] = "andToOr:/root-body/left"

    with pytest.raises(ValidationError, match="operation/path binding differs"):
        EngineCandidateV2.model_validate(payload)


def test_candidate_choice_preserves_frozen_family_policy() -> None:
    sources = tuple(_source(index, "train") for index in range(96))
    candidates = tuple(
        candidate
        for source in sources
        for candidate in (
            _candidate(source, "N21", "negateAtom", "/root-body/left"),
            _candidate(source, "N22", "iffToImp", "/root-body"),
        )
    )

    chosen = choose_candidates(candidates, sources)

    counts = Counter(row.family for row in chosen)
    assert len(chosen) == 96
    assert counts["N22"] >= 0.6 * len(chosen)
    assert counts["N21"] <= 0.4 * len(chosen)


def test_drivers_use_only_v2_engine_commands(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate = _candidate(_source(0, "train"))

    primary = render_primary_driver(config, tmp_path / "names.txt")
    audit = render_audit_driver(config, [candidate])

    assert "lfNegativeSkeletonV2Batch" in primary
    assert "lfAuditNegativeSkeletonV2" in audit
    assert candidate.operation in audit
    assert candidate.candidate_type_hash in audit
    assert "final_test" not in primary + audit


def test_summary_keeps_all_gates_and_no_direct_training(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sources = tuple(
        _source(index, "train" if index < 16 else "validation" if index < 20 else "test")
        for index in range(24)
    )
    candidates = tuple(
        _candidate(
            source,
            "N22" if index < 15 else "N21",
            ("andToOr", "orToAnd", "iffToImp", "negateAtom")[index % 4],
        )
        for index, source in enumerate(sources)
    )
    negatives = tuple(
        FinalRow(
            trainer=TrainingRecord(
                record_id=f"negative-{index}",
                reference_headless=candidate.source,
                candidate_headless=candidate.candidate,
                label=False,
                group_key=source.trainer.group_key,
                family=candidate.family,
                source="typed_negative_skeleton_v2",
                weight=1.0,
            ),
            provenance={},
            split=source.split,
        )
        for index, (source, candidate) in enumerate(zip(sources, candidates, strict=True))
    )

    summary = pilot._summary(
        config,
        sources,
        candidates,
        candidates,
        candidates,
        negatives,
        tuple(_audit(candidate) for candidate in candidates),
        (),
        _canary(0.85, 0.84),
        _canary(0.83, 0.82),
        _canary(0.60, 0.61, target_met=True),
    )

    assert summary["pilot_gate_passed"] is True
    assert summary["decision"] == {
        "scale_authorized": True,
        "training_authorized": False,
        "rebuild_required_before_training": True,
        "sample_size_increase_authorized": False,
        "final_test_accessed": False,
    }
