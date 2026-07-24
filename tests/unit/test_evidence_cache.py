"""LF-020 evidence-cache identity, integrity, and immutability."""

from __future__ import annotations

import datetime
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file
from leanfaith.lean.cache import (
    EvidenceCache,
    EvidenceCacheConflictError,
    EvidenceCacheCorruptionError,
    EvidenceCacheKey,
    compute_evidence_cache_key_hash,
    make_evidence_cache_entry,
)
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.evidence import CounterexampleValue, EvidenceRecord, ProofValue
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    PAIR_PREFIX,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    make_id,
)

_CONFIG_HASH = "c" * 64


def _key(*, kind: EvidenceKind = EvidenceKind.PROOF_A_IMPLIES_B) -> EvidenceCacheKey:
    direction = {
        EvidenceKind.PROOF_A_IMPLIES_B: "A_to_B",
        EvidenceKind.PROOF_B_IMPLIES_A: "B_to_A",
        EvidenceKind.COUNTEREXAMPLE: "equivalence_only",
    }.get(kind, "none")
    return EvidenceCacheKey(
        pair_id=make_id(PAIR_PREFIX, {"case": "pair"}),
        theorem_a_id=make_id(THEOREM_PREFIX, {"case": "theorem-a"}),
        theorem_b_id=make_id(THEOREM_PREFIX, {"case": "theorem-b"}),
        theorem_a_statement_hash="1" * 64,
        theorem_b_statement_hash="2" * 64,
        representation_a_id=make_id(REPRESENTATION_PREFIX, {"case": "representation-a"}),
        representation_b_id=make_id(REPRESENTATION_PREFIX, {"case": "representation-b"}),
        representation_a_content_hash="3" * 64,
        representation_b_content_hash="4" * 64,
        representation_version="normalization_v1",
        context_id=f"ctx:{'a' * 64}",
        context_fingerprint="a" * 64,
        environment_schema_version=1,
        environment_hash="b" * 64,
        evidence_kind=kind,
        evidence_direction=direction,
        method_version="portfolio_v1/exact_assumption_v1",
        timeout_seconds=2.0,
        config_hash=_CONFIG_HASH,
        semantic_policy_version="semantic_policy_v1",
        semantic_policy_hash="5" * 64,
        lean_version="v4.31.0-rc1",
        lean_interact_version="0.11.4",
        repl_revision="repl-v4.31.0-rc1",
        project_revision="mathlib-revision",
    )


def _proof_evidence(
    key: EvidenceCacheKey,
    *,
    outcome: str = "not_proved",
    case: str = "proof",
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=make_id(EVIDENCE_PREFIX, {"case": case}),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=key.pair_id,
        kind=key.evidence_kind,
        status=EvidenceExecutionStatus.SUCCESS,
        value=ProofValue(
            outcome=outcome,  # type: ignore[arg-type]
            tactic="exact_assumption_v1" if outcome == "proved" else None,
        ),
        method_version=key.method_version,
        config_hash=key.config_hash,
        created_at=datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC),
    )


def _counterexample_evidence(key: EvidenceCacheKey) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=make_id(EVIDENCE_PREFIX, {"case": "counterexample"}),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=key.pair_id,
        kind=EvidenceKind.COUNTEREXAMPLE,
        status=EvidenceExecutionStatus.SUCCESS,
        value=CounterexampleValue(
            outcome="not_found",
            direction="A_to_B",
            domain="Fin 8",
            encoding="kernel_decide_v1",
        ),
        method_version=key.method_version,
        config_hash=key.config_hash,
        created_at=datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC),
    )


def test_cache_round_trip_and_clean_miss(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")
    key = _key()
    assert cache.get(key) is None

    evidence = _proof_evidence(key)
    written = cache.put(key, evidence)
    loaded = cache.get(key)

    assert loaded == written
    assert loaded is not None and loaded.evidence == evidence
    assert cache.entry_path(key).read_bytes().endswith(b"\n")
    assert cache.entry_path(key).stat().st_mode & 0o777 == 0o444
    assert not tuple(cache.entry_path(key).parent.glob("*.partial"))


def test_identical_write_is_idempotent_but_different_write_conflicts(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")
    key = _key()
    first = _proof_evidence(key)
    assert cache.put(key, first) == cache.put(key, first)

    second = _proof_evidence(key, outcome="proved", case="different-proof-result")
    with pytest.raises(EvidenceCacheConflictError, match="different evidence"):
        cache.put(key, second)
    assert cache.get(key) is not None
    assert cache.get(key).evidence == first  # type: ignore[union-attr]


def test_concurrent_identical_writers_install_one_complete_entry(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")
    key = _key()
    evidence = _proof_evidence(key)
    with ThreadPoolExecutor(max_workers=8) as executor:
        entries = list(executor.map(lambda _index: cache.put(key, evidence), range(32)))

    assert all(entry == entries[0] for entry in entries)
    assert cache.get(key) == entries[0]
    assert len(tuple(cache.entry_path(key).parent.glob("*.json"))) == 1
    assert not tuple(cache.entry_path(key).parent.glob("*.partial"))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("pair_id", make_id(PAIR_PREFIX, {"changed": "pair"})),
        ("theorem_a_id", make_id(THEOREM_PREFIX, {"changed": "theorem"})),
        ("theorem_a_statement_hash", "6" * 64),
        ("theorem_b_statement_hash", "7" * 64),
        ("representation_a_id", make_id(REPRESENTATION_PREFIX, {"changed": "representation"})),
        ("representation_a_content_hash", "8" * 64),
        ("representation_b_content_hash", "9" * 64),
        ("representation_version", "normalization_v2"),
        ("context_id", f"ctx:{'d' * 64}"),
        ("context_fingerprint", "d" * 64),
        ("environment_schema_version", 2),
        ("environment_hash", "e" * 64),
        ("evidence_kind", EvidenceKind.PROOF_B_IMPLIES_A),
        ("evidence_direction", "B_to_A"),
        ("method_version", "portfolio_v2/exact_assumption_v2"),
        ("timeout_seconds", 3.0),
        ("config_hash", "f" * 64),
        ("semantic_policy_version", "semantic_policy_v2"),
        ("semantic_policy_hash", "0" * 64),
        ("lean_version", "v4.31.0"),
        ("lean_interact_version", "0.11.5"),
        ("repl_revision", "different-repl"),
        ("project_revision", "different-project"),
    ],
)
def test_every_required_semantic_input_fragments_cache_key(
    field: str,
    replacement: object,
) -> None:
    key = _key()
    changed = key.model_copy(update={field: replacement})
    assert compute_evidence_cache_key_hash(changed) != compute_evidence_cache_key_hash(key)


def test_cache_entry_rejects_lineage_mismatch() -> None:
    key = _key()
    wrong_target = _proof_evidence(key).model_copy(
        update={"target_id": make_id(PAIR_PREFIX, {"wrong": "pair"})}
    )
    with pytest.raises(ValidationError, match="does not match cache pair"):
        make_evidence_cache_entry(key, wrong_target)

    wrong_method = _proof_evidence(key).model_copy(update={"method_version": "other"})
    with pytest.raises(ValidationError, match="does not match cache method"):
        make_evidence_cache_entry(key, wrong_method)


def test_tampered_evidence_hash_fails_closed(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")
    key = _key()
    evidence = _proof_evidence(key)
    cache.put(key, evidence)
    path = cache.entry_path(key)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b"not_proved", b"proved"))

    with pytest.raises(EvidenceCacheCorruptionError, match="evidence_hash"):
        cache.get(key)
    with pytest.raises(EvidenceCacheCorruptionError):
        cache.put(key, evidence)


def test_noncanonical_or_duplicate_json_fails_closed(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")
    key = _key()
    cache.put(key, _proof_evidence(key))
    path = cache.entry_path(key)
    original = path.read_bytes()

    path.chmod(0o644)
    path.write_bytes(b" " + original)
    with pytest.raises(EvidenceCacheCorruptionError, match="canonical immutable encoding"):
        cache.get(key)

    path.write_bytes(
        original.replace(
            b'{"artifact_hashes":{}',
            b'{"artifact_hashes":{},"artifact_hashes":{}',
            1,
        )
    )
    with pytest.raises(EvidenceCacheCorruptionError, match="duplicate JSON key"):
        cache.get(key)


def test_symlink_entry_fails_closed(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")
    key = _key()
    path = cache.entry_path(key)
    path.parent.mkdir(parents=True)
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    os.symlink(target, path)

    with pytest.raises(EvidenceCacheCorruptionError, match="non-symlink"):
        cache.get(key)


def test_negative_search_outcomes_remain_evidence_not_labels(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")

    proof_key = _key()
    proof_entry = cache.put(proof_key, _proof_evidence(proof_key))
    assert isinstance(proof_entry.evidence.value, ProofValue)
    assert proof_entry.evidence.value.outcome == "not_proved"

    counterexample_key = _key(kind=EvidenceKind.COUNTEREXAMPLE).model_copy(
        update={"method_version": "counterexample_v1/kernel_decide_v1"}
    )
    counterexample_entry = cache.put(
        counterexample_key,
        _counterexample_evidence(counterexample_key),
    )
    assert isinstance(counterexample_entry.evidence.value, CounterexampleValue)
    assert counterexample_entry.evidence.value.outcome == "not_found"
    assert "label" not in counterexample_entry.model_dump(mode="json")


def test_execution_failure_is_cacheable_without_semantic_value(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")
    key = _key()
    timeout = EvidenceRecord(
        evidence_id=make_id(EVIDENCE_PREFIX, {"case": "timeout"}),
        target_kind=EvidenceTargetKind.LEAN_PAIR,
        target_id=key.pair_id,
        kind=key.evidence_kind,
        status=EvidenceExecutionStatus.TIMEOUT,
        value=None,
        method_version=key.method_version,
        config_hash=key.config_hash,
        created_at=datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC),
    )
    assert cache.put(key, timeout).evidence.status == EvidenceExecutionStatus.TIMEOUT


def test_collection_time_and_operational_metadata_do_not_conflict(tmp_path: Path) -> None:
    cache = EvidenceCache(tmp_path / "cache")
    key = _key()
    first = _proof_evidence(key).model_copy(
        update={"metadata": {"cache_hit": False, "run_id": "run-a"}}
    )
    second = first.model_copy(
        update={
            "created_at": datetime.datetime(2026, 7, 24, tzinfo=datetime.UTC),
            "metadata": {"cache_hit": True, "run_id": "run-b"},
        }
    )

    written = cache.put(key, first)
    assert cache.put(key, second) == written
    assert cache.get(key) == written


def test_referenced_artifact_must_be_bound_and_validated_on_put(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifact = artifact_root / "proof.json"
    artifact.write_text('{"result":"not_proved"}\n', encoding="utf-8")

    cache = EvidenceCache(tmp_path / "cache", artifact_root=artifact_root)
    key = _key()
    evidence = _proof_evidence(key).model_copy(update={"raw_artifact": "proof.json"})

    with pytest.raises(ValidationError, match="not content-bound"):
        make_evidence_cache_entry(key, evidence)
    with pytest.raises(EvidenceCacheCorruptionError, match="does not match"):
        cache.put(key, evidence, artifact_hashes={"proof.json": "0" * 64})

    entry = cache.put(
        key,
        evidence,
        artifact_hashes={"proof.json": hash_file(artifact)},
    )
    assert cache.get(key) == entry

    artifact.write_text('{"result":"tampered"}\n', encoding="utf-8")
    with pytest.raises(EvidenceCacheCorruptionError, match="does not match"):
        cache.get(key)


def test_relative_artifact_path_cannot_escape_artifact_root() -> None:
    key = _key()
    evidence = _proof_evidence(key).model_copy(update={"raw_artifact": "../proof.json"})
    with pytest.raises(ValidationError, match="may not escape"):
        make_evidence_cache_entry(
            key,
            evidence,
            artifact_hashes={"../proof.json": "0" * 64},
        )
