"""Offline contract tests for the bounded S1 public-repair smoke."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.corpus2.build_v1 import PROVENANCE_FIELDS, TRAINER_FIELDS
from leanfaith.corpus2.s1_public_repair import (
    FrozenInput,
    MetaPoolCounts,
    PublicBaselineCounts,
    S1PublicRepairConfig,
    S1PublicRepairError,
    convert_one_verified_meta,
    materialize_smoke,
    production_config,
    verify_smoke,
)


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in rows))


def _binding(path: Path) -> FrozenInput:
    return FrozenInput(path=path, sha256=hash_file(path))


def _trainer_row(index: int, label: bool) -> dict[str, Any]:
    return {
        "record_id": f"base-{index}",
        "reference_headless": f"(n : Nat) : n + {index} = n + {index}",
        "candidate_headless": f"(n : Nat) : {index} + n = {index} + n",
        "label": label,
        "group_key": f"base-group-{index}",
        "family": f"base-family-{index}",
        "source": "fixture",
        "weight": 1.0,
    }


def _provenance_row(trainer: dict[str, Any], split: str, *, d3: bool = False) -> dict[str, Any]:
    index = cast_record_index(trainer["record_id"])
    row = {
        "schema_version": 1,
        "record_id": trainer["record_id"],
        "pair_id": f"pair-{index}",
        "pair_key": [f"hash-a-{index}", f"hash-b-{index}"],
        "reference_sha256": f"{index + 1:064x}",
        "candidate_sha256": f"{index + 2:064x}",
        "label": trainer["label"],
        "group_key": trainer["group_key"],
        "split": split,
        "split_group_ids": [trainer["group_key"]],
        "component_group_ids": [trainer["group_key"]],
        "component_statement_near_hashes": [f"hash-a-{index}", f"hash-b-{index}"],
        "family_ids": [trainer["family"]],
        "origin_ids": [f"origin-{index}"],
        "source_kinds": ["d3_codex_scale_v1" if d3 else "fixture"],
        "provenance_ids": [f"provenance-{index}"],
        "forward_tokens": 10,
        "reverse_tokens": 10,
        "private_source_content": False,
        "redistribution_allowed": True,
        "external_transmission_allowed": True,
        "release_eligible": True,
    }
    assert set(row) == PROVENANCE_FIELDS
    return row


def cast_record_index(record_id: object) -> int:
    assert isinstance(record_id, str)
    return int(record_id.rsplit("-", 1)[1])


def _meta_candidate() -> dict[str, Any]:
    source = "(n : Nat) : n = n"
    candidate = "(n : Nat) : Eq n n"
    return {
        "schemaVersion": 2,
        "kind": "candidate",
        "recordKind": "candidate",
        "status": "ok",
        "declaration": "Fixture.reflexive",
        "family": "P20",
        "evidenceClass": "P-DEF",
        "operation": "unfold:Eq",
        "operationKind": "unfold",
        "sitePath": "/1",
        "source": source,
        "sourcePretty": source,
        "candidate": candidate,
        "candidatePretty": candidate,
        "sourceTypeHash": sha256_hex(source.encode()),
        "candidateTypeHash": sha256_hex(candidate.encode()),
        "candidateElaborates": True,
        "wholeTypeDefEq": True,
        "axioms": "none",
        "evidence": {"relation": "definitionalEquality"},
    }


def _meta_audit(candidate: dict[str, Any], *, verified: bool = True) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "kind": "audit",
        "recordKind": "audit",
        "declaration": candidate["declaration"],
        "family": candidate["family"],
        "operation": candidate["operation"],
        "sitePath": candidate["sitePath"],
        "expectedCandidateTypeHash": candidate["candidateTypeHash"],
        "actualCandidateTypeHash": candidate["candidateTypeHash"],
        "verified": verified,
        "inverseFoldVerified": True,
        "status": "verified",
        "reason": "verified",
        "auditMode": "independent-site-reconstruction",
    }


def _fixture_config(tmp_path: Path, *, audit_verified: bool = True) -> S1PublicRepairConfig:
    source = tmp_path / "source"
    source.mkdir()
    split_rows = {
        "train": [_trainer_row(0, True)],
        "validation": [_trainer_row(1, False)],
        "test": [_trainer_row(2, True)],
    }
    record_paths: dict[str, Path] = {}
    for split, rows in split_rows.items():
        path = source / f"records_{split}_v1.jsonl"
        _write_jsonl(path, rows)
        record_paths[split] = path
    provenance_path = source / "provenance_v1.jsonl"
    _write_jsonl(
        provenance_path,
        [
            _provenance_row(split_rows["train"][0], "train", d3=True),
            _provenance_row(split_rows["validation"][0], "validation"),
            _provenance_row(split_rows["test"][0], "test"),
        ],
    )
    corpus_outputs = {
        "provenance_v1.jsonl": {
            "path": str(provenance_path),
            "sha256": hash_file(provenance_path),
        },
        **{
            f"records_{split}_v1.jsonl": {
                "path": str(path),
                "sha256": hash_file(path),
            }
            for split, path in record_paths.items()
        },
    }
    corpus_manifest_path = source / "corpus_v1_manifest.json"
    _write_json(
        corpus_manifest_path,
        {"schema_version": 1, "status": "completed", "outputs": corpus_outputs},
    )

    blocklist_path = source / "golden_blocklist_v1.json"
    _write_json(
        blocklist_path,
        {"version": ["golden_blocklist_v1"], "near_dup_hashes": [], "group_keys": []},
    )
    candidate = _meta_candidate()
    audit = _meta_audit(candidate, verified=audit_verified)
    candidates_path = source / "lean.stdout.jsonl"
    audits_path = source / "audit.stdout.jsonl"
    names_path = source / "declaration_names.txt"
    summary_path = source / "summary.json"
    _write_jsonl(candidates_path, [candidate])
    _write_jsonl(audits_path, [audit])
    names_path.write_text("Fixture.reflexive\n", encoding="utf-8")
    summary = {
        "total_candidate_count": 1,
        "selected_declaration_count": 1,
        "successful_declaration_count": 1,
        "per_family_counts": {"P20": 1},
        "independent_audit": {"verified_count": 1},
    }
    _write_json(summary_path, summary)
    meta_outputs = {
        "lean.stdout.jsonl": {
            "path": str(candidates_path),
            "sha256": hash_file(candidates_path),
        },
        "audit.stdout.jsonl": {
            "path": str(audits_path),
            "sha256": hash_file(audits_path),
        },
        "declaration_names.txt": {"path": str(names_path), "sha256": hash_file(names_path)},
        "summary.json": {"path": str(summary_path), "sha256": hash_file(summary_path)},
    }
    meta_manifest_path = source / "manifest.json"
    privacy = {
        "public_only": True,
        "private_source_content": False,
        "external_transmission": False,
    }
    _write_json(
        meta_manifest_path,
        {
            "schema_version": 2,
            "method_version": "meta_engine_slice2_yield_probe_v4",
            "status": "completed",
            "privacy": privacy,
            "config": privacy,
            "selection": {"selected_names_sha256": hash_file(names_path)},
            "summary": summary,
            "outputs": meta_outputs,
        },
    )
    inputs = {
        "corpus_manifest": _binding(corpus_manifest_path),
        "corpus_provenance": _binding(provenance_path),
        "corpus_train": _binding(record_paths["train"]),
        "corpus_validation": _binding(record_paths["validation"]),
        "corpus_test": _binding(record_paths["test"]),
        "golden_blocklist": _binding(blocklist_path),
        "meta_manifest": _binding(meta_manifest_path),
        "meta_summary": _binding(summary_path),
        "meta_candidates": _binding(candidates_path),
        "meta_audits": _binding(audits_path),
        "meta_declaration_names": _binding(names_path),
    }
    return S1PublicRepairConfig(
        output_root=tmp_path / "smoke",
        inputs=inputs,
        public_baseline=PublicBaselineCounts(
            total=3,
            positive=2,
            negative=1,
            train=1,
            validation=1,
            test=1,
            d3_rows=1,
        ),
        meta_pool=MetaPoolCounts(
            candidates=1,
            audited=1,
            selected_declarations=1,
            successful_declarations=1,
            family_counts={"P20": 1},
        ),
        enforce_storage_root=False,
    )


def test_one_row_smoke_materializes_and_replays(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)
    manifest = materialize_smoke(config)

    assert verify_smoke(config) == manifest
    assert manifest["counts"] == {
        "provenance_records": 1,
        "trainer_records": 1,
        "verified_audits": 1,
    }
    assert manifest["execution"] == {
        "external_calls": False,
        "final_test_accessed": False,
        "lean_reexecution": False,
    }
    trainer = json.loads((config.output_root / "trainer_record.jsonl").read_text())
    provenance = json.loads((config.output_root / "provenance.jsonl").read_text())
    assert set(trainer) == TRAINER_FIELDS
    assert trainer["label"] is True
    assert provenance["audit_verified"] is True
    assert provenance["private_source_content"] is False


def test_converter_retains_public_policy_and_declaration_ancestry(tmp_path: Path) -> None:
    admission = convert_one_verified_meta(_fixture_config(tmp_path))

    assert admission.corpus_candidate.private_source_content is False
    assert admission.corpus_candidate.release_eligible is True
    assert admission.corpus_candidate.external_transmission_allowed is False
    assert admission.corpus_candidate.split_group_ids[0].startswith("mathlib-declaration:")
    assert admission.trainer_record.group_key == admission.corpus_candidate.split_group_ids[0]
    assert admission.provenance.candidate_key[-1] == admission.provenance.candidate_sha256


def test_unverified_audit_fails_closed(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path, audit_verified=False)

    with pytest.raises(S1PublicRepairError, match="invalid Meta audit"):
        convert_one_verified_meta(config)


def test_public_baseline_d3_count_is_part_of_contract(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)
    config = config.model_copy(
        update={
            "public_baseline": config.public_baseline.model_copy(update={"d3_rows": 0}),
        }
    )

    with pytest.raises(S1PublicRepairError, match="public corpus-v1 projection differs"):
        convert_one_verified_meta(config)


def test_smoke_tampering_is_detected(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)
    materialize_smoke(config)
    (config.output_root / "trainer_record.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(S1PublicRepairError, match="smoke output differs"):
        verify_smoke(config)


def test_production_contract_freezes_measured_counts_and_caps() -> None:
    config = production_config(Path("/storage/milikic/leanfaith/corpus2/contract-test"))

    assert config.public_baseline.model_dump() == {
        "total": 9585,
        "positive": 2351,
        "negative": 7234,
        "train": 7645,
        "validation": 942,
        "test": 998,
        "d3_rows": 146,
    }
    assert config.meta_pool.candidates == config.meta_pool.audited == 16138
    assert config.caps.family_percent == 8
    assert config.caps.direct_per_source_ancestry == 4
