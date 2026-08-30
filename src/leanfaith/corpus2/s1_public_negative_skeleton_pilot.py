"""Run the preregistered 96-declaration typed N21/N22 separator pilot.

The pilot samples already-admitted public Meta-positive declaration groups,
generates one typed Boolean-skeleton mutation per yielding declaration, audits
every selected candidate by independent Lean reconstruction, and measures both
its incremental effect on the frozen repair canary and a paired-only shortcut
canary.  It never reads ``final_test`` and never authorizes training directly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.corpus2.build_v1 import TRAINER_FIELDS, FinalRow, run_lexical_canary
from leanfaith.eval.m1_runtime import pack_pair
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.train2.trainer import TrainingRecord

METHOD_VERSION: Literal["s1_public_negative_skeleton_pilot_v1"] = (
    "s1_public_negative_skeleton_pilot_v1"
)
SOURCE_REVISION: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = (
    "d568c8c09630de097a046763c17b9ea99f95f950"
)
EXPECTED_LAKE_VERSION = "Lake version 5.0.0-src+fd00994 (Lean version 4.31.0-rc1)"
SELECTION_DOMAIN = "s1_public_negative_skeleton_pilot_selection_v1"
SELECTION_QUOTAS = {"train": 72, "validation": 12, "test": 12}
SAMPLE_SIZE = sum(SELECTION_QUOTAS.values())
MIN_CERTIFIED_YIELD = 24
MIN_DIAGNOSTIC_YIELD = 4
MIN_FULL_CANARY_IMPROVEMENT = 0.01
PAIRED_CANARY_TARGET = 0.72
MIN_N22_SHARE = 0.60
MAX_N21_SHARE = 0.40
MAX_OPERATION_SHARE = 0.40
ADDRESS_SPACE_BYTES = 25_769_803_776
LEAN_MEMORY_MB = 24_576

_REPAIR_ROOT = Path("/storage/milikic/leanfaith/corpus2/s1_public_repair_v1_22386b7_9e2425f")
_META_ROOT = Path("/storage/milikic/leanfaith/meta_engine_slice2_6ace45e")
_TOKENIZER_ROOT = Path("/storage/milikic/leanfaith/cpt/modernbert_lean_v1_run1")
_MATHLIB_ROOT = Path("/storage/milikic/leanfaith/mathlib4")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_PATH = _REPO_ROOT / "LeanFaith" / "Meta" / "NegativeSkeletonEngine.lean"
_BLOCKLIST_PATH = _REPO_ROOT / "data" / "benchmarks" / "golden_blocklist_v1.json"
_INPUT_NAMES = frozenset(
    {
        "repair_manifest",
        "repair_config",
        "repair_provenance",
        "repair_train",
        "repair_validation",
        "repair_test",
        "repair_canary",
        "meta_manifest",
        "meta_candidates",
        "golden_blocklist",
        "tokenizer_json",
        "tokenizer_config",
        "special_tokens_map",
        "negative_engine",
        "lean_toolchain",
        "lake_manifest",
    }
)
_PRODUCTION_INPUTS = {
    "repair_manifest": (
        _REPAIR_ROOT / "manifest.json",
        "3d72e9923f08242b44d3e4f012d6e8c6aec8cf12783ef67becaf8bddb1b01a85",
    ),
    "repair_config": (
        _REPAIR_ROOT / "run_config.json",
        "8fdd9a132120483365309ebc605e526eae76a47d86631b00fbe187a56f88dd2f",
    ),
    "repair_provenance": (
        _REPAIR_ROOT / "provenance_v1.jsonl",
        "5be633e224ba45c41d687026d5abb8b757e679b43a33c47f256d513ffb1a796b",
    ),
    "repair_train": (
        _REPAIR_ROOT / "records_train_v1.jsonl",
        "d2820f4d37fa5b4941dbfdd362ae8eeb3d9fadef7b8b89c4479eb4be7dc864f6",
    ),
    "repair_validation": (
        _REPAIR_ROOT / "records_validation_v1.jsonl",
        "f49bf95baeb05c1d333a358978e7f5f9c0bef2381d5409f572922f438da8b35c",
    ),
    "repair_test": (
        _REPAIR_ROOT / "records_test_v1.jsonl",
        "870778a765012e2ae99eec4477c77a6c1c782026f51990b70fe9467c8f3324b9",
    ),
    "repair_canary": (
        _REPAIR_ROOT / "lexical_canary.json",
        "ae937c145e2ce476789e2900b6ad78aff759085ce593baa11d02cbdc6fc945bf",
    ),
    "meta_manifest": (
        _META_ROOT / "manifest.json",
        "9e2425f17a44fa2005d2856c290b2f551a19e46c8a10bf0ae9888875ab311fe0",
    ),
    "meta_candidates": (
        _META_ROOT / "lean.stdout.jsonl",
        "61acf7436e03025a173249360915833dcbba7527d1b2504e10235445922a59f8",
    ),
    "golden_blocklist": (
        _BLOCKLIST_PATH,
        "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7",
    ),
    "tokenizer_json": (
        _TOKENIZER_ROOT / "tokenizer.json",
        "c7a995f78d60cc3c253902f4b5becfe2f9d0b44f78e6e2f81a343a0cb71789e6",
    ),
    "tokenizer_config": (
        _TOKENIZER_ROOT / "tokenizer_config.json",
        "2966a59b9e9cf122279aec1249e22e5bc7ad8430c754e95031b13fd128d4e560",
    ),
    "special_tokens_map": (
        _TOKENIZER_ROOT / "special_tokens_map.json",
        "ea97ecdbcc73713039d8d64dbb05e3689495c96657fbd9a18f5bed381be81049",
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
        "selection.jsonl",
        "declaration_names.txt",
        "primary_driver.lean",
        "primary.stdout.jsonl",
        "primary.stderr.txt",
        "primary.process.json",
        "selected_candidates.jsonl",
        "exclusions.jsonl",
        "audit_driver.lean",
        "audit.stdout.jsonl",
        "audit.stderr.txt",
        "audit.process.json",
        "trainer_records.jsonl",
        "certificates.jsonl",
        "baseline_canary.json",
        "augmented_canary.json",
        "paired_canary.json",
        "summary.json",
    }
)
_OUTPUTS = _STATIC_OUTPUTS | {"manifest.json"}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class S1NegativeSkeletonPilotError(RuntimeError):
    """A frozen input, Lean result, audit, or pilot gate failed closed."""


class FrozenInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PilotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_negative_skeleton_pilot_v1"] = METHOD_VERSION
    output_root: Path
    mathlib_root: Path
    tokenizer_root: Path
    inputs: dict[str, FrozenInput]
    selection_quotas: dict[str, int] = Field(default_factory=lambda: dict(SELECTION_QUOTAS))
    min_certified_yield: Literal[24] = 24
    min_diagnostic_yield: Literal[4] = 4
    min_full_canary_improvement: float = MIN_FULL_CANARY_IMPROVEMENT
    paired_canary_target: float = PAIRED_CANARY_TARGET
    min_n22_share: float = MIN_N22_SHARE
    max_n21_share: float = MAX_N21_SHARE
    max_operation_share: float = MAX_OPERATION_SHARE
    timeout_seconds: int = Field(default=120, ge=1, le=300, strict=True)
    expected_lake_version: str = EXPECTED_LAKE_VERSION
    mathlib_revision: Literal["d568c8c09630de097a046763c17b9ea99f95f950"] = SOURCE_REVISION
    enforce_storage_root: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if set(self.inputs) != _INPUT_NAMES:
            raise ValueError("skeleton pilot must bind the exact frozen input set")
        if self.selection_quotas != SELECTION_QUOTAS:
            raise ValueError("skeleton pilot split quotas differ")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("skeleton pilot artifacts must be under /storage/milikic")
        return self


class EngineCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    kind: Literal["candidate"]
    record_kind: Literal["candidate"] = Field(alias="recordKind")
    status: Literal["ok"]
    declaration: str = Field(min_length=1)
    family: Literal["N21", "N22"]
    operation: str = Field(min_length=1)
    operation_kind: str = Field(alias="operationKind", min_length=1)
    site_path: Literal["/root-body"] = Field(alias="sitePath")
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
            raise ValueError("negative skeleton candidate is unchanged")
        if sha256_hex(self.source.encode()) != self.source_type_hash:
            raise ValueError("negative skeleton source hash differs")
        if sha256_hex(self.candidate.encode()) != self.candidate_type_hash:
            raise ValueError("negative skeleton candidate hash differs")
        if not self.candidate_elaborates or self.whole_type_def_eq:
            raise ValueError("negative skeleton type checks differ")
        if self.evidence != {
            "relation": "schemaInequivalence",
            "exactBooleanSkeleton": True,
            "distinctAtoms": True,
            "rootInfluence": True,
            "separatorVerified": True,
            "contractScope": "abstract-propositional-schema",
        }:
            raise ValueError("negative skeleton separator contract differs")
        if self.witness.get("sourceValue") is self.witness.get("candidateValue"):
            raise ValueError("negative skeleton valuation does not separate")
        valuation = self.witness.get("valuation")
        if not isinstance(valuation, dict) or set(valuation) != {"A", "B"}:
            raise ValueError("negative skeleton valuation differs")
        return self


class EngineAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[1] = Field(alias="schemaVersion")
    kind: Literal["audit"]
    record_kind: Literal["audit"] = Field(alias="recordKind")
    declaration: str
    family: Literal["N21", "N22"]
    operation: str
    expected_candidate_type_hash: str = Field(alias="expectedCandidateTypeHash")
    actual_candidate_type_hash: str = Field(alias="actualCandidateTypeHash")
    verified: Literal[True]
    status: Literal["verified"]
    reason: Literal["verified"]
    audit_mode: Literal["independent-root-reconstruction"] = Field(alias="auditMode")

    @model_validator(mode="after")
    def _same_hash(self) -> Self:
        if (
            _HEX64.fullmatch(self.expected_candidate_type_hash) is None
            or self.actual_candidate_type_hash != self.expected_candidate_type_hash
        ):
            raise ValueError("independent audit candidate hash differs")
        return self


@dataclass(frozen=True, slots=True)
class SourceRow:
    declaration: str
    ancestry_id: str
    split: Literal["train", "validation", "test"]
    trainer: TrainingRecord
    final_row: FinalRow


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


def _jsonl_bytes(rows: Iterable[object]) -> bytes:
    return b"".join(_canonical_line(row) for row in rows)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise S1NegativeSkeletonPilotError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise S1NegativeSkeletonPilotError(f"expected JSON object: {path}")
    return cast(dict[str, Any], raw)


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise S1NegativeSkeletonPilotError(f"cannot read JSONL {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line:
            raise S1NegativeSkeletonPilotError(f"{path}:{line_number}: empty JSONL line")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise S1NegativeSkeletonPilotError(
                f"{path}:{line_number}: invalid JSONL: {exc}"
            ) from exc
        if not isinstance(raw, dict) or _canonical_line(raw).rstrip(b"\n") != line:
            raise S1NegativeSkeletonPilotError(
                f"{path}:{line_number}: row is not a canonical object"
            )
        yield line_number, cast(dict[str, Any], raw)


def production_config(output_root: Path) -> PilotConfig:
    inputs = {
        name: FrozenInput(path=path, sha256=digest)
        for name, (path, digest) in _PRODUCTION_INPUTS.items()
    }
    inputs["negative_engine"] = FrozenInput(path=_ENGINE_PATH, sha256=hash_file(_ENGINE_PATH))
    return PilotConfig(
        output_root=output_root,
        mathlib_root=_MATHLIB_ROOT,
        tokenizer_root=_TOKENIZER_ROOT,
        inputs=inputs,
    )


def verify_input_bindings(config: PilotConfig) -> None:
    for name, binding in sorted(config.inputs.items()):
        if binding.path.is_symlink() or not binding.path.is_file():
            raise S1NegativeSkeletonPilotError(f"unsafe or missing frozen input: {name}")
        if hash_file(binding.path) != binding.sha256:
            raise S1NegativeSkeletonPilotError(f"frozen input hash differs: {name}")


def _ancestry_id(declaration: str) -> str:
    return "mathlib-declaration:" + hash_canonical(
        {
            "schema": "mathlib_declaration_ancestry_v1",
            "revision": SOURCE_REVISION,
            "declaration": declaration,
        }
    )


def _load_base_rows(config: PilotConfig) -> tuple[list[FinalRow], dict[str, SourceRow]]:
    verify_input_bindings(config)
    manifest = _read_json(config.inputs["repair_manifest"].path)
    source_summary = manifest.get("selection_summary")
    if (
        manifest.get("status") != "completed"
        or not isinstance(source_summary, Mapping)
        or source_summary.get("training_gate_passed") is not False
        or manifest.get("execution", {}).get("final_test_accessed") is not False
        or manifest.get("privacy", {}).get("private_source_content") is not False
    ):
        raise S1NegativeSkeletonPilotError("repair diagnostic contract differs")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise S1NegativeSkeletonPilotError("repair output bindings are missing")
    repair_outputs = {
        "run_config.json": "repair_config",
        "provenance_v1.jsonl": "repair_provenance",
        "records_train_v1.jsonl": "repair_train",
        "records_validation_v1.jsonl": "repair_validation",
        "records_test_v1.jsonl": "repair_test",
        "lexical_canary.json": "repair_canary",
    }
    for output_name, input_name in repair_outputs.items():
        binding = outputs.get(output_name)
        if (
            not isinstance(binding, Mapping)
            or binding.get("sha256") != config.inputs[input_name].sha256
        ):
            raise S1NegativeSkeletonPilotError(f"repair output binding differs: {output_name}")

    trainers: dict[str, tuple[Literal["train", "validation", "test"], TrainingRecord]] = {}
    for split, input_name in (
        ("train", "repair_train"),
        ("validation", "repair_validation"),
        ("test", "repair_test"),
    ):
        for line_number, row in _iter_jsonl(config.inputs[input_name].path):
            if set(row) != TRAINER_FIELDS:
                raise S1NegativeSkeletonPilotError(
                    f"{input_name}:{line_number}: trainer fields differ"
                )
            try:
                trainer = TrainingRecord.model_validate(row)
            except ValidationError as exc:
                raise S1NegativeSkeletonPilotError(
                    f"{input_name}:{line_number}: invalid trainer row: {exc}"
                ) from exc
            trainers[trainer.record_id] = (cast(Any, split), trainer)

    declaration_by_ancestry: dict[str, str] = {}
    meta_manifest = _read_json(config.inputs["meta_manifest"].path)
    meta_outputs = meta_manifest.get("outputs")
    if (
        meta_manifest.get("status") != "completed"
        or not isinstance(meta_outputs, Mapping)
        or not isinstance(meta_outputs.get("lean.stdout.jsonl"), Mapping)
        or meta_outputs["lean.stdout.jsonl"].get("sha256")
        != config.inputs["meta_candidates"].sha256
    ):
        raise S1NegativeSkeletonPilotError("Meta source contract differs")
    for _, row in _iter_jsonl(config.inputs["meta_candidates"].path):
        if row.get("recordKind") != "candidate":
            continue
        declaration = row.get("declaration")
        if isinstance(declaration, str):
            ancestry = _ancestry_id(declaration)
            previous_declaration = declaration_by_ancestry.setdefault(ancestry, declaration)
            if previous_declaration != declaration:
                raise S1NegativeSkeletonPilotError("mathlib ancestry hash collision")

    base_rows: list[FinalRow] = []
    eligible: dict[str, SourceRow] = {}
    seen_provenance: set[str] = set()
    for line_number, provenance in _iter_jsonl(config.inputs["repair_provenance"].path):
        record_id = provenance.get("record_id")
        if not isinstance(record_id, str) or record_id in seen_provenance:
            raise S1NegativeSkeletonPilotError(
                f"repair_provenance:{line_number}: provenance identity differs"
            )
        seen_provenance.add(record_id)
        joined = trainers.get(record_id)
        if joined is None or provenance.get("split") != joined[0]:
            raise S1NegativeSkeletonPilotError("repair trainer/provenance join differs")
        split, trainer = joined
        final = FinalRow(trainer=trainer, provenance=provenance, split=split)
        base_rows.append(final)
        if (
            not trainer.label
            or trainer.family not in {"P20", "P21"}
            or "meta_engine_slice2" not in provenance.get("source_kinds", [])
        ):
            continue
        groups = provenance.get("split_group_ids")
        if not isinstance(groups, list):
            raise S1NegativeSkeletonPilotError("Meta source split groups differ")
        source_ancestry = next(
            (
                value
                for value in groups
                if isinstance(value, str) and value.startswith("mathlib-declaration:")
            ),
            None,
        )
        if source_ancestry is None or source_ancestry not in declaration_by_ancestry:
            raise S1NegativeSkeletonPilotError("Meta source declaration join differs")
        candidate = SourceRow(
            declaration=declaration_by_ancestry[source_ancestry],
            ancestry_id=source_ancestry,
            split=split,
            trainer=trainer,
            final_row=final,
        )
        previous_source = eligible.get(source_ancestry)
        if (
            previous_source is None
            or candidate.trainer.record_id < previous_source.trainer.record_id
        ):
            eligible[source_ancestry] = candidate
    if set(trainers) != seen_provenance or len(base_rows) != 7488:
        raise S1NegativeSkeletonPilotError("repair row inventory differs")
    return base_rows, eligible


def select_sources(config: PilotConfig) -> tuple[SourceRow, ...]:
    """Select exact split-stratified declaration groups before Lean execution."""

    _, eligible = _load_base_rows(config)
    selected: list[SourceRow] = []
    for split in ("train", "validation", "test"):
        ranked = sorted(
            (row for row in eligible.values() if row.split == split),
            key=lambda row: hash_canonical(
                {
                    "schema": SELECTION_DOMAIN,
                    "split": split,
                    "ancestry_id": row.ancestry_id,
                }
            ),
        )
        quota = config.selection_quotas[split]
        if len(ranked) < quota:
            raise S1NegativeSkeletonPilotError(f"insufficient eligible {split} declarations")
        selected.extend(ranked[:quota])
    if len(selected) != SAMPLE_SIZE or len({row.declaration for row in selected}) != SAMPLE_SIZE:
        raise S1NegativeSkeletonPilotError("selected declarations are not unique")
    return tuple(selected)


def _engine_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    imports = [line.strip() for line in lines if line.strip().startswith("import ")]
    if imports != ["import Lean"]:
        raise S1NegativeSkeletonPilotError("negative engine imports differ")
    body = "\n".join(line for line in lines if not line.strip().startswith("import ")).strip()
    markers = (
        "namespace LeanFaith.Meta.NegativeSkeletonEngineHelper",
        "lfNegativeSkeletonBatch",
        "lfAuditNegativeSkeleton",
        "end LeanFaith.Meta.NegativeSkeletonEngineHelper",
    )
    if not all(marker in body for marker in markers):
        raise S1NegativeSkeletonPilotError("negative engine command contract differs")
    return body + "\n"


def _lean_string(value: str) -> str:
    if any(character in value for character in ("\0", "\n", "\r")):
        raise S1NegativeSkeletonPilotError("Lean literal contains a control character")
    return json.dumps(value, ensure_ascii=False)


def render_primary_driver(config: PilotConfig, names_path: Path) -> str:
    names = _lean_string(str(names_path))
    return (
        "import Mathlib\n\n"
        + _engine_body(config.inputs["negative_engine"].path)
        + "\nset_option maxHeartbeats 0 in\n"
        + f"lfNegativeSkeletonBatch {names}\n"
    )


def render_audit_driver(config: PilotConfig, candidates: Sequence[EngineCandidate]) -> str:
    commands = []
    for candidate in candidates:
        arguments = " ".join(
            _lean_string(value)
            for value in (
                candidate.declaration,
                candidate.family,
                candidate.operation,
                candidate.candidate_type_hash,
            )
        )
        commands.append(f"lfAuditNegativeSkeleton {arguments}")
    return (
        "import Mathlib\n\n"
        + _engine_body(config.inputs["negative_engine"].path)
        + "\n"
        + "\n".join(commands)
        + "\n"
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
        raise S1NegativeSkeletonPilotError(
            f"cannot verify Lean checkout with {command[0]!r}: {exc}"
        ) from exc
    return result.stdout.strip()


def _run_lean(driver_path: Path, config: PilotConfig) -> ProcessResult:
    revision = _checked_text(("git", "rev-parse", "HEAD"), cwd=config.mathlib_root)
    status = _checked_text(("git", "status", "--porcelain"), cwd=config.mathlib_root)
    lake_version = _checked_text(("lake", "--version"), cwd=config.mathlib_root)
    if (
        revision != config.mathlib_revision
        or status
        or lake_version != config.expected_lake_version
    ):
        raise S1NegativeSkeletonPilotError("Lean checkout/toolchain differs from pilot contract")
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
        raise S1NegativeSkeletonPilotError(f"cannot launch Lean: {exc}") from exc
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


def _validate_process(result: ProcessResult, config: PilotConfig, *, stage: str) -> None:
    if result.timed_out:
        raise S1NegativeSkeletonPilotError(f"{stage} Lean process timed out")
    if result.exit_code != 0:
        tail = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")[-3000:]
        raise S1NegativeSkeletonPilotError(f"{stage} Lean process failed:\n{tail}")
    if (
        result.mathlib_revision != config.mathlib_revision
        or result.lake_version != config.expected_lake_version
        or not result.mathlib_clean
    ):
        raise S1NegativeSkeletonPilotError(f"{stage} Lean environment differs")


def _process_payload(
    result: ProcessResult, config: PilotConfig, *, stage: str
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


def _parse_primary(
    payload: bytes,
    selected_sources: Sequence[SourceRow],
) -> tuple[tuple[EngineCandidate, ...], tuple[dict[str, Any], ...]]:
    source_by_declaration = {row.declaration: row for row in selected_sources}
    terminals: dict[str, dict[str, Any]] = {}
    candidates: list[EngineCandidate] = []
    batch: dict[str, Any] | None = None
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise S1NegativeSkeletonPilotError(
                f"primary stdout:{line_number}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise S1NegativeSkeletonPilotError("primary stdout row is not an object")
        row = cast(dict[str, Any], raw)
        kind = row.get("recordKind")
        if kind == "candidate":
            try:
                candidate = EngineCandidate.model_validate(row)
            except ValidationError as exc:
                raise S1NegativeSkeletonPilotError(
                    f"primary stdout:{line_number}: invalid candidate: {exc}"
                ) from exc
            source = source_by_declaration.get(candidate.declaration)
            if source is None or candidate.source != source.trainer.reference_headless:
                raise S1NegativeSkeletonPilotError("engine candidate/source join differs")
            candidates.append(candidate)
        elif kind == "status":
            declaration = row.get("declaration")
            if not isinstance(declaration, str) or declaration in terminals:
                raise S1NegativeSkeletonPilotError("engine terminal identity differs")
            terminals[declaration] = row
        elif kind == "batch":
            if batch is not None:
                raise S1NegativeSkeletonPilotError("duplicate engine batch terminal")
            batch = row
        else:
            raise S1NegativeSkeletonPilotError("unknown primary engine row kind")
    if set(terminals) != set(source_by_declaration):
        raise S1NegativeSkeletonPilotError("engine terminal declaration set differs")
    if any(row.get("status") != "complete" for row in terminals.values()):
        raise S1NegativeSkeletonPilotError("one or more engine declarations failed")
    if (
        batch is None
        or batch.get("declarationCount") != SAMPLE_SIZE
        or batch.get("failedCount") != 0
    ):
        raise S1NegativeSkeletonPilotError("engine batch terminal differs")
    keys = [
        (
            candidate.declaration,
            candidate.family,
            candidate.operation,
            candidate.candidate_type_hash,
        )
        for candidate in candidates
    ]
    if len(keys) != len(set(keys)):
        raise S1NegativeSkeletonPilotError("engine emitted duplicate candidate keys")
    return tuple(candidates), tuple(terminals[name] for name in sorted(terminals))


def choose_candidates(
    candidates: Sequence[EngineCandidate],
    selected_sources: Sequence[SourceRow],
) -> tuple[EngineCandidate, ...]:
    """Choose at most one candidate per declaration with a frozen 60/40 N22/N21 policy."""

    by_declaration: dict[str, list[EngineCandidate]] = {}
    for candidate in candidates:
        by_declaration.setdefault(candidate.declaration, []).append(candidate)
    chosen: list[EngineCandidate] = []
    for source in selected_sources:
        available = by_declaration.get(source.declaration, [])
        if not available:
            continue
        rank = hash_canonical(
            {
                "schema": "s1_negative_skeleton_family_choice_v1",
                "declaration": source.declaration,
            }
        )
        preferred_family = "N21" if int(rank[:8], 16) % 5 < 2 else "N22"
        family_rows = [row for row in available if row.family == preferred_family]
        if not family_rows:
            family_rows = available
        chosen.append(
            min(
                family_rows,
                key=lambda row: hash_canonical(
                    {
                        "schema": "s1_negative_skeleton_operation_choice_v1",
                        "declaration": source.declaration,
                        "family": row.family,
                        "operation": row.operation,
                        "candidate_type_hash": row.candidate_type_hash,
                    }
                ),
            )
        )
    if len({row.declaration for row in chosen}) != len(chosen):
        raise S1NegativeSkeletonPilotError("candidate chooser crossed declaration groups")
    return tuple(chosen)


def _parse_audits(payload: bytes, selected: Sequence[EngineCandidate]) -> tuple[EngineAudit, ...]:
    expected = {
        (row.declaration, row.family, row.operation, row.candidate_type_hash) for row in selected
    }
    audits: list[EngineAudit] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
            audit = EngineAudit.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise S1NegativeSkeletonPilotError(
                f"audit stdout:{line_number}: invalid audit: {exc}"
            ) from exc
        audits.append(audit)
    observed = {
        (
            row.declaration,
            row.family,
            row.operation,
            row.expected_candidate_type_hash,
        )
        for row in audits
    }
    if len(audits) != len(expected) or observed != expected:
        raise S1NegativeSkeletonPilotError("independent audit set differs")
    return tuple(audits)


def _load_tokenizer(config: PilotConfig) -> Any:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover
        raise S1NegativeSkeletonPilotError(
            "negative skeleton pilot requires local-inference dependencies"
        ) from exc
    return AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
        str(config.tokenizer_root), local_files_only=True, trust_remote_code=False
    )


def _screen_and_project(
    config: PilotConfig,
    selected: Sequence[EngineCandidate],
    sources: Sequence[SourceRow],
    tokenizer: Any,
) -> tuple[
    tuple[EngineCandidate, ...],
    tuple[FinalRow, ...],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
]:
    source_by_declaration = {row.declaration: row for row in sources}
    blocklist = GoldenBlocklist.load(config.inputs["golden_blocklist"].path)
    admitted: list[EngineCandidate] = []
    final_rows: list[FinalRow] = []
    certificates: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    pair_hashes: set[tuple[str, str]] = set()
    for candidate in selected:
        source = source_by_declaration[candidate.declaration]
        reference_near = signature_near_dup_hash(candidate.source)
        candidate_near = signature_near_dup_hash(candidate.candidate)
        pair_key = cast(tuple[str, str], tuple(sorted((reference_near, candidate_near))))
        reason: str | None = None
        if (
            reference_near in blocklist.near_dup_hashes
            or candidate_near in blocklist.near_dup_hashes
            or blocklist.problem_is_blocked(source.ancestry_id)
        ):
            reason = "golden_blocklist"
        elif reference_near == candidate_near:
            reason = "degenerate_near_identical_sides"
        elif pair_key in pair_hashes:
            reason = "duplicate_pair"
        forward = len(
            tokenizer.encode(
                pack_pair(candidate.source, candidate.candidate), add_special_tokens=True
            )
        )
        reverse = len(
            tokenizer.encode(
                pack_pair(candidate.candidate, candidate.source), add_special_tokens=True
            )
        )
        if not forward or not reverse:
            raise S1NegativeSkeletonPilotError("tokenizer returned an empty packed pair")
        if forward > 1024 or reverse > 1024:
            reason = "overlength"
        if reason is not None:
            exclusions.append(
                {
                    "schema_version": 1,
                    "declaration": candidate.declaration,
                    "family": candidate.family,
                    "operation": candidate.operation,
                    "candidate_type_hash": candidate.candidate_type_hash,
                    "reason": reason,
                    "forward_tokens": forward,
                    "reverse_tokens": reverse,
                }
            )
            continue
        pair_hashes.add(pair_key)
        record_id = "s1_negative_skeleton:" + hash_canonical(
            {
                "schema": "s1_negative_skeleton_trainer_projection_v1",
                "source_record_id": source.trainer.record_id,
                "family": candidate.family,
                "operation": candidate.operation,
                "candidate_type_hash": candidate.candidate_type_hash,
            }
        )
        trainer = TrainingRecord(
            record_id=record_id,
            reference_headless=candidate.source,
            candidate_headless=candidate.candidate,
            label=False,
            group_key=source.trainer.group_key,
            family=candidate.family,
            source="typed_negative_skeleton_v1",
            weight=1.0,
        )
        provenance = {
            "schema_version": 1,
            "method_version": METHOD_VERSION,
            "record_id": record_id,
            "source_record_id": source.trainer.record_id,
            "declaration": candidate.declaration,
            "source_revision": config.mathlib_revision,
            "family": candidate.family,
            "operation": candidate.operation,
            "evidence_class": "N-SEP",
            "contract_scope": "abstract-propositional-schema",
            "reference_sha256": candidate.source_type_hash,
            "candidate_sha256": candidate.candidate_type_hash,
            "ancestry_id": source.ancestry_id,
            "split": source.split,
            "group_key": source.trainer.group_key,
            "separator": candidate.witness,
            "forward_tokens": forward,
            "reverse_tokens": reverse,
            "independent_audit_required": True,
            "private_source_content": False,
            "redistribution_allowed": True,
            "external_transmission_allowed": False,
            "release_eligible": True,
        }
        admitted.append(candidate)
        certificates.append(provenance)
        final_rows.append(FinalRow(trainer=trainer, provenance=provenance, split=source.split))
    return (
        tuple(admitted),
        tuple(final_rows),
        tuple(certificates),
        tuple(sorted(exclusions, key=lambda row: cast(str, row["declaration"]))),
    )


def _run_canaries(
    config: PilotConfig,
    base_rows: Sequence[FinalRow],
    sources: Sequence[SourceRow],
    admitted_candidates: Sequence[EngineCandidate],
    negative_rows: Sequence[FinalRow],
    tokenizer: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    repair_config = _read_json(config.inputs["repair_config"].path)
    seed = cast(int, repair_config.get("seed"))
    epochs = cast(int, repair_config.get("canary_epochs"))
    learning_rate = cast(float, repair_config.get("canary_learning_rate"))
    target = cast(float, repair_config.get("canary_target_balanced_accuracy"))
    parameters = {
        "seed": seed,
        "epochs": epochs,
        "learning_rate": learning_rate,
        "target": target,
    }
    if parameters != {"seed": 20260829, "epochs": 6, "learning_rate": 0.15, "target": 0.72}:
        raise S1NegativeSkeletonPilotError("repair canary parameters differ")
    baseline = run_lexical_canary(
        base_rows,
        tokenizer=tokenizer,
        seed=seed,
        epochs=epochs,
        learning_rate=learning_rate,
        target=target,
    )
    if baseline != _read_json(config.inputs["repair_canary"].path):
        raise S1NegativeSkeletonPilotError("baseline lexical canary replay differs")
    augmented = run_lexical_canary(
        [*base_rows, *negative_rows],
        tokenizer=tokenizer,
        seed=seed,
        epochs=epochs,
        learning_rate=learning_rate,
        target=target,
    )
    source_by_declaration = {source.declaration: source for source in sources}
    negative_by_declaration = {
        candidate.declaration: row
        for candidate, row in zip(admitted_candidates, negative_rows, strict=True)
    }
    paired_rows: list[FinalRow] = []
    for declaration in sorted(negative_by_declaration):
        paired_rows.append(source_by_declaration[declaration].final_row)
        paired_rows.append(negative_by_declaration[declaration])
    diagnostic_counts = Counter(row.split for row in negative_rows)
    if (
        diagnostic_counts["validation"] < config.min_diagnostic_yield
        or diagnostic_counts["test"] < config.min_diagnostic_yield
    ):
        return (
            baseline,
            augmented,
            {
                "schema_version": 1,
                "status": "insufficient_diagnostic_yield",
                "negative_split_counts": dict(sorted(diagnostic_counts.items())),
                "target_balanced_accuracy_below": config.paired_canary_target,
                "target_met": False,
            },
        )
    paired = run_lexical_canary(
        paired_rows,
        tokenizer=tokenizer,
        seed=seed,
        epochs=epochs,
        learning_rate=learning_rate,
        target=config.paired_canary_target,
    )
    return baseline, augmented, paired


def _metric(canary: Mapping[str, Any], split: str) -> float:
    diagnostics = canary.get("diagnostics")
    if not isinstance(diagnostics, Mapping) or not isinstance(diagnostics.get(split), Mapping):
        raise S1NegativeSkeletonPilotError(f"canary lacks {split} diagnostics")
    value = diagnostics[split].get("balanced_accuracy")
    if not isinstance(value, int | float):
        raise S1NegativeSkeletonPilotError(f"canary {split} metric differs")
    return float(value)


def _summary(
    config: PilotConfig,
    sources: Sequence[SourceRow],
    emitted: Sequence[EngineCandidate],
    chosen: Sequence[EngineCandidate],
    admitted: Sequence[EngineCandidate],
    negative_rows: Sequence[FinalRow],
    audits: Sequence[EngineAudit],
    exclusions: Sequence[Mapping[str, object]],
    baseline: Mapping[str, Any],
    augmented: Mapping[str, Any],
    paired: Mapping[str, Any],
) -> dict[str, object]:
    split_counts = Counter(row.split for row in negative_rows)
    family_counts = Counter(row.family for row in admitted)
    operation_counts = Counter(row.operation for row in admitted)
    admitted_count = len(admitted)
    n22_share = family_counts["N22"] / admitted_count if admitted_count else 0.0
    n21_share = family_counts["N21"] / admitted_count if admitted_count else 0.0
    operation_share = (
        max(operation_counts.values(), default=0) / admitted_count if admitted_count else 0.0
    )
    improvements = {
        split: _metric(baseline, split) - _metric(augmented, split)
        for split in ("validation", "test")
    }
    yield_passed = (
        admitted_count >= config.min_certified_yield
        and split_counts["validation"] >= config.min_diagnostic_yield
        and split_counts["test"] >= config.min_diagnostic_yield
    )
    audit_passed = len(audits) == admitted_count and all(row.verified for row in audits)
    family_mix_passed = n22_share >= config.min_n22_share and n21_share <= config.max_n21_share
    operation_cap_passed = operation_share <= config.max_operation_share
    full_canary_passed = all(
        improvement >= config.min_full_canary_improvement for improvement in improvements.values()
    )
    paired_canary_passed = paired.get("target_met") is True
    gates: dict[str, dict[str, object]] = {
        "certified_yield": {
            "minimum_total": config.min_certified_yield,
            "minimum_validation": config.min_diagnostic_yield,
            "minimum_test": config.min_diagnostic_yield,
            "observed_total": admitted_count,
            "observed_by_split": dict(sorted(split_counts.items())),
            "passed": yield_passed,
        },
        "independent_audit": {
            "expected": admitted_count,
            "verified": len(audits),
            "passed": audit_passed,
        },
        "family_mix": {
            "minimum_n22_share": config.min_n22_share,
            "maximum_n21_share": config.max_n21_share,
            "observed_n22_share": n22_share,
            "observed_n21_share": n21_share,
            "passed": family_mix_passed,
        },
        "operation_cap": {
            "maximum_share": config.max_operation_share,
            "observed_maximum_share": operation_share,
            "passed": operation_cap_passed,
        },
        "full_canary_improvement": {
            "minimum_absolute_each_split": config.min_full_canary_improvement,
            "observed": improvements,
            "passed": full_canary_passed,
        },
        "paired_shortcut_canary": {
            "target_balanced_accuracy_below": config.paired_canary_target,
            "validation": _metric(paired, "validation") if "diagnostics" in paired else None,
            "test": _metric(paired, "test") if "diagnostics" in paired else None,
            "passed": paired_canary_passed,
        },
    }
    pilot_passed = all(cast(bool, gate["passed"]) for gate in gates.values())
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "selection": {
            "domain": SELECTION_DOMAIN,
            "requested": len(sources),
            "quotas": config.selection_quotas,
            "selected_names_sha256": sha256_hex(
                "".join(f"{row.declaration}\n" for row in sources).encode()
            ),
        },
        "counts": {
            "engine_emitted": len(emitted),
            "chosen_before_screen": len(chosen),
            "certified_admitted": admitted_count,
            "excluded": len(exclusions),
            "family": dict(sorted(family_counts.items())),
            "operation": dict(sorted(operation_counts.items())),
            "split": dict(sorted(split_counts.items())),
        },
        "gates": gates,
        "pilot_gate_passed": pilot_passed,
        "decision": {
            "scale_authorized": pilot_passed,
            "training_authorized": False,
            "rebuild_required_before_training": True,
            "final_test_accessed": False,
        },
    }


def _staging_path(config: PilotConfig) -> Path:
    return config.output_root.with_name(f".{config.output_root.name}.partial")


def _selection_rows(sources: Sequence[SourceRow]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "schema_version": 1,
            "selection_domain": SELECTION_DOMAIN,
            "ordinal": index,
            "declaration": source.declaration,
            "ancestry_id": source.ancestry_id,
            "split": source.split,
            "source_record_id": source.trainer.record_id,
            "source_family": source.trainer.family,
            "reference_sha256": sha256_hex(source.trainer.reference_headless.encode()),
        }
        for index, source in enumerate(sources)
    )


def _write_payload(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def _process_from_artifact(config: PilotConfig, *, stage: str) -> ProcessResult:
    process = _read_json(config.output_root / f"{stage}.process.json")
    stdout = (config.output_root / f"{stage}.stdout.jsonl").read_bytes()
    stderr = (config.output_root / f"{stage}.stderr.txt").read_bytes()
    if process.get("stdout_sha256") != sha256_hex(stdout) or process.get(
        "stderr_sha256"
    ) != sha256_hex(stderr):
        raise S1NegativeSkeletonPilotError(f"{stage} process stream hash differs")
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
        raise S1NegativeSkeletonPilotError(f"invalid {stage} process record") from exc
    _validate_process(result, config, stage=stage)
    return result


def _artifact_replay(
    config: PilotConfig,
) -> tuple[
    tuple[SourceRow, ...],
    tuple[EngineCandidate, ...],
    tuple[EngineCandidate, ...],
    tuple[FinalRow, ...],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    tuple[EngineAudit, ...],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, object],
]:
    base_rows, _ = _load_base_rows(config)
    sources = select_sources(config)
    selection_payload = _jsonl_bytes(_selection_rows(sources))
    if (config.output_root / "selection.jsonl").read_bytes() != selection_payload:
        raise S1NegativeSkeletonPilotError("selection artifact differs")
    names_payload = "".join(f"{source.declaration}\n" for source in sources).encode()
    if (config.output_root / "declaration_names.txt").read_bytes() != names_payload:
        raise S1NegativeSkeletonPilotError("declaration names artifact differs")
    expected_primary = render_primary_driver(
        config, _staging_path(config) / "declaration_names.txt"
    )
    if (config.output_root / "primary_driver.lean").read_text(encoding="utf-8") != expected_primary:
        raise S1NegativeSkeletonPilotError("primary driver differs")
    primary = _process_from_artifact(config, stage="primary")
    emitted, _ = _parse_primary(primary.stdout, sources)
    chosen = choose_candidates(emitted, sources)
    tokenizer = _load_tokenizer(config)
    admitted, negative_rows, certificates, exclusions = _screen_and_project(
        config, chosen, sources, tokenizer
    )
    expected_audit = render_audit_driver(config, admitted)
    if (config.output_root / "audit_driver.lean").read_text(encoding="utf-8") != expected_audit:
        raise S1NegativeSkeletonPilotError("audit driver differs")
    audit_result = _process_from_artifact(config, stage="audit")
    audits = _parse_audits(audit_result.stdout, admitted)
    baseline, augmented, paired = _run_canaries(
        config, base_rows, sources, admitted, negative_rows, tokenizer
    )
    summary = _summary(
        config,
        sources,
        emitted,
        chosen,
        admitted,
        negative_rows,
        audits,
        exclusions,
        baseline,
        augmented,
        paired,
    )
    return (
        sources,
        emitted,
        chosen,
        negative_rows,
        certificates,
        exclusions,
        audits,
        baseline,
        augmented,
        paired,
        summary,
    )


def verify_pilot(config: PilotConfig) -> dict[str, Any]:
    """Replay selection, parsing, audits, projections, and all canary gates."""

    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise S1NegativeSkeletonPilotError("pilot root must be a non-symlink directory")
    observed = {path.name for path in config.output_root.iterdir() if path.is_file()}
    if observed != _OUTPUTS:
        raise S1NegativeSkeletonPilotError("pilot output file set differs")
    manifest = _read_json(config.output_root / "manifest.json")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _STATIC_OUTPUTS:
        raise S1NegativeSkeletonPilotError("pilot manifest output inventory differs")
    for name, raw_binding in outputs.items():
        if not isinstance(raw_binding, Mapping):
            raise S1NegativeSkeletonPilotError(f"invalid output binding: {name}")
        path = config.output_root / name
        if (
            path.is_symlink()
            or raw_binding.get("path") != str(path)
            or raw_binding.get("sha256") != hash_file(path)
        ):
            raise S1NegativeSkeletonPilotError(f"output binding differs: {name}")
    (
        _,
        _,
        chosen,
        negative_rows,
        certificates,
        exclusions,
        audits,
        baseline,
        augmented,
        paired,
        summary,
    ) = _artifact_replay(config)
    expected_payloads = {
        "selected_candidates.jsonl": _jsonl_bytes(
            row.model_dump(mode="json", by_alias=True) for row in chosen
        ),
        "exclusions.jsonl": _jsonl_bytes(exclusions),
        "trainer_records.jsonl": _jsonl_bytes(
            row.trainer.model_dump(mode="json") for row in negative_rows
        ),
        "certificates.jsonl": _jsonl_bytes(
            {
                **certificate,
                "audit_verified": True,
                "audit_mode": "independent-root-reconstruction",
            }
            for certificate in certificates
        ),
        "baseline_canary.json": _canonical_line(baseline),
        "augmented_canary.json": _canonical_line(augmented),
        "paired_canary.json": _canonical_line(paired),
        "summary.json": _canonical_line(summary),
    }
    for name, payload in expected_payloads.items():
        if (config.output_root / name).read_bytes() != payload:
            raise S1NegativeSkeletonPilotError(f"replayed output differs: {name}")
    if len(audits) != len(negative_rows):
        raise S1NegativeSkeletonPilotError("audit/trainer count differs")
    if (
        manifest.get("status") != "completed"
        or manifest.get("config_sha256") != hash_canonical(config.model_dump(mode="json"))
        or manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("negative_engine_sha256") != config.inputs["negative_engine"].sha256
        or manifest.get("summary") != summary
        or manifest.get("execution", {}).get("final_test_accessed") is not False
        or manifest.get("privacy")
        != {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        }
    ):
        raise S1NegativeSkeletonPilotError("pilot manifest contract differs")
    return manifest


def materialize_pilot(config: PilotConfig) -> dict[str, Any]:
    """Run two bounded Lean stages and atomically freeze the measured pilot."""

    if config.output_root.exists():
        return verify_pilot(config)
    verify_input_bindings(config)
    base_rows, _ = _load_base_rows(config)
    sources = select_sources(config)
    staging = _staging_path(config)
    if staging.exists():
        raise S1NegativeSkeletonPilotError(f"stale pilot staging root exists: {staging}")
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o700)
    try:
        selection_rows = _selection_rows(sources)
        _write_payload(staging / "selection.jsonl", _jsonl_bytes(selection_rows))
        _write_payload(
            staging / "declaration_names.txt",
            "".join(f"{source.declaration}\n" for source in sources).encode(),
        )
        primary_driver = render_primary_driver(config, staging / "declaration_names.txt")
        _write_payload(staging / "primary_driver.lean", primary_driver.encode())
        primary = _run_lean(staging / "primary_driver.lean", config)
        _validate_process(primary, config, stage="primary")
        _write_payload(staging / "primary.stdout.jsonl", primary.stdout)
        _write_payload(staging / "primary.stderr.txt", primary.stderr)
        _write_payload(
            staging / "primary.process.json",
            _canonical_line(_process_payload(primary, config, stage="primary")),
        )
        emitted, _ = _parse_primary(primary.stdout, sources)
        chosen = choose_candidates(emitted, sources)
        tokenizer = _load_tokenizer(config)
        admitted, negative_rows, certificates, exclusions = _screen_and_project(
            config, chosen, sources, tokenizer
        )
        _write_payload(
            staging / "selected_candidates.jsonl",
            _jsonl_bytes(row.model_dump(mode="json", by_alias=True) for row in chosen),
        )
        _write_payload(staging / "exclusions.jsonl", _jsonl_bytes(exclusions))
        audit_driver = render_audit_driver(config, admitted)
        _write_payload(staging / "audit_driver.lean", audit_driver.encode())
        audit = _run_lean(staging / "audit_driver.lean", config)
        _validate_process(audit, config, stage="audit")
        _write_payload(staging / "audit.stdout.jsonl", audit.stdout)
        _write_payload(staging / "audit.stderr.txt", audit.stderr)
        _write_payload(
            staging / "audit.process.json",
            _canonical_line(_process_payload(audit, config, stage="audit")),
        )
        audits = _parse_audits(audit.stdout, admitted)
        baseline, augmented, paired = _run_canaries(
            config, base_rows, sources, admitted, negative_rows, tokenizer
        )
        summary = _summary(
            config,
            sources,
            emitted,
            chosen,
            admitted,
            negative_rows,
            audits,
            exclusions,
            baseline,
            augmented,
            paired,
        )
        payloads = {
            "trainer_records.jsonl": _jsonl_bytes(
                row.trainer.model_dump(mode="json") for row in negative_rows
            ),
            "certificates.jsonl": _jsonl_bytes(
                {
                    **certificate,
                    "audit_verified": True,
                    "audit_mode": "independent-root-reconstruction",
                }
                for certificate in certificates
            ),
            "baseline_canary.json": _canonical_line(baseline),
            "augmented_canary.json": _canonical_line(augmented),
            "paired_canary.json": _canonical_line(paired),
            "summary.json": _canonical_line(summary),
        }
        for name, payload in payloads.items():
            _write_payload(staging / name, payload)
        manifest = {
            "schema_version": 1,
            "method_version": METHOD_VERSION,
            "status": "completed",
            "config_sha256": hash_canonical(config.model_dump(mode="json")),
            "implementation_module_sha256": hash_file(Path(__file__)),
            "negative_engine_sha256": config.inputs["negative_engine"].sha256,
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
                "audit_lean_exit_code": audit.exit_code,
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
    return verify_pilot(config)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run-pilot", "verify-pilot", "show-selection"))
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = production_config(cast(Path, args.output_root))
    if args.command == "show-selection":
        print(json.dumps(_selection_rows(select_sources(config)), sort_keys=True))
        return 0
    manifest = materialize_pilot(config) if args.command == "run-pilot" else verify_pilot(config)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
