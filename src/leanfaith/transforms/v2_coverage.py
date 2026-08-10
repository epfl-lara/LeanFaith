"""Read-only LF-031 deterministic-v2 surface-coverage probe.

The probe reports deliberately broad, pre-generation signals.  A hit is not
rule applicability, semantic evidence, a draft, or a label.  It never invokes
Lean and never writes to the input partitions.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import RepresentationRecord
from leanfaith.transforms.v2_contract import (
    V2CoverageDetector,
    V2EvidenceClass,
    load_v2_portfolio,
)

_REPORT_PREFIX = "v2coverage"
_REPORT_ID_PATTERN = rf"^{_REPORT_PREFIX}:[0-9a-f]{{64}}$"
_QUALIFIED_NAME = re.compile(r"(?<![\w.])(?:[A-Z][\w']*\.)+[A-Za-z_][\w']*")
_EXPLICIT_APPLICATION = re.compile(r"(?<![\w'])@[A-Za-z_«]")
_TYPE_ASCRIPTION = re.compile(r"\([^()\n]+\s:\s[^()\n]+\)")
_BOUNDED_QUANTIFIER = re.compile(r"[∀∃]\s*[^,\n]+\s+[∈∉]\s*[^,\n]+,")
_ETA = re.compile(
    r"(?:fun\s+|λ\s*)([A-Za-z_][\w']*)\s*(?:=>|↦)\s*"
    r"[A-Za-z_][\w'.]*\s+\1\b"
)
_ADJACENT_BINDERS = re.compile(r"\([^()\n]+:[^()\n]+\)\s*\([^()\n]+:[^()\n]+\)")
_NAMED_PROP_BINDER = re.compile(r"\((?:h|hp|hq|hr)[\w']*\s*:\s*[^()\n]+\)")
_GROUPED_TYPED_BINDERS = re.compile(r"\(([A-Za-z_][\w']*)\s+([A-Za-z_][\w']*)\s*:\s*[^()\n]+\)")
_FORALL_EXISTS = re.compile(r"∀[^\n]{0,500}∃")
_NEGATION_QUANTIFIER = re.compile(r"(?:¬[^\n]{0,300}∀|∀[^\n]{0,300}¬)")
_ROLE_APPLICATION = re.compile(r"\b[A-Za-z_][\w'.]*\s+[A-Za-z_][\w']*\s+[A-Za-z_][\w']*")


class V2CoverageError(ValueError):
    """The read-only probe cannot produce a complete deterministic report."""


class V2FamilyCoverage(StrictModel):
    """Broad signal counts for one disabled family."""

    family_id: str = Field(pattern=r"^[pn][0-9]{2}_[a-z0-9_]+$")
    evidence_class: V2EvidenceClass
    detector: V2CoverageDetector
    theorem_hit_count: int = Field(ge=0)
    surface_signal_count: int = Field(ge=0)
    missing_required_view_count: int = Field(ge=0)
    interpretation: Literal["upper_bound_signal_not_applicability"] = (
        "upper_bound_signal_not_applicability"
    )

    @model_validator(mode="after")
    def _counts(self) -> V2FamilyCoverage:
        if self.theorem_hit_count > self.surface_signal_count:
            raise ValueError("theorem_hit_count cannot exceed surface_signal_count")
        return self


class V2CoverageReport(StrictModel):
    """Deterministic report containing no candidate or semantic record."""

    schema_version: Literal[1] = 1
    report_id: str = Field(pattern=_REPORT_ID_PATTERN)
    portfolio_id: Literal["leanfaith_deterministic_v2_design"]
    portfolio_version: str
    portfolio_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    representations_path: str = Field(min_length=1)
    representations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    representation_record_count: int = Field(ge=0)
    unique_theorem_count: int = Field(ge=0)
    family_coverage: tuple[V2FamilyCoverage, ...]
    probe_only: Literal[True] = True
    lean_requests_executed: Literal[0] = 0
    drafts_emitted: Literal[0] = 0
    labels_emitted: Literal[0] = 0
    inputs_mutated: Literal[False] = False
    interpretation: Literal["upper_bound_signal_not_applicability"] = (
        "upper_bound_signal_not_applicability"
    )

    @model_validator(mode="after")
    def _identity(self) -> V2CoverageReport:
        if self.representation_record_count != self.unique_theorem_count:
            raise ValueError("coverage input theorem IDs must be unique")
        family_ids = tuple(item.family_id for item in self.family_coverage)
        if family_ids != tuple(sorted(set(family_ids))):
            raise ValueError("family_coverage must be sorted by unique family_id")
        expected = make_id(_REPORT_PREFIX, _report_id_payload(self))
        if self.report_id != expected:
            raise ValueError("coverage report_id does not match its semantic payload")
        return self


def _report_identity(
    *,
    schema_version: int,
    portfolio_id: str,
    portfolio_version: str,
    portfolio_config_hash: str,
    representations_sha256: str,
    representation_record_count: int,
    unique_theorem_count: int,
    family_coverage: tuple[V2FamilyCoverage, ...],
    probe_only: bool,
    lean_requests_executed: int,
    drafts_emitted: int,
    labels_emitted: int,
    inputs_mutated: bool,
    interpretation: str,
) -> dict[str, object]:
    """Build the machine-independent identity; the path is display metadata."""
    return {
        "schema_version": schema_version,
        "portfolio_id": portfolio_id,
        "portfolio_version": portfolio_version,
        "portfolio_config_hash": portfolio_config_hash,
        "representations_sha256": representations_sha256,
        "representation_record_count": representation_record_count,
        "unique_theorem_count": unique_theorem_count,
        "family_coverage": [item.model_dump(mode="json") for item in family_coverage],
        "probe_only": probe_only,
        "lean_requests_executed": lean_requests_executed,
        "drafts_emitted": drafts_emitted,
        "labels_emitted": labels_emitted,
        "inputs_mutated": inputs_mutated,
        "interpretation": interpretation,
    }


def _report_id_payload(report: V2CoverageReport) -> dict[str, object]:
    return _report_identity(
        schema_version=report.schema_version,
        portfolio_id=report.portfolio_id,
        portfolio_version=report.portfolio_version,
        portfolio_config_hash=report.portfolio_config_hash,
        representations_sha256=report.representations_sha256,
        representation_record_count=report.representation_record_count,
        unique_theorem_count=report.unique_theorem_count,
        family_coverage=report.family_coverage,
        probe_only=report.probe_only,
        lean_requests_executed=report.lean_requests_executed,
        drafts_emitted=report.drafts_emitted,
        labels_emitted=report.labels_emitted,
        inputs_mutated=report.inputs_mutated,
        interpretation=report.interpretation,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise V2CoverageError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> float:
    raise V2CoverageError(f"non-finite JSON value {value!r}")


def _count_tree_kind(value: object, kind: str) -> int:
    if isinstance(value, dict):
        own = int(value.get("k") == kind)
        return own + sum(_count_tree_kind(item, kind) for item in value.values())
    if isinstance(value, list):
        return sum(_count_tree_kind(item, kind) for item in value)
    return 0


def _signal_count(
    detector: V2CoverageDetector,
    record: RepresentationRecord,
) -> int:
    headless = record.headless or ""
    raw = record.raw_proof_stripped or ""
    text = headless or raw
    if detector == V2CoverageDetector.QUALIFIED_IDENTIFIER:
        return len(_QUALIFIED_NAME.findall(text))
    if detector == V2CoverageDetector.EXPLICIT_APPLICATION:
        return len(_EXPLICIT_APPLICATION.findall(raw))
    if detector == V2CoverageDetector.COERCION_CONSTANT:
        return sum(
            atom.startswith(("const:Coe.coe", "const:CoeT.coe", "const:CoeTC.coe"))
            for atom in (record.semantic_atoms or ())
        )
    if detector == V2CoverageDetector.TYPE_ASCRIPTION:
        return len(_TYPE_ASCRIPTION.findall(text))
    if detector == V2CoverageDetector.PROJECTION_EXPR:
        return _count_tree_kind(record.operator_tree, "proj")
    if detector == V2CoverageDetector.CONSTRUCTOR_SURFACE:
        return raw.count("⟨")
    if detector in {
        V2CoverageDetector.BOUNDED_QUANTIFIER,
        V2CoverageDetector.BOUNDED_GUARD,
    }:
        return len(_BOUNDED_QUANTIFIER.findall(text))
    if detector in {
        V2CoverageDetector.PROOF_ARROW,
        V2CoverageDetector.IMPLICATION_SURFACE,
    }:
        return text.count("→") + text.count("->")
    if detector == V2CoverageDetector.ETA_SURFACE:
        return len(_ETA.findall(text))
    if detector == V2CoverageDetector.ADJACENT_EXPLICIT_BINDERS:
        return len(_ADJACENT_BINDERS.findall(text))
    if detector == V2CoverageDetector.IFF_SURFACE:
        return text.count("↔")
    if detector == V2CoverageDetector.CONJUNCTION_CHAIN:
        return max(0, text.count("∧") - 1)
    if detector == V2CoverageDetector.MULTIPLE_PROPOSITIONAL_HYPOTHESES:
        count = len(_NAMED_PROP_BINDER.findall(text))
        return max(0, count - 1)
    if detector == V2CoverageDetector.SAME_TYPED_BINDERS:
        return len(_GROUPED_TYPED_BINDERS.findall(text))
    if detector == V2CoverageDetector.FORALL_EXISTS_NESTING:
        return len(_FORALL_EXISTS.findall(text))
    if detector == V2CoverageDetector.NEGATION_QUANTIFIER:
        return len(_NEGATION_QUANTIFIER.findall(text))
    if detector == V2CoverageDetector.CONJUNCTION_SURFACE:
        return text.count("∧")
    if detector == V2CoverageDetector.ROLE_ARGUMENT_SLOTS:
        return min(
            len(_GROUPED_TYPED_BINDERS.findall(text)),
            len(_ROLE_APPLICATION.findall(text)),
        )
    raise AssertionError(f"unhandled v2 coverage detector {detector}")


def _load_records(path: Path) -> tuple[RepresentationRecord, ...]:
    records: list[RepresentationRecord] = []
    theorem_ids: set[str] = set()
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise V2CoverageError(f"cannot read representation input {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise V2CoverageError(f"blank JSONL line {line_number} in {path}")
            try:
                payload = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
                record = RepresentationRecord.model_validate(payload)
            except (ValueError, TypeError) as exc:
                raise V2CoverageError(
                    f"invalid RepresentationRecord at {path}:{line_number}: {exc}"
                ) from exc
            if record.theorem_id in theorem_ids:
                raise V2CoverageError(
                    f"duplicate theorem_id {record.theorem_id!r} at {path}:{line_number}"
                )
            theorem_ids.add(record.theorem_id)
            records.append(record)
    return tuple(records)


def build_v2_coverage_report(
    *,
    representations_path: Path,
    repo_root: Path | None = None,
    config_path: Path | None = None,
) -> V2CoverageReport:
    """Scan immutable representations without Lean execution or data emission."""

    root = Path(repo_root).resolve() if repo_root is not None else None
    loaded = load_v2_portfolio(root, path=config_path)
    resolved_input = representations_path.resolve()
    before_hash = hash_file(resolved_input)
    records = _load_records(resolved_input)
    if hash_file(resolved_input) != before_hash:
        raise V2CoverageError("representation input changed during the read-only probe")

    coverage: list[V2FamilyCoverage] = []
    required_views = loaded.config.coverage_probe.required_views
    for family in loaded.config.families:
        hit_count = 0
        signal_count = 0
        missing_count = 0
        for record in records:
            if any(getattr(record, view) is None for view in required_views):
                missing_count += 1
                continue
            count = _signal_count(family.detector, record)
            if count:
                hit_count += 1
                signal_count += count
        coverage.append(
            V2FamilyCoverage(
                family_id=family.family_id,
                evidence_class=family.evidence_class,
                detector=family.detector,
                theorem_hit_count=hit_count,
                surface_signal_count=signal_count,
                missing_required_view_count=missing_count,
            )
        )

    data: dict[str, object] = {
        "schema_version": 1,
        "portfolio_id": loaded.config.portfolio_id,
        "portfolio_version": loaded.config.portfolio_version,
        "portfolio_config_hash": loaded.config_hash,
        "representations_path": str(resolved_input),
        "representations_sha256": before_hash,
        "representation_record_count": len(records),
        "unique_theorem_count": len(records),
        "family_coverage": tuple(coverage),
        "probe_only": True,
        "lean_requests_executed": 0,
        "drafts_emitted": 0,
        "labels_emitted": 0,
        "inputs_mutated": False,
        "interpretation": "upper_bound_signal_not_applicability",
    }
    report_id = make_id(
        _REPORT_PREFIX,
        _report_identity(
            schema_version=1,
            portfolio_id=loaded.config.portfolio_id,
            portfolio_version=loaded.config.portfolio_version,
            portfolio_config_hash=loaded.config_hash,
            representations_sha256=before_hash,
            representation_record_count=len(records),
            unique_theorem_count=len(records),
            family_coverage=tuple(coverage),
            probe_only=True,
            lean_requests_executed=0,
            drafts_emitted=0,
            labels_emitted=0,
            inputs_mutated=False,
            interpretation="upper_bound_signal_not_applicability",
        ),
    )
    return V2CoverageReport.model_validate({"report_id": report_id, **data})


def write_v2_coverage_report(report: V2CoverageReport, output_path: Path) -> str:
    """Atomically create a canonical report; never overwrite an existing one."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(report.model_dump(mode="json")) + b"\n"
    temporary = output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if output_path.exists():
            raise V2CoverageError(f"coverage report already exists: {output_path}")
        os.link(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return hash_file(output_path)


def run_v2_coverage_probe(
    *,
    representations_path: Path,
    output_path: Path,
    repo_root: Path | None = None,
    config_path: Path | None = None,
) -> tuple[V2CoverageReport, str]:
    report = build_v2_coverage_report(
        representations_path=representations_path,
        repo_root=repo_root,
        config_path=config_path,
    )
    return report, write_v2_coverage_report(report, output_path)


__all__ = [
    "V2CoverageError",
    "V2CoverageReport",
    "V2FamilyCoverage",
    "build_v2_coverage_report",
    "run_v2_coverage_probe",
    "write_v2_coverage_report",
]
