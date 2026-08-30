"""Run one frozen implication-aware N22 generation and reconstruction smoke.

The smoke binds an exact train declaration from the fixed 96-name sample,
combines the frozen v2 Boolean utilities with the additive v3 implication
engine, requires a root ``implication -> iff`` mutation, and independently
reconstructs its exact candidate hash.  Both Lean stages have hard timeouts;
the smoke cannot authorize scaling, sample growth, or training.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

import leanfaith.corpus2.s1_public_negative_skeleton_nested_smoke as base
from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex

METHOD_VERSION: Literal["s1_public_negative_skeleton_implication_smoke_v1"] = (
    "s1_public_negative_skeleton_implication_smoke_v1"
)
DECLARATION: Literal["Dynamics.IsDynCoverOf.monotone_subset"] = (
    "Dynamics.IsDynCoverOf.monotone_subset"
)
SOURCE_SPLIT: Literal["train"] = "train"
SOURCE_SELECTION_ORDINAL: Literal[2] = 2
SOURCE_TYPE_HASH = "b97a1d0d271fe69c94a8da2ca37e5f4eb491d78e7f46aa114bb47a32e68c7aba"
TARGET_FAMILY: Literal["N22"] = "N22"
TARGET_OPERATION_KIND: Literal["impToIff"] = "impToIff"
TARGET_SITE_PATH: Literal["/root-body"] = "/root-body"
TARGET_OPERATION: Literal["impToIff:/root-body"] = "impToIff:/root-body"
SOURCE_REVISION = base.SOURCE_REVISION
EXPECTED_LAKE_VERSION = base.EXPECTED_LAKE_VERSION

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_V2_PATH = _REPO_ROOT / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV2.lean"
_ENGINE_V3_PATH = _REPO_ROOT / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV3.lean"
_BASE_MODULE_PATH = Path(base.__file__).resolve()
_PILOT_V2_ROOT = Path(
    "/storage/milikic/leanfaith/corpus2/s1_public_negative_skeleton_pilot_v2_3d72e99_d568c8c"
)
_MATHLIB_ROOT = base._MATHLIB_ROOT
_INPUT_NAMES = frozenset(
    {
        "pilot_v2_manifest",
        "pilot_v2_selection",
        "negative_engine_v2",
        "negative_engine_v3",
        "base_smoke_module",
        "lean_toolchain",
        "lake_manifest",
    }
)
_PRODUCTION_INPUTS = {
    "pilot_v2_manifest": (
        _PILOT_V2_ROOT / "manifest.json",
        "4fd2c6a769d28d24322f7cedbfc5a2a01ef9edec5e2686eed74add1b914dbe44",
    ),
    "pilot_v2_selection": (
        _PILOT_V2_ROOT / "selection.jsonl",
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
_STATIC_OUTPUTS = base._STATIC_OUTPUTS
_OUTPUTS = _STATIC_OUTPUTS | {"manifest.json"}


class ImplicationSmokeError(RuntimeError):
    """A frozen input, implication candidate, or audit failed closed."""


class SmokeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_negative_skeleton_implication_smoke_v1"] = METHOD_VERSION
    output_root: Path
    mathlib_root: Path
    inputs: dict[str, base.FrozenInput]
    declaration: Literal["Dynamics.IsDynCoverOf.monotone_subset"] = DECLARATION
    source_split: Literal["train"] = SOURCE_SPLIT
    source_selection_ordinal: Literal[2] = SOURCE_SELECTION_ORDINAL
    target_family: Literal["N22"] = TARGET_FAMILY
    target_operation_kind: Literal["impToIff"] = TARGET_OPERATION_KIND
    target_site_path: Literal["/root-body"] = TARGET_SITE_PATH
    timeout_seconds: int = Field(default=120, ge=1, le=300, strict=True)
    expected_lake_version: str = EXPECTED_LAKE_VERSION
    mathlib_revision: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = SOURCE_REVISION
    enforce_storage_root: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if set(self.inputs) != _INPUT_NAMES:
            raise ValueError("implication smoke must bind the exact frozen input set")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("implication smoke artifacts must be under /storage/milikic")
        return self


class EngineCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[3] = Field(alias="schemaVersion")
    kind: Literal["candidate"]
    record_kind: Literal["candidate"] = Field(alias="recordKind")
    status: Literal["ok"]
    declaration: Literal["Dynamics.IsDynCoverOf.monotone_subset"]
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
            raise ValueError("implication skeleton candidate is unchanged")
        if sha256_hex(self.source.encode()) != self.source_type_hash:
            raise ValueError("implication skeleton source hash differs")
        if sha256_hex(self.candidate.encode()) != self.candidate_type_hash:
            raise ValueError("implication skeleton candidate hash differs")
        if not self.candidate_elaborates or self.whole_type_def_eq:
            raise ValueError("implication skeleton type checks differ")
        if self.operation != f"{self.operation_kind}:{self.site_path}":
            raise ValueError("implication skeleton operation/path binding differs")
        if self.evidence != {
            "relation": "schemaInequivalence",
            "exactBooleanSkeleton": True,
            "deduplicatedAtoms": True,
            "fullTruthTableEnumerated": True,
            "implicationAware": True,
            "parameterTelescopePreserved": True,
            "rootInfluence": True,
            "separatorVerified": True,
            "contractScope": "abstract-propositional-schema",
        }:
            raise ValueError("implication skeleton separator contract differs")
        atom_count = self.witness.get("atomCount")
        atom_hashes = self.witness.get("atomHashes")
        valuation = self.witness.get("valuation")
        if (
            not isinstance(atom_count, int)
            or isinstance(atom_count, bool)
            or not 1 <= atom_count <= 8
            or not isinstance(atom_hashes, list)
            or len(atom_hashes) != atom_count
            or not all(
                isinstance(value, str) and base._HEX64.fullmatch(value) for value in atom_hashes
            )
            or not isinstance(valuation, list)
            or len(valuation) != atom_count
            or not all(isinstance(value, bool) for value in valuation)
            or self.witness.get("valuationSpaceSize") != 2**atom_count
        ):
            raise ValueError("implication skeleton valuation inventory differs")
        if self.witness.get("sourceValue") is self.witness.get("candidateValue"):
            raise ValueError("implication skeleton valuation does not separate")
        return self


class EngineAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[3] = Field(alias="schemaVersion")
    kind: Literal["audit"]
    record_kind: Literal["audit"] = Field(alias="recordKind")
    declaration: Literal["Dynamics.IsDynCoverOf.monotone_subset"]
    family: Literal["N22"]
    operation: Literal["impToIff:/root-body"]
    expected_candidate_type_hash: str = Field(alias="expectedCandidateTypeHash")
    actual_candidate_type_hash: str = Field(alias="actualCandidateTypeHash")
    verified: Literal[True]
    status: Literal["verified"]
    reason: Literal["verified"]
    audit_mode: Literal["independent-implication-aware-reconstruction"] = Field(alias="auditMode")

    @model_validator(mode="after")
    def _same_hash(self) -> Self:
        if (
            base._HEX64.fullmatch(self.expected_candidate_type_hash) is None
            or self.actual_candidate_type_hash != self.expected_candidate_type_hash
        ):
            raise ValueError("independent implication audit hash differs")
        return self


def production_config(output_root: Path) -> SmokeConfig:
    inputs = {
        name: base.FrozenInput(path=path, sha256=digest)
        for name, (path, digest) in _PRODUCTION_INPUTS.items()
    }
    inputs["negative_engine_v2"] = base.FrozenInput(
        path=_ENGINE_V2_PATH, sha256=hash_file(_ENGINE_V2_PATH)
    )
    inputs["negative_engine_v3"] = base.FrozenInput(
        path=_ENGINE_V3_PATH, sha256=hash_file(_ENGINE_V3_PATH)
    )
    inputs["base_smoke_module"] = base.FrozenInput(
        path=_BASE_MODULE_PATH, sha256=hash_file(_BASE_MODULE_PATH)
    )
    return SmokeConfig(
        output_root=output_root,
        mathlib_root=_MATHLIB_ROOT,
        inputs=inputs,
    )


def verify_input_bindings(config: SmokeConfig) -> None:
    for name, binding in sorted(config.inputs.items()):
        if binding.path.is_symlink() or not binding.path.is_file():
            raise ImplicationSmokeError(f"unsafe or missing frozen input: {name}")
        if hash_file(binding.path) != binding.sha256:
            raise ImplicationSmokeError(f"frozen implication input hash differs: {name}")
    pilot = base._read_json(config.inputs["pilot_v2_manifest"].path)
    outputs = pilot.get("outputs")
    if (
        pilot.get("status") != "completed"
        or pilot.get("summary", {}).get("pilot_gate_passed") is not False
        or not isinstance(outputs, Mapping)
        or not isinstance(outputs.get("selection.jsonl"), Mapping)
        or outputs["selection.jsonl"].get("sha256") != config.inputs["pilot_v2_selection"].sha256
        or pilot.get("execution", {}).get("final_test_accessed") is not False
        or pilot.get("execution", {}).get("training_launched") is not False
    ):
        raise ImplicationSmokeError("failed v2 pilot binding differs")
    matched = [
        row
        for _, row in base._iter_jsonl(config.inputs["pilot_v2_selection"].path)
        if row.get("declaration") == config.declaration
    ]
    if len(matched) != 1:
        raise ImplicationSmokeError("implication smoke declaration selection is not unique")
    row = matched[0]
    if (
        row.get("ordinal") != config.source_selection_ordinal
        or row.get("split") != config.source_split
        or row.get("reference_sha256") != SOURCE_TYPE_HASH
    ):
        raise ImplicationSmokeError("implication smoke declaration selection binding differs")


def _body(path: Path, expected_import: str, markers: Sequence[str]) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    imports = [line.strip() for line in lines if line.strip().startswith("import ")]
    if imports != [expected_import]:
        raise ImplicationSmokeError(f"unexpected import contract: {path}")
    body = "\n".join(line for line in lines if not line.strip().startswith("import ")).strip()
    if not all(marker in body for marker in markers):
        raise ImplicationSmokeError(f"engine command contract differs: {path}")
    return body + "\n"


def _combined_engine(config: SmokeConfig) -> str:
    v2_body = _body(
        config.inputs["negative_engine_v2"].path,
        "import Lean",
        (
            "namespace LeanFaith.Meta.NegativeSkeletonEngineV2Helper",
            "end LeanFaith.Meta.NegativeSkeletonEngineV2Helper",
        ),
    )
    v3_body = _body(
        config.inputs["negative_engine_v3"].path,
        "import LeanFaith.Meta.NegativeSkeletonEngineV2",
        (
            "namespace LeanFaith.Meta.NegativeSkeletonEngineV3Helper",
            "lfNegativeSkeletonV3Batch",
            "lfAuditNegativeSkeletonV3",
            "end LeanFaith.Meta.NegativeSkeletonEngineV3Helper",
        ),
    )
    return v2_body + "\n" + v3_body


def render_primary_driver(config: SmokeConfig, names_path: Path) -> str:
    return (
        "import Mathlib\n\n"
        + _combined_engine(config)
        + "\nset_option maxHeartbeats 0 in\n"
        + f"lfNegativeSkeletonV3Batch {base._lean_string(str(names_path))}\n"
    )


def render_audit_driver(config: SmokeConfig, candidate: EngineCandidate) -> str:
    arguments = " ".join(
        base._lean_string(value)
        for value in (
            candidate.declaration,
            candidate.family,
            candidate.operation,
            candidate.candidate_type_hash,
        )
    )
    return (
        "import Mathlib\n\n"
        + _combined_engine(config)
        + f"\nlfAuditNegativeSkeletonV3 {arguments}\n"
    )


def parse_primary(payload: bytes) -> tuple[tuple[EngineCandidate, ...], EngineCandidate]:
    candidates: list[EngineCandidate] = []
    terminal: dict[str, Any] | None = None
    batch: dict[str, Any] | None = None
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ImplicationSmokeError(f"primary:{line_number}: invalid JSON: {exc}") from exc
        if (
            not isinstance(raw, dict)
            or base._canonical_line(raw).rstrip(b"\n") != line
            or raw.get("schemaVersion") != 3
        ):
            raise ImplicationSmokeError(f"primary:{line_number}: row contract differs")
        if raw.get("kind") == "candidate":
            candidates.append(EngineCandidate.model_validate(raw))
        elif raw.get("kind") == "terminal":
            if terminal is not None:
                raise ImplicationSmokeError("duplicate implication declaration terminal")
            terminal = cast(dict[str, Any], raw)
        elif raw.get("kind") == "batch":
            if batch is not None:
                raise ImplicationSmokeError("duplicate implication batch terminal")
            batch = cast(dict[str, Any], raw)
        else:
            raise ImplicationSmokeError(f"primary:{line_number}: unknown row kind")
    if (
        terminal is None
        or terminal.get("declaration") != DECLARATION
        or terminal.get("status") != "complete"
        or terminal.get("discoveredCount") != len(candidates)
        or terminal.get("emittedCount") != len(candidates)
        or terminal.get("sourceTypeHash") != SOURCE_TYPE_HASH
        or terminal.get("implicationAware") is not True
        or batch
        != {
            "schemaVersion": 3,
            "kind": "batch",
            "recordKind": "batch",
            "status": "complete",
            "declarationCount": 1,
            "completedCount": 1,
            "failedCount": 0,
        }
    ):
        raise ImplicationSmokeError("implication primary terminal contract differs")
    if not candidates or len({row.candidate_type_hash for row in candidates}) != len(candidates):
        raise ImplicationSmokeError("implication candidate inventory differs")
    selected = [
        row
        for row in candidates
        if row.family == TARGET_FAMILY
        and row.operation_kind == TARGET_OPERATION_KIND
        and row.site_path == TARGET_SITE_PATH
        and row.operation == TARGET_OPERATION
    ]
    if len(selected) != 1:
        raise ImplicationSmokeError("exact implication N22 candidate was not emitted once")
    candidate = selected[0]
    if (
        candidate.source_type_hash != SOURCE_TYPE_HASH
        or candidate.witness.get("sourceSkeleton") != "(A0 \u2192 (A1 \u2192 A2))"
        or candidate.witness.get("candidateSkeleton") != "(A0 \u2194 (A1 \u2192 A2))"
        or candidate.witness.get("atomCount") != 3
        or candidate.witness.get("valuationSpaceSize") != 8
    ):
        raise ImplicationSmokeError("exact implication skeleton contract differs")
    return tuple(candidates), candidate


def parse_audit(payload: bytes, candidate: EngineCandidate) -> EngineAudit:
    lines = payload.splitlines()
    if len(lines) != 1:
        raise ImplicationSmokeError("implication audit row count differs")
    try:
        raw = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ImplicationSmokeError(f"implication audit is invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or base._canonical_line(raw).rstrip(b"\n") != lines[0]:
        raise ImplicationSmokeError("implication audit is not canonical")
    audit = EngineAudit.model_validate(raw)
    if audit.expected_candidate_type_hash != candidate.candidate_type_hash:
        raise ImplicationSmokeError("implication audit/candidate hash binding differs")
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
            "implication_generation": True,
            "candidate_elaboration": candidate.candidate_elaborates,
            "exact_full_truth_table_separator": True,
            "independent_reconstruction": audit.verified,
            "train_split_source_only": True,
            "hard_timeouts_configured": True,
        },
        "decision": {
            "same_fixed_96_feasibility_precheck_authorized": True,
            "canary_fit_authorized": False,
            "sample_size_increase_authorized": False,
            "scale_authorized": False,
            "training_authorized": False,
            "final_test_accessed": False,
        },
    }


def _staging_path(config: SmokeConfig) -> Path:
    return config.output_root.with_name(f".{config.output_root.name}.partial")


def _process_from_artifact(config: SmokeConfig, *, stage: str) -> base.ProcessResult:
    return base._process_from_artifact(cast(Any, config), stage=stage)


def _validate_process(result: base.ProcessResult, config: SmokeConfig, *, stage: str) -> None:
    if result.exit_code != 0 and not result.timed_out:
        detail = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")[-20_000:]
        raise ImplicationSmokeError(f"{stage} Lean process failed:\n{detail}")
    base._validate_process(result, cast(Any, config), stage=stage)


def verify_smoke(config: SmokeConfig) -> dict[str, Any]:
    """Replay the exact implication generation and reconstruction artifact."""

    verify_input_bindings(config)
    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise ImplicationSmokeError("implication smoke root must be a non-symlink directory")
    observed = {path.name for path in config.output_root.iterdir() if path.is_file()}
    if observed != _OUTPUTS:
        raise ImplicationSmokeError("implication smoke output file set differs")
    manifest = base._read_json(config.output_root / "manifest.json")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _STATIC_OUTPUTS:
        raise ImplicationSmokeError("implication manifest output inventory differs")
    for name, raw_binding in outputs.items():
        if not isinstance(raw_binding, Mapping):
            raise ImplicationSmokeError(f"invalid implication output binding: {name}")
        path = config.output_root / name
        if (
            path.is_symlink()
            or raw_binding.get("path") != str(path)
            or raw_binding.get("sha256") != hash_file(path)
        ):
            raise ImplicationSmokeError(f"implication output binding differs: {name}")
    if (config.output_root / "declaration_names.txt").read_bytes() != f"{DECLARATION}\n".encode():
        raise ImplicationSmokeError("implication declaration artifact differs")
    expected_primary = render_primary_driver(
        config, _staging_path(config) / "declaration_names.txt"
    )
    if (config.output_root / "primary_driver.lean").read_text(encoding="utf-8") != expected_primary:
        raise ImplicationSmokeError("implication primary driver differs")
    primary = _process_from_artifact(config, stage="primary")
    candidates, candidate = parse_primary(primary.stdout)
    if (config.output_root / "selected_candidate.json").read_bytes() != base._canonical_line(
        candidate.model_dump(mode="json", by_alias=True)
    ):
        raise ImplicationSmokeError("implication selected candidate artifact differs")
    expected_audit = render_audit_driver(config, candidate)
    if (config.output_root / "audit_driver.lean").read_text(encoding="utf-8") != expected_audit:
        raise ImplicationSmokeError("implication audit driver differs")
    audit_result = _process_from_artifact(config, stage="audit")
    audit = parse_audit(audit_result.stdout, candidate)
    summary = _summary(candidates, candidate, audit)
    if (config.output_root / "summary.json").read_bytes() != base._canonical_line(summary):
        raise ImplicationSmokeError("implication summary artifact differs")
    if (
        manifest.get("status") != "completed"
        or manifest.get("config_sha256") != hash_canonical(config.model_dump(mode="json"))
        or manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("base_smoke_module_sha256") != config.inputs["base_smoke_module"].sha256
        or manifest.get("negative_engine_v2_sha256") != config.inputs["negative_engine_v2"].sha256
        or manifest.get("negative_engine_v3_sha256") != config.inputs["negative_engine_v3"].sha256
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
        raise ImplicationSmokeError("implication smoke manifest contract differs")
    return manifest


def materialize_smoke(config: SmokeConfig) -> dict[str, Any]:
    """Run two timeout-bounded Lean stages and atomically freeze their evidence."""

    if config.output_root.exists():
        return verify_smoke(config)
    verify_input_bindings(config)
    staging = _staging_path(config)
    if staging.exists():
        raise ImplicationSmokeError(f"stale implication staging root exists: {staging}")
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o700)
    try:
        base._write_payload(staging / "declaration_names.txt", f"{DECLARATION}\n".encode())
        primary_driver = render_primary_driver(config, staging / "declaration_names.txt")
        base._write_payload(staging / "primary_driver.lean", primary_driver.encode())
        primary = base._run_lean(staging / "primary_driver.lean", cast(Any, config))
        base._write_payload(staging / "primary.stdout.jsonl", primary.stdout)
        base._write_payload(staging / "primary.stderr.txt", primary.stderr)
        base._write_payload(
            staging / "primary.process.json",
            base._canonical_line(
                base._process_payload(primary, cast(Any, config), stage="primary")
            ),
        )
        _validate_process(primary, config, stage="primary")
        candidates, candidate = parse_primary(primary.stdout)
        base._write_payload(
            staging / "selected_candidate.json",
            base._canonical_line(candidate.model_dump(mode="json", by_alias=True)),
        )
        audit_driver = render_audit_driver(config, candidate)
        base._write_payload(staging / "audit_driver.lean", audit_driver.encode())
        audit_result = base._run_lean(staging / "audit_driver.lean", cast(Any, config))
        base._write_payload(staging / "audit.stdout.jsonl", audit_result.stdout)
        base._write_payload(staging / "audit.stderr.txt", audit_result.stderr)
        base._write_payload(
            staging / "audit.process.json",
            base._canonical_line(
                base._process_payload(audit_result, cast(Any, config), stage="audit")
            ),
        )
        _validate_process(audit_result, config, stage="audit")
        audit = parse_audit(audit_result.stdout, candidate)
        summary = _summary(candidates, candidate, audit)
        base._write_payload(staging / "summary.json", base._canonical_line(summary))
        manifest = {
            "schema_version": 1,
            "method_version": METHOD_VERSION,
            "status": "completed",
            "config_sha256": hash_canonical(config.model_dump(mode="json")),
            "implementation_module_sha256": hash_file(Path(__file__)),
            "base_smoke_module_sha256": config.inputs["base_smoke_module"].sha256,
            "negative_engine_v2_sha256": config.inputs["negative_engine_v2"].sha256,
            "negative_engine_v3_sha256": config.inputs["negative_engine_v3"].sha256,
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
        base._write_payload(staging / "manifest.json", base._canonical_line(manifest))
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
