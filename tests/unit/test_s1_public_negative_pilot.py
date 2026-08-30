"""Offline tests for the one-declaration public N19 certificate pilot."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.corpus2.s1_public_negative_pilot import (
    CANDIDATE_REFUTATION_THEOREM,
    EXPECTED_LAKE_VERSION,
    SOURCE_CERTIFICATE_THEOREM,
    SOURCE_DECLARATION,
    SOURCE_REVISION,
    FrozenInput,
    LeanCompileResult,
    S1PublicNegativePilotConfig,
    S1PublicNegativePilotError,
    build_admission,
    materialize_smoke,
    render_driver,
    verify_smoke,
)
from leanfaith.representations.views import signature_near_dup_hash


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _write_jsonl(path: Path, value: object) -> None:
    _write_json(path, value)


def _binding(path: Path) -> FrozenInput:
    return FrozenInput(path=path, sha256=hash_file(path))


def _fixture_config(
    tmp_path: Path,
    *,
    blocked_hash: str | None = None,
) -> S1PublicNegativePilotConfig:
    source_root = tmp_path / "source"
    source_root.mkdir()
    reference = "(n : Nat) : n = n"
    group_key = "mathlib-declaration:fixture"
    source_trainer = {
        "record_id": "source-positive",
        "reference_headless": reference,
        "candidate_headless": "(n : Nat) : Eq n n",
        "label": True,
        "group_key": group_key,
        "family": "P20",
        "source": "meta_engine_slice2",
        "weight": 1.0,
    }
    source_provenance = {
        "schema_version": 1,
        "record_id": source_trainer["record_id"],
        "declaration": SOURCE_DECLARATION,
        "source_revision": SOURCE_REVISION,
        "reference_sha256": sha256_hex(reference.encode()),
        "split_group_ids": [group_key],
        "private_source_content": False,
        "redistribution_allowed": True,
        "release_eligible": True,
    }
    trainer_path = source_root / "trainer_record.jsonl"
    provenance_path = source_root / "provenance.jsonl"
    _write_jsonl(trainer_path, source_trainer)
    _write_jsonl(provenance_path, source_provenance)
    manifest_path = source_root / "manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": 1,
            "status": "completed",
            "outputs": {
                "trainer_record.jsonl": {
                    "path": str(trainer_path),
                    "sha256": hash_file(trainer_path),
                },
                "provenance.jsonl": {
                    "path": str(provenance_path),
                    "sha256": hash_file(provenance_path),
                },
            },
            "privacy": {
                "public_only": True,
                "private_source_content": False,
                "external_transmission": False,
            },
            "execution": {
                "lean_reexecution": False,
                "external_calls": False,
                "final_test_accessed": False,
            },
        },
    )
    blocklist_path = source_root / "golden_blocklist_v1.json"
    _write_json(
        blocklist_path,
        {
            "version": ["golden_blocklist_v1"],
            "near_dup_hashes": [blocked_hash] if blocked_hash is not None else [],
            "group_keys": [],
        },
    )
    mathlib_root = tmp_path / "mathlib"
    mathlib_root.mkdir()
    toolchain_path = mathlib_root / "lean-toolchain"
    lake_manifest_path = mathlib_root / "lake-manifest.json"
    toolchain_path.write_text("leanprover/lean4:test\n", encoding="utf-8")
    _write_json(lake_manifest_path, {"version": "fixture"})
    return S1PublicNegativePilotConfig(
        output_root=tmp_path / "pilot",
        mathlib_root=mathlib_root,
        enforce_storage_root=False,
        inputs={
            "source_manifest": _binding(manifest_path),
            "source_trainer": _binding(trainer_path),
            "source_provenance": _binding(provenance_path),
            "golden_blocklist": _binding(blocklist_path),
            "lean_toolchain": _binding(toolchain_path),
            "lake_manifest": _binding(lake_manifest_path),
        },
    )


class _FakeExecutor:
    def __init__(self, *, exit_code: int = 0, forbidden_output: bool = False) -> None:
        self.exit_code = exit_code
        self.forbidden_output = forbidden_output
        self.calls = 0
        self.drivers: list[str] = []

    def run(
        self,
        driver_path: Path,
        config: S1PublicNegativePilotConfig,
    ) -> LeanCompileResult:
        self.calls += 1
        self.drivers.append(driver_path.read_text(encoding="utf-8"))
        output = (
            f"'{SOURCE_CERTIFICATE_THEOREM}' does not depend on any axioms\n"
            f"'{CANDIDATE_REFUTATION_THEOREM}' does not depend on any axioms\n"
        )
        if self.forbidden_output:
            output += "depends on sorryAx\n"
        return LeanCompileResult(
            exit_code=self.exit_code,
            stdout=output.encode(),
            stderr=b"fixture failure" if self.exit_code else b"",
            duration_seconds=0.125,
            timed_out=False,
            mathlib_revision=SOURCE_REVISION,
            lake_version=EXPECTED_LAKE_VERSION,
            mathlib_clean=True,
        )


def test_render_driver_is_proof_complete_and_bounded() -> None:
    driver = render_driver("(n : Nat) : n = n")

    assert f"exact {SOURCE_DECLARATION}" in driver
    assert f"exact candidate {SOURCE_CERTIFICATE_THEOREM}" in driver
    assert f"#print axioms {SOURCE_CERTIFICATE_THEOREM}" in driver
    assert f"#print axioms {CANDIDATE_REFUTATION_THEOREM}" in driver
    assert "sorry" not in driver
    assert "native_decide" not in driver


def test_build_admission_preserves_source_group_and_emits_negative(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)

    admission = build_admission(config)

    row = admission.trainer_record
    assert row.reference_headless == "(n : Nat) : n = n"
    assert row.candidate_headless == "¬ ((n : Nat) : n = n)"
    assert row.label is False
    assert row.family == "N19"
    assert row.group_key == "mathlib-declaration:fixture"
    assert admission.certificate["evidence_class"] == "N-PROOF"
    assert admission.certificate["kernel_verified"] is True


def test_materialize_is_atomic_verified_and_idempotent(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)
    executor = _FakeExecutor()

    first = materialize_smoke(config, executor=executor)
    second = materialize_smoke(config, executor=executor)

    assert first == second == verify_smoke(config)
    assert executor.calls == 1
    assert {path.name for path in config.output_root.iterdir()} == {
        "driver.lean",
        "lean.stdout.txt",
        "lean.stderr.txt",
        "process.json",
        "trainer_record.jsonl",
        "certificate.jsonl",
        "manifest.json",
    }
    assert first["decision"] == {
        "certificate_path_passed": True,
        "yield": {"attempted": 1, "certified": 1},
        "canary_effect": "not_estimable_from_one_pair",
        "scale_authorized": False,
        "training_authorized": False,
        "next_required": "small_multi_declaration_source_matched_negative_pilot",
    }


@pytest.mark.parametrize("exit_code, forbidden_output", [(1, False), (0, True)])
def test_materialize_fails_closed_without_admitting_partial_root(
    tmp_path: Path,
    exit_code: int,
    forbidden_output: bool,
) -> None:
    config = _fixture_config(tmp_path)
    executor = _FakeExecutor(exit_code=exit_code, forbidden_output=forbidden_output)

    with pytest.raises(S1PublicNegativePilotError):
        materialize_smoke(config, executor=executor)

    assert not config.output_root.exists()
    assert not tuple(config.output_root.parent.glob(f".{config.output_root.name}.*.partial"))


def test_verify_detects_output_mutation(tmp_path: Path) -> None:
    config = _fixture_config(tmp_path)
    materialize_smoke(config, executor=_FakeExecutor())
    stdout_path = config.output_root / "lean.stdout.txt"
    stdout_path.write_bytes(stdout_path.read_bytes() + b"mutated\n")

    with pytest.raises(S1PublicNegativePilotError, match="output hash differs"):
        verify_smoke(config)


def test_candidate_golden_collision_fails_before_compile(tmp_path: Path) -> None:
    candidate = "¬ ((n : Nat) : n = n)"
    config = _fixture_config(tmp_path, blocked_hash=signature_near_dup_hash(candidate))

    with pytest.raises(S1PublicNegativePilotError, match="candidate collides"):
        build_admission(config)
