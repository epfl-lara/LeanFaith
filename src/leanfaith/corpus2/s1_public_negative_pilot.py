"""Kernel-check one source-matched public N19 negative certificate.

This is deliberately a one-declaration pilot.  It consumes the frozen positive
S1 repair smoke, negates that exact public mathlib claim, and asks Lean to check
both the original theorem certificate and the refutation of the negated
candidate.  It does not estimate corpus-level lexical-canary performance and
cannot authorize a scale run or training.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.corpus2.build_v1 import TRAINER_FIELDS
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.train2.trainer import TrainingRecord

METHOD_VERSION: Literal["s1_public_negative_n19_smoke_v1"] = "s1_public_negative_n19_smoke_v1"
SOURCE_DECLARATION = "MeasureTheory.IsFundamentalDomain.measure_ne_zero"
SOURCE_REVISION: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = (
    "d568c8c09630de097a046763c17b9ea99f95f950"
)
EXPECTED_LAKE_VERSION = "Lake version 5.0.0-src+fd00994 (Lean version 4.31.0-rc1)"
FAMILY: Literal["N19"] = "N19"
EVIDENCE_CLASS: Literal["N-PROOF"] = "N-PROOF"
SOURCE_CERTIFICATE_THEOREM = "leanfaith_n19_source_certificate"
CANDIDATE_REFUTATION_THEOREM = "leanfaith_n19_candidate_refutation"

_SOURCE_SMOKE_ROOT = Path(
    "/storage/milikic/leanfaith/corpus2/s1_public_repair_smoke_v1_22386b7_9e2425f"
)
_MATHLIB_ROOT = Path("/storage/milikic/leanfaith/mathlib4")
_BLOCKLIST_PATH = Path("/localhome/milikic/LeanFaith/data/benchmarks/golden_blocklist_v1.json")
_INPUT_NAMES = frozenset(
    {
        "source_manifest",
        "source_trainer",
        "source_provenance",
        "golden_blocklist",
        "lean_toolchain",
        "lake_manifest",
    }
)
_PRODUCTION_HASHES = {
    "source_manifest": "32f825b94d77ad578372537dfdc45a10c8a9dfbdeaeb9559ace3ae6687feaf49",
    "source_trainer": "9a8c712a53626baacac2d2abe54138b4d0990f044825a05bf389c3b7240d4c0a",
    "source_provenance": "ca81836dd7ff0a132c4ed04f7593575a9335b13294c7731007c4f30211bf10bc",
    "golden_blocklist": "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7",
    "lean_toolchain": "33cbab0d3ba76bdf58d9f3638748f12cb9e3befb1336b223ddbd3567589a09e8",
    "lake_manifest": "a57d555a62046897b995eb353f8667a96d87352a30874023937af39ea3b6b36b",
}
_OUTPUT_NAMES = frozenset(
    {
        "driver.lean",
        "lean.stdout.txt",
        "lean.stderr.txt",
        "process.json",
        "trainer_record.jsonl",
        "certificate.jsonl",
        "manifest.json",
    }
)
_FORBIDDEN_LEAN_EVIDENCE = re.compile(
    r"\b(?:sorry|admit|native_decide)\b|sorryAx|Lean\.ofReduceBool",
    flags=re.IGNORECASE,
)


class S1PublicNegativePilotError(RuntimeError):
    """The frozen source, Lean environment, certificate, or artifact failed closed."""


class FrozenInput(BaseModel):
    """One exact immutable pilot input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class S1PublicNegativePilotConfig(BaseModel):
    """Complete one-declaration N19 pilot contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_negative_n19_smoke_v1"] = METHOD_VERSION
    output_root: Path
    mathlib_root: Path
    mathlib_revision: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = SOURCE_REVISION
    expected_lake_version: str = Field(default=EXPECTED_LAKE_VERSION, min_length=1)
    timeout_seconds: int = Field(default=120, ge=1, le=300, strict=True)
    inputs: dict[str, FrozenInput]
    enforce_storage_root: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if set(self.inputs) != _INPUT_NAMES:
            raise ValueError("negative pilot must bind the exact frozen input set")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("negative pilot artifacts must be under /storage/milikic")
        return self


@dataclass(frozen=True, slots=True)
class LeanCompileResult:
    """Captured result of one bounded Lean invocation."""

    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool
    mathlib_revision: str
    lake_version: str
    mathlib_clean: bool


class LeanExecutor(Protocol):
    """Injectable one-file Lean executor used by the offline tests."""

    def run(self, driver_path: Path, config: S1PublicNegativePilotConfig) -> LeanCompileResult:
        """Compile ``driver_path`` under the exact configured mathlib checkout."""


class SubprocessLeanExecutor:
    """Execute one Lean driver after verifying the pinned local checkout."""

    @staticmethod
    def _checked(command: Sequence[str], *, cwd: Path) -> str:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise S1PublicNegativePilotError(
                f"cannot verify Lean checkout with {command[0]!r}: {exc}"
            ) from exc
        return completed.stdout.strip()

    def run(self, driver_path: Path, config: S1PublicNegativePilotConfig) -> LeanCompileResult:
        revision = self._checked(("git", "rev-parse", "HEAD"), cwd=config.mathlib_root)
        status = self._checked(("git", "status", "--porcelain"), cwd=config.mathlib_root)
        lake_version = self._checked(("lake", "--version"), cwd=config.mathlib_root)
        if revision != config.mathlib_revision:
            raise S1PublicNegativePilotError("mathlib revision differs from the frozen pilot")
        if status:
            raise S1PublicNegativePilotError("mathlib checkout is dirty")
        if lake_version != config.expected_lake_version:
            raise S1PublicNegativePilotError("Lean/Lake version differs from the frozen pilot")

        started = time.monotonic()
        try:
            completed = subprocess.run(
                ("lake", "env", "lean", str(driver_path)),
                cwd=config.mathlib_root,
                check=False,
                capture_output=True,
                timeout=config.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
            stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            return LeanCompileResult(
                exit_code=124,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
                timed_out=True,
                mathlib_revision=revision,
                lake_version=lake_version,
                mathlib_clean=True,
            )
        except OSError as exc:
            raise S1PublicNegativePilotError(f"cannot launch Lean: {exc}") from exc
        return LeanCompileResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=False,
            mathlib_revision=revision,
            lake_version=lake_version,
            mathlib_clean=True,
        )


@dataclass(frozen=True, slots=True)
class NegativeAdmission:
    """Exact trainer and certificate projection derived from the source smoke."""

    trainer_record: TrainingRecord
    certificate: dict[str, object]
    driver: str


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise S1PublicNegativePilotError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise S1PublicNegativePilotError(f"expected a JSON object: {path}")
    return cast(dict[str, Any], raw)


def _read_one_jsonl(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise S1PublicNegativePilotError(f"cannot read JSONL {path}: {exc}") from exc
    if len(lines) != 1 or not lines[0]:
        raise S1PublicNegativePilotError(f"expected exactly one JSONL row: {path}")
    try:
        raw = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise S1PublicNegativePilotError(f"invalid JSONL row {path}: {exc}") from exc
    if not isinstance(raw, dict) or _canonical_line(raw) != path.read_bytes():
        raise S1PublicNegativePilotError(f"JSONL row is not a canonical object: {path}")
    return cast(dict[str, Any], raw)


def production_config(output_root: Path) -> S1PublicNegativePilotConfig:
    """Return the exact production one-declaration contract."""

    paths = {
        "source_manifest": _SOURCE_SMOKE_ROOT / "manifest.json",
        "source_trainer": _SOURCE_SMOKE_ROOT / "trainer_record.jsonl",
        "source_provenance": _SOURCE_SMOKE_ROOT / "provenance.jsonl",
        "golden_blocklist": _BLOCKLIST_PATH,
        "lean_toolchain": _MATHLIB_ROOT / "lean-toolchain",
        "lake_manifest": _MATHLIB_ROOT / "lake-manifest.json",
    }
    return S1PublicNegativePilotConfig(
        output_root=output_root,
        mathlib_root=_MATHLIB_ROOT,
        inputs={
            name: FrozenInput(path=path, sha256=_PRODUCTION_HASHES[name])
            for name, path in paths.items()
        },
    )


def verify_input_bindings(config: S1PublicNegativePilotConfig) -> None:
    """Require every source and toolchain file to retain its frozen hash."""

    for name, binding in sorted(config.inputs.items()):
        if binding.path.is_symlink() or not binding.path.is_file():
            raise S1PublicNegativePilotError(f"unsafe or missing frozen input: {name}")
        if hash_file(binding.path) != binding.sha256:
            raise S1PublicNegativePilotError(f"frozen input hash differs: {name}")


def _load_source(config: S1PublicNegativePilotConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    verify_input_bindings(config)
    manifest = _read_json(config.inputs["source_manifest"].path)
    trainer = _read_one_jsonl(config.inputs["source_trainer"].path)
    provenance = _read_one_jsonl(config.inputs["source_provenance"].path)
    if (
        manifest.get("status") != "completed"
        or manifest.get("privacy")
        != {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        }
        or manifest.get("execution")
        != {
            "lean_reexecution": False,
            "external_calls": False,
            "final_test_accessed": False,
        }
    ):
        raise S1PublicNegativePilotError("source smoke completion/privacy contract differs")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise S1PublicNegativePilotError("source smoke output bindings are missing")
    expected_outputs = {
        "trainer_record.jsonl": config.inputs["source_trainer"].sha256,
        "provenance.jsonl": config.inputs["source_provenance"].sha256,
    }
    for name, expected_hash in expected_outputs.items():
        binding = outputs.get(name)
        if not isinstance(binding, Mapping) or binding.get("sha256") != expected_hash:
            raise S1PublicNegativePilotError(f"source smoke output binding differs: {name}")
    if set(trainer) != TRAINER_FIELDS or trainer.get("label") is not True:
        raise S1PublicNegativePilotError("source trainer row is not an exact positive record")
    if (
        provenance.get("declaration") != SOURCE_DECLARATION
        or provenance.get("source_revision") != config.mathlib_revision
        or provenance.get("record_id") != trainer.get("record_id")
        or provenance.get("reference_sha256")
        != sha256_hex(cast(str, trainer.get("reference_headless")).encode())
        or provenance.get("private_source_content") is not False
        or provenance.get("redistribution_allowed") is not True
        or provenance.get("release_eligible") is not True
    ):
        raise S1PublicNegativePilotError("source provenance does not bind the public theorem row")
    split_groups = provenance.get("split_group_ids")
    if split_groups != [trainer.get("group_key")]:
        raise S1PublicNegativePilotError("source ancestry group differs from trainer row")
    return trainer, provenance


def render_driver(reference_headless: str) -> str:
    """Render a proof-complete driver for ``A`` and the refutation of ``not A``."""

    proposition = reference_headless.strip()
    if not proposition or _FORBIDDEN_LEAN_EVIDENCE.search(proposition):
        raise S1PublicNegativePilotError("unsafe or empty source proposition")
    return (
        "import Mathlib\n\n"
        "universe u_0 u_1\n"
        "set_option autoImplicit false\n\n"
        f"theorem {SOURCE_CERTIFICATE_THEOREM} :\n"
        f"  {proposition} := by\n"
        f"  exact {SOURCE_DECLARATION}\n\n"
        f"theorem {CANDIDATE_REFUTATION_THEOREM} :\n"
        f"  ¬ (¬ ({proposition})) := by\n"
        "  intro candidate\n"
        f"  exact candidate {SOURCE_CERTIFICATE_THEOREM}\n\n"
        f"#print axioms {SOURCE_CERTIFICATE_THEOREM}\n"
        f"#print axioms {CANDIDATE_REFUTATION_THEOREM}\n"
    )


def build_admission(config: S1PublicNegativePilotConfig) -> NegativeAdmission:
    """Project the frozen positive source into one source-matched N19 negative."""

    source, provenance = _load_source(config)
    reference = cast(str, source["reference_headless"])
    candidate = f"¬ ({reference})"
    group_key = cast(str, source["group_key"])
    blocklist = GoldenBlocklist.load(config.inputs["golden_blocklist"].path)
    if group_key.casefold() in blocklist.group_keys:
        raise S1PublicNegativePilotError("source ancestry collides with the golden blocklist")
    if signature_near_dup_hash(reference) in blocklist.near_dup_hashes:
        raise S1PublicNegativePilotError("source statement collides with the golden blocklist")
    if signature_near_dup_hash(candidate) in blocklist.near_dup_hashes:
        raise S1PublicNegativePilotError("negative candidate collides with the golden blocklist")

    candidate_sha = sha256_hex(candidate.encode())
    record_id = "s1_public_negative_n19:" + hash_canonical(
        {
            "schema": "s1_public_negative_n19_projection_v1",
            "source_record_id": source["record_id"],
            "candidate_sha256": candidate_sha,
            "mathlib_revision": config.mathlib_revision,
        }
    )
    trainer = TrainingRecord(
        record_id=record_id,
        reference_headless=reference,
        candidate_headless=candidate,
        label=False,
        group_key=group_key,
        family=FAMILY,
        source="mathlib_nproof_n19",
        weight=1.0,
    )
    certificate: dict[str, object] = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "record_id": record_id,
        "source_record_id": source["record_id"],
        "source_declaration": SOURCE_DECLARATION,
        "source_revision": config.mathlib_revision,
        "family": FAMILY,
        "evidence_class": EVIDENCE_CLASS,
        "transformation": "whole_claim_negation",
        "reference_sha256": sha256_hex(reference.encode()),
        "candidate_sha256": candidate_sha,
        "group_key": group_key,
        "split_group_ids": provenance["split_group_ids"],
        "source_certificate_theorem": SOURCE_CERTIFICATE_THEOREM,
        "candidate_refutation_theorem": CANDIDATE_REFUTATION_THEOREM,
        "kernel_verified": True,
        "private_source_content": False,
        "redistribution_allowed": True,
        "external_transmission_allowed": False,
        "release_eligible": True,
    }
    return NegativeAdmission(
        trainer_record=trainer,
        certificate=certificate,
        driver=render_driver(reference),
    )


def _validate_compile(
    result: LeanCompileResult,
    *,
    driver: bytes,
    config: S1PublicNegativePilotConfig,
) -> None:
    combined = result.stdout + b"\n" + result.stderr
    if result.timed_out:
        raise S1PublicNegativePilotError("one-declaration Lean pilot timed out")
    if result.exit_code != 0:
        tail = combined.decode("utf-8", errors="replace")[-2000:]
        raise S1PublicNegativePilotError(f"one-declaration Lean pilot failed:\n{tail}")
    if result.mathlib_revision != config.mathlib_revision or not result.mathlib_clean:
        raise S1PublicNegativePilotError("Lean result does not bind the clean mathlib revision")
    if result.lake_version != config.expected_lake_version:
        raise S1PublicNegativePilotError("Lean result does not bind the expected toolchain")
    if _FORBIDDEN_LEAN_EVIDENCE.search(driver.decode("utf-8")):
        raise S1PublicNegativePilotError("Lean driver contains forbidden proof evidence")
    output = combined.decode("utf-8", errors="replace")
    if _FORBIDDEN_LEAN_EVIDENCE.search(output):
        raise S1PublicNegativePilotError("Lean axiom audit contains forbidden proof evidence")
    if SOURCE_CERTIFICATE_THEOREM not in output or CANDIDATE_REFUTATION_THEOREM not in output:
        raise S1PublicNegativePilotError("Lean axiom audit did not report both certificates")


def _process_payload(
    result: LeanCompileResult,
    config: S1PublicNegativePilotConfig,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "launcher": "lake env lean",
        "logical_driver_path": str(config.output_root / "driver.lean"),
        "cwd": str(config.mathlib_root),
        "timeout_seconds": config.timeout_seconds,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_seconds": round(result.duration_seconds, 6),
        "mathlib_revision": result.mathlib_revision,
        "lake_version": result.lake_version,
        "mathlib_clean": result.mathlib_clean,
        "stdout_sha256": sha256_hex(result.stdout),
        "stderr_sha256": sha256_hex(result.stderr),
    }


def _write_staged_payloads(
    staging: Path,
    config: S1PublicNegativePilotConfig,
    admission: NegativeAdmission,
    result: LeanCompileResult,
) -> None:
    trainer = admission.trainer_record.model_dump(mode="json")
    if set(trainer) != TRAINER_FIELDS:
        raise S1PublicNegativePilotError("negative trainer projection fields differ")
    process = _process_payload(result, config)
    payloads = {
        "driver.lean": admission.driver.encode(),
        "lean.stdout.txt": result.stdout,
        "lean.stderr.txt": result.stderr,
        "process.json": _canonical_line(process),
        "trainer_record.jsonl": _canonical_line(trainer),
        "certificate.jsonl": _canonical_line(admission.certificate),
    }
    for name, payload in payloads.items():
        path = staging / name
        path.write_bytes(payload)
        os.chmod(path, 0o600)

    manifest = {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": "completed",
        "config_sha256": hash_canonical(config.model_dump(mode="json")),
        "implementation_module_sha256": hash_file(Path(__file__)),
        "source_declaration": SOURCE_DECLARATION,
        "source_revision": config.mathlib_revision,
        "family": FAMILY,
        "evidence_class": EVIDENCE_CLASS,
        "inputs": {
            name: {"path": str(binding.path), "sha256": binding.sha256}
            for name, binding in sorted(config.inputs.items())
        },
        "outputs": {
            name: {
                "path": str(config.output_root / name),
                "sha256": hash_file(staging / name),
            }
            for name in sorted(payloads)
        },
        "counts": {"source_declarations": 1, "trainer_records": 1, "certificates": 1},
        "privacy": {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        },
        "execution": {
            "lean_files_compiled": 1,
            "lean_exit_code": result.exit_code,
            "lean_timed_out": result.timed_out,
            "external_calls": False,
            "final_test_accessed": False,
        },
        "decision": {
            "certificate_path_passed": True,
            "yield": {"attempted": 1, "certified": 1},
            "canary_effect": "not_estimable_from_one_pair",
            "scale_authorized": False,
            "training_authorized": False,
            "next_required": "small_multi_declaration_source_matched_negative_pilot",
        },
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_bytes(_canonical_line(manifest))
    os.chmod(manifest_path, 0o600)


def verify_smoke(config: S1PublicNegativePilotConfig) -> dict[str, Any]:
    """Verify inputs, deterministic projection, process evidence, and all output hashes."""

    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise S1PublicNegativePilotError("negative pilot root must be a non-symlink directory")
    admission = build_admission(config)
    observed_names = {path.name for path in config.output_root.iterdir() if path.is_file()}
    if observed_names != _OUTPUT_NAMES:
        raise S1PublicNegativePilotError("negative pilot output file set differs")
    manifest = _read_json(config.output_root / "manifest.json")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _OUTPUT_NAMES - {"manifest.json"}:
        raise S1PublicNegativePilotError("negative pilot manifest output set differs")
    for name, raw_binding in outputs.items():
        if not isinstance(raw_binding, Mapping):
            raise S1PublicNegativePilotError(f"invalid output binding: {name}")
        path = config.output_root / name
        if path.is_symlink() or raw_binding.get("path") != str(path):
            raise S1PublicNegativePilotError(f"unsafe output binding: {name}")
        if raw_binding.get("sha256") != hash_file(path):
            raise S1PublicNegativePilotError(f"output hash differs: {name}")
    if (config.output_root / "driver.lean").read_text(encoding="utf-8") != admission.driver:
        raise S1PublicNegativePilotError("Lean driver differs from deterministic reconstruction")
    trainer = _read_one_jsonl(config.output_root / "trainer_record.jsonl")
    if trainer != admission.trainer_record.model_dump(mode="json"):
        raise S1PublicNegativePilotError("negative trainer row differs")
    certificate = _read_one_jsonl(config.output_root / "certificate.jsonl")
    if certificate != admission.certificate:
        raise S1PublicNegativePilotError("negative certificate row differs")
    process = _read_json(config.output_root / "process.json")
    stdout = (config.output_root / "lean.stdout.txt").read_bytes()
    stderr = (config.output_root / "lean.stderr.txt").read_bytes()
    try:
        result = LeanCompileResult(
            exit_code=cast(int, process["exit_code"]),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=cast(float, process["duration_seconds"]),
            timed_out=cast(bool, process["timed_out"]),
            mathlib_revision=cast(str, process["mathlib_revision"]),
            lake_version=cast(str, process["lake_version"]),
            mathlib_clean=cast(bool, process["mathlib_clean"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise S1PublicNegativePilotError("invalid Lean process record") from exc
    if process.get("stdout_sha256") != sha256_hex(stdout) or process.get(
        "stderr_sha256"
    ) != sha256_hex(stderr):
        raise S1PublicNegativePilotError("Lean process stream hash differs")
    _validate_compile(result, driver=admission.driver.encode(), config=config)
    if (
        manifest.get("status") != "completed"
        or manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("decision")
        != {
            "certificate_path_passed": True,
            "yield": {"attempted": 1, "certified": 1},
            "canary_effect": "not_estimable_from_one_pair",
            "scale_authorized": False,
            "training_authorized": False,
            "next_required": "small_multi_declaration_source_matched_negative_pilot",
        }
        or manifest.get("execution", {}).get("final_test_accessed") is not False
    ):
        raise S1PublicNegativePilotError("negative pilot manifest contract differs")
    return manifest


def materialize_smoke(
    config: S1PublicNegativePilotConfig,
    *,
    executor: LeanExecutor | None = None,
) -> dict[str, Any]:
    """Compile one driver and atomically emit, or idempotently verify, the smoke."""

    if config.output_root.exists():
        return verify_smoke(config)
    admission = build_admission(config)
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{config.output_root.name}.",
            suffix=".partial",
            dir=config.output_root.parent,
        )
    )
    try:
        driver_path = staging / "driver.lean"
        driver_path.write_text(admission.driver, encoding="utf-8")
        result = (executor or SubprocessLeanExecutor()).run(driver_path, config)
        _validate_compile(result, driver=admission.driver.encode(), config=config)
        _write_staged_payloads(staging, config, admission, result)
        os.replace(staging, config.output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return verify_smoke(config)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run-smoke", "verify-smoke"))
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = production_config(cast(Path, args.output_root))
    manifest = materialize_smoke(config) if args.command == "run-smoke" else verify_smoke(config)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
