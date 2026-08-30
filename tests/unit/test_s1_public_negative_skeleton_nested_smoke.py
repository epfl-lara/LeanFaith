"""Offline contracts for the one-declaration full-skeleton smoke."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import leanfaith.corpus2.s1_public_negative_skeleton_nested_smoke as smoke
from leanfaith.config.hashing import sha256_hex
from leanfaith.corpus2.s1_public_negative_skeleton_nested_smoke import (
    EngineAudit,
    EngineCandidate,
    FrozenInput,
    SmokeConfig,
    render_audit_driver,
    render_primary_driver,
)


def _inputs(path: Path) -> dict[str, FrozenInput]:
    return {name: FrozenInput(path=path, sha256="0" * 64) for name in smoke._INPUT_NAMES}


def _config(tmp_path: Path) -> SmokeConfig:
    inputs = _inputs(tmp_path / "placeholder")
    inputs["negative_engine_v2"] = FrozenInput(
        path=Path(smoke.__file__).resolve().parents[3]
        / "LeanFaith"
        / "Meta"
        / "NegativeSkeletonEngineV2.lean",
        sha256="0" * 64,
    )
    return SmokeConfig(
        output_root=tmp_path / "output",
        mathlib_root=tmp_path / "mathlib",
        inputs=inputs,
        enforce_storage_root=False,
    )


def _candidate() -> EngineCandidate:
    source = "P \u2194 Q \u2227 R"
    candidate = "P \u2194 Q \u2228 R"
    return EngineCandidate.model_validate(
        {
            "schemaVersion": 2,
            "kind": "candidate",
            "recordKind": "candidate",
            "status": "ok",
            "declaration": smoke.DECLARATION,
            "family": "N22",
            "operation": smoke.TARGET_OPERATION,
            "operationKind": smoke.TARGET_OPERATION_KIND,
            "sitePath": smoke.TARGET_SITE_PATH,
            "source": source,
            "candidate": candidate,
            "sourceTypeHash": sha256_hex(source.encode()),
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
                "outerBinderCount": 10,
            },
            "candidateElaborates": True,
            "wholeTypeDefEq": False,
            "axioms": "none",
        }
    )


def _audit(candidate: EngineCandidate) -> EngineAudit:
    return EngineAudit.model_validate(
        {
            "schemaVersion": 2,
            "kind": "audit",
            "recordKind": "audit",
            "declaration": smoke.DECLARATION,
            "family": "N22",
            "operation": smoke.TARGET_OPERATION,
            "expectedCandidateTypeHash": candidate.candidate_type_hash,
            "actualCandidateTypeHash": candidate.candidate_type_hash,
            "verified": True,
            "status": "verified",
            "reason": "verified",
            "auditMode": "independent-full-skeleton-reconstruction",
        }
    )


def test_config_rejects_nonstorage_production_root(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must be under /storage/milikic"):
        SmokeConfig(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            inputs=_inputs(tmp_path / "placeholder"),
        )


def test_config_rejects_changed_input_inventory(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path / "placeholder")
    inputs.pop("root_pilot_selection")
    with pytest.raises(ValidationError, match="exact frozen input set"):
        SmokeConfig(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            inputs=inputs,
            enforce_storage_root=False,
        )


def test_candidate_requires_operation_path_binding() -> None:
    payload = _candidate().model_dump(mode="json", by_alias=True)
    payload["operation"] = "andToOr:/root-body/left"

    with pytest.raises(ValidationError, match="operation/path binding differs"):
        EngineCandidate.model_validate(payload)


def test_candidate_requires_complete_valuation_inventory() -> None:
    payload = _candidate().model_dump(mode="json", by_alias=True)
    payload["witness"]["valuationSpaceSize"] = 4

    with pytest.raises(ValidationError, match="valuation inventory differs"):
        EngineCandidate.model_validate(payload)


def test_candidate_requires_root_separation() -> None:
    payload = _candidate().model_dump(mode="json", by_alias=True)
    payload["witness"]["candidateValue"] = True

    with pytest.raises(ValidationError, match="does not separate"):
        EngineCandidate.model_validate(payload)


def test_drivers_bind_one_declaration_and_exact_nested_audit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate = _candidate()

    primary = render_primary_driver(config, tmp_path / "declaration_names.txt")
    audit = render_audit_driver(config, candidate)

    assert "lfNegativeSkeletonV2Batch" in primary
    assert str(tmp_path / "declaration_names.txt") in primary
    assert "lfAuditNegativeSkeletonV2" in audit
    assert smoke.DECLARATION in audit
    assert smoke.TARGET_OPERATION in audit
    assert candidate.candidate_type_hash in audit
    assert "final_test" not in primary + audit


def test_summary_allows_only_same_fixed_pilot_rerun() -> None:
    candidate = _candidate()
    summary = smoke._summary((candidate,), candidate, _audit(candidate))
    decision = summary["decision"]

    assert isinstance(decision, dict)
    assert decision == {
        "same_fixed_96_pilot_rerun_authorized": True,
        "sample_size_increase_authorized": False,
        "scale_authorized": False,
        "training_authorized": False,
        "final_test_accessed": False,
    }


def test_audit_rejects_different_reconstructed_hash() -> None:
    candidate = _candidate()
    payload: dict[str, Any] = _audit(candidate).model_dump(mode="json", by_alias=True)
    payload["actualCandidateTypeHash"] = "f" * 64

    with pytest.raises(ValidationError, match="audit hash differs"):
        EngineAudit.model_validate(payload)
