"""Offline contract tests for the typed N21/N22 separator pilot."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

import leanfaith.corpus2.s1_public_negative_skeleton_pilot as pilot
from leanfaith.config.hashing import sha256_hex
from leanfaith.corpus2.build_v1 import FinalRow
from leanfaith.corpus2.s1_public_negative_skeleton_pilot import (
    EngineAudit,
    EngineCandidate,
    FrozenInput,
    PilotConfig,
    SourceRow,
    choose_candidates,
    render_audit_driver,
    render_primary_driver,
    select_sources,
)
from leanfaith.train2.trainer import TrainingRecord


def _inputs(path: Path) -> dict[str, FrozenInput]:
    return {name: FrozenInput(path=path, sha256="0" * 64) for name in pilot._INPUT_NAMES}


def _config(tmp_path: Path) -> PilotConfig:
    inputs = _inputs(tmp_path / "placeholder")
    inputs["negative_engine"] = FrozenInput(
        path=Path(pilot.__file__).resolve().parents[3]
        / "LeanFaith"
        / "Meta"
        / "NegativeSkeletonEngine.lean",
        sha256="0" * 64,
    )
    return PilotConfig(
        output_root=tmp_path / "output",
        mathlib_root=tmp_path / "mathlib",
        tokenizer_root=tmp_path / "tokenizer",
        inputs=inputs,
        enforce_storage_root=False,
    )


def _source(index: int, split: str) -> SourceRow:
    trainer = TrainingRecord(
        record_id=f"source-{index}",
        reference_headless=f"P_{index} ↔ Q_{index}",
        candidate_headless=f"Q_{index} ↔ P_{index}",
        label=True,
        group_key=f"component-{index}",
        family="P20",
        source="meta_engine_slice2",
        weight=1.0,
    )
    split_name = cast(Literal["train", "validation", "test"], split)
    final = FinalRow(trainer=trainer, provenance={}, split=split_name)
    return SourceRow(
        declaration=f"Fixture.theorem_{index}",
        ancestry_id=f"mathlib-declaration:{index:064x}",
        split=split_name,
        trainer=trainer,
        final_row=final,
    )


def _candidate(source: SourceRow, family: str, operation: str) -> EngineCandidate:
    reference = source.trainer.reference_headless
    candidate = f"P_{source.trainer.record_id} → Q_{source.trainer.record_id}"
    return EngineCandidate.model_validate(
        {
            "schemaVersion": 1,
            "kind": "candidate",
            "recordKind": "candidate",
            "status": "ok",
            "declaration": source.declaration,
            "family": family,
            "operation": operation,
            "operationKind": operation.split(":", 1)[0],
            "sitePath": "/root-body",
            "source": reference,
            "candidate": candidate,
            "sourceTypeHash": sha256_hex(reference.encode()),
            "candidateTypeHash": sha256_hex(candidate.encode()),
            "evidenceClass": "N-SEP",
            "evidence": {
                "relation": "schemaInequivalence",
                "exactBooleanSkeleton": True,
                "distinctAtoms": True,
                "rootInfluence": True,
                "separatorVerified": True,
                "contractScope": "abstract-propositional-schema",
            },
            "witness": {
                "sourceSkeleton": "A ↔ B",
                "candidateSkeleton": "A → B",
                "atomAHash": "1" * 64,
                "atomBHash": "2" * 64,
                "valuation": {"A": False, "B": True},
                "sourceValue": False,
                "candidateValue": True,
                "outerBinderCount": 0,
            },
            "candidateElaborates": True,
            "wholeTypeDefEq": False,
            "axioms": "none",
        }
    )


def _audit(candidate: EngineCandidate) -> EngineAudit:
    return EngineAudit.model_validate(
        {
            "schemaVersion": 1,
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
            "auditMode": "independent-root-reconstruction",
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
        PilotConfig(
            output_root=tmp_path / "output",
            mathlib_root=tmp_path / "mathlib",
            tokenizer_root=tmp_path / "tokenizer",
            inputs=_inputs(tmp_path / "placeholder"),
            selection_quotas={"train": 73, "validation": 11, "test": 12},
            enforce_storage_root=False,
        )


def test_selection_is_split_stratified_unique_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    eligible = {
        source.ancestry_id: source
        for split, count, offset in (("train", 90, 0), ("validation", 20, 100), ("test", 20, 200))
        for source in (_source(offset + index, split) for index in range(count))
    }
    monkeypatch.setattr(pilot, "_load_base_rows", lambda _config: ([], eligible))

    first = select_sources(config)
    second = select_sources(config)

    assert first == second
    assert len(first) == 96
    assert len({source.declaration for source in first}) == 96
    assert Counter(source.split for source in first) == {
        "train": 72,
        "validation": 12,
        "test": 12,
    }


def test_engine_candidate_requires_a_real_separator() -> None:
    source = _source(0, "train")
    payload = _candidate(source, "N22", "iffToImp").model_dump(mode="json", by_alias=True)
    payload["witness"]["candidateValue"] = False

    with pytest.raises(ValidationError, match="does not separate"):
        EngineCandidate.model_validate(payload)


def test_candidate_choice_is_one_per_declaration_and_n22_prioritized() -> None:
    sources = tuple(_source(index, "train") for index in range(96))
    available = tuple(
        candidate
        for source in sources
        for candidate in (
            _candidate(source, "N21", "negateLeft:iff"),
            _candidate(source, "N21", "negateRight:iff"),
            _candidate(source, "N22", "iffToImp"),
        )
    )

    chosen = choose_candidates(available, sources)

    counts = Counter(row.family for row in chosen)
    assert len(chosen) == len(sources)
    assert len({row.declaration for row in chosen}) == len(sources)
    assert counts["N22"] >= 0.6 * len(chosen)
    assert counts["N21"] <= 0.4 * len(chosen)


def test_drivers_embed_only_the_typed_engine_and_exact_audit_keys(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _source(0, "train")
    candidate = _candidate(source, "N22", "iffToImp")

    primary = render_primary_driver(config, tmp_path / "names.txt")
    audit = render_audit_driver(config, [candidate])

    assert "lfNegativeSkeletonBatch" in primary
    assert str(tmp_path / "names.txt") in primary
    assert "lfAuditNegativeSkeleton" in audit
    assert candidate.declaration in audit
    assert candidate.candidate_type_hash in audit
    assert "final_test" not in primary + audit


def test_summary_requires_both_canary_gates_and_template_diversity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sources = tuple(
        _source(index, "train" if index < 16 else "validation" if index < 20 else "test")
        for index in range(24)
    )
    candidates = tuple(
        _candidate(source, "N22" if index < 15 else "N21", f"op-{index % 4}")
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
                source="typed_negative_skeleton_v1",
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
        _canary(0.70, 0.69, target_met=True),
    )

    assert summary["pilot_gate_passed"] is True
    assert summary["decision"] == {
        "scale_authorized": True,
        "training_authorized": False,
        "rebuild_required_before_training": True,
        "final_test_accessed": False,
    }

    failed = pilot._summary(
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
        _canary(0.78, 0.80, target_met=False),
    )
    assert failed["pilot_gate_passed"] is False
    assert cast(dict[str, object], failed["decision"])["scale_authorized"] is False
