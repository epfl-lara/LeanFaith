"""Build the additive mechanically conservative SFT2B source-v3 release.

This sprint path is intentionally separate from the hash-frozen human-review v3
builder.  It quarantines the frozen 469 meta-instruction hits and every one of
the 293 frozen Workbook heuristic hits, preserves the prior core order, and
backfills only from the prior tail order.  It never creates or consumes a human
or model review record and it never invokes Lean or a formalizer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

from pydantic import model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.sft2b.meta_instruction_filter import (
    verify_v2_active_impact_fixture,
    verify_v2_impact_fixture,
)
from leanfaith.sft2b.schemas import Sha256, SourceRecord
from leanfaith.sft2b.source_bundle_schemas import (
    SourceIdViewV2,
    SourceSelectionAuditV2,
    WorkbookDiscourseAuditV2,
    WorkbookQuarantineV2,
)
from leanfaith.sft2b.source_bundle_v3 import (
    ACTIVE_META_ROWS,
    ACTIVE_META_VIEW_COUNTS,
    BASELINE_META_ROWS,
    BASELINE_META_VIEW_COUNTS,
    FROZEN_V2_COPY_NAMES,
    V2_ACTIVE_ROWS,
    V2_CORE_ROWS,
    V2_FROZEN_FILE_NAMES,
    V2_HF_REPOSITORY,
    V2_HF_REVISION,
    V2_REMOTE_PREFIX,
    V2_TAIL_ROWS,
    V2_UNIVERSE_ROWS,
    V2_WORKBOOK_QUARANTINE_ROWS,
    MechanicalSourceEvidenceV3,
    SourceBundleV3Error,
    _frozen_v2_manifest_evidence,
    _prompt_counts,
    canonical_source_line,
)
from leanfaith.sft2b.source_conservation_v3 import (
    ConservationAction,
    DeltaReasonCode,
    ExplicitDeltaReasonV3,
    SourceConservationEventV3,
    SourceConservationReceiptV3,
    build_conservation_events,
    summarize_conservation,
)

CONFIG_SCHEMA = "sft2b_reform_diverse_full_sources_v3_mechanical_conservative_v1"
RELEASE_MODE = "mechanical_conservative_v1"
REMOTE_PREFIX = "source_inputs/reform_diverse_full_v3_mechanical_conservative_v1"
CORE_SELECTION_RULE = "retained_v2_core_order_then_retained_v2_tail_order_v1"
TAIL_SELECTION_RULE = "ordered_surviving_v2_tail_after_core_backfill_v1"
WORKBOOK_HIT_ROWS = 293
WORKBOOK_ACTIVE_HIT_ROWS = 8
WORKBOOK_PRIOR_QUARANTINE_ROWS = 285
EXPECTED_ACTIVE_ROWS = 54_144
EXPECTED_TAIL_ROWS = 4_144
EXPECTED_QUARANTINE_ROWS = 762
EXPECTED_SURVIVING_PRIOR_CORE_ROWS = 49_598
EXPECTED_PRIOR_TAIL_BACKFILL_ROWS = 402
UNKNOWN_QUARANTINE_DOMAIN = "unclassified_workbook_quarantine_v2"
EXPECTED_CORE_RELEASE_CLASS_COUNTS = {
    "lean_workbook": 8_035,
    "library_cslib": 330,
    "library_mathlib": 13_003,
    "library_physlib": 482,
    "numina_current_auto": 1_154,
    "numina_current_human": 10_046,
    "numina_legacy_owner": 16_950,
}

OUTPUT_NAMES = frozenset(
    {
        "SHA256SUMS",
        "frozen_active_meta_instruction_impact.json",
        "frozen_v2_library_docstring_corrections.jsonl",
        "frozen_v2_source_audit.jsonl",
        "frozen_v2_source_manifest.json",
        "frozen_v2_workbook_discourse_audit.jsonl",
        "legacy_tail_source_ids.json",
        "matched_50000_source_ids.json",
        "mechanical_conservative_receipt.json",
        "prompt_token_counts.json",
        "source_conservation_events.jsonl",
        "source_conservation_receipt.json",
        "source_manifest.json",
        "source_mechanical_evidence.jsonl",
        "source_mix.json",
        "source_quarantine.jsonl",
        "sources.jsonl",
    }
)


class MechanicalQuarantinedSourceV1(StrictModel):
    schema_version: Literal["sft2b_mechanical_quarantined_source_v1"] = (
        "sft2b_mechanical_quarantined_source_v1"
    )
    source: SourceRecord
    source_record_sha256: Sha256
    v2_view: Literal["core", "tail", "quarantine"]
    terminal_basis: Literal[
        "active_meta_instruction_filter_v2",
        "frozen_workbook_heuristic_hit_v2",
    ]
    evidence_sha256: Sha256
    semantic_or_human_review: Literal[False] = False

    @model_validator(mode="after")
    def validate_binding(self) -> MechanicalQuarantinedSourceV1:
        if self.source_record_sha256 != hash_canonical(self.source.model_dump(mode="json")):
            raise ValueError("mechanical quarantine SourceRecord hash mismatch")
        return self


class MechanicalConservativeReceiptV1(StrictModel):
    schema_version: Literal["sft2b_mechanical_conservative_receipt_v1"] = (
        "sft2b_mechanical_conservative_receipt_v1"
    )
    release_mode: Literal["mechanical_conservative_v1"]
    source_universe_count: int
    active_count: int
    core_count: int
    tail_count: int
    quarantine_count: int
    meta_instruction_count: int
    workbook_heuristic_count: int
    meta_workbook_overlap_count: Literal[0]
    surviving_prior_core_count: int
    prior_tail_backfill_count: int
    review_record_count: Literal[0]
    human_or_model_review_used: Literal[False]
    core_release_class_counts: dict[str, int]
    source_mix_sha256: Sha256


@dataclass(frozen=True, slots=True)
class MechanicalPreflight:
    v2_file_count: int
    source_universe_count: int
    meta_instruction_count: int
    workbook_heuristic_count: int
    meta_workbook_overlap_count: int
    review_record_count: int
    release_gate_passed: bool


@dataclass(frozen=True, slots=True)
class MechanicalState:
    rows: dict[str, SourceRecord]
    source_lines: dict[str, bytes]
    release_classes: dict[str, str]
    domains: dict[str, str]
    active_order: tuple[str, ...]
    core_ids: tuple[str, ...]
    tail_ids: tuple[str, ...]
    prior_quarantine_ids: tuple[str, ...]
    meta_ids: tuple[str, ...]
    workbook_ids: tuple[str, ...]
    meta_evidence: dict[str, str]
    workbook_evidence: dict[str, str]
    mechanical_evidence: dict[str, tuple[str, str]]


@dataclass(frozen=True, slots=True)
class MechanicalPlan:
    ordered_active_ids: tuple[str, ...]
    core_ids: tuple[str, ...]
    tail_ids: tuple[str, ...]
    quarantine_ids: tuple[str, ...]
    source_bytes: bytes
    events: tuple[SourceConservationEventV3, ...]
    event_stream: bytes
    action_counts: dict[ConservationAction, int]
    reason_counts: dict[DeltaReasonCode, int]


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SourceBundleV3Error(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _jsonl_objects(path: Path) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(keepends=True), start=1):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise SourceBundleV3Error(f"non-object JSONL at {path}:{line_number}")
        result.append(cast(dict[str, Any], value))
    if not result:
        raise SourceBundleV3Error(f"empty JSONL: {path}")
    return tuple(result)


def _canonical_model_jsonl(models: Sequence[StrictModel]) -> bytes:
    return b"".join(canonical_json_bytes(row.model_dump(mode="json")) + b"\n" for row in models)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SourceBundleV3Error(f"{label} keys drifted")


def _section(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name)
    if not isinstance(value, Mapping):
        raise SourceBundleV3Error(f"missing config section: {name}")
    return cast(Mapping[str, Any], value)


def _repo_path(repo_root: Path, value: object, *, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise SourceBundleV3Error(f"{label} must be a safe repository-relative path")
    root = repo_root.resolve()
    path = (root / value).resolve(strict=True)
    if path != root and root not in path.parents:
        raise SourceBundleV3Error(f"{label} escapes the repository")
    if path.is_symlink():
        raise SourceBundleV3Error(f"{label} may not be a symlink")
    return path


def _require_hash(path: Path, expected: object, label: str) -> None:
    if not path.is_file() or not isinstance(expected, str) or hash_file(path) != expected:
        raise SourceBundleV3Error(f"{label} hash mismatch")


def _validate_config(repo_root: Path, config: Mapping[str, Any]) -> None:
    _require_exact_keys(
        config,
        {
            "schema_version",
            "release_mode",
            "output_subdir",
            "matched_view_rows",
            "builder",
            "frozen_implementations",
            "v2_evidence",
            "v2_source_config",
            "meta_instruction_filter",
            "mechanical_quarantine",
            "conservation",
            "expected_partition",
            "expected_core_release_class_counts",
            "publication",
            "generation_gate",
        },
        "mechanical v3 source config",
    )
    if (
        config.get("schema_version") != CONFIG_SCHEMA
        or config.get("release_mode") != RELEASE_MODE
        or config.get("output_subdir") != REMOTE_PREFIX
        or config.get("matched_view_rows") != V2_CORE_ROWS
        or config.get("expected_core_release_class_counts") != EXPECTED_CORE_RELEASE_CLASS_COUNTS
    ):
        raise SourceBundleV3Error("mechanical v3 config identity/counts drifted")
    expected_partition = {
        "source_universe_count": V2_UNIVERSE_ROWS,
        "active_count": EXPECTED_ACTIVE_ROWS,
        "core_count": V2_CORE_ROWS,
        "tail_count": EXPECTED_TAIL_ROWS,
        "quarantine_count": EXPECTED_QUARANTINE_ROWS,
        "surviving_prior_core_count": EXPECTED_SURVIVING_PRIOR_CORE_ROWS,
        "prior_tail_backfill_count": EXPECTED_PRIOR_TAIL_BACKFILL_ROWS,
    }
    if config.get("expected_partition") != expected_partition:
        raise SourceBundleV3Error("mechanical v3 expected partition drifted")
    v2 = _section(config, "v2_evidence")
    if (
        v2.get("hf_repository") != V2_HF_REPOSITORY
        or v2.get("hf_revision") != V2_HF_REVISION
        or v2.get("remote_prefix") != V2_REMOTE_PREFIX
        or v2.get("source_count") != V2_ACTIVE_ROWS
        or v2.get("matched_count") != V2_CORE_ROWS
        or v2.get("tail_count") != V2_TAIL_ROWS
        or v2.get("workbook_quarantine_count") != V2_WORKBOOK_QUARANTINE_ROWS
    ):
        raise SourceBundleV3Error("frozen v2 identity/counts drifted")
    file_hashes = v2.get("file_sha256")
    if not isinstance(file_hashes, Mapping) or set(file_hashes) != V2_FROZEN_FILE_NAMES:
        raise SourceBundleV3Error("frozen v2 file set drifted")
    meta = _section(config, "meta_instruction_filter")
    if (
        meta.get("baseline_expected_rows") != BASELINE_META_ROWS
        or meta.get("baseline_expected_view_counts") != BASELINE_META_VIEW_COUNTS
        or meta.get("active_expected_rows") != ACTIVE_META_ROWS
        or meta.get("active_expected_view_counts") != ACTIVE_META_VIEW_COUNTS
    ):
        raise SourceBundleV3Error("meta-instruction fixture counts drifted")
    quarantine = _section(config, "mechanical_quarantine")
    if quarantine != {
        "workbook_audit_file": "workbook_discourse_audit.jsonl",
        "workbook_audit_sha256": cast(Mapping[str, Any], file_hashes)[
            "workbook_discourse_audit.jsonl"
        ],
        "expected_workbook_hits": WORKBOOK_HIT_ROWS,
        "expected_active_workbook_hits": WORKBOOK_ACTIVE_HIT_ROWS,
        "expected_prior_quarantine_workbook_hits": WORKBOOK_PRIOR_QUARANTINE_ROWS,
        "expected_meta_workbook_overlap": 0,
        "review_records_used": 0,
        "human_or_model_review_used": False,
    }:
        raise SourceBundleV3Error("mechanical quarantine contract drifted")
    conservation = _section(config, "conservation")
    if conservation != {
        "v2_universe_rule": "v2_active_sources_plus_full_workbook_quarantine_v1",
        "allow_new_sources": False,
        "allow_removed_sources": False,
        "expected_additions": 0,
        "expected_removals": 0,
        "core_selection_rule": CORE_SELECTION_RULE,
    }:
        raise SourceBundleV3Error("mechanical conservation contract drifted")
    if config.get("publication") != {
        "hf_repository": V2_HF_REPOSITORY,
        "remote_prefix": REMOTE_PREFIX,
        "private": True,
        "additive_only": True,
        "requires_human_review_gate": False,
        "review_packet_is_nonblocking_qa": True,
    }:
        raise SourceBundleV3Error("mechanical publication contract drifted")
    if config.get("generation_gate") != {
        "allow_core_generation": False,
        "allow_tail_generation": False,
    }:
        raise SourceBundleV3Error("generation must remain disabled during source build")
    pins: list[tuple[str, Mapping[str, Any]]] = [
        ("builder", _section(config, "builder")),
        *[
            (name, cast(Mapping[str, Any], value))
            for name, value in cast(Mapping[str, Any], config["frozen_implementations"]).items()
        ],
        ("v2 source config", _section(config, "v2_source_config")),
    ]
    for label, pin in pins:
        _require_exact_keys(pin, {"path", "sha256"}, label)
        _require_hash(_repo_path(repo_root, pin["path"], label=label), pin["sha256"], label)
    for label, path_key, hash_key in (
        ("baseline meta fixture", "baseline_fixture_path", "baseline_fixture_sha256"),
        ("active meta fixture", "active_fixture_path", "active_fixture_sha256"),
    ):
        _require_hash(
            _repo_path(repo_root, meta[path_key], label=label),
            meta[hash_key],
            label,
        )


def _verify_static_inputs(
    repo_root: Path,
    *,
    config: Mapping[str, Any],
    v2_bundle_dir: Path,
) -> None:
    _validate_config(repo_root, config)
    v2 = _section(config, "v2_evidence")
    files = cast(Mapping[str, str], v2["file_sha256"])
    if set(files) != {path.name for path in v2_bundle_dir.iterdir() if path.is_file()}:
        raise SourceBundleV3Error("mounted v2 file set drifted")
    for name, digest in sorted(files.items()):
        _require_hash(v2_bundle_dir / name, digest, f"v2 {name}")
    meta = _section(config, "meta_instruction_filter")
    baseline = _repo_path(repo_root, meta["baseline_fixture_path"], label="baseline fixture")
    active = _repo_path(repo_root, meta["active_fixture_path"], label="active fixture")
    verify_v2_impact_fixture(v2_bundle_dir, baseline)
    verify_v2_active_impact_fixture(v2_bundle_dir, baseline, active)


def load_state(
    repo_root: Path,
    *,
    config: Mapping[str, Any],
    v2_bundle_dir: Path,
) -> MechanicalState:
    rows: dict[str, SourceRecord] = {}
    source_lines: dict[str, bytes] = {}
    active_order: list[str] = []
    for line in (v2_bundle_dir / "sources.jsonl").read_bytes().splitlines(keepends=True):
        source = SourceRecord.model_validate(json.loads(line))
        canonical = canonical_source_line(source)
        if line != canonical or source.source_id in rows:
            raise SourceBundleV3Error("v2 active sources are noncanonical or duplicated")
        rows[source.source_id] = source
        source_lines[source.source_id] = canonical
        active_order.append(source.source_id)
    core = SourceIdViewV2.model_validate(_object(v2_bundle_dir / "matched_50000_source_ids.json"))
    tail = SourceIdViewV2.model_validate(_object(v2_bundle_dir / "legacy_tail_source_ids.json"))
    if set(active_order) != set(core.source_ids) | set(tail.source_ids):
        raise SourceBundleV3Error("v2 active source bytes do not cover the exact core/tail views")
    release_classes: dict[str, str] = {}
    domains: dict[str, str] = {}
    mechanical_evidence: dict[str, tuple[str, str]] = {}
    for value in _jsonl_objects(v2_bundle_dir / "source_audit.jsonl"):
        selection_audit = SourceSelectionAuditV2.model_validate(value)
        if selection_audit.source_id in release_classes:
            raise SourceBundleV3Error("duplicate v2 source audit")
        release_classes[selection_audit.source_id] = selection_audit.release_class
        domains[selection_audit.source_id] = selection_audit.domain
        mechanical_evidence[selection_audit.source_id] = (
            "v2_source_selection_audit",
            hash_canonical(selection_audit.model_dump(mode="json")),
        )
    prior_quarantine: list[str] = []
    for value in _jsonl_objects(v2_bundle_dir / "workbook_quarantine.jsonl"):
        wrapper = WorkbookQuarantineV2.model_validate(value)
        source = wrapper.source
        if source.source_id in rows:
            raise SourceBundleV3Error("v2 Workbook quarantine overlaps active sources")
        rows[source.source_id] = source
        source_lines[source.source_id] = canonical_source_line(source)
        prior_quarantine.append(source.source_id)
        release_classes[source.source_id] = "lean_workbook"
        domains[source.source_id] = UNKNOWN_QUARANTINE_DOMAIN
        mechanical_evidence[source.source_id] = (
            "v2_workbook_automatic_disposition",
            hash_canonical(wrapper.discourse_audit.model_dump(mode="json")),
        )
    workbook_evidence: dict[str, str] = {}
    for value in _jsonl_objects(v2_bundle_dir / "workbook_discourse_audit.jsonl"):
        workbook_audit = WorkbookDiscourseAuditV2.model_validate(value)
        if workbook_audit.source_id in workbook_evidence:
            raise SourceBundleV3Error("duplicate Workbook heuristic evidence")
        workbook_evidence[workbook_audit.source_id] = hash_canonical(
            workbook_audit.model_dump(mode="json")
        )
    meta = _section(config, "meta_instruction_filter")
    fixture = _object(
        _repo_path(repo_root, meta["active_fixture_path"], label="active meta fixture")
    )
    meta_evidence: dict[str, str] = {}
    for value in cast(list[dict[str, Any]], fixture["rows"]):
        source_id = str(value["source_id"])
        if source_id in meta_evidence:
            raise SourceBundleV3Error("duplicate active meta fixture source")
        meta_evidence[source_id] = hash_canonical(value)
    state = MechanicalState(
        rows=rows,
        source_lines=source_lines,
        release_classes=release_classes,
        domains=domains,
        active_order=tuple(active_order),
        core_ids=core.source_ids,
        tail_ids=tail.source_ids,
        prior_quarantine_ids=tuple(sorted(prior_quarantine)),
        meta_ids=tuple(sorted(meta_evidence)),
        workbook_ids=tuple(sorted(workbook_evidence)),
        meta_evidence=meta_evidence,
        workbook_evidence=workbook_evidence,
        mechanical_evidence=mechanical_evidence,
    )
    active = set(state.active_order)
    prior = set(state.prior_quarantine_ids)
    workbook = set(state.workbook_ids)
    meta_ids = set(state.meta_ids)
    if (
        len(state.rows) != V2_UNIVERSE_ROWS
        or len(active) != V2_ACTIVE_ROWS
        or len(state.core_ids) != V2_CORE_ROWS
        or len(state.tail_ids) != V2_TAIL_ROWS
        or len(prior) != V2_WORKBOOK_QUARANTINE_ROWS
        or len(workbook) != WORKBOOK_HIT_ROWS
        or len(workbook & active) != WORKBOOK_ACTIVE_HIT_ROWS
        or workbook & prior != prior
        or len(meta_ids) != ACTIVE_META_ROWS
        or not meta_ids.issubset(active)
        or meta_ids & workbook
        or set(state.release_classes) != set(state.rows)
        or set(state.domains) != set(state.rows)
        or set(state.mechanical_evidence) != set(state.rows)
    ):
        raise SourceBundleV3Error("mechanical source state counts/coverage drifted")
    return state


def plan_release(
    state: MechanicalState, *, target_core_count: int = V2_CORE_ROWS
) -> MechanicalPlan:
    active = set(state.active_order)
    core = set(state.core_ids)
    tail = set(state.tail_ids)
    prior_quarantine = set(state.prior_quarantine_ids)
    meta = set(state.meta_ids)
    workbook = set(state.workbook_ids)
    if core | tail != active or core & tail or active & prior_quarantine:
        raise SourceBundleV3Error("v2 views do not form the expected partition")
    quarantine = meta | workbook
    surviving_core = tuple(source_id for source_id in state.core_ids if source_id not in quarantine)
    surviving_tail = tuple(source_id for source_id in state.tail_ids if source_id not in quarantine)
    needed = target_core_count - len(surviving_core)
    if needed < 0 or needed > len(surviving_tail):
        raise SourceBundleV3Error("mechanical core boundary cannot be filled from prior tail")
    core_ids = surviving_core + surviving_tail[:needed]
    tail_ids = surviving_tail[needed:]
    ordered_active = core_ids + tail_ids
    quarantine_ids = tuple(sorted(prior_quarantine | quarantine))
    selection_evidence = hash_canonical(
        {
            "rule": CORE_SELECTION_RULE,
            "target_core_count": target_core_count,
            "surviving_prior_core_sha256": sha256_hex("\n".join(surviving_core).encode()),
            "surviving_prior_tail_sha256": sha256_hex("\n".join(surviving_tail).encode()),
        }
    )
    reasons: list[ExplicitDeltaReasonV3] = []
    for source_id in sorted(meta):
        reasons.append(
            ExplicitDeltaReasonV3(
                source_id=source_id,
                direction="quarantined",
                reason_code="meta_instruction_quarantine",
                rationale="frozen fail-closed meta-instruction detector matched this NL",
                evidence_sha256=state.meta_evidence[source_id],
            )
        )
    for source_id in sorted((workbook & active) - meta):
        reasons.append(
            ExplicitDeltaReasonV3(
                source_id=source_id,
                direction="quarantined",
                reason_code="source_contract_correction",
                rationale=(
                    "mechanical_conservative_v1 quarantines every frozen Workbook heuristic hit"
                ),
                evidence_sha256=state.workbook_evidence[source_id],
            )
        )
    for source_id in surviving_tail[:needed]:
        reasons.append(
            ExplicitDeltaReasonV3(
                source_id=source_id,
                direction="moved",
                reason_code="core_boundary_reselection",
                rationale="prior-tail order backfill restores the deterministic 50,000-row core",
                evidence_sha256=selection_evidence,
            )
        )
    events = build_conservation_events(
        v2_rows=state.rows,
        v2_core_ids=state.core_ids,
        v2_quarantine_ids=state.prior_quarantine_ids,
        v2_tail_ids=state.tail_ids,
        v3_rows=state.rows,
        v3_core_ids=core_ids,
        v3_quarantine_ids=quarantine_ids,
        v3_tail_ids=tail_ids,
        delta_reasons=reasons,
    )
    event_stream = _canonical_model_jsonl(events)
    actions_raw, reasons_raw = summarize_conservation(events)
    actions = {name: actions_raw.get(name, 0) for name in get_args(ConservationAction)}
    reason_counts = {name: reasons_raw.get(name, 0) for name in get_args(DeltaReasonCode)}
    return MechanicalPlan(
        ordered_active_ids=ordered_active,
        core_ids=core_ids,
        tail_ids=tail_ids,
        quarantine_ids=quarantine_ids,
        source_bytes=b"".join(state.source_lines[source_id] for source_id in ordered_active),
        events=events,
        event_stream=event_stream,
        action_counts=cast(dict[ConservationAction, int], actions),
        reason_counts=cast(dict[DeltaReasonCode, int], reason_counts),
    )


def _mix(state: MechanicalState, plan: MechanicalPlan) -> dict[str, Any]:
    views = {
        "core": plan.core_ids,
        "tail": plan.tail_ids,
        "quarantine": plan.quarantine_ids,
    }
    payload: dict[str, Any] = {
        "schema_version": "sft2b_source_partition_mix_v1",
        "domain_policy": {
            "prior_quarantined_workbook_rows_without_source_audit": UNKNOWN_QUARANTINE_DOMAIN
        },
        "views": {},
    }
    for name, ids in views.items():
        payload["views"][name] = {
            "count": len(ids),
            "release_class_counts": dict(
                sorted(Counter(state.release_classes[source_id] for source_id in ids).items())
            ),
            "domain_counts": dict(
                sorted(Counter(state.domains[source_id] for source_id in ids).items())
            ),
        }
    return payload


def _validate_plan(config: Mapping[str, Any], state: MechanicalState, plan: MechanicalPlan) -> None:
    mix = _mix(state, plan)
    core_mix = cast(Mapping[str, Any], cast(Mapping[str, Any], mix["views"])["core"])
    if (
        len(plan.ordered_active_ids) != EXPECTED_ACTIVE_ROWS
        or len(plan.core_ids) != V2_CORE_ROWS
        or len(plan.tail_ids) != EXPECTED_TAIL_ROWS
        or len(plan.quarantine_ids) != EXPECTED_QUARANTINE_ROWS
        or len(set(state.core_ids) & set(plan.core_ids)) != EXPECTED_SURVIVING_PRIOR_CORE_ROWS
        or len(set(state.tail_ids) & set(plan.core_ids)) != EXPECTED_PRIOR_TAIL_BACKFILL_ROWS
        or core_mix["release_class_counts"] != config["expected_core_release_class_counts"]
        or plan.action_counts["quarantined_from_core"] != 402
        or plan.action_counts["quarantined_from_tail"] != 75
        or plan.action_counts["moved_tail_to_core"] != EXPECTED_PRIOR_TAIL_BACKFILL_ROWS
        or plan.reason_counts["meta_instruction_quarantine"] != ACTIVE_META_ROWS
        or plan.reason_counts["source_contract_correction"] != WORKBOOK_ACTIVE_HIT_ROWS
        or plan.action_counts["added"]
        or plan.action_counts["removed"]
    ):
        raise SourceBundleV3Error("mechanical plan does not match the sprint partition contract")


def preflight_release(
    repo_root: Path,
    *,
    config_path: Path,
    v2_bundle_dir: Path,
    output_dir: Path,
) -> MechanicalPreflight:
    if output_dir.exists():
        raise SourceBundleV3Error("mechanical v3 output path already exists")
    config = _object(config_path)
    _verify_static_inputs(repo_root, config=config, v2_bundle_dir=v2_bundle_dir)
    state = load_state(repo_root, config=config, v2_bundle_dir=v2_bundle_dir)
    plan = plan_release(state, target_core_count=int(config["matched_view_rows"]))
    _validate_plan(config, state, plan)
    return MechanicalPreflight(
        v2_file_count=len(cast(Mapping[str, Any], config["v2_evidence"])["file_sha256"]),
        source_universe_count=len(state.rows),
        meta_instruction_count=len(state.meta_ids),
        workbook_heuristic_count=len(state.workbook_ids),
        meta_workbook_overlap_count=len(set(state.meta_ids) & set(state.workbook_ids)),
        review_record_count=0,
        release_gate_passed=True,
    )


def _v2_view(state: MechanicalState, source_id: str) -> Literal["core", "tail", "quarantine"]:
    if source_id in set(state.core_ids):
        return "core"
    if source_id in set(state.tail_ids):
        return "tail"
    return "quarantine"


def _mechanical_evidence(
    state: MechanicalState, plan: MechanicalPlan
) -> tuple[MechanicalSourceEvidenceV3, ...]:
    v3_core, v3_tail = set(plan.core_ids), set(plan.tail_ids)
    rows: list[MechanicalSourceEvidenceV3] = []
    for source_id in sorted(state.rows):
        kind, digest = state.mechanical_evidence[source_id]
        rows.append(
            MechanicalSourceEvidenceV3(
                source_id=source_id,
                release_class=state.release_classes[source_id],
                source_record_sha256=hash_canonical(state.rows[source_id].model_dump(mode="json")),
                v2_view=_v2_view(state, source_id),
                v3_view=(
                    "core"
                    if source_id in v3_core
                    else "tail"
                    if source_id in v3_tail
                    else "quarantine"
                ),
                v2_evidence_kind=cast(Any, kind),
                v2_evidence_sha256=digest,
                semantic_or_human_review=False,
            )
        )
    return tuple(rows)


def _git_revision(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    revision = completed.stdout.strip()
    if len(revision) != 40:
        raise SourceBundleV3Error("could not resolve the committed Git revision")
    return revision


def _write_release(
    repo_root: Path,
    *,
    config_path: Path,
    config: Mapping[str, Any],
    v2_bundle_dir: Path,
    state: MechanicalState,
    plan: MechanicalPlan,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=False, exist_ok=False)
    (output_dir / "sources.jsonl").write_bytes(plan.source_bytes)
    source_hash = hash_file(output_dir / "sources.jsonl")
    for view, view_id, rule, ids in (
        (
            "matched_50000_source_ids.json",
            "corrected_core_50000",
            CORE_SELECTION_RULE,
            plan.core_ids,
        ),
        ("legacy_tail_source_ids.json", "legacy_tail", TAIL_SELECTION_RULE, plan.tail_ids),
    ):
        record = SourceIdViewV2(
            view_id=cast(Any, view_id),
            source_count=len(ids),
            selection_rule=rule,
            parent_sources_sha256=source_hash,
            source_ids=ids,
        )
        (output_dir / view).write_bytes(
            canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
        )
    quarantine_rows = tuple(
        MechanicalQuarantinedSourceV1(
            source=state.rows[source_id],
            source_record_sha256=hash_canonical(state.rows[source_id].model_dump(mode="json")),
            v2_view=_v2_view(state, source_id),
            terminal_basis=(
                "active_meta_instruction_filter_v2"
                if source_id in state.meta_evidence
                else "frozen_workbook_heuristic_hit_v2"
            ),
            evidence_sha256=(
                state.meta_evidence[source_id]
                if source_id in state.meta_evidence
                else state.workbook_evidence[source_id]
            ),
            semantic_or_human_review=False,
        )
        for source_id in plan.quarantine_ids
    )
    (output_dir / "source_quarantine.jsonl").write_bytes(_canonical_model_jsonl(quarantine_rows))
    (output_dir / "source_mechanical_evidence.jsonl").write_bytes(
        _canonical_model_jsonl(_mechanical_evidence(state, plan))
    )
    (output_dir / "source_conservation_events.jsonl").write_bytes(plan.event_stream)
    for source_name, target_name in FROZEN_V2_COPY_NAMES.items():
        shutil.copyfile(v2_bundle_dir / source_name, output_dir / target_name)
    shutil.copyfile(
        v2_bundle_dir / "workbook_discourse_audit.jsonl",
        output_dir / "frozen_v2_workbook_discourse_audit.jsonl",
    )
    active_fixture = _repo_path(
        repo_root,
        cast(Mapping[str, Any], config["meta_instruction_filter"])["active_fixture_path"],
        label="active meta fixture",
    )
    shutil.copyfile(active_fixture, output_dir / "frozen_active_meta_instruction_impact.json")
    ordered_sources = [state.rows[source_id] for source_id in plan.ordered_active_ids]
    v2_config = _repo_path(
        repo_root,
        cast(Mapping[str, Any], config["v2_source_config"])["path"],
        label="v2 source config",
    )
    prompt_bytes, maximum_prompt_tokens, required_max_model_len = _prompt_counts(
        repo_root,
        v2_config_path=v2_config,
        sources=ordered_sources,
    )
    (output_dir / "prompt_token_counts.json").write_bytes(prompt_bytes)
    mix = _mix(state, plan)
    mix_bytes = canonical_json_bytes(mix) + b"\n"
    (output_dir / "source_mix.json").write_bytes(mix_bytes)
    conservation = SourceConservationReceiptV3(
        v2_sources_sha256=hash_file(v2_bundle_dir / "sources.jsonl"),
        v2_core_view_sha256=hash_file(v2_bundle_dir / "matched_50000_source_ids.json"),
        v2_quarantine_view_sha256=hash_file(v2_bundle_dir / "workbook_quarantine.jsonl"),
        v2_tail_view_sha256=hash_file(v2_bundle_dir / "legacy_tail_source_ids.json"),
        v3_sources_sha256=source_hash,
        v3_core_view_sha256=hash_file(output_dir / "matched_50000_source_ids.json"),
        v3_quarantine_view_sha256=hash_file(output_dir / "source_quarantine.jsonl"),
        v3_tail_view_sha256=hash_file(output_dir / "legacy_tail_source_ids.json"),
        event_stream_sha256=hash_file(output_dir / "source_conservation_events.jsonl"),
        event_count=len(plan.events),
        v2_source_count=len(state.rows),
        v3_source_count=len(state.rows),
        action_counts=plan.action_counts,
        reason_counts=plan.reason_counts,
        v2_partition_complete=True,
        v3_partition_complete=True,
        every_delta_explained=True,
    )
    (output_dir / "source_conservation_receipt.json").write_bytes(
        canonical_json_bytes(conservation.model_dump(mode="json")) + b"\n"
    )
    core_mix = cast(Mapping[str, Any], cast(Mapping[str, Any], mix["views"])["core"])
    receipt = MechanicalConservativeReceiptV1(
        release_mode="mechanical_conservative_v1",
        source_universe_count=len(state.rows),
        active_count=len(plan.ordered_active_ids),
        core_count=len(plan.core_ids),
        tail_count=len(plan.tail_ids),
        quarantine_count=len(plan.quarantine_ids),
        meta_instruction_count=len(state.meta_ids),
        workbook_heuristic_count=len(state.workbook_ids),
        meta_workbook_overlap_count=0,
        surviving_prior_core_count=len(set(state.core_ids) & set(plan.core_ids)),
        prior_tail_backfill_count=len(set(state.tail_ids) & set(plan.core_ids)),
        review_record_count=0,
        human_or_model_review_used=False,
        core_release_class_counts=cast(dict[str, int], core_mix["release_class_counts"]),
        source_mix_sha256=sha256_hex(mix_bytes),
    )
    (output_dir / "mechanical_conservative_receipt.json").write_bytes(
        canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n"
    )
    current_files = {path.name: hash_file(path) for path in output_dir.iterdir() if path.is_file()}
    manifest = {
        "schema_version": "sft2b_diverse_full_source_manifest_v3_mechanical_conservative_v1",
        "release_mode": RELEASE_MODE,
        "source_config_path": str(config_path.relative_to(repo_root)),
        "source_config_sha256": hash_file(config_path),
        "builder_implementation_sha256": hash_file(Path(__file__)),
        "git_revision": _git_revision(repo_root),
        "v2_evidence": config["v2_evidence"],
        "frozen_v2_evidence": _frozen_v2_manifest_evidence(config, v2_bundle_dir),
        "source_count": len(plan.ordered_active_ids),
        "core_count": len(plan.core_ids),
        "tail_count": len(plan.tail_ids),
        "quarantine_count": len(plan.quarantine_ids),
        "source_universe_count": len(state.rows),
        "selection_rule": CORE_SELECTION_RULE,
        "review_usage": {
            "review_record_count": 0,
            "human_or_model_review_used": False,
            "remaining_packet_rows_are_nonblocking_qa": True,
        },
        "prompt_tokens": {
            "maximum_prompt_tokens": maximum_prompt_tokens,
            "required_max_model_len": required_max_model_len,
        },
        "conservation": {
            "action_counts": plan.action_counts,
            "reason_counts": plan.reason_counts,
        },
        "source_mix": mix,
        "data_files": {name: {"sha256": digest} for name, digest in sorted(current_files.items())},
    }
    (output_dir / "source_manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    hashes = {path.name: hash_file(path) for path in output_dir.iterdir() if path.is_file()}
    (output_dir / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())),
        encoding="utf-8",
    )


def _verify_checksums(bundle_dir: Path) -> None:
    if {path.name for path in bundle_dir.iterdir() if path.is_file()} != OUTPUT_NAMES:
        raise SourceBundleV3Error("mechanical v3 output file set drifted")
    observed: dict[str, str] = {}
    for line in (bundle_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name in observed:
            raise SourceBundleV3Error("malformed mechanical v3 checksum ledger")
        observed[name] = digest
    expected = OUTPUT_NAMES - {"SHA256SUMS"}
    if set(observed) != expected or any(
        hash_file(bundle_dir / name) != digest for name, digest in observed.items()
    ):
        raise SourceBundleV3Error("mechanical v3 checksum verification failed")


def verify_release(
    repo_root: Path,
    *,
    config_path: Path,
    v2_bundle_dir: Path,
    bundle_dir: Path,
) -> None:
    config = _object(config_path)
    _verify_static_inputs(repo_root, config=config, v2_bundle_dir=v2_bundle_dir)
    _verify_checksums(bundle_dir)
    state = load_state(repo_root, config=config, v2_bundle_dir=v2_bundle_dir)
    plan = plan_release(state, target_core_count=int(config["matched_view_rows"]))
    _validate_plan(config, state, plan)
    if (bundle_dir / "sources.jsonl").read_bytes() != plan.source_bytes:
        raise SourceBundleV3Error("mechanical v3 source bytes do not replay")
    core = SourceIdViewV2.model_validate(_object(bundle_dir / "matched_50000_source_ids.json"))
    tail = SourceIdViewV2.model_validate(_object(bundle_dir / "legacy_tail_source_ids.json"))
    if core.source_ids != plan.core_ids or tail.source_ids != plan.tail_ids:
        raise SourceBundleV3Error("mechanical v3 core/tail views do not replay")
    if (bundle_dir / "source_conservation_events.jsonl").read_bytes() != plan.event_stream:
        raise SourceBundleV3Error("mechanical v3 conservation stream does not replay")
    expected_mix = canonical_json_bytes(_mix(state, plan)) + b"\n"
    if (bundle_dir / "source_mix.json").read_bytes() != expected_mix:
        raise SourceBundleV3Error("mechanical v3 source mix does not replay")
    quarantine = tuple(
        MechanicalQuarantinedSourceV1.model_validate(value)
        for value in _jsonl_objects(bundle_dir / "source_quarantine.jsonl")
    )
    if tuple(row.source.source_id for row in quarantine) != plan.quarantine_ids:
        raise SourceBundleV3Error("mechanical v3 quarantine IDs do not replay")
    for row in quarantine:
        expected_basis = (
            "active_meta_instruction_filter_v2"
            if row.source.source_id in state.meta_evidence
            else "frozen_workbook_heuristic_hit_v2"
        )
        expected_evidence = (
            state.meta_evidence[row.source.source_id]
            if row.source.source_id in state.meta_evidence
            else state.workbook_evidence[row.source.source_id]
        )
        if row.terminal_basis != expected_basis or row.evidence_sha256 != expected_evidence:
            raise SourceBundleV3Error("mechanical quarantine evidence does not replay")
    if (bundle_dir / "source_mechanical_evidence.jsonl").read_bytes() != _canonical_model_jsonl(
        _mechanical_evidence(state, plan)
    ):
        raise SourceBundleV3Error("mechanical source evidence does not replay")
    for source_name, target_name in FROZEN_V2_COPY_NAMES.items():
        if (bundle_dir / target_name).read_bytes() != (v2_bundle_dir / source_name).read_bytes():
            raise SourceBundleV3Error(f"frozen v2 copy drifted: {target_name}")
    if (bundle_dir / "frozen_v2_workbook_discourse_audit.jsonl").read_bytes() != (
        v2_bundle_dir / "workbook_discourse_audit.jsonl"
    ).read_bytes():
        raise SourceBundleV3Error("frozen Workbook heuristic evidence drifted")
    active_fixture = _repo_path(
        repo_root,
        cast(Mapping[str, Any], config["meta_instruction_filter"])["active_fixture_path"],
        label="active meta fixture",
    )
    if (
        bundle_dir / "frozen_active_meta_instruction_impact.json"
    ).read_bytes() != active_fixture.read_bytes():
        raise SourceBundleV3Error("frozen meta fixture copy drifted")
    v2_config = _repo_path(
        repo_root,
        cast(Mapping[str, Any], config["v2_source_config"])["path"],
        label="v2 source config",
    )
    expected_prompt, _, required_max_model_len = _prompt_counts(
        repo_root,
        v2_config_path=v2_config,
        sources=[state.rows[source_id] for source_id in plan.ordered_active_ids],
    )
    if (bundle_dir / "prompt_token_counts.json").read_bytes() != expected_prompt:
        raise SourceBundleV3Error("mechanical v3 prompt counts do not replay")
    conservation = SourceConservationReceiptV3.model_validate(
        _object(bundle_dir / "source_conservation_receipt.json")
    )
    if (
        conservation.action_counts != plan.action_counts
        or conservation.reason_counts != plan.reason_counts
        or conservation.event_stream_sha256
        != hash_file(bundle_dir / "source_conservation_events.jsonl")
    ):
        raise SourceBundleV3Error("mechanical v3 conservation receipt does not replay")
    receipt = MechanicalConservativeReceiptV1.model_validate(
        _object(bundle_dir / "mechanical_conservative_receipt.json")
    )
    if (
        receipt.active_count != len(plan.ordered_active_ids)
        or receipt.core_count != len(plan.core_ids)
        or receipt.tail_count != len(plan.tail_ids)
        or receipt.quarantine_count != len(plan.quarantine_ids)
        or receipt.source_mix_sha256 != sha256_hex(expected_mix)
        or receipt.core_release_class_counts != EXPECTED_CORE_RELEASE_CLASS_COUNTS
    ):
        raise SourceBundleV3Error("mechanical conservative receipt does not replay")
    manifest = _object(bundle_dir / "source_manifest.json")
    prompt_payload = cast(Mapping[str, Any], json.loads(expected_prompt))
    if (
        manifest.get("schema_version")
        != "sft2b_diverse_full_source_manifest_v3_mechanical_conservative_v1"
        or manifest.get("release_mode") != RELEASE_MODE
        or manifest.get("source_config_sha256") != hash_file(config_path)
        or manifest.get("builder_implementation_sha256") != hash_file(Path(__file__))
        or manifest.get("source_count") != EXPECTED_ACTIVE_ROWS
        or manifest.get("core_count") != V2_CORE_ROWS
        or manifest.get("tail_count") != EXPECTED_TAIL_ROWS
        or manifest.get("quarantine_count") != EXPECTED_QUARANTINE_ROWS
        or manifest.get("review_usage")
        != {
            "review_record_count": 0,
            "human_or_model_review_used": False,
            "remaining_packet_rows_are_nonblocking_qa": True,
        }
        or manifest.get("source_mix") != _mix(state, plan)
        or manifest.get("prompt_tokens")
        != {
            "maximum_prompt_tokens": prompt_payload["maximum_prompt_tokens"],
            "required_max_model_len": required_max_model_len,
        }
    ):
        raise SourceBundleV3Error("mechanical v3 source manifest does not replay")
    files = cast(Mapping[str, Any], manifest.get("data_files", {}))
    expected_files = OUTPUT_NAMES - {"SHA256SUMS", "source_manifest.json"}
    if set(files) != expected_files:
        raise SourceBundleV3Error("mechanical v3 manifest file set drifted")
    for name in expected_files:
        if cast(Mapping[str, Any], files[name]).get("sha256") != hash_file(bundle_dir / name):
            raise SourceBundleV3Error(f"mechanical v3 manifest hash drifted: {name}")


def build_release(
    repo_root: Path,
    *,
    config_path: Path,
    v2_bundle_dir: Path,
    output_dir: Path,
) -> None:
    preflight_release(
        repo_root,
        config_path=config_path,
        v2_bundle_dir=v2_bundle_dir,
        output_dir=output_dir,
    )
    config = _object(config_path)
    state = load_state(repo_root, config=config, v2_bundle_dir=v2_bundle_dir)
    plan = plan_release(state, target_core_count=int(config["matched_view_rows"]))
    _validate_plan(config, state, plan)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    temporary.rmdir()
    try:
        _write_release(
            repo_root,
            config_path=config_path,
            config=config,
            v2_bundle_dir=v2_bundle_dir,
            state=state,
            plan=plan,
            output_dir=temporary,
        )
        verify_release(
            repo_root,
            config_path=config_path,
            v2_bundle_dir=v2_bundle_dir,
            bundle_dir=temporary,
        )
        with tempfile.TemporaryDirectory(prefix="leanfaith-sft2b-mechanical-v3-") as root:
            fresh = Path(root) / output_dir.name
            shutil.copytree(temporary, fresh)
            verify_release(
                repo_root,
                config_path=config_path,
                v2_bundle_dir=v2_bundle_dir,
                bundle_dir=fresh,
            )
        os.replace(temporary, output_dir)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "build", "verify"))
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--v2-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "preflight":
        print(
            json.dumps(
                asdict(
                    preflight_release(
                        args.repo_root,
                        config_path=args.config,
                        v2_bundle_dir=args.v2_bundle,
                        output_dir=args.output,
                    )
                ),
                sort_keys=True,
            )
        )
    elif args.command == "build":
        build_release(
            args.repo_root,
            config_path=args.config,
            v2_bundle_dir=args.v2_bundle,
            output_dir=args.output,
        )
    else:
        verify_release(
            args.repo_root,
            config_path=args.config,
            v2_bundle_dir=args.v2_bundle,
            bundle_dir=args.output,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
