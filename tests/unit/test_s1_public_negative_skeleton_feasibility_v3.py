"""Offline contracts for the fixed-sample implication feasibility precheck."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

import leanfaith.corpus2.s1_public_negative_skeleton_feasibility_v3 as feasibility
from leanfaith.config.hashing import sha256_hex
from leanfaith.corpus2.build_v1 import FinalRow
from leanfaith.corpus2.s1_public_negative_skeleton_feasibility_v3 import (
    EngineCandidateV3,
    FeasibilityConfig,
    parse_primary,
    render_primary_driver,
    solve_feasible_subset,
)
from leanfaith.corpus2.s1_public_negative_skeleton_pilot import FrozenInput, SourceRow
from leanfaith.train2.trainer import TrainingRecord


def _inputs(path: Path) -> dict[str, FrozenInput]:
    return {name: FrozenInput(path=path, sha256="0" * 64) for name in feasibility._INPUT_NAMES}


def _config(tmp_path: Path) -> FeasibilityConfig:
    inputs = _inputs(tmp_path / "placeholder")
    repo = Path(feasibility.__file__).resolve().parents[3]
    inputs["negative_engine_v2"] = FrozenInput(
        path=repo / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV2.lean",
        sha256="0" * 64,
    )
    inputs["negative_engine_v3"] = FrozenInput(
        path=repo / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV3.lean",
        sha256="0" * 64,
    )
    return FeasibilityConfig(
        output_root=tmp_path / "output",
        mathlib_root=tmp_path / "mathlib",
        tokenizer_root=tmp_path / "tokenizer",
        inputs=inputs,
        enforce_storage_root=False,
    )


def _source(
    index: int,
    split: Literal["train", "validation", "test"],
    *,
    reference: str | None = None,
) -> SourceRow:
    declaration = f"Example.theorem{index:02d}"
    reference = reference or f"P{index}"
    trainer = TrainingRecord(
        record_id=f"source:{index}",
        reference_headless=reference,
        candidate_headless=f"positive:{index}",
        label=True,
        group_key=f"group:{index}",
        family="positive",
        source="unit",
        weight=1.0,
    )
    final = FinalRow(trainer=trainer, provenance={}, split=split)
    return SourceRow(
        declaration=declaration,
        ancestry_id=f"ancestry:{index}",
        split=split,
        trainer=trainer,
        final_row=final,
    )


def _candidate(
    source: SourceRow,
    family: Literal["N21", "N22"],
    operation_kind: str,
    *,
    candidate_text: str | None = None,
) -> EngineCandidateV3:
    reference = source.trainer.reference_headless
    candidate_text = candidate_text or f"{operation_kind}({reference})"
    operation = f"{operation_kind}:/root-body"
    return EngineCandidateV3.model_validate(
        {
            "schemaVersion": 3,
            "kind": "candidate",
            "recordKind": "candidate",
            "status": "ok",
            "declaration": source.declaration,
            "family": family,
            "operation": operation,
            "operationKind": operation_kind,
            "sitePath": "/root-body",
            "source": reference,
            "candidate": candidate_text,
            "sourceTypeHash": sha256_hex(reference.encode()),
            "candidateTypeHash": sha256_hex(candidate_text.encode()),
            "evidenceClass": "N-SEP",
            "evidence": {
                "relation": "schemaInequivalence",
                "exactBooleanSkeleton": True,
                "deduplicatedAtoms": True,
                "fullTruthTableEnumerated": True,
                "implicationAware": True,
                "parameterTelescopePreserved": True,
                "rootInfluence": True,
                "separatorVerified": True,
                "contractScope": "abstract-propositional-schema",
            },
            "witness": {
                "sourceSkeleton": "A0",
                "candidateSkeleton": "¬A0",
                "atomHashes": ["1" * 64],
                "atomCount": 1,
                "valuationSpaceSize": 2,
                "valuation": [False],
                "sourceValue": False,
                "candidateValue": True,
                "outerBinderCount": 0,
            },
            "candidateElaborates": True,
            "wholeTypeDefEq": False,
            "axioms": "none",
        }
    )


def _passing_inventory() -> tuple[tuple[SourceRow, ...], tuple[EngineCandidateV3, ...]]:
    sources = tuple(
        _source(
            index,
            "validation" if index < 4 else "test" if index < 8 else "train",
        )
        for index in range(24)
    )
    operation_families = (
        *(("N21", "negateAtom") for _ in range(9)),
        *(("N22", "impToIff") for _ in range(5)),
        *(("N22", "impConverse") for _ in range(5)),
        *(("N22", "impToAnd") for _ in range(5)),
    )
    candidates = tuple(
        _candidate(source, family, operation)
        for source, (family, operation) in zip(sources, operation_families, strict=True)
    )
    return sources, candidates


def test_config_rejects_nonstorage_production_root(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must be under /storage/milikic"):
        FeasibilityConfig(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            tokenizer_root=tmp_path / "tokenizer",
            inputs=_inputs(tmp_path / "placeholder"),
        )


def test_config_rejects_changed_input_inventory(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path / "placeholder")
    inputs.pop("implication_smoke_manifest")
    with pytest.raises(ValidationError, match="exact frozen input set"):
        FeasibilityConfig(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            tokenizer_root=tmp_path / "tokenizer",
            inputs=inputs,
            enforce_storage_root=False,
        )


def test_candidate_requires_implication_aware_evidence() -> None:
    source = _source(0, "train")
    payload = _candidate(source, "N22", "impToIff").model_dump(mode="json", by_alias=True)
    payload["evidence"]["implicationAware"] = False

    with pytest.raises(ValidationError, match="separator contract differs"):
        EngineCandidateV3.model_validate(payload)


def test_driver_combines_frozen_v2_and_v3_engines(tmp_path: Path) -> None:
    driver = render_primary_driver(_config(tmp_path), tmp_path / "names.txt")

    assert "NegativeSkeletonEngineV2Helper" in driver
    assert "NegativeSkeletonEngineV3Helper" in driver
    assert "lfNegativeSkeletonV3Batch" in driver
    assert "maxHeartbeats 0" in driver
    assert "final_test" not in driver


def test_parser_requires_complete_fixed_batch(tmp_path: Path) -> None:
    sources = (_source(0, "validation"), _source(1, "test"))
    candidate = _candidate(sources[0], "N22", "impToIff")
    rows = [candidate.model_dump(mode="json", by_alias=True)]
    for source in sources:
        rows.append(
            {
                "schemaVersion": 3,
                "kind": "terminal",
                "recordKind": "status",
                "declaration": source.declaration,
                "status": "complete",
                "discoveredCount": int(source is sources[0]),
                "emittedCount": int(source is sources[0]),
                "rejectedCount": 0,
                "source": source.trainer.reference_headless,
                "sourceTypeHash": sha256_hex(source.trainer.reference_headless.encode()),
                "sourceTextRoundtripVerified": True,
                "maxSkeletonAtoms": 8,
                "implicationAware": True,
            }
        )
    rows.append(
        {
            "schemaVersion": 3,
            "kind": "batch",
            "recordKind": "batch",
            "status": "complete",
            "declarationCount": 2,
            "completedCount": 2,
            "failedCount": 0,
        }
    )
    payload = b"".join(feasibility.v1._canonical_line(row) for row in rows)

    parsed, terminals = parse_primary(payload, sources)

    assert parsed == (candidate,)
    assert len(terminals) == 2


def test_solver_finds_deterministic_exact_24_subset() -> None:
    sources, candidates = _passing_inventory()

    first = solve_feasible_subset(candidates, sources)
    second = solve_feasible_subset(tuple(reversed(candidates)), sources)

    assert first.status == "passed"
    assert first.selected == second.selected
    assert len(first.selected) == 24
    assert sum(candidate.family == "N22" for candidate in first.selected) == 15
    operations = {
        operation: sum(candidate.operation_kind == operation for candidate in first.selected)
        for operation in {candidate.operation_kind for candidate in first.selected}
    }
    assert max(operations.values()) == 9


def test_solver_fails_closed_when_one_operation_would_dominate() -> None:
    sources = tuple(
        _source(
            index,
            "validation" if index < 4 else "test" if index < 8 else "train",
        )
        for index in range(24)
    )
    candidates = tuple(_candidate(source, "N21", "negateAtom") for source in sources)

    result = solve_feasible_subset(candidates, sources)

    assert result.status == "failed"
    assert result.selected == ()
    assert result.reason == "no_exact_24_subset_satisfies_all_constraints"


def test_solver_state_limit_is_indeterminate_not_passed() -> None:
    sources, candidates = _passing_inventory()

    result = solve_feasible_subset(candidates, sources, state_limit=1)

    assert result.status == "indeterminate"
    assert result.selected == ()
    assert result.reason == "solver_state_limit_exceeded"


def test_solver_enforces_cross_declaration_pair_uniqueness() -> None:
    sources, candidates = _passing_inventory()
    duplicate_sources = list(sources)
    duplicate_sources[1] = _source(1, "validation", reference="P0")
    duplicate_candidates = list(candidates)
    duplicate_candidates[1] = _candidate(
        duplicate_sources[1],
        "N21",
        "negateAtom",
        candidate_text=candidates[0].candidate,
    )

    result = solve_feasible_subset(tuple(duplicate_candidates), tuple(duplicate_sources))

    assert result.status == "failed"
    assert result.selected == ()
