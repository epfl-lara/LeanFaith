"""Offline contracts for the frozen v3 audit and canary runner."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

import leanfaith.corpus2.s1_public_negative_skeleton_audit_canary_v3 as audit_canary
from leanfaith.corpus2.build_v1 import FinalRow
from leanfaith.corpus2.s1_public_negative_skeleton_audit_canary_v3 import (
    AuditCanaryConfig,
    EngineAuditV3,
    parse_audits,
    render_audit_driver,
)
from leanfaith.corpus2.s1_public_negative_skeleton_pilot import FrozenInput
from leanfaith.train2.trainer import TrainingRecord


def _inputs(path: Path) -> dict[str, FrozenInput]:
    return {name: FrozenInput(path=path, sha256="0" * 64) for name in audit_canary._INPUT_NAMES}


def _config(tmp_path: Path) -> AuditCanaryConfig:
    inputs = _inputs(tmp_path / "placeholder")
    repo = Path(audit_canary.__file__).resolve().parents[3]
    inputs["negative_engine_v2"] = FrozenInput(
        path=repo / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV2.lean",
        sha256="0" * 64,
    )
    inputs["negative_engine_v3"] = FrozenInput(
        path=repo / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV3.lean",
        sha256="0" * 64,
    )
    return AuditCanaryConfig(
        output_root=tmp_path / "output",
        mathlib_root=tmp_path / "mathlib",
        tokenizer_root=tmp_path / "tokenizer",
        inputs=inputs,
        enforce_storage_root=False,
    )


def _candidate(
    index: int = 0,
    *,
    family: str = "N22",
    operation_kind: str = "impToIff",
) -> SimpleNamespace:
    return SimpleNamespace(
        declaration=f"Example.theorem{index}",
        family=family,
        operation=f"{operation_kind}:/root-body",
        operation_kind=operation_kind,
        candidate_type_hash=f"{index + 1:064x}",
    )


def _audit_payload(candidate: SimpleNamespace) -> dict[str, object]:
    return {
        "schemaVersion": 3,
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
        "auditMode": "independent-implication-aware-reconstruction",
    }


def _negative_row(index: int, split: str) -> FinalRow:
    trainer = TrainingRecord(
        record_id=f"negative:{index}",
        reference_headless=f"P{index}",
        candidate_headless=f"Q{index}",
        label=False,
        group_key=f"group:{index}",
        family="N22",
        source="unit",
        weight=1.0,
    )
    return FinalRow(trainer=trainer, provenance={}, split=cast(Any, split))


def _summary_inputs() -> tuple[
    tuple[SimpleNamespace, ...],
    tuple[FinalRow, ...],
    tuple[SimpleNamespace, ...],
]:
    operations = (
        *(("N21", "negateAtom") for _ in range(9)),
        *(("N22", "iffToImp") for _ in range(5)),
        *(("N22", "impConverse") for _ in range(3)),
        *(("N22", "impToAnd") for _ in range(3)),
        *(("N22", "impToIff") for _ in range(2)),
        ("N22", "andToOr"),
        ("N22", "orToAnd"),
    )
    candidates = tuple(
        _candidate(index, family=family, operation_kind=operation)
        for index, (family, operation) in enumerate(operations)
    )
    rows = tuple(
        _negative_row(
            index,
            "train" if index < 16 else "validation" if index < 20 else "test",
        )
        for index in range(24)
    )
    audits = tuple(SimpleNamespace(verified=True) for _ in range(24))
    return candidates, rows, audits


def _canary(validation: float, test: float, *, target_met: bool = True) -> dict[str, Any]:
    return {
        "diagnostics": {
            "validation": {"balanced_accuracy": validation},
            "test": {"balanced_accuracy": test},
        },
        "target_met": target_met,
    }


def test_config_rejects_nonstorage_production_root(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must be under /storage/milikic"):
        AuditCanaryConfig(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            tokenizer_root=tmp_path / "tokenizer",
            inputs=_inputs(tmp_path / "placeholder"),
        )


def test_config_rejects_changed_input_inventory(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path / "placeholder")
    inputs.pop("feasibility_selection")
    with pytest.raises(ValidationError, match="exact frozen input set"):
        AuditCanaryConfig(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            tokenizer_root=tmp_path / "tokenizer",
            inputs=inputs,
            enforce_storage_root=False,
        )


def test_audit_rejects_changed_reconstruction_hash() -> None:
    candidate = _candidate()
    payload = _audit_payload(candidate)
    payload["actualCandidateTypeHash"] = "f" * 64

    with pytest.raises(ValidationError, match="did not verify the exact hash"):
        EngineAuditV3.model_validate(payload)


def test_driver_contains_only_v3_audits_and_no_batch_regeneration(tmp_path: Path) -> None:
    candidate = _candidate()

    driver = render_audit_driver(_config(tmp_path), cast(Any, (candidate,)))

    assert "lfAuditNegativeSkeletonV3" in driver
    assert candidate.candidate_type_hash in driver
    assert driver.count("lfNegativeSkeletonV3Batch") == 1
    assert "final_test" not in driver


def test_parser_requires_the_exact_selected_audit_set() -> None:
    candidates = (_candidate(0), _candidate(1, operation_kind="impConverse"))
    payload = b"".join(
        audit_canary.v1._canonical_line(_audit_payload(candidate)) for candidate in candidates
    )

    audits = parse_audits(payload, cast(Any, candidates))

    assert len(audits) == 2
    assert all(audit.verified for audit in audits)


def test_summary_passes_only_when_all_unchanged_gates_pass(tmp_path: Path) -> None:
    candidates, rows, audits = _summary_inputs()
    baseline = _canary(0.85, 0.84)
    augmented = _canary(0.82, 0.81)
    paired = _canary(0.625, 0.625)

    summary = audit_canary._summary(
        _config(tmp_path),
        cast(Any, candidates),
        rows,
        cast(Any, audits),
        baseline,
        augmented,
        paired,
    )

    assert summary["pilot_gate_passed"] is True
    assert summary["decision"]["public_rebuild_authorized"] is True
    assert summary["decision"]["training_authorized"] is False
    assert summary["counts"]["split"] == {"test": 4, "train": 16, "validation": 4}


def test_summary_fails_closed_on_paired_canary(tmp_path: Path) -> None:
    candidates, rows, audits = _summary_inputs()

    summary = audit_canary._summary(
        _config(tmp_path),
        cast(Any, candidates),
        rows,
        cast(Any, audits),
        _canary(0.85, 0.84),
        _canary(0.82, 0.81),
        _canary(0.75, 0.75, target_met=False),
    )

    assert summary["pilot_gate_passed"] is False
    assert summary["decision"]["public_rebuild_authorized"] is False
    assert summary["decision"]["training_authorized"] is False
