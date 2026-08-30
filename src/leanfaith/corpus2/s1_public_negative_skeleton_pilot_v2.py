"""Rerun the frozen 96-declaration N21/N22 pilot with the full-skeleton engine.

This additive v2 runner reuses the exact 72/12/12 public source selection and
all preregistered v1 gates.  Only the typed Lean engine and its evidence schema
change: candidates may occur at nested logical paths and must bind an
exhaustively evaluated full Boolean skeleton.  The runner never reads
``final_test`` and cannot authorize training directly.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

import leanfaith.corpus2.s1_public_negative_skeleton_pilot as v1
from leanfaith.collect2.postprocess import GoldenBlocklist
from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.corpus2.build_v1 import FinalRow
from leanfaith.eval.m1_runtime import pack_pair
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.train2.trainer import TrainingRecord

METHOD_VERSION: Literal["s1_public_negative_skeleton_pilot_v2"] = (
    "s1_public_negative_skeleton_pilot_v2"
)
SOURCE_REVISION = v1.SOURCE_REVISION
EXPECTED_LAKE_VERSION = v1.EXPECTED_LAKE_VERSION
SELECTION_DOMAIN = v1.SELECTION_DOMAIN
SELECTION_QUOTAS = dict(v1.SELECTION_QUOTAS)
SAMPLE_SIZE = v1.SAMPLE_SIZE
MIN_CERTIFIED_YIELD = v1.MIN_CERTIFIED_YIELD
MIN_DIAGNOSTIC_YIELD = v1.MIN_DIAGNOSTIC_YIELD
MIN_FULL_CANARY_IMPROVEMENT = v1.MIN_FULL_CANARY_IMPROVEMENT
PAIRED_CANARY_TARGET = v1.PAIRED_CANARY_TARGET
MIN_N22_SHARE = v1.MIN_N22_SHARE
MAX_N21_SHARE = v1.MAX_N21_SHARE
MAX_OPERATION_SHARE = v1.MAX_OPERATION_SHARE

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_PATH = _REPO_ROOT / "LeanFaith" / "Meta" / "NegativeSkeletonEngineV2.lean"
_BASE_PILOT_MODULE = Path(v1.__file__).resolve()
_NESTED_SMOKE_ROOT = Path(
    "/storage/milikic/leanfaith/corpus2/"
    "s1_public_negative_skeleton_nested_smoke_v1_7805448_d568c8c_exhaustive"
)
_NESTED_SMOKE_MANIFEST_SHA256 = "fd86cc34789944ef285d8877ade408b326b9ecdf82a449a858122172004a549b"
_INPUT_NAMES = frozenset(v1._INPUT_NAMES) | {
    "base_pilot_module",
    "nested_smoke_manifest",
}
_STATIC_OUTPUTS = v1._STATIC_OUTPUTS
_OUTPUTS = _STATIC_OUTPUTS | {"manifest.json"}


class S1NegativeSkeletonPilotV2Error(RuntimeError):
    """A frozen input, Lean result, audit, or unchanged pilot gate failed closed."""


class PilotV2Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_negative_skeleton_pilot_v2"] = METHOD_VERSION
    output_root: Path
    mathlib_root: Path
    tokenizer_root: Path
    inputs: dict[str, v1.FrozenInput]
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
            raise ValueError("v2 skeleton pilot must bind the exact frozen input set")
        if self.selection_quotas != SELECTION_QUOTAS:
            raise ValueError("v2 skeleton pilot split quotas differ")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("v2 skeleton pilot artifacts must be under /storage/milikic")
        return self


class EngineCandidateV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[2] = Field(alias="schemaVersion")
    kind: Literal["candidate"]
    record_kind: Literal["candidate"] = Field(alias="recordKind")
    status: Literal["ok"]
    declaration: str = Field(min_length=1)
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
            raise ValueError("v2 negative skeleton candidate is unchanged")
        if sha256_hex(self.source.encode()) != self.source_type_hash:
            raise ValueError("v2 negative skeleton source hash differs")
        if sha256_hex(self.candidate.encode()) != self.candidate_type_hash:
            raise ValueError("v2 negative skeleton candidate hash differs")
        if not self.candidate_elaborates or self.whole_type_def_eq:
            raise ValueError("v2 negative skeleton type checks differ")
        if self.operation != f"{self.operation_kind}:{self.site_path}":
            raise ValueError("v2 negative skeleton operation/path binding differs")
        if self.evidence != {
            "relation": "schemaInequivalence",
            "exactBooleanSkeleton": True,
            "deduplicatedAtoms": True,
            "fullTruthTableEnumerated": True,
            "rootInfluence": True,
            "separatorVerified": True,
            "contractScope": "abstract-propositional-schema",
        }:
            raise ValueError("v2 negative skeleton separator contract differs")
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
                isinstance(value, str) and v1._HEX64.fullmatch(value) for value in atom_hashes
            )
            or not isinstance(valuation, list)
            or len(valuation) != atom_count
            or not all(isinstance(value, bool) for value in valuation)
            or self.witness.get("valuationSpaceSize") != 2**atom_count
        ):
            raise ValueError("v2 negative skeleton valuation inventory differs")
        if self.witness.get("sourceValue") is self.witness.get("candidateValue"):
            raise ValueError("v2 negative skeleton valuation does not separate")
        return self


class EngineAuditV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[2] = Field(alias="schemaVersion")
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
    audit_mode: Literal["independent-full-skeleton-reconstruction"] = Field(alias="auditMode")

    @model_validator(mode="after")
    def _same_hash(self) -> Self:
        if (
            v1._HEX64.fullmatch(self.expected_candidate_type_hash) is None
            or self.actual_candidate_type_hash != self.expected_candidate_type_hash
        ):
            raise ValueError("independent v2 audit candidate hash differs")
        return self


def production_config(output_root: Path) -> PilotV2Config:
    inputs = {
        name: v1.FrozenInput(path=path, sha256=digest)
        for name, (path, digest) in v1._PRODUCTION_INPUTS.items()
        if name != "negative_engine"
    }
    inputs["negative_engine"] = v1.FrozenInput(
        path=_ENGINE_PATH,
        sha256=hash_file(_ENGINE_PATH),
    )
    inputs["base_pilot_module"] = v1.FrozenInput(
        path=_BASE_PILOT_MODULE,
        sha256=hash_file(_BASE_PILOT_MODULE),
    )
    inputs["nested_smoke_manifest"] = v1.FrozenInput(
        path=_NESTED_SMOKE_ROOT / "manifest.json",
        sha256=_NESTED_SMOKE_MANIFEST_SHA256,
    )
    return PilotV2Config(
        output_root=output_root,
        mathlib_root=v1._MATHLIB_ROOT,
        tokenizer_root=v1._TOKENIZER_ROOT,
        inputs=inputs,
    )


def verify_input_bindings(config: PilotV2Config) -> None:
    v1.verify_input_bindings(cast(Any, config))
    smoke = v1._read_json(config.inputs["nested_smoke_manifest"].path)
    if (
        smoke.get("status") != "completed"
        or smoke.get("negative_engine_v2_sha256") != config.inputs["negative_engine"].sha256
        or smoke.get("summary", {}).get("status") != "passed"
        or smoke.get("summary", {}).get("decision", {}).get("same_fixed_96_pilot_rerun_authorized")
        is not True
        or smoke.get("execution", {}).get("final_test_accessed") is not False
        or smoke.get("execution", {}).get("training_launched") is not False
    ):
        raise S1NegativeSkeletonPilotV2Error("nested smoke authorization binding differs")


def select_sources(config: PilotV2Config) -> tuple[v1.SourceRow, ...]:
    return v1.select_sources(cast(Any, config))


def _engine_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    imports = [line.strip() for line in lines if line.strip().startswith("import ")]
    if imports != ["import Lean"]:
        raise S1NegativeSkeletonPilotV2Error("v2 negative engine imports differ")
    body = "\n".join(line for line in lines if not line.strip().startswith("import ")).strip()
    markers = (
        "namespace LeanFaith.Meta.NegativeSkeletonEngineV2Helper",
        "lfNegativeSkeletonV2Batch",
        "lfAuditNegativeSkeletonV2",
        "end LeanFaith.Meta.NegativeSkeletonEngineV2Helper",
    )
    if not all(marker in body for marker in markers):
        raise S1NegativeSkeletonPilotV2Error("v2 negative engine command contract differs")
    return body + "\n"


def render_primary_driver(config: PilotV2Config, names_path: Path) -> str:
    return (
        "import Mathlib\n\n"
        + _engine_body(config.inputs["negative_engine"].path)
        + "\nset_option maxHeartbeats 0 in\n"
        + f"lfNegativeSkeletonV2Batch {v1._lean_string(str(names_path))}\n"
    )


def render_audit_driver(config: PilotV2Config, candidates: Sequence[EngineCandidateV2]) -> str:
    commands = []
    for candidate in candidates:
        arguments = " ".join(
            v1._lean_string(value)
            for value in (
                candidate.declaration,
                candidate.family,
                candidate.operation,
                candidate.candidate_type_hash,
            )
        )
        commands.append(f"lfAuditNegativeSkeletonV2 {arguments}")
    return (
        "import Mathlib\n\n"
        + _engine_body(config.inputs["negative_engine"].path)
        + "\n"
        + "\n".join(commands)
        + "\n"
    )


def _parse_primary(
    payload: bytes,
    selected_sources: Sequence[v1.SourceRow],
) -> tuple[tuple[EngineCandidateV2, ...], tuple[dict[str, Any], ...]]:
    source_by_declaration = {row.declaration: row for row in selected_sources}
    terminals: dict[str, dict[str, Any]] = {}
    candidates: list[EngineCandidateV2] = []
    batch: dict[str, Any] | None = None
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise S1NegativeSkeletonPilotV2Error(
                f"primary stdout:{line_number}: invalid JSON: {exc}"
            ) from exc
        if (
            not isinstance(raw, dict)
            or v1._canonical_line(raw).rstrip(b"\n") != line
            or raw.get("schemaVersion") != 2
        ):
            raise S1NegativeSkeletonPilotV2Error("primary v2 engine row contract differs")
        row = cast(dict[str, Any], raw)
        kind = row.get("recordKind")
        if kind == "candidate":
            try:
                candidate = EngineCandidateV2.model_validate(row)
            except ValidationError as exc:
                raise S1NegativeSkeletonPilotV2Error(
                    f"primary stdout:{line_number}: invalid v2 candidate: {exc}"
                ) from exc
            source = source_by_declaration.get(candidate.declaration)
            if source is None or candidate.source != source.trainer.reference_headless:
                raise S1NegativeSkeletonPilotV2Error("v2 engine candidate/source join differs")
            candidates.append(candidate)
        elif kind == "status":
            declaration = row.get("declaration")
            if not isinstance(declaration, str) or declaration in terminals:
                raise S1NegativeSkeletonPilotV2Error("v2 engine terminal identity differs")
            terminals[declaration] = row
        elif kind == "batch":
            if batch is not None:
                raise S1NegativeSkeletonPilotV2Error("duplicate v2 engine batch terminal")
            batch = row
        else:
            raise S1NegativeSkeletonPilotV2Error("unknown primary v2 engine row kind")
    if set(terminals) != set(source_by_declaration):
        raise S1NegativeSkeletonPilotV2Error("v2 engine terminal declaration set differs")
    if any(row.get("status") != "complete" for row in terminals.values()):
        raise S1NegativeSkeletonPilotV2Error("one or more v2 engine declarations failed")
    if (
        batch is None
        or batch.get("declarationCount") != SAMPLE_SIZE
        or batch.get("completedCount") != SAMPLE_SIZE
        or batch.get("failedCount") != 0
    ):
        raise S1NegativeSkeletonPilotV2Error("v2 engine batch terminal differs")
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
        raise S1NegativeSkeletonPilotV2Error("v2 engine emitted duplicate candidate keys")
    return tuple(candidates), tuple(terminals[name] for name in sorted(terminals))


def choose_candidates(
    candidates: Sequence[EngineCandidateV2],
    selected_sources: Sequence[v1.SourceRow],
) -> tuple[EngineCandidateV2, ...]:
    return cast(
        tuple[EngineCandidateV2, ...],
        v1.choose_candidates(cast(Any, candidates), selected_sources),
    )


def _parse_audits(
    payload: bytes, selected: Sequence[EngineCandidateV2]
) -> tuple[EngineAuditV2, ...]:
    expected = {
        (row.declaration, row.family, row.operation, row.candidate_type_hash) for row in selected
    }
    audits: list[EngineAuditV2] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
            if v1._canonical_line(raw).rstrip(b"\n") != line:
                raise ValueError("noncanonical audit row")
            audit = EngineAuditV2.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise S1NegativeSkeletonPilotV2Error(
                f"audit stdout:{line_number}: invalid v2 audit: {exc}"
            ) from exc
        audits.append(audit)
    observed = {
        (row.declaration, row.family, row.operation, row.expected_candidate_type_hash)
        for row in audits
    }
    if len(audits) != len(expected) or observed != expected:
        raise S1NegativeSkeletonPilotV2Error("independent v2 audit set differs")
    return tuple(audits)


def _screen_and_project(
    config: PilotV2Config,
    selected: Sequence[EngineCandidateV2],
    sources: Sequence[v1.SourceRow],
    tokenizer: Any,
) -> tuple[
    tuple[EngineCandidateV2, ...],
    tuple[FinalRow, ...],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
]:
    source_by_declaration = {row.declaration: row for row in sources}
    blocklist = GoldenBlocklist.load(config.inputs["golden_blocklist"].path)
    admitted: list[EngineCandidateV2] = []
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
            raise S1NegativeSkeletonPilotV2Error("tokenizer returned an empty packed pair")
        if forward > 1024 or reverse > 1024:
            reason = "overlength"
        if reason is not None:
            exclusions.append(
                {
                    "schema_version": 1,
                    "declaration": candidate.declaration,
                    "family": candidate.family,
                    "operation": candidate.operation,
                    "site_path": candidate.site_path,
                    "candidate_type_hash": candidate.candidate_type_hash,
                    "reason": reason,
                    "forward_tokens": forward,
                    "reverse_tokens": reverse,
                }
            )
            continue
        pair_hashes.add(pair_key)
        record_id = "s1_negative_skeleton_v2:" + hash_canonical(
            {
                "schema": "s1_negative_skeleton_trainer_projection_v2",
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
            source="typed_negative_skeleton_v2",
            weight=1.0,
        )
        provenance = {
            "schema_version": 2,
            "method_version": METHOD_VERSION,
            "record_id": record_id,
            "source_record_id": source.trainer.record_id,
            "declaration": candidate.declaration,
            "source_revision": config.mathlib_revision,
            "family": candidate.family,
            "operation": candidate.operation,
            "operation_kind": candidate.operation_kind,
            "site_path": candidate.site_path,
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


def _summary(
    config: PilotV2Config,
    sources: Sequence[v1.SourceRow],
    emitted: Sequence[EngineCandidateV2],
    chosen: Sequence[EngineCandidateV2],
    admitted: Sequence[EngineCandidateV2],
    negative_rows: Sequence[FinalRow],
    audits: Sequence[EngineAuditV2],
    exclusions: Sequence[Mapping[str, object]],
    baseline: Mapping[str, Any],
    augmented: Mapping[str, Any],
    paired: Mapping[str, Any],
) -> dict[str, object]:
    split_counts = Counter(row.split for row in negative_rows)
    family_counts = Counter(row.family for row in admitted)
    operation_kind_counts = Counter(row.operation_kind for row in admitted)
    exact_operation_counts = Counter(row.operation for row in admitted)
    root_nested_counts = Counter(
        "root" if row.site_path == "/root-body" else "nested" for row in admitted
    )
    admitted_count = len(admitted)
    n22_share = family_counts["N22"] / admitted_count if admitted_count else 0.0
    n21_share = family_counts["N21"] / admitted_count if admitted_count else 0.0
    operation_share = (
        max(operation_kind_counts.values(), default=0) / admitted_count if admitted_count else 0.0
    )
    improvements = {
        split: v1._metric(baseline, split) - v1._metric(augmented, split)
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
            "basis": "operation_kind",
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
            "validation": v1._metric(paired, "validation") if "diagnostics" in paired else None,
            "test": v1._metric(paired, "test") if "diagnostics" in paired else None,
            "passed": paired_canary_passed,
        },
    }
    pilot_passed = all(cast(bool, gate["passed"]) for gate in gates.values())
    return {
        "schema_version": 2,
        "method_version": METHOD_VERSION,
        "selection": {
            "domain": SELECTION_DOMAIN,
            "requested": len(sources),
            "quotas": config.selection_quotas,
            "selected_names_sha256": sha256_hex(
                "".join(f"{row.declaration}\n" for row in sources).encode()
            ),
            "identical_to_v1": True,
        },
        "counts": {
            "engine_emitted": len(emitted),
            "chosen_before_screen": len(chosen),
            "certified_admitted": admitted_count,
            "excluded": len(exclusions),
            "family": dict(sorted(family_counts.items())),
            "operation_kind": dict(sorted(operation_kind_counts.items())),
            "exact_operation": dict(sorted(exact_operation_counts.items())),
            "site_depth": dict(sorted(root_nested_counts.items())),
            "split": dict(sorted(split_counts.items())),
        },
        "gates": gates,
        "pilot_gate_passed": pilot_passed,
        "decision": {
            "scale_authorized": pilot_passed,
            "training_authorized": False,
            "rebuild_required_before_training": True,
            "sample_size_increase_authorized": False,
            "final_test_accessed": False,
        },
    }


def _staging_path(config: PilotV2Config) -> Path:
    return config.output_root.with_name(f".{config.output_root.name}.partial")


def _artifact_replay(
    config: PilotV2Config,
) -> tuple[
    tuple[v1.SourceRow, ...],
    tuple[EngineCandidateV2, ...],
    tuple[EngineCandidateV2, ...],
    tuple[FinalRow, ...],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
    tuple[EngineAuditV2, ...],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, object],
]:
    base_rows, _ = v1._load_base_rows(cast(Any, config))
    sources = select_sources(config)
    selection_payload = v1._jsonl_bytes(v1._selection_rows(sources))
    if (config.output_root / "selection.jsonl").read_bytes() != selection_payload:
        raise S1NegativeSkeletonPilotV2Error("v2 selection artifact differs")
    names_payload = "".join(f"{source.declaration}\n" for source in sources).encode()
    if (config.output_root / "declaration_names.txt").read_bytes() != names_payload:
        raise S1NegativeSkeletonPilotV2Error("v2 declaration names artifact differs")
    expected_primary = render_primary_driver(
        config, _staging_path(config) / "declaration_names.txt"
    )
    if (config.output_root / "primary_driver.lean").read_text(encoding="utf-8") != expected_primary:
        raise S1NegativeSkeletonPilotV2Error("v2 primary driver differs")
    primary = v1._process_from_artifact(cast(Any, config), stage="primary")
    emitted, _ = _parse_primary(primary.stdout, sources)
    chosen = choose_candidates(emitted, sources)
    tokenizer = v1._load_tokenizer(cast(Any, config))
    admitted, negative_rows, certificates, exclusions = _screen_and_project(
        config, chosen, sources, tokenizer
    )
    expected_audit = render_audit_driver(config, admitted)
    if (config.output_root / "audit_driver.lean").read_text(encoding="utf-8") != expected_audit:
        raise S1NegativeSkeletonPilotV2Error("v2 audit driver differs")
    audit_result = v1._process_from_artifact(cast(Any, config), stage="audit")
    audits = _parse_audits(audit_result.stdout, admitted)
    baseline, augmented, paired = v1._run_canaries(
        cast(Any, config),
        base_rows,
        sources,
        cast(Any, admitted),
        negative_rows,
        tokenizer,
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


def verify_pilot(config: PilotV2Config) -> dict[str, Any]:
    """Replay selection, v2 parsing, audits, projections, and unchanged gates."""

    verify_input_bindings(config)
    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise S1NegativeSkeletonPilotV2Error("v2 pilot root must be a non-symlink directory")
    observed = {path.name for path in config.output_root.iterdir() if path.is_file()}
    if observed != _OUTPUTS:
        raise S1NegativeSkeletonPilotV2Error("v2 pilot output file set differs")
    manifest = v1._read_json(config.output_root / "manifest.json")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _STATIC_OUTPUTS:
        raise S1NegativeSkeletonPilotV2Error("v2 pilot manifest output inventory differs")
    for name, raw_binding in outputs.items():
        if not isinstance(raw_binding, Mapping):
            raise S1NegativeSkeletonPilotV2Error(f"invalid v2 output binding: {name}")
        path = config.output_root / name
        if (
            path.is_symlink()
            or raw_binding.get("path") != str(path)
            or raw_binding.get("sha256") != hash_file(path)
        ):
            raise S1NegativeSkeletonPilotV2Error(f"v2 output binding differs: {name}")
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
        "selected_candidates.jsonl": v1._jsonl_bytes(
            row.model_dump(mode="json", by_alias=True) for row in chosen
        ),
        "exclusions.jsonl": v1._jsonl_bytes(exclusions),
        "trainer_records.jsonl": v1._jsonl_bytes(
            row.trainer.model_dump(mode="json") for row in negative_rows
        ),
        "certificates.jsonl": v1._jsonl_bytes(
            {
                **certificate,
                "audit_verified": True,
                "audit_mode": "independent-full-skeleton-reconstruction",
            }
            for certificate in certificates
        ),
        "baseline_canary.json": v1._canonical_line(baseline),
        "augmented_canary.json": v1._canonical_line(augmented),
        "paired_canary.json": v1._canonical_line(paired),
        "summary.json": v1._canonical_line(summary),
    }
    for name, payload in expected_payloads.items():
        if (config.output_root / name).read_bytes() != payload:
            raise S1NegativeSkeletonPilotV2Error(f"replayed v2 output differs: {name}")
    if len(audits) != len(negative_rows):
        raise S1NegativeSkeletonPilotV2Error("v2 audit/trainer count differs")
    if (
        manifest.get("status") != "completed"
        or manifest.get("config_sha256") != hash_canonical(config.model_dump(mode="json"))
        or manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("base_pilot_module_sha256") != config.inputs["base_pilot_module"].sha256
        or manifest.get("negative_engine_v2_sha256") != config.inputs["negative_engine"].sha256
        or manifest.get("summary") != summary
        or manifest.get("execution", {}).get("final_test_accessed") is not False
        or manifest.get("privacy")
        != {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        }
    ):
        raise S1NegativeSkeletonPilotV2Error("v2 pilot manifest contract differs")
    return manifest


def materialize_pilot(config: PilotV2Config) -> dict[str, Any]:
    """Run the fixed 96-declaration v2 pilot and atomically freeze its decision."""

    if config.output_root.exists():
        return verify_pilot(config)
    verify_input_bindings(config)
    base_rows, _ = v1._load_base_rows(cast(Any, config))
    sources = select_sources(config)
    staging = _staging_path(config)
    if staging.exists():
        raise S1NegativeSkeletonPilotV2Error(f"stale v2 pilot staging root exists: {staging}")
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o700)
    try:
        v1._write_payload(staging / "selection.jsonl", v1._jsonl_bytes(v1._selection_rows(sources)))
        v1._write_payload(
            staging / "declaration_names.txt",
            "".join(f"{source.declaration}\n" for source in sources).encode(),
        )
        primary_driver = render_primary_driver(config, staging / "declaration_names.txt")
        v1._write_payload(staging / "primary_driver.lean", primary_driver.encode())
        primary = v1._run_lean(staging / "primary_driver.lean", cast(Any, config))
        v1._write_payload(staging / "primary.stdout.jsonl", primary.stdout)
        v1._write_payload(staging / "primary.stderr.txt", primary.stderr)
        v1._write_payload(
            staging / "primary.process.json",
            v1._canonical_line(v1._process_payload(primary, cast(Any, config), stage="primary")),
        )
        v1._validate_process(primary, cast(Any, config), stage="primary")
        emitted, _ = _parse_primary(primary.stdout, sources)
        chosen = choose_candidates(emitted, sources)
        tokenizer = v1._load_tokenizer(cast(Any, config))
        admitted, negative_rows, certificates, exclusions = _screen_and_project(
            config, chosen, sources, tokenizer
        )
        v1._write_payload(
            staging / "selected_candidates.jsonl",
            v1._jsonl_bytes(row.model_dump(mode="json", by_alias=True) for row in chosen),
        )
        v1._write_payload(staging / "exclusions.jsonl", v1._jsonl_bytes(exclusions))
        audit_driver = render_audit_driver(config, admitted)
        v1._write_payload(staging / "audit_driver.lean", audit_driver.encode())
        audit = v1._run_lean(staging / "audit_driver.lean", cast(Any, config))
        v1._write_payload(staging / "audit.stdout.jsonl", audit.stdout)
        v1._write_payload(staging / "audit.stderr.txt", audit.stderr)
        v1._write_payload(
            staging / "audit.process.json",
            v1._canonical_line(v1._process_payload(audit, cast(Any, config), stage="audit")),
        )
        v1._validate_process(audit, cast(Any, config), stage="audit")
        audits = _parse_audits(audit.stdout, admitted)
        baseline, augmented, paired = v1._run_canaries(
            cast(Any, config),
            base_rows,
            sources,
            cast(Any, admitted),
            negative_rows,
            tokenizer,
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
            "trainer_records.jsonl": v1._jsonl_bytes(
                row.trainer.model_dump(mode="json") for row in negative_rows
            ),
            "certificates.jsonl": v1._jsonl_bytes(
                {
                    **certificate,
                    "audit_verified": True,
                    "audit_mode": "independent-full-skeleton-reconstruction",
                }
                for certificate in certificates
            ),
            "baseline_canary.json": v1._canonical_line(baseline),
            "augmented_canary.json": v1._canonical_line(augmented),
            "paired_canary.json": v1._canonical_line(paired),
            "summary.json": v1._canonical_line(summary),
        }
        for name, payload in payloads.items():
            v1._write_payload(staging / name, payload)
        manifest = {
            "schema_version": 2,
            "method_version": METHOD_VERSION,
            "status": "completed",
            "config_sha256": hash_canonical(config.model_dump(mode="json")),
            "implementation_module_sha256": hash_file(Path(__file__)),
            "base_pilot_module_sha256": config.inputs["base_pilot_module"].sha256,
            "negative_engine_v2_sha256": config.inputs["negative_engine"].sha256,
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
                "primary_timeout_seconds": config.timeout_seconds,
                "audit_timeout_seconds": config.timeout_seconds,
                "external_calls": False,
                "final_test_accessed": False,
                "training_launched": False,
            },
        }
        v1._write_payload(staging / "manifest.json", v1._canonical_line(manifest))
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
        print(json.dumps(v1._selection_rows(select_sources(config)), sort_keys=True))
        return 0
    manifest = materialize_pilot(config) if args.command == "run-pilot" else verify_pilot(config)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
