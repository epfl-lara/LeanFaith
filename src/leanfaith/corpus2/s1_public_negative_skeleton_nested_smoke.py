"""Run one frozen nested-site N21/N22 generation and reconstruction smoke.

The smoke binds the exact train-split declaration selected by the failed
96-declaration root-only pilot, runs the versioned full-skeleton Lean engine,
requires the nested ``And -> Or`` candidate, and independently reconstructs
that exact candidate hash.  Both Lean stages have hard timeouts.  This smoke
does not access ``final_test`` and cannot authorize scaling or training.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex

METHOD_VERSION: Literal["s1_public_negative_skeleton_nested_smoke_v1"] = (
    "s1_public_negative_skeleton_nested_smoke_v1"
)
SOURCE_REVISION: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = (
    "d568c8c09630de097a046763c17b9ea99f95f950"
)
EXPECTED_LAKE_VERSION = "Lake version 5.0.0-src+fd00994 (Lean version 4.31.0-rc1)"
DECLARATION: Literal["NonUnitalStarSubalgebra.mem_prod"] = "NonUnitalStarSubalgebra.mem_prod"
SOURCE_SPLIT: Literal["train"] = "train"
SOURCE_SELECTION_ORDINAL: Literal[13] = 13
SOURCE_TYPE_HASH = "eab1fef19662f6be97eb8de2a067e9dd30f1fd06ae6b817ae33713a08d2c4799"
TARGET_FAMILY: Literal["N22"] = "N22"
TARGET_OPERATION_KIND: Literal["andToOr"] = "andToOr"
TARGET_SITE_PATH: Literal["/root-body/right"] = "/root-body/right"
TARGET_OPERATION = f"{TARGET_OPERATION_KIND}:{TARGET_SITE_PATH}"
ADDRESS_SPACE_BYTES = 25_769_803_776
LEAN_MEMORY_MB = 24_576

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_PATH = _REPO_ROOT / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV2.lean"
_PILOT_ROOT = Path(
    "/storage/milikic/leanfaith/corpus2/s1_public_negative_skeleton_pilot_v1_3d72e99_d568c8c"
)
_MATHLIB_ROOT = Path("/storage/milikic/leanfaith/mathlib4")
_INPUT_NAMES = frozenset(
    {
        "root_pilot_manifest",
        "root_pilot_selection",
        "negative_engine_v2",
        "lean_toolchain",
        "lake_manifest",
    }
)
_PRODUCTION_INPUTS = {
    "root_pilot_manifest": (
        _PILOT_ROOT / "manifest.json",
        "78054484ddabdf0da24988dc8651c4d194b52b66f792d3f788b39cb2a75bfa4a",
    ),
    "root_pilot_selection": (
        _PILOT_ROOT / "selection.jsonl",
        "fd02051abc4902efee3238017052fafb717aaa5c285ab0d9e28010b1838d0809",
    ),
    "lean_toolchain": (
        _MATHLIB_ROOT / "lean-toolchain",
        "33cbab0d3ba76bdf58d9f3638748f12cb9e3befb1336b223ddbd3567589a09e8",
    ),
    "lake_manifest": (
        _MATHLIB_ROOT / "lake-manifest.json",
        "a57d555a62046897b995eb353f8667a96d87352a30874023937af39ea3b6b36b",
    ),
}
_STATIC_OUTPUTS = frozenset(
    {
        "declaration_names.txt",
        "primary_driver.lean",
        "primary.stdout.jsonl",
        "primary.stderr.txt",
        "primary.process.json",
        "selected_candidate.json",
        "audit_driver.lean",
        "audit.stdout.jsonl",
        "audit.stderr.txt",
        "audit.process.json",
        "summary.json",
    }
)
_OUTPUTS = _STATIC_OUTPUTS | {"manifest.json"}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class NestedSkeletonSmokeError(RuntimeError):
    """A frozen input, Lean stage, candidate, or replay check failed closed."""


class FrozenInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SmokeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_negative_skeleton_nested_smoke_v1"] = METHOD_VERSION
    output_root: Path
    mathlib_root: Path
    inputs: dict[str, FrozenInput]
    declaration: Literal["NonUnitalStarSubalgebra.mem_prod"] = DECLARATION
    source_split: Literal["train"] = SOURCE_SPLIT
    source_selection_ordinal: Literal[13] = SOURCE_SELECTION_ORDINAL
    target_family: Literal["N22"] = TARGET_FAMILY
    target_operation_kind: Literal["andToOr"] = TARGET_OPERATION_KIND
    target_site_path: Literal["/root-body/right"] = TARGET_SITE_PATH
    timeout_seconds: int = Field(default=120, ge=1, le=300, strict=True)
    expected_lake_version: str = EXPECTED_LAKE_VERSION
    mathlib_revision: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = SOURCE_REVISION
    enforce_storage_root: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if set(self.inputs) != _INPUT_NAMES:
            raise ValueError("nested smoke must bind the exact frozen input set")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("nested smoke artifacts must be under /storage/milikic")
        return self


class EngineCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[2] = Field(alias="schemaVersion")
    kind: Literal["candidate"]
    record_kind: Literal["candidate"] = Field(alias="recordKind")
    status: Literal["ok"]
    declaration: Literal["NonUnitalStarSubalgebra.mem_prod"]
    family: Literal["N21", "N22"]
    operation: str = Field(min_length=1)
    operation_kind: str = Field(alias="operationKind", min_length=1)
    site_path: str = Field(alias="sitePath", pattern=r"^/root-body(?:/(?:left|right|not))*$")
    source: str = Field(min_length=1)
    candidate: str = Field(min_length=1)
    source_type_hash: str = Field(alias="sourceTypeHash", pattern=r"^[0-9a-f]{64}$")
    candidate_type_hash: str = Field(alias="candidateTypeHash", pattern=r"^[0-9a-f]{64}$")
    evidence_class: Literal["N-SEP"] = Field(alias="evidenceClass")
    evidence: dict[str, object]
    witness: dict[str, object]
    candidate_elaborates: StrictBool = Field(alias="candidateElaborates")
    whole_type_def_eq: StrictBool = Field(alias="wholeTypeDefEq")
    axioms: Literal["none"]

    @model_validator(mode="after")
    def _valid_evidence(self) -> Self:
        if self.source == self.candidate:
            raise ValueError("full skeleton candidate is unchanged")
        if sha256_hex(self.source.encode()) != self.source_type_hash:
            raise ValueError("full skeleton source hash differs")
        if sha256_hex(self.candidate.encode()) != self.candidate_type_hash:
            raise ValueError("full skeleton candidate hash differs")
        if not self.candidate_elaborates or self.whole_type_def_eq:
            raise ValueError("full skeleton type checks differ")
        if self.operation != f"{self.operation_kind}:{self.site_path}":
            raise ValueError("full skeleton operation/path binding differs")
        if self.evidence != {
            "relation": "schemaInequivalence",
            "exactBooleanSkeleton": True,
            "deduplicatedAtoms": True,
            "fullTruthTableEnumerated": True,
            "rootInfluence": True,
            "separatorVerified": True,
            "contractScope": "abstract-propositional-schema",
        }:
            raise ValueError("full skeleton separator contract differs")
        atom_count = self.witness.get("atomCount")
        atom_hashes = self.witness.get("atomHashes")
        valuation = self.witness.get("valuation")
        valuation_space = self.witness.get("valuationSpaceSize")
        if (
            not isinstance(atom_count, int)
            or isinstance(atom_count, bool)
            or not 1 <= atom_count <= 8
            or not isinstance(atom_hashes, list)
            or len(atom_hashes) != atom_count
            or not all(isinstance(value, str) and _HEX64.fullmatch(value) for value in atom_hashes)
            or not isinstance(valuation, list)
            or len(valuation) != atom_count
            or not all(isinstance(value, bool) for value in valuation)
            or valuation_space != 2**atom_count
        ):
            raise ValueError("full skeleton valuation inventory differs")
        if self.witness.get("sourceValue") is self.witness.get("candidateValue"):
            raise ValueError("full skeleton valuation does not separate")
        return self


class EngineAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[2] = Field(alias="schemaVersion")
    kind: Literal["audit"]
    record_kind: Literal["audit"] = Field(alias="recordKind")
    declaration: Literal["NonUnitalStarSubalgebra.mem_prod"]
    family: Literal["N22"]
    operation: Literal["andToOr:/root-body/right"]
    expected_candidate_type_hash: str = Field(alias="expectedCandidateTypeHash")
    actual_candidate_type_hash: str = Field(alias="actualCandidateTypeHash")
    verified: Literal[True]
    status: Literal["verified"]
    reason: Literal["verified"]
    audit_mode: Literal["independent-full-skeleton-reconstruction"] = Field(alias="auditMode")

    @model_validator(mode="after")
    def _same_hash(self) -> Self:
        if (
            _HEX64.fullmatch(self.expected_candidate_type_hash) is None
            or self.actual_candidate_type_hash != self.expected_candidate_type_hash
        ):
            raise ValueError("independent full-skeleton audit hash differs")
        return self


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool
    mathlib_revision: str
    lake_version: str
    mathlib_clean: bool


def _canonical_line(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NestedSkeletonSmokeError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise NestedSkeletonSmokeError(f"expected JSON object: {path}")
    return cast(dict[str, Any], raw)


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise NestedSkeletonSmokeError(f"cannot read JSONL {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise NestedSkeletonSmokeError(f"{path}:{line_number}: empty JSONL line")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NestedSkeletonSmokeError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
        if not isinstance(raw, dict) or _canonical_line(raw).rstrip(b"\n") != line:
            raise NestedSkeletonSmokeError(f"{path}:{line_number}: row is not canonical")
        yield line_number, cast(dict[str, Any], raw)


def production_config(output_root: Path) -> SmokeConfig:
    inputs = {
        name: FrozenInput(path=path, sha256=digest)
        for name, (path, digest) in _PRODUCTION_INPUTS.items()
    }
    inputs["negative_engine_v2"] = FrozenInput(path=_ENGINE_PATH, sha256=hash_file(_ENGINE_PATH))
    return SmokeConfig(
        output_root=output_root,
        mathlib_root=_MATHLIB_ROOT,
        inputs=inputs,
    )


def verify_input_bindings(config: SmokeConfig) -> None:
    for name, binding in sorted(config.inputs.items()):
        if binding.path.is_symlink() or not binding.path.is_file():
            raise NestedSkeletonSmokeError(f"unsafe or missing frozen input: {name}")
        if hash_file(binding.path) != binding.sha256:
            raise NestedSkeletonSmokeError(f"frozen input hash differs: {name}")

    pilot = _read_json(config.inputs["root_pilot_manifest"].path)
    outputs = pilot.get("outputs")
    if (
        pilot.get("status") != "completed"
        or not isinstance(outputs, Mapping)
        or not isinstance(outputs.get("selection.jsonl"), Mapping)
        or outputs["selection.jsonl"].get("sha256") != config.inputs["root_pilot_selection"].sha256
        or pilot.get("summary", {}).get("pilot_gate_passed") is not False
        or pilot.get("execution", {}).get("final_test_accessed") is not False
    ):
        raise NestedSkeletonSmokeError("root-only pilot binding differs")

    matched: list[dict[str, Any]] = []
    for _, row in _iter_jsonl(config.inputs["root_pilot_selection"].path):
        if row.get("declaration") == config.declaration:
            matched.append(row)
    if len(matched) != 1:
        raise NestedSkeletonSmokeError("nested smoke declaration selection is not unique")
    row = matched[0]
    if (
        row.get("ordinal") != config.source_selection_ordinal
        or row.get("split") != config.source_split
        or row.get("reference_sha256") != SOURCE_TYPE_HASH
    ):
        raise NestedSkeletonSmokeError("nested smoke declaration selection binding differs")


def _engine_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    imports = [line.strip() for line in lines if line.strip().startswith("import ")]
    if imports != ["import Lean"]:
        raise NestedSkeletonSmokeError("full skeleton engine imports differ")
    body = "\n".join(line for line in lines if not line.strip().startswith("import ")).strip()
    markers = (
        "namespace LeanFaith.Meta.NegativeSkeletonEngineV2Helper",
        "lfNegativeSkeletonV2Batch",
        "lfAuditNegativeSkeletonV2",
        "end LeanFaith.Meta.NegativeSkeletonEngineV2Helper",
    )
    if not all(marker in body for marker in markers):
        raise NestedSkeletonSmokeError("full skeleton engine command contract differs")
    return body + "\n"


def _lean_string(value: str) -> str:
    if any(character in value for character in ("\0", "\n", "\r")):
        raise NestedSkeletonSmokeError("Lean literal contains a control character")
    return json.dumps(value, ensure_ascii=False)


def render_primary_driver(config: SmokeConfig, names_path: Path) -> str:
    return (
        "import Mathlib\n\n"
        + _engine_body(config.inputs["negative_engine_v2"].path)
        + "\nset_option maxHeartbeats 0 in\n"
        + f"lfNegativeSkeletonV2Batch {_lean_string(str(names_path))}\n"
    )


def render_audit_driver(config: SmokeConfig, candidate: EngineCandidate) -> str:
    arguments = " ".join(
        _lean_string(value)
        for value in (
            candidate.declaration,
            candidate.family,
            candidate.operation,
            candidate.candidate_type_hash,
        )
    )
    return (
        "import Mathlib\n\n"
        + _engine_body(config.inputs["negative_engine_v2"].path)
        + f"\nlfAuditNegativeSkeletonV2 {arguments}\n"
    )


def _checked_text(command: Sequence[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise NestedSkeletonSmokeError(
            f"cannot verify Lean checkout with {command[0]!r}: {exc}"
        ) from exc
    return result.stdout.strip()


def _run_lean(driver_path: Path, config: SmokeConfig) -> ProcessResult:
    revision = _checked_text(("git", "rev-parse", "HEAD"), cwd=config.mathlib_root)
    status = _checked_text(("git", "status", "--porcelain"), cwd=config.mathlib_root)
    lake_version = _checked_text(("lake", "--version"), cwd=config.mathlib_root)
    if (
        revision != config.mathlib_revision
        or status
        or lake_version != config.expected_lake_version
    ):
        raise NestedSkeletonSmokeError("Lean checkout/toolchain differs from smoke contract")
    command = (
        "/usr/bin/prlimit",
        f"--as={ADDRESS_SPACE_BYTES}",
        "--",
        "lake",
        "env",
        "lean",
        f"-M{LEAN_MEMORY_MB}",
        "-j1",
        str(driver_path),
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=config.mathlib_root,
            check=False,
            capture_output=True,
            timeout=config.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return ProcessResult(
            exit_code=124,
            stdout=exc.stdout if isinstance(exc.stdout, bytes) else b"",
            stderr=exc.stderr if isinstance(exc.stderr, bytes) else b"",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
            mathlib_revision=revision,
            lake_version=lake_version,
            mathlib_clean=True,
        )
    except OSError as exc:
        raise NestedSkeletonSmokeError(f"cannot launch Lean: {exc}") from exc
    return ProcessResult(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=time.monotonic() - started,
        timed_out=False,
        mathlib_revision=revision,
        lake_version=lake_version,
        mathlib_clean=True,
    )


def _validate_process(result: ProcessResult, config: SmokeConfig, *, stage: str) -> None:
    if result.timed_out:
        raise NestedSkeletonSmokeError(f"{stage} Lean process timed out")
    if result.exit_code != 0:
        tail = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")[-3000:]
        raise NestedSkeletonSmokeError(f"{stage} Lean process failed:\n{tail}")
    if (
        result.mathlib_revision != config.mathlib_revision
        or result.lake_version != config.expected_lake_version
        or not result.mathlib_clean
    ):
        raise NestedSkeletonSmokeError(f"{stage} Lean environment differs")


def _process_payload(
    result: ProcessResult, config: SmokeConfig, *, stage: str
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "stage": stage,
        "launcher": "prlimit -- lake env lean -M24576 -j1",
        "cwd": str(config.mathlib_root),
        "logical_driver_path": str(config.output_root / f"{stage}_driver.lean"),
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


def parse_primary(payload: bytes) -> tuple[tuple[EngineCandidate, ...], EngineCandidate]:
    candidates: list[EngineCandidate] = []
    terminal: dict[str, Any] | None = None
    batch: dict[str, Any] | None = None
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NestedSkeletonSmokeError(f"primary:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(raw, dict) or _canonical_line(raw).rstrip(b"\n") != line:
            raise NestedSkeletonSmokeError(f"primary:{line_number}: noncanonical row")
        if raw.get("kind") == "candidate":
            candidates.append(EngineCandidate.model_validate(raw))
        elif raw.get("kind") == "terminal":
            if terminal is not None:
                raise NestedSkeletonSmokeError("primary emitted duplicate declaration terminal")
            terminal = cast(dict[str, Any], raw)
        elif raw.get("kind") == "batch":
            if batch is not None:
                raise NestedSkeletonSmokeError("primary emitted duplicate batch terminal")
            batch = cast(dict[str, Any], raw)
        else:
            raise NestedSkeletonSmokeError(f"primary:{line_number}: unknown record kind")
    if (
        terminal is None
        or terminal.get("schemaVersion") != 2
        or terminal.get("declaration") != DECLARATION
        or terminal.get("status") != "complete"
        or terminal.get("discoveredCount") != len(candidates)
        or terminal.get("emittedCount") != len(candidates)
        or terminal.get("sourceTypeHash") != SOURCE_TYPE_HASH
        or batch
        != {
            "schemaVersion": 2,
            "kind": "batch",
            "recordKind": "batch",
            "status": "complete",
            "declarationCount": 1,
            "completedCount": 1,
            "failedCount": 0,
        }
    ):
        raise NestedSkeletonSmokeError("primary terminal contract differs")
    if not candidates or len({row.candidate_type_hash for row in candidates}) != len(candidates):
        raise NestedSkeletonSmokeError("primary candidate inventory differs")
    selected = [
        row
        for row in candidates
        if row.family == TARGET_FAMILY
        and row.operation_kind == TARGET_OPERATION_KIND
        and row.site_path == TARGET_SITE_PATH
        and row.operation == TARGET_OPERATION
    ]
    if len(selected) != 1:
        raise NestedSkeletonSmokeError("exact nested N22 smoke candidate was not emitted once")
    candidate = selected[0]
    if (
        candidate.source_type_hash != SOURCE_TYPE_HASH
        or candidate.site_path == "/root-body"
        or candidate.witness.get("sourceSkeleton") != "(A0 \u2194 (A1 \u2227 A2))"
        or candidate.witness.get("candidateSkeleton") != "(A0 \u2194 (A1 \u2228 A2))"
        or candidate.witness.get("atomCount") != 3
        or candidate.witness.get("valuationSpaceSize") != 8
    ):
        raise NestedSkeletonSmokeError("exact nested N22 skeleton contract differs")
    return tuple(candidates), candidate


def parse_audit(payload: bytes, candidate: EngineCandidate) -> EngineAudit:
    lines = payload.splitlines()
    if len(lines) != 1:
        raise NestedSkeletonSmokeError("audit row count differs")
    try:
        raw = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise NestedSkeletonSmokeError(f"audit output is invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or _canonical_line(raw).rstrip(b"\n") != lines[0]:
        raise NestedSkeletonSmokeError("audit output is not canonical")
    audit = EngineAudit.model_validate(raw)
    if audit.expected_candidate_type_hash != candidate.candidate_type_hash:
        raise NestedSkeletonSmokeError("audit/candidate hash binding differs")
    return audit


def _summary(
    candidates: Sequence[EngineCandidate], candidate: EngineCandidate, audit: EngineAudit
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": "passed",
        "declaration": DECLARATION,
        "source_split": SOURCE_SPLIT,
        "source_selection_ordinal": SOURCE_SELECTION_ORDINAL,
        "source_type_sha256": SOURCE_TYPE_HASH,
        "engine_candidate_count": len(candidates),
        "selected": {
            "family": candidate.family,
            "operation": candidate.operation,
            "operation_kind": candidate.operation_kind,
            "site_path": candidate.site_path,
            "candidate_type_sha256": candidate.candidate_type_hash,
            "atom_count": candidate.witness["atomCount"],
            "valuation_space_size": candidate.witness["valuationSpaceSize"],
            "source_skeleton": candidate.witness["sourceSkeleton"],
            "candidate_skeleton": candidate.witness["candidateSkeleton"],
        },
        "gates": {
            "nested_generation": True,
            "candidate_elaboration": candidate.candidate_elaborates,
            "exact_full_truth_table_separator": True,
            "independent_reconstruction": audit.verified,
            "train_split_source_only": True,
            "hard_timeouts_configured": True,
        },
        "decision": {
            "same_fixed_96_pilot_rerun_authorized": True,
            "sample_size_increase_authorized": False,
            "scale_authorized": False,
            "training_authorized": False,
            "final_test_accessed": False,
        },
    }


def _staging_path(config: SmokeConfig) -> Path:
    return config.output_root.with_name(f".{config.output_root.name}.partial")


def _write_payload(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def _process_from_artifact(config: SmokeConfig, *, stage: str) -> ProcessResult:
    process = _read_json(config.output_root / f"{stage}.process.json")
    stdout = (config.output_root / f"{stage}.stdout.jsonl").read_bytes()
    stderr = (config.output_root / f"{stage}.stderr.txt").read_bytes()
    if process.get("stdout_sha256") != sha256_hex(stdout) or process.get(
        "stderr_sha256"
    ) != sha256_hex(stderr):
        raise NestedSkeletonSmokeError(f"{stage} process stream hash differs")
    try:
        result = ProcessResult(
            exit_code=cast(int, process["exit_code"]),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=float(process["duration_seconds"]),
            timed_out=cast(bool, process["timed_out"]),
            mathlib_revision=cast(str, process["mathlib_revision"]),
            lake_version=cast(str, process["lake_version"]),
            mathlib_clean=cast(bool, process["mathlib_clean"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NestedSkeletonSmokeError(f"invalid {stage} process record") from exc
    _validate_process(result, config, stage=stage)
    return result


def verify_smoke(config: SmokeConfig) -> dict[str, Any]:
    """Replay the exact one-declaration generation and audit artifact."""

    verify_input_bindings(config)
    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise NestedSkeletonSmokeError("smoke root must be a non-symlink directory")
    observed = {path.name for path in config.output_root.iterdir() if path.is_file()}
    if observed != _OUTPUTS:
        raise NestedSkeletonSmokeError("smoke output file set differs")
    manifest = _read_json(config.output_root / "manifest.json")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _STATIC_OUTPUTS:
        raise NestedSkeletonSmokeError("smoke manifest output inventory differs")
    for name, raw_binding in outputs.items():
        if not isinstance(raw_binding, Mapping):
            raise NestedSkeletonSmokeError(f"invalid output binding: {name}")
        path = config.output_root / name
        if (
            path.is_symlink()
            or raw_binding.get("path") != str(path)
            or raw_binding.get("sha256") != hash_file(path)
        ):
            raise NestedSkeletonSmokeError(f"output binding differs: {name}")

    names_payload = f"{DECLARATION}\n".encode()
    if (config.output_root / "declaration_names.txt").read_bytes() != names_payload:
        raise NestedSkeletonSmokeError("declaration names artifact differs")
    expected_primary = render_primary_driver(
        config, _staging_path(config) / "declaration_names.txt"
    )
    if (config.output_root / "primary_driver.lean").read_text(encoding="utf-8") != expected_primary:
        raise NestedSkeletonSmokeError("primary driver differs")
    primary = _process_from_artifact(config, stage="primary")
    candidates, candidate = parse_primary(primary.stdout)
    if (config.output_root / "selected_candidate.json").read_bytes() != _canonical_line(
        candidate.model_dump(mode="json", by_alias=True)
    ):
        raise NestedSkeletonSmokeError("selected candidate artifact differs")
    expected_audit = render_audit_driver(config, candidate)
    if (config.output_root / "audit_driver.lean").read_text(encoding="utf-8") != expected_audit:
        raise NestedSkeletonSmokeError("audit driver differs")
    audit_result = _process_from_artifact(config, stage="audit")
    audit = parse_audit(audit_result.stdout, candidate)
    summary = _summary(candidates, candidate, audit)
    if (config.output_root / "summary.json").read_bytes() != _canonical_line(summary):
        raise NestedSkeletonSmokeError("summary artifact differs")
    if (
        manifest.get("status") != "completed"
        or manifest.get("config_sha256") != hash_canonical(config.model_dump(mode="json"))
        or manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("negative_engine_v2_sha256") != config.inputs["negative_engine_v2"].sha256
        or manifest.get("summary") != summary
        or manifest.get("privacy")
        != {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        }
        or manifest.get("execution", {}).get("final_test_accessed") is not False
        or manifest.get("execution", {}).get("training_launched") is not False
    ):
        raise NestedSkeletonSmokeError("smoke manifest contract differs")
    return manifest


def materialize_smoke(config: SmokeConfig) -> dict[str, Any]:
    """Run two timeout-bounded Lean stages and atomically freeze their evidence."""

    if config.output_root.exists():
        return verify_smoke(config)
    verify_input_bindings(config)
    staging = _staging_path(config)
    if staging.exists():
        raise NestedSkeletonSmokeError(f"stale smoke staging root exists: {staging}")
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o700)
    try:
        _write_payload(staging / "declaration_names.txt", f"{DECLARATION}\n".encode())
        primary_driver = render_primary_driver(config, staging / "declaration_names.txt")
        _write_payload(staging / "primary_driver.lean", primary_driver.encode())
        primary = _run_lean(staging / "primary_driver.lean", config)
        _write_payload(staging / "primary.stdout.jsonl", primary.stdout)
        _write_payload(staging / "primary.stderr.txt", primary.stderr)
        _write_payload(
            staging / "primary.process.json",
            _canonical_line(_process_payload(primary, config, stage="primary")),
        )
        _validate_process(primary, config, stage="primary")
        candidates, candidate = parse_primary(primary.stdout)
        _write_payload(
            staging / "selected_candidate.json",
            _canonical_line(candidate.model_dump(mode="json", by_alias=True)),
        )
        audit_driver = render_audit_driver(config, candidate)
        _write_payload(staging / "audit_driver.lean", audit_driver.encode())
        audit_result = _run_lean(staging / "audit_driver.lean", config)
        _write_payload(staging / "audit.stdout.jsonl", audit_result.stdout)
        _write_payload(staging / "audit.stderr.txt", audit_result.stderr)
        _write_payload(
            staging / "audit.process.json",
            _canonical_line(_process_payload(audit_result, config, stage="audit")),
        )
        _validate_process(audit_result, config, stage="audit")
        audit = parse_audit(audit_result.stdout, candidate)
        summary = _summary(candidates, candidate, audit)
        _write_payload(staging / "summary.json", _canonical_line(summary))
        manifest = {
            "schema_version": 1,
            "method_version": METHOD_VERSION,
            "status": "completed",
            "config_sha256": hash_canonical(config.model_dump(mode="json")),
            "implementation_module_sha256": hash_file(Path(__file__)),
            "negative_engine_v2_sha256": config.inputs["negative_engine_v2"].sha256,
            "inputs": {
                name: {"path": str(binding.path), "sha256": binding.sha256}
                for name, binding in sorted(config.inputs.items())
            },
            "outputs": {
                name: {
                    "path": str(config.output_root / name),
                    "sha256": hash_file(staging / name),
                }
                for name in sorted(_STATIC_OUTPUTS)
            },
            "summary": summary,
            "privacy": {
                "public_only": True,
                "private_source_content": False,
                "external_transmission": False,
            },
            "execution": {
                "primary_lean_exit_code": primary.exit_code,
                "audit_lean_exit_code": audit_result.exit_code,
                "primary_timeout_seconds": config.timeout_seconds,
                "audit_timeout_seconds": config.timeout_seconds,
                "external_calls": False,
                "final_test_accessed": False,
                "training_launched": False,
            },
        }
        _write_payload(staging / "manifest.json", _canonical_line(manifest))
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
