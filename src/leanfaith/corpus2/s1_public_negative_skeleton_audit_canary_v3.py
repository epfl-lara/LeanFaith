"""Audit the frozen balanced v3 subset, then run unchanged canary gates.

This additive runner binds the passed fixed-sample feasibility artifact, reuses
its exact 24-row selection without regenerating the 96-name candidate pool,
independently reconstructs every selected Lean candidate, and only after all
24 audits pass runs the preregistered full and paired lexical canaries.  It
never reads ``final_test`` and cannot authorize training directly.
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

import leanfaith.corpus2.s1_public_negative_skeleton_feasibility_v3 as feasibility
import leanfaith.corpus2.s1_public_negative_skeleton_pilot as v1
from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.corpus2.build_v1 import FinalRow
from leanfaith.eval.m1_runtime import pack_pair
from leanfaith.train2.trainer import TrainingRecord

METHOD_VERSION: Literal["s1_public_negative_skeleton_audit_canary_v3"] = (
    "s1_public_negative_skeleton_audit_canary_v3"
)
SOURCE_REVISION = feasibility.SOURCE_REVISION
EXPECTED_LAKE_VERSION = feasibility.EXPECTED_LAKE_VERSION
SELECTION_QUOTAS = dict(feasibility.SELECTION_QUOTAS)
TARGET_TOTAL = feasibility.TARGET_TOTAL
MIN_DIAGNOSTIC_YIELD = feasibility.MIN_DIAGNOSTIC_YIELD
MIN_N22_SHARE = feasibility.MIN_N22_SHARE
MAX_N21_SHARE = feasibility.MAX_N21_SHARE
MAX_OPERATION_SHARE = feasibility.MAX_OPERATION_SHARE
MIN_FULL_CANARY_IMPROVEMENT = v1.MIN_FULL_CANARY_IMPROVEMENT
PAIRED_CANARY_TARGET = v1.PAIRED_CANARY_TARGET

_FEASIBILITY_ROOT = Path(
    "/storage/milikic/leanfaith/corpus2/"
    "s1_public_negative_skeleton_feasibility_v3_4fd2c6a_d568c8c_balanced"
)
_FEASIBILITY_MANIFEST_SHA256 = "e02af59acf32fa0ee011cf00edab33f08aa9af7ec3e557d2469fd32f93a8b475"
_FEASIBILITY_SELECTION_SHA256 = "0358e0297b3addf01da8a8da13f8c9c44d74858a89f10e54fb708ca0b306289f"
_FEASIBILITY_POOL_SHA256 = "916da404e2d57d3d4b85e49574a2e82e35f5e227983ffeb2fdd6484d3c09f430"
_BASE_FEASIBILITY_MODULE = Path(feasibility.__file__).resolve()
_INPUT_NAMES = feasibility._INPUT_NAMES | {
    "base_feasibility_module",
    "feasibility_manifest",
    "feasibility_selection",
    "feasibility_candidate_pool",
}
_STATIC_OUTPUTS = frozenset(
    {
        "selection.jsonl",
        "selected_candidates.jsonl",
        "audit_driver.lean",
        "audit.stdout.jsonl",
        "audit.stderr.txt",
        "audit.process.json",
        "exclusions.jsonl",
        "trainer_records.jsonl",
        "certificates.jsonl",
        "baseline_canary.json",
        "augmented_canary.json",
        "paired_canary.json",
        "summary.json",
    }
)
_OUTPUTS = _STATIC_OUTPUTS | {"manifest.json"}


class NegativeSkeletonAuditCanaryError(RuntimeError):
    """A frozen input, selected audit, projection, canary, or replay differed."""


class AuditCanaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    method_version: Literal["s1_public_negative_skeleton_audit_canary_v3"] = METHOD_VERSION
    output_root: Path
    mathlib_root: Path
    tokenizer_root: Path
    inputs: dict[str, v1.FrozenInput]
    selection_quotas: dict[str, int] = Field(default_factory=lambda: dict(SELECTION_QUOTAS))
    min_certified_yield: Literal[24] = TARGET_TOTAL
    min_diagnostic_yield: Literal[4] = MIN_DIAGNOSTIC_YIELD
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
            raise ValueError("v3 audit-canary must bind the exact frozen input set")
        if self.selection_quotas != SELECTION_QUOTAS:
            raise ValueError("v3 audit-canary split quotas differ")
        if self.enforce_storage_root and not self.output_root.resolve().is_relative_to(
            Path("/storage/milikic")
        ):
            raise ValueError("v3 audit-canary artifacts must be under /storage/milikic")
        return self


class EngineAuditV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal[3] = Field(alias="schemaVersion")
    kind: Literal["audit"]
    record_kind: Literal["audit"] = Field(alias="recordKind")
    declaration: str = Field(min_length=1)
    family: Literal["N21", "N22"]
    operation: str = Field(min_length=1)
    expected_candidate_type_hash: str = Field(
        alias="expectedCandidateTypeHash", pattern=r"^[0-9a-f]{64}$"
    )
    actual_candidate_type_hash: str = Field(
        alias="actualCandidateTypeHash", pattern=r"^[0-9a-f]{64}$"
    )
    verified: StrictBool
    status: str
    reason: str
    audit_mode: Literal["independent-implication-aware-reconstruction"] = Field(alias="auditMode")

    @model_validator(mode="after")
    def _verified_exact_hash(self) -> Self:
        if (
            not self.verified
            or self.status != "verified"
            or self.reason != "verified"
            or self.actual_candidate_type_hash != self.expected_candidate_type_hash
        ):
            raise ValueError("independent v3 audit did not verify the exact hash")
        return self


def production_config(output_root: Path) -> AuditCanaryConfig:
    base = feasibility.production_config(output_root)
    inputs = dict(base.inputs)
    inputs.update(
        {
            "base_feasibility_module": v1.FrozenInput(
                path=_BASE_FEASIBILITY_MODULE,
                sha256=hash_file(_BASE_FEASIBILITY_MODULE),
            ),
            "feasibility_manifest": v1.FrozenInput(
                path=_FEASIBILITY_ROOT / "manifest.json",
                sha256=_FEASIBILITY_MANIFEST_SHA256,
            ),
            "feasibility_selection": v1.FrozenInput(
                path=_FEASIBILITY_ROOT / "feasible_selection.jsonl",
                sha256=_FEASIBILITY_SELECTION_SHA256,
            ),
            "feasibility_candidate_pool": v1.FrozenInput(
                path=_FEASIBILITY_ROOT / "candidate_pool.jsonl",
                sha256=_FEASIBILITY_POOL_SHA256,
            ),
        }
    )
    return AuditCanaryConfig(
        output_root=output_root,
        mathlib_root=base.mathlib_root,
        tokenizer_root=base.tokenizer_root,
        inputs=inputs,
    )


def verify_input_bindings(config: AuditCanaryConfig) -> None:
    for name, binding in sorted(config.inputs.items()):
        if binding.path.is_symlink() or not binding.path.is_file():
            raise NegativeSkeletonAuditCanaryError(f"unsafe or missing frozen input: {name}")
        if hash_file(binding.path) != binding.sha256:
            raise NegativeSkeletonAuditCanaryError(f"frozen audit-canary input differs: {name}")
    feasibility_config = feasibility.production_config(_FEASIBILITY_ROOT)
    feasibility_manifest = feasibility.verify_feasibility(feasibility_config)
    outputs = feasibility_manifest.get("outputs")
    if (
        hash_file(config.inputs["feasibility_manifest"].path)
        != config.inputs["feasibility_manifest"].sha256
        or not isinstance(outputs, Mapping)
        or not isinstance(outputs.get("feasible_selection.jsonl"), Mapping)
        or outputs["feasible_selection.jsonl"].get("sha256")
        != config.inputs["feasibility_selection"].sha256
        or not isinstance(outputs.get("candidate_pool.jsonl"), Mapping)
        or outputs["candidate_pool.jsonl"].get("sha256")
        != config.inputs["feasibility_candidate_pool"].sha256
        or feasibility_manifest.get("summary", {}).get("feasibility_gate_passed") is not True
        or feasibility_manifest.get("summary", {})
        .get("decision", {})
        .get("fixed_subset_audit_and_canary_authorized")
        is not True
        or feasibility_manifest.get("execution", {}).get("audit_launched") is not False
        or feasibility_manifest.get("execution", {}).get("canary_fitted") is not False
        or feasibility_manifest.get("execution", {}).get("final_test_accessed") is not False
        or feasibility_manifest.get("execution", {}).get("training_launched") is not False
    ):
        raise NegativeSkeletonAuditCanaryError("feasibility authorization binding differs")


def select_sources(config: AuditCanaryConfig) -> tuple[v1.SourceRow, ...]:
    return feasibility.select_sources(cast(Any, config))


def _load_candidate_rows(path: Path) -> tuple[feasibility.EngineCandidateV3, ...]:
    candidates: list[feasibility.EngineCandidateV3] = []
    for line_number, raw in v1._iter_jsonl(path):
        try:
            candidates.append(feasibility.EngineCandidateV3.model_validate(raw))
        except ValidationError as exc:
            raise NegativeSkeletonAuditCanaryError(
                f"{path}:{line_number}: invalid v3 candidate: {exc}"
            ) from exc
    return tuple(candidates)


def load_frozen_selection(
    config: AuditCanaryConfig,
    sources: Sequence[v1.SourceRow],
) -> tuple[feasibility.EngineCandidateV3, ...]:
    selected = _load_candidate_rows(config.inputs["feasibility_selection"].path)
    pool = _load_candidate_rows(config.inputs["feasibility_candidate_pool"].path)
    pool_by_key = {
        (
            candidate.declaration,
            candidate.family,
            candidate.operation,
            candidate.candidate_type_hash,
        ): candidate
        for candidate in pool
    }
    selected_keys = [
        (
            candidate.declaration,
            candidate.family,
            candidate.operation,
            candidate.candidate_type_hash,
        )
        for candidate in selected
    ]
    source_declarations = {source.declaration for source in sources}
    if (
        len(selected) != TARGET_TOTAL
        or len(selected_keys) != len(set(selected_keys))
        or len({candidate.declaration for candidate in selected}) != TARGET_TOTAL
        or any(key not in pool_by_key for key in selected_keys)
        or any(candidate.declaration not in source_declarations for candidate in selected)
        or any(
            pool_by_key[key] != candidate
            for key, candidate in zip(selected_keys, selected, strict=True)
        )
    ):
        raise NegativeSkeletonAuditCanaryError("frozen feasibility selection differs")
    return selected


def render_audit_driver(
    config: AuditCanaryConfig,
    selected: Sequence[feasibility.EngineCandidateV3],
) -> str:
    commands: list[str] = []
    for candidate in selected:
        arguments = " ".join(
            v1._lean_string(value)
            for value in (
                candidate.declaration,
                candidate.family,
                candidate.operation,
                candidate.candidate_type_hash,
            )
        )
        commands.append(f"lfAuditNegativeSkeletonV3 {arguments}")
    return (
        "import Mathlib\n\n"
        + feasibility._combined_engine(cast(Any, config))
        + "\n"
        + "\n".join(commands)
        + "\n"
    )


def parse_audits(
    payload: bytes,
    selected: Sequence[feasibility.EngineCandidateV3],
) -> tuple[EngineAuditV3, ...]:
    expected = {
        (
            candidate.declaration,
            candidate.family,
            candidate.operation,
            candidate.candidate_type_hash,
        )
        for candidate in selected
    }
    audits: list[EngineAuditV3] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        try:
            raw = json.loads(line)
            if v1._canonical_line(raw).rstrip(b"\n") != line:
                raise ValueError("noncanonical audit row")
            audit = EngineAuditV3.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise NegativeSkeletonAuditCanaryError(
                f"audit stdout:{line_number}: invalid v3 audit: {exc}"
            ) from exc
        audits.append(audit)
    observed = {
        (
            audit.declaration,
            audit.family,
            audit.operation,
            audit.expected_candidate_type_hash,
        )
        for audit in audits
    }
    if len(audits) != len(expected) or observed != expected:
        raise NegativeSkeletonAuditCanaryError("independent v3 audit set differs")
    return tuple(audits)


def _project_selected(
    config: AuditCanaryConfig,
    selected: Sequence[feasibility.EngineCandidateV3],
    sources: Sequence[v1.SourceRow],
    tokenizer: Any,
) -> tuple[tuple[FinalRow, ...], tuple[dict[str, object], ...]]:
    screened, exclusions = feasibility._screen_candidates(
        cast(Any, config), selected, sources, tokenizer
    )
    if tuple(screened) != tuple(selected) or exclusions:
        raise NegativeSkeletonAuditCanaryError("frozen selected candidate screen differs")
    if len({feasibility._pair_group(candidate) for candidate in selected}) != len(selected):
        raise NegativeSkeletonAuditCanaryError("frozen selected pair keys are not unique")
    source_by_declaration = {source.declaration: source for source in sources}
    rows: list[FinalRow] = []
    certificates: list[dict[str, object]] = []
    for candidate in selected:
        source = source_by_declaration[candidate.declaration]
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
        record_id = "s1_negative_skeleton_v3:" + hash_canonical(
            {
                "schema": "s1_negative_skeleton_trainer_projection_v3",
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
            source="typed_negative_skeleton_v3",
            weight=1.0,
        )
        provenance: dict[str, object] = {
            "schema_version": 3,
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
            "implication_aware": True,
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
        rows.append(FinalRow(trainer=trainer, provenance=provenance, split=source.split))
        certificates.append(provenance)
    return tuple(rows), tuple(certificates)


def _summary(
    config: AuditCanaryConfig,
    selected: Sequence[feasibility.EngineCandidateV3],
    negative_rows: Sequence[FinalRow],
    audits: Sequence[EngineAuditV3],
    baseline: Mapping[str, Any],
    augmented: Mapping[str, Any],
    paired: Mapping[str, Any],
) -> dict[str, object]:
    split_counts = Counter(row.split for row in negative_rows)
    family_counts = Counter(candidate.family for candidate in selected)
    operation_counts = Counter(candidate.operation_kind for candidate in selected)
    selected_count = len(selected)
    n22_share = family_counts["N22"] / selected_count if selected_count else 0.0
    n21_share = family_counts["N21"] / selected_count if selected_count else 0.0
    operation_share = (
        max(operation_counts.values(), default=0) / selected_count if selected_count else 0.0
    )
    improvements = {
        split: v1._metric(baseline, split) - v1._metric(augmented, split)
        for split in ("validation", "test")
    }
    gates: dict[str, dict[str, object]] = {
        "certified_yield": {
            "minimum_total": config.min_certified_yield,
            "minimum_validation": config.min_diagnostic_yield,
            "minimum_test": config.min_diagnostic_yield,
            "observed_total": selected_count,
            "observed_by_split": dict(sorted(split_counts.items())),
            "passed": selected_count >= config.min_certified_yield
            and split_counts["validation"] >= config.min_diagnostic_yield
            and split_counts["test"] >= config.min_diagnostic_yield,
        },
        "independent_audit": {
            "expected": selected_count,
            "verified": len(audits),
            "passed": len(audits) == selected_count and all(audit.verified for audit in audits),
        },
        "family_mix": {
            "minimum_n22_share": config.min_n22_share,
            "maximum_n21_share": config.max_n21_share,
            "observed_n22_share": n22_share,
            "observed_n21_share": n21_share,
            "passed": n22_share >= config.min_n22_share and n21_share <= config.max_n21_share,
        },
        "operation_cap": {
            "basis": "operation_kind",
            "maximum_share": config.max_operation_share,
            "observed_maximum_share": operation_share,
            "passed": operation_share <= config.max_operation_share,
        },
        "full_canary_improvement": {
            "minimum_absolute_each_split": config.min_full_canary_improvement,
            "observed": improvements,
            "baseline": {split: v1._metric(baseline, split) for split in ("validation", "test")},
            "augmented": {split: v1._metric(augmented, split) for split in ("validation", "test")},
            "passed": all(
                improvement >= config.min_full_canary_improvement
                for improvement in improvements.values()
            ),
        },
        "paired_shortcut_canary": {
            "target_balanced_accuracy_below": config.paired_canary_target,
            "validation": v1._metric(paired, "validation") if "diagnostics" in paired else None,
            "test": v1._metric(paired, "test") if "diagnostics" in paired else None,
            "passed": paired.get("target_met") is True,
        },
    }
    pilot_passed = all(cast(bool, gate["passed"]) for gate in gates.values())
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": "passed" if pilot_passed else "failed",
        "selection": {
            "source": str(config.inputs["feasibility_selection"].path),
            "sha256": config.inputs["feasibility_selection"].sha256,
            "candidate_pool_sha256": config.inputs["feasibility_candidate_pool"].sha256,
            "regenerated": False,
        },
        "counts": {
            "selected": selected_count,
            "certified_admitted": len(negative_rows),
            "family": dict(sorted(family_counts.items())),
            "operation_kind": dict(sorted(operation_counts.items())),
            "split": dict(sorted(split_counts.items())),
        },
        "gates": gates,
        "pilot_gate_passed": pilot_passed,
        "decision": {
            "public_rebuild_authorized": pilot_passed,
            "sample_size_increase_authorized": False,
            "scale_authorized": False,
            "training_authorized": False,
            "final_test_accessed": False,
        },
    }


def _staging_path(config: AuditCanaryConfig) -> Path:
    return config.output_root.with_name(f".{config.output_root.name}.partial")


def _artifact_replay(
    config: AuditCanaryConfig,
) -> tuple[
    tuple[feasibility.EngineCandidateV3, ...],
    tuple[FinalRow, ...],
    tuple[dict[str, object], ...],
    tuple[EngineAuditV3, ...],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, object],
]:
    base_rows, _ = v1._load_base_rows(cast(Any, config))
    sources = select_sources(config)
    expected_selection = v1._jsonl_bytes(v1._selection_rows(sources))
    if (config.output_root / "selection.jsonl").read_bytes() != expected_selection:
        raise NegativeSkeletonAuditCanaryError("audit-canary source selection differs")
    selected = load_frozen_selection(config, sources)
    selected_payload = v1._jsonl_bytes(
        candidate.model_dump(mode="json", by_alias=True) for candidate in selected
    )
    if (config.output_root / "selected_candidates.jsonl").read_bytes() != selected_payload:
        raise NegativeSkeletonAuditCanaryError("audit-canary frozen subset differs")
    expected_driver = render_audit_driver(config, selected)
    if (config.output_root / "audit_driver.lean").read_text(encoding="utf-8") != expected_driver:
        raise NegativeSkeletonAuditCanaryError("audit-canary driver differs")
    audit_result = v1._process_from_artifact(cast(Any, config), stage="audit")
    audits = parse_audits(audit_result.stdout, selected)
    tokenizer = v1._load_tokenizer(cast(Any, config))
    negative_rows, certificates = _project_selected(config, selected, sources, tokenizer)
    baseline, augmented, paired = v1._run_canaries(
        cast(Any, config),
        base_rows,
        sources,
        cast(Any, selected),
        negative_rows,
        tokenizer,
    )
    summary = _summary(config, selected, negative_rows, audits, baseline, augmented, paired)
    return (
        selected,
        negative_rows,
        certificates,
        audits,
        baseline,
        augmented,
        paired,
        summary,
    )


def verify_audit_canary(config: AuditCanaryConfig) -> dict[str, Any]:
    """Replay the exact subset, audit evidence, projections, and canary gates."""

    verify_input_bindings(config)
    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise NegativeSkeletonAuditCanaryError("audit-canary root must be a non-symlink directory")
    observed = {path.name for path in config.output_root.iterdir() if path.is_file()}
    if observed != _OUTPUTS:
        raise NegativeSkeletonAuditCanaryError("audit-canary output file set differs")
    manifest = v1._read_json(config.output_root / "manifest.json")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != _STATIC_OUTPUTS:
        raise NegativeSkeletonAuditCanaryError("audit-canary manifest output inventory differs")
    for name, raw_binding in outputs.items():
        if not isinstance(raw_binding, Mapping):
            raise NegativeSkeletonAuditCanaryError(f"invalid output binding: {name}")
        path = config.output_root / name
        if (
            path.is_symlink()
            or raw_binding.get("path") != str(path)
            or raw_binding.get("sha256") != hash_file(path)
        ):
            raise NegativeSkeletonAuditCanaryError(f"output binding differs: {name}")
    (
        selected,
        negative_rows,
        certificates,
        audits,
        baseline,
        augmented,
        paired,
        summary,
    ) = _artifact_replay(config)
    expected_payloads = {
        "selected_candidates.jsonl": v1._jsonl_bytes(
            candidate.model_dump(mode="json", by_alias=True) for candidate in selected
        ),
        "exclusions.jsonl": b"",
        "trainer_records.jsonl": v1._jsonl_bytes(
            row.trainer.model_dump(mode="json") for row in negative_rows
        ),
        "certificates.jsonl": v1._jsonl_bytes(
            {
                **certificate,
                "audit_verified": True,
                "audit_mode": "independent-implication-aware-reconstruction",
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
            raise NegativeSkeletonAuditCanaryError(f"replayed output differs: {name}")
    if len(audits) != len(negative_rows):
        raise NegativeSkeletonAuditCanaryError("audit/trainer row count differs")
    if (
        manifest.get("status") != "completed"
        or manifest.get("config_sha256") != hash_canonical(config.model_dump(mode="json"))
        or manifest.get("implementation_module_sha256") != hash_file(Path(__file__))
        or manifest.get("base_feasibility_module_sha256")
        != config.inputs["base_feasibility_module"].sha256
        or manifest.get("negative_engine_v2_sha256") != config.inputs["negative_engine_v2"].sha256
        or manifest.get("negative_engine_v3_sha256") != config.inputs["negative_engine_v3"].sha256
        or manifest.get("summary") != summary
        or manifest.get("privacy")
        != {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        }
        or manifest.get("execution", {}).get("candidate_pool_regenerated") is not False
        or manifest.get("execution", {}).get("final_test_accessed") is not False
        or manifest.get("execution", {}).get("training_launched") is not False
    ):
        raise NegativeSkeletonAuditCanaryError("audit-canary manifest contract differs")
    return manifest


def materialize_audit_canary(config: AuditCanaryConfig) -> dict[str, Any]:
    """Run the 24-row audit, then atomically freeze unchanged canary decisions."""

    if config.output_root.exists():
        return verify_audit_canary(config)
    verify_input_bindings(config)
    base_rows, _ = v1._load_base_rows(cast(Any, config))
    sources = select_sources(config)
    selected = load_frozen_selection(config, sources)
    staging = _staging_path(config)
    if staging.exists():
        raise NegativeSkeletonAuditCanaryError(f"stale audit-canary staging root exists: {staging}")
    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(mode=0o700)
    try:
        v1._write_payload(staging / "selection.jsonl", v1._jsonl_bytes(v1._selection_rows(sources)))
        v1._write_payload(
            staging / "selected_candidates.jsonl",
            v1._jsonl_bytes(
                candidate.model_dump(mode="json", by_alias=True) for candidate in selected
            ),
        )
        audit_driver = render_audit_driver(config, selected)
        v1._write_payload(staging / "audit_driver.lean", audit_driver.encode())
        audit_result = v1._run_lean(staging / "audit_driver.lean", cast(Any, config))
        v1._write_payload(staging / "audit.stdout.jsonl", audit_result.stdout)
        v1._write_payload(staging / "audit.stderr.txt", audit_result.stderr)
        v1._write_payload(
            staging / "audit.process.json",
            v1._canonical_line(v1._process_payload(audit_result, cast(Any, config), stage="audit")),
        )
        v1._validate_process(audit_result, cast(Any, config), stage="audit")
        audits = parse_audits(audit_result.stdout, selected)
        tokenizer = v1._load_tokenizer(cast(Any, config))
        negative_rows, certificates = _project_selected(config, selected, sources, tokenizer)
        baseline, augmented, paired = v1._run_canaries(
            cast(Any, config),
            base_rows,
            sources,
            cast(Any, selected),
            negative_rows,
            tokenizer,
        )
        summary = _summary(config, selected, negative_rows, audits, baseline, augmented, paired)
        payloads = {
            "exclusions.jsonl": b"",
            "trainer_records.jsonl": v1._jsonl_bytes(
                row.trainer.model_dump(mode="json") for row in negative_rows
            ),
            "certificates.jsonl": v1._jsonl_bytes(
                {
                    **certificate,
                    "audit_verified": True,
                    "audit_mode": "independent-implication-aware-reconstruction",
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
            "schema_version": 1,
            "method_version": METHOD_VERSION,
            "status": "completed",
            "config_sha256": hash_canonical(config.model_dump(mode="json")),
            "implementation_module_sha256": hash_file(Path(__file__)),
            "base_feasibility_module_sha256": config.inputs["base_feasibility_module"].sha256,
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
                "audit_lean_exit_code": audit_result.exit_code,
                "audit_timeout_seconds": config.timeout_seconds,
                "candidate_pool_regenerated": False,
                "full_canary_fitted": True,
                "paired_canary_fitted": True,
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
    return verify_audit_canary(config)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run-audit-canary", "verify-audit-canary"))
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = production_config(cast(Path, args.output_root))
    manifest = (
        materialize_audit_canary(config)
        if args.command == "run-audit-canary"
        else verify_audit_canary(config)
    )
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
