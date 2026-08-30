"""Offline contracts for the one-declaration implication-aware smoke."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import leanfaith.corpus2.s1_public_negative_skeleton_implication_smoke as smoke
from leanfaith.config.hashing import sha256_hex
from leanfaith.corpus2.s1_public_negative_skeleton_implication_smoke import (
    EngineAudit,
    EngineCandidate,
    SmokeConfig,
    render_audit_driver,
    render_primary_driver,
)
from leanfaith.corpus2.s1_public_negative_skeleton_nested_smoke import FrozenInput


def _inputs(path: Path) -> dict[str, FrozenInput]:
    return {name: FrozenInput(path=path, sha256="0" * 64) for name in smoke._INPUT_NAMES}


def _config(tmp_path: Path) -> SmokeConfig:
    inputs = _inputs(tmp_path / "placeholder")
    repo = Path(smoke.__file__).resolve().parents[3]
    inputs["negative_engine_v2"] = FrozenInput(
        path=repo / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV2.lean",
        sha256="0" * 64,
    )
    inputs["negative_engine_v3"] = FrozenInput(
        path=repo / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV3.lean",
        sha256="0" * 64,
    )
    return SmokeConfig(
        output_root=tmp_path / "output",
        mathlib_root=tmp_path / "mathlib",
        inputs=inputs,
        enforce_storage_root=False,
    )


def _candidate() -> EngineCandidate:
    source = "P \u2192 Q \u2192 R"
    candidate = "P \u2194 (Q \u2192 R)"
    return EngineCandidate.model_validate(
        {
            "schemaVersion": 3,
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
                "implicationAware": True,
                "parameterTelescopePreserved": True,
                "rootInfluence": True,
                "separatorVerified": True,
                "contractScope": "abstract-propositional-schema",
            },
            "witness": {
                "sourceSkeleton": "(A0 \u2192 (A1 \u2192 A2))",
                "candidateSkeleton": "(A0 \u2194 (A1 \u2192 A2))",
                "atomHashes": ["1" * 64, "2" * 64, "3" * 64],
                "atomCount": 3,
                "valuationSpaceSize": 8,
                "valuation": [False, False, False],
                "sourceValue": True,
                "candidateValue": False,
                "outerBinderCount": 5,
            },
            "candidateElaborates": True,
            "wholeTypeDefEq": False,
            "axioms": "none",
        }
    )


def _audit(candidate: EngineCandidate) -> EngineAudit:
    return EngineAudit.model_validate(
        {
            "schemaVersion": 3,
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
            "auditMode": "independent-implication-aware-reconstruction",
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
    inputs.pop("pilot_v2_selection")
    with pytest.raises(ValidationError, match="exact frozen input set"):
        SmokeConfig(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            inputs=inputs,
            enforce_storage_root=False,
        )


def test_candidate_requires_implication_evidence() -> None:
    payload = _candidate().model_dump(mode="json", by_alias=True)
    payload["evidence"]["implicationAware"] = False

    with pytest.raises(ValidationError, match="separator contract differs"):
        EngineCandidate.model_validate(payload)


def test_candidate_requires_complete_valuation_inventory() -> None:
    payload = _candidate().model_dump(mode="json", by_alias=True)
    payload["witness"]["valuationSpaceSize"] = 4

    with pytest.raises(ValidationError, match="valuation inventory differs"):
        EngineCandidate.model_validate(payload)


def test_drivers_combine_frozen_v2_and_v3_commands(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate = _candidate()

    primary = render_primary_driver(config, tmp_path / "declaration_names.txt")
    audit = render_audit_driver(config, candidate)

    assert "NegativeSkeletonEngineV2Helper" in primary
    assert "NegativeSkeletonEngineV3Helper" in primary
    assert "lfNegativeSkeletonV3Batch" in primary
    assert "lfAuditNegativeSkeletonV3" in audit
    assert smoke.TARGET_OPERATION in audit
    assert candidate.candidate_type_hash in audit
    assert "final_test" not in primary + audit


def test_summary_authorizes_only_feasibility_precheck() -> None:
    candidate = _candidate()
    summary = smoke._summary((candidate,), candidate, _audit(candidate))

    assert summary["decision"] == {
        "same_fixed_96_feasibility_precheck_authorized": True,
        "canary_fit_authorized": False,
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
