"""Fail-closed finalization for the Phase-4 generation gate.

Gate 4G is a mechanical generation gate.  It is deliberately separate from
the statistical positive/negative promotion gates (4A/4B), which remain open.
This module never runs transformations.  It verifies and binds the immutable
Run-A/Run-B LF-019 reports, their run and output manifests, every catalogued
artifact, the archived code bundle, and the finalized milestone documents
before writing ``reports/gates/gate_4g.json``.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.release.guard import ArtifactUse, assess_artifact
from leanfaith.schemas import ArtifactClass, OutputManifest, RunManifest
from leanfaith.schemas.manifest import read_manifest, run_manifest_path

_HEX64 = r"^[0-9a-f]{64}$"
_RUN_ID = r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$"
_DEFAULT_GATE_PATH = Path("reports/gates/gate_4g.json")
_DEFAULT_PHASE_REPORT = Path("reports/milestones/phase_4_transforms.md")
_DEFAULT_LF019_REPORT = Path("reports/milestones/lf_019_smoke_vertical_slice.md")

GATE_4G_SMOKE_CHECKS = (
    "current_registry_snapshot_frozen",
    "active_family_inventory_exact",
    "all_eight_active_families_executed",
    "disabled_family_dispatch_rejected",
    "ten_or_more_fixture_sources_extracted",
    "all_candidates_reelaborated",
    "complete_attempt_draft_audit_variant_pair_lineage",
    "all_candidates_have_validation_and_audit_status",
    "n10_dual_ancestry_persisted",
    "transformation_evidence_linked",
    "p01_only_smoke_resolution",
    "non_p01_semantics_unresolved",
    "zero_intention_to_label_inference",
    "zero_gold_labels",
    "zero_promotions",
    "zero_protected_benchmark_overlap",
    "zero_connected_split_leakage",
    "batch_failure_isolation_passed",
    "deterministic_semantic_replay_passed",
    "smoke_release_guard_passed",
    "smoke_selection_guard_passed",
)

_FINALIZER_CHECKS = (
    "run_reports_schema_valid",
    "run_report_hashes_bound_by_run_manifests",
    "artifact_catalogs_hash_complete",
    "output_manifests_smoke_and_clean",
    "code_bundles_identical_and_valid",
    "semantic_replay_bound",
    "milestones_finalized_and_bound",
    "gate_4a_and_gate_4b_remain_open",
    "smoke_artifacts_rejected_for_all_protected_uses",
)


class Gate4GFinalizationError(ValueError):
    """Raised before a Gate-4G report can be emitted."""


class _FamilyResult(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    rule_id: str
    pair_id: str


class _SmokeRunReport(BaseModel):
    """Critical LF-019 report projection.

    LF-019 owns the complete report schema.  The finalizer intentionally
    validates only the gate-critical projection and then binds the exact report
    bytes, allowing additive non-gating fields without weakening this audit.
    """

    model_config = ConfigDict(extra="allow", strict=True)

    schema_version: Literal[1]
    artifact_kind: Literal["lf019_smoke_vertical_audit"]
    artifact_class: Literal["smoke"]
    release_eligible: Literal[False]
    model_selection_eligible: Literal[False]
    calibration_eligible: Literal[False]
    scientific_table_eligible: Literal[False]
    mechanical_pass: bool
    clean_checkout_pass: bool
    lf019_accepted: bool
    gate_4g_closed: bool
    gate_4a_closed: Literal[False]
    gate_4b_closed: Literal[False]
    run_id: str = Field(pattern=_RUN_ID)
    registry_hash: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    bound_input_hashes: dict[str, Annotated[str, Field(pattern=_HEX64)]]
    context_ids: list[str] = Field(min_length=1)
    configured_source_count: int = Field(ge=10)
    accepted_source_count: int = Field(ge=10)
    expected_failure_count: int = Field(ge=1)
    unexpected_failure_count: Literal[0]
    family_results: list[_FamilyResult] = Field(min_length=8, max_length=8)
    generated_pair_count: Literal[8]
    evidence_count: Literal[8]
    smoke_label_count: Literal[1]
    gold_label_count: Literal[0]
    promoted_item_count: Literal[0]
    split_component_count: int = Field(ge=2)
    check_results: dict[str, bool]
    output_manifest_path: str
    output_manifest_sha256: str = Field(pattern=_HEX64)
    artifact_catalog_path: str
    artifact_catalog_sha256: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _closed_gate_projection(self) -> _SmokeRunReport:
        if set(self.check_results) != set(GATE_4G_SMOKE_CHECKS):
            raise ValueError("LF-019 report has a noncanonical Gate-4G check inventory")
        expected_rules = {
            "p01_alpha",
            "p02_binders",
            "p04_notation_lite",
            "n01_operator",
            "n02_quantifier",
            "n03_drop_hypothesis",
            "n07_literal_bound",
            "n10_nearby_theorem",
        }
        if {result.rule_id for result in self.family_results} != expected_rules:
            raise ValueError("LF-019 report does not bind exactly the eight active families")
        if len({result.pair_id for result in self.family_results}) != 8:
            raise ValueError("LF-019 report family results must bind eight distinct pairs")
        return self


class _SmokeArtifactCatalog(StrictModel):
    schema_version: Literal[1] = 1
    artifact_class: Literal[ArtifactClass.SMOKE] = ArtifactClass.SMOKE
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    scientific_table_eligible: Literal[False] = False
    run_id: str = Field(pattern=_RUN_ID)
    artifact_paths: list[str]
    artifact_hashes: dict[str, Annotated[str, Field(pattern=_HEX64)]]

    @model_validator(mode="after")
    def _complete_catalog(self) -> _SmokeArtifactCatalog:
        if not self.artifact_paths:
            raise ValueError("smoke artifact catalog cannot be empty")
        if self.artifact_paths != sorted(set(self.artifact_paths)):
            raise ValueError("smoke artifact paths must be sorted and unique")
        if set(self.artifact_paths) != set(self.artifact_hashes):
            raise ValueError("smoke artifact catalog must hash every path exactly")
        return self


class Gate4GRunBinding(StrictModel):
    role: Literal["run_a", "run_b"]
    run_id: str = Field(pattern=_RUN_ID)
    report_path: str
    report_sha256: str = Field(pattern=_HEX64)
    run_manifest_path: str
    run_manifest_sha256: str = Field(pattern=_HEX64)
    output_manifest_path: str
    output_manifest_sha256: str = Field(pattern=_HEX64)
    artifact_catalog_path: str
    artifact_catalog_sha256: str = Field(pattern=_HEX64)
    semantic_fingerprint: str = Field(pattern=_HEX64)
    code_bundle_path: str
    code_bundle_sha256: str = Field(pattern=_HEX64)
    code_tree_hash: str = Field(pattern=_HEX64)
    registry_hash: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)


class Gate4GReport(StrictModel):
    """Canonical passed Gate-4G report.

    There is intentionally no failed gate-report schema: a failed finalization
    raises before writing, so the canonical path can never look authoritative
    while carrying a partial decision.
    """

    schema_version: Literal[1] = 1
    gate: Literal["gate_4g"] = "gate_4g"
    date: datetime.date
    decision: Literal["pass"] = "pass"
    evidence: str
    evidence_sha256: str = Field(pattern=_HEX64)
    lf019_milestone: str
    lf019_milestone_sha256: str = Field(pattern=_HEX64)
    artifact_boundary: Literal["smoke_only"] = "smoke_only"
    run_a: Gate4GRunBinding
    run_b: Gate4GRunBinding
    completed_checks: dict[str, Literal[True]]
    gate_4g_closed: Literal[True] = True
    gate_4a_closed: Literal[False] = False
    gate_4b_closed: Literal[False] = False
    blocking_checks: tuple[str, ...] = ()
    notes: tuple[str, ...]

    @model_validator(mode="after")
    def _coherent_gate_decision(self) -> Gate4GReport:
        expected_checks = {*GATE_4G_SMOKE_CHECKS, *_FINALIZER_CHECKS}
        if set(self.completed_checks) != expected_checks:
            raise ValueError("Gate-4G report has a noncanonical completed-check inventory")
        if not all(self.completed_checks.values()):
            raise ValueError("a passed Gate-4G report cannot contain a failed check")
        if self.blocking_checks:
            raise ValueError("a passed Gate-4G report cannot contain blocking checks")
        if self.run_a.run_id == self.run_b.run_id:
            raise ValueError("Gate-4G replay requires two distinct run IDs")
        for field in (
            "semantic_fingerprint",
            "code_bundle_sha256",
            "code_tree_hash",
            "registry_hash",
            "config_hash",
        ):
            if getattr(self.run_a, field) != getattr(self.run_b, field):
                raise ValueError(f"Gate-4G runs disagree on {field}")
        return self


class Gate4GFinalizationArtifacts(StrictModel):
    report_path: str
    report_sha256: str = Field(pattern=_HEX64)
    report: Gate4GReport


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise Gate4GFinalizationError(f"cannot read strict JSON {path}: {exc}") from exc


def _repo_file(paths: RepoPaths, path: Path | str, *, label: str) -> tuple[Path, str]:
    root = paths.root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise Gate4GFinalizationError(f"{label} must remain inside the repository")
    if not resolved.is_file():
        raise Gate4GFinalizationError(f"{label} is missing: {resolved}")
    if candidate.is_symlink():
        raise Gate4GFinalizationError(f"{label} cannot be a symlink: {candidate}")
    return resolved, str(resolved.relative_to(root))


def _parse_smoke_report(path: Path) -> _SmokeRunReport:
    try:
        return _SmokeRunReport.model_validate(_load_json(path))
    except ValueError as exc:
        raise Gate4GFinalizationError(f"invalid LF-019 report {path}: {exc}") from exc


def _parse_catalog(path: Path) -> _SmokeArtifactCatalog:
    try:
        return _SmokeArtifactCatalog.model_validate(_load_json(path))
    except ValueError as exc:
        raise Gate4GFinalizationError(f"invalid smoke catalog {path}: {exc}") from exc


def _require_hash(path: Path, expected: str, *, label: str) -> None:
    observed = hash_file(path)
    if observed != expected:
        raise Gate4GFinalizationError(
            f"{label} hash mismatch: expected {expected}, observed {observed}"
        )


def _verify_output_manifest_files(
    paths: RepoPaths,
    manifest: OutputManifest,
) -> None:
    if manifest.artifact_class != ArtifactClass.SMOKE:
        raise Gate4GFinalizationError("LF-019 output manifest is not smoke-only")
    if manifest.code.git_dirty:
        raise Gate4GFinalizationError("LF-019 output manifest was produced from dirty code")
    if manifest.code_tree_hash is None or manifest.code.code_tree_hash is None:
        raise Gate4GFinalizationError("LF-019 output manifest lacks a code-tree hash")
    if manifest.code_tree_hash != manifest.code.code_tree_hash:
        raise Gate4GFinalizationError("LF-019 output manifest code-tree hashes disagree")
    for relative, expected in manifest.file_checksums.items():
        artifact, _ = _repo_file(paths, relative, label="manifest-declared artifact")
        _require_hash(artifact, expected, label=f"manifest artifact {relative}")
    for mapping in (
        manifest.output_partition_checksums,
        manifest.failure_partition_checksums,
    ):
        for relative, expected in mapping.items():
            if manifest.file_checksums.get(relative) != expected:
                raise Gate4GFinalizationError(
                    f"partition checksum for {relative} is not bound by file_checksums"
                )


def _find_code_bundle(
    paths: RepoPaths,
    *,
    report: _SmokeRunReport,
    expected_hash: str,
    code_tree_hash: str,
) -> tuple[Path, str]:
    candidates: list[tuple[Path, str]] = []
    for raw_path, digest in report.bound_input_hashes.items():
        if digest != expected_hash:
            continue
        try:
            candidate, relative = _repo_file(
                paths,
                raw_path,
                label="LF-019 code bundle",
            )
        except Gate4GFinalizationError:
            continue
        if hash_file(candidate) == expected_hash:
            candidates.append((candidate, relative))
    unique = {(path, relative) for path, relative in candidates}
    if len(unique) != 1:
        raise Gate4GFinalizationError(
            "LF-019 report must bind exactly one present code bundle by path and hash"
        )
    bundle, relative = unique.pop()
    try:
        observed = validate_code_bundle(bundle, code_tree_hash)
    except (OSError, ValueError) as exc:
        raise Gate4GFinalizationError(f"invalid LF-019 code bundle: {exc}") from exc
    if observed != expected_hash:
        raise Gate4GFinalizationError("validated code bundle hash differs from run manifest")
    return bundle, relative


def _verify_catalog(
    paths: RepoPaths,
    *,
    report: _SmokeRunReport,
    run_manifest: RunManifest,
) -> tuple[_SmokeArtifactCatalog, Path, tuple[OutputManifest, ...]]:
    catalog_path, catalog_relative = _repo_file(
        paths,
        report.artifact_catalog_path,
        label="LF-019 artifact catalog",
    )
    if catalog_relative != report.artifact_catalog_path:
        raise Gate4GFinalizationError("LF-019 catalog path is not canonical repository-relative")
    _require_hash(
        catalog_path,
        report.artifact_catalog_sha256,
        label="LF-019 artifact catalog",
    )
    catalog = _parse_catalog(catalog_path)
    if catalog.run_id != report.run_id:
        raise Gate4GFinalizationError("LF-019 catalog run ID differs from its report")
    if catalog.artifact_hashes.get(report.output_manifest_path) != report.output_manifest_sha256:
        raise Gate4GFinalizationError(
            "LF-019 catalog does not bind the report's main output manifest"
        )
    manifests: list[OutputManifest] = []
    for relative in catalog.artifact_paths:
        artifact, canonical_relative = _repo_file(
            paths,
            relative,
            label="catalogued LF-019 artifact",
        )
        if canonical_relative != relative:
            raise Gate4GFinalizationError(
                f"catalog path is not canonical repository-relative: {relative}"
            )
        _require_hash(
            artifact,
            catalog.artifact_hashes[relative],
            label=f"catalogued artifact {relative}",
        )
        if artifact.name == "manifest.json":
            try:
                output_manifest = read_manifest(artifact, OutputManifest)
            except ValueError as exc:
                raise Gate4GFinalizationError(
                    f"invalid catalogued output manifest {relative}: {exc}"
                ) from exc
            if output_manifest.run_id != report.run_id:
                raise Gate4GFinalizationError(
                    f"catalogued output manifest {relative} has the wrong run ID"
                )
            _verify_output_manifest_files(paths, output_manifest)
            manifests.append(output_manifest)
    expected_manifest_sources = {
        "lf019_smoke_fixture",
        "lf019_smoke_evidence",
        "lf019_smoke_labels",
    }
    if (
        len(manifests) != 3
        or {manifest.source for manifest in manifests} != expected_manifest_sources
    ):
        raise Gate4GFinalizationError(
            "LF-019 catalog must bind exactly the main, evidence, and label output manifests"
        )
    for use in ArtifactUse:
        decision = assess_artifact(catalog, use=use)
        if decision.allowed or "smoke_artifact_forbidden" not in decision.reason_codes:
            raise Gate4GFinalizationError(f"smoke catalog was not fail-closed for {use.value}")
    run_hash = run_manifest.output_hashes.get(report.artifact_catalog_path)
    if run_hash != report.artifact_catalog_sha256:
        raise Gate4GFinalizationError("run manifest does not bind the exact artifact-catalog hash")
    for relative, digest in catalog.artifact_hashes.items():
        if run_manifest.output_hashes.get(relative) != digest:
            raise Gate4GFinalizationError(
                f"run manifest does not bind catalogued artifact {relative}"
            )
    return catalog, catalog_path, tuple(manifests)


def _verify_one_run(
    paths: RepoPaths,
    *,
    role: Literal["run_a", "run_b"],
    report_path: Path,
) -> tuple[Gate4GRunBinding, _SmokeRunReport, RunManifest]:
    report_file, report_relative = _repo_file(
        paths,
        report_path,
        label=f"LF-019 {role} report",
    )
    expected_report_relative = f"reports/transformation_audits/lf019_smoke/{report_file.stem}.json"
    if report_relative != expected_report_relative:
        raise Gate4GFinalizationError(
            f"LF-019 {role} report is outside its canonical immutable directory"
        )
    report = _parse_smoke_report(report_file)
    if report_file.stem != report.run_id:
        raise Gate4GFinalizationError(f"LF-019 {role} report filename differs from run ID")
    report_hash = hash_file(report_file)

    expected_replay = role == "run_b"
    if report.check_results["deterministic_semantic_replay_passed"] is not expected_replay:
        raise Gate4GFinalizationError(f"LF-019 {role} has the wrong replay-check state")
    for name, passed in report.check_results.items():
        if name != "deterministic_semantic_replay_passed" and not passed:
            raise Gate4GFinalizationError(f"LF-019 {role} failed Gate-4G check {name}")
    if not report.mechanical_pass or not report.clean_checkout_pass:
        raise Gate4GFinalizationError(f"LF-019 {role} is not a clean mechanical pass")
    if role == "run_a" and (report.lf019_accepted or report.gate_4g_closed):
        raise Gate4GFinalizationError("LF-019 Run A cannot close replay-dependent Gate 4G")
    if role == "run_b" and not (report.lf019_accepted and report.gate_4g_closed):
        raise Gate4GFinalizationError("LF-019 Run B did not accept and close Gate 4G")
    if report.gate_4a_closed or report.gate_4b_closed:
        raise Gate4GFinalizationError("LF-019 cannot close Gate 4A or Gate 4B")

    manifest_path = run_manifest_path(paths, report.run_id)
    try:
        run_manifest = read_manifest(manifest_path, RunManifest)
    except ValueError as exc:
        raise Gate4GFinalizationError(f"invalid LF-019 {role} run manifest: {exc}") from exc
    if run_manifest.run_id != report.run_id:
        raise Gate4GFinalizationError(f"LF-019 {role} run-manifest ID mismatch")
    if run_manifest.artifact_class != ArtifactClass.SMOKE:
        raise Gate4GFinalizationError(f"LF-019 {role} run manifest is not smoke-only")
    if run_manifest.command != "leanfaith generate-deterministic --run-smoke-vertical-slice":
        raise Gate4GFinalizationError(f"LF-019 {role} run command is not canonical")
    if run_manifest.code.git_dirty:
        raise Gate4GFinalizationError(f"LF-019 {role} run manifest records dirty code")
    code_tree_hash = run_manifest.code.code_tree_hash
    if code_tree_hash is None:
        raise Gate4GFinalizationError(f"LF-019 {role} lacks a code-tree hash")
    if run_manifest.output_hashes.get(report_relative) != report_hash:
        raise Gate4GFinalizationError(f"LF-019 {role} run manifest does not bind its report bytes")
    expected_flags: Mapping[str, bool] = {
        "release_eligible": False,
        "model_selection_eligible": False,
        "calibration_eligible": False,
        "scientific_table_eligible": False,
    }
    for name, expected in expected_flags.items():
        if run_manifest.execution.get(name) is not expected:
            raise Gate4GFinalizationError(f"LF-019 {role} execution flag {name} is not fail-closed")
    semantic_fingerprint = run_manifest.execution.get("semantic_fingerprint")
    if (
        not isinstance(semantic_fingerprint, str)
        or len(semantic_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in semantic_fingerprint)
    ):
        raise Gate4GFinalizationError(f"LF-019 {role} semantic fingerprint is missing or malformed")

    _, _, output_manifests = _verify_catalog(
        paths,
        report=report,
        run_manifest=run_manifest,
    )
    output_path, output_relative = _repo_file(
        paths,
        report.output_manifest_path,
        label=f"LF-019 {role} main output manifest",
    )
    if output_relative != report.output_manifest_path:
        raise Gate4GFinalizationError(f"LF-019 {role} output-manifest path is not canonical")
    _require_hash(
        output_path,
        report.output_manifest_sha256,
        label=f"LF-019 {role} main output manifest",
    )
    if run_manifest.output_hashes.get(output_relative) != report.output_manifest_sha256:
        raise Gate4GFinalizationError(
            f"LF-019 {role} run manifest does not bind its main output manifest"
        )
    if not any(manifest.run_id == report.run_id for manifest in output_manifests):
        raise Gate4GFinalizationError(
            f"LF-019 {role} catalog did not yield a run-matched output manifest"
        )
    code_trees = {manifest.code_tree_hash for manifest in output_manifests}
    if code_trees != {code_tree_hash}:
        raise Gate4GFinalizationError(
            f"LF-019 {role} output manifests do not share the run code tree"
        )

    code_bundle_hash = run_manifest.input_hashes.get("code_bundle")
    if code_bundle_hash is None:
        raise Gate4GFinalizationError(f"LF-019 {role} did not bind a code bundle")
    bundle, bundle_relative = _find_code_bundle(
        paths,
        report=report,
        expected_hash=code_bundle_hash,
        code_tree_hash=code_tree_hash,
    )
    return (
        Gate4GRunBinding(
            role=role,
            run_id=report.run_id,
            report_path=report_relative,
            report_sha256=report_hash,
            run_manifest_path=str(manifest_path.relative_to(paths.root)),
            run_manifest_sha256=hash_file(manifest_path),
            output_manifest_path=output_relative,
            output_manifest_sha256=report.output_manifest_sha256,
            artifact_catalog_path=report.artifact_catalog_path,
            artifact_catalog_sha256=report.artifact_catalog_sha256,
            semantic_fingerprint=semantic_fingerprint,
            code_bundle_path=bundle_relative,
            code_bundle_sha256=hash_file(bundle),
            code_tree_hash=code_tree_hash,
            registry_hash=report.registry_hash,
            config_hash=report.config_hash,
        ),
        report,
        run_manifest,
    )


def _require_finalized_milestones(
    *,
    phase_path: Path,
    lf019_path: Path,
    run_a: Gate4GRunBinding,
    run_b: Gate4GRunBinding,
) -> None:
    try:
        milestone_text = {
            "Phase-4": phase_path.read_text(encoding="utf-8"),
            "LF-019": lf019_path.read_text(encoding="utf-8"),
        }
    except (OSError, UnicodeError) as exc:
        raise Gate4GFinalizationError(f"cannot read Gate-4G milestones: {exc}") from exc
    required_literals = (
        run_a.run_id,
        run_b.run_id,
        run_a.report_sha256,
        run_b.report_sha256,
        "Gate 4G",
        "Gate 4A",
        "Gate 4B",
    )
    for name, content in milestone_text.items():
        missing = tuple(value for value in required_literals if value not in content)
        if missing:
            raise Gate4GFinalizationError(
                f"{name} Gate-4G milestone is stale or incomplete; missing bindings {missing!r}"
            )
        lower = content.lower()
        if "gate 4a remains open" not in lower or "gate 4b remains open" not in lower:
            raise Gate4GFinalizationError(
                f"{name} milestone must explicitly keep Gate 4A and Gate 4B open"
            )


def finalize_gate4g(
    *,
    paths: RepoPaths,
    run_a_report_path: Path,
    run_b_report_path: Path,
    phase_report_path: Path | None = None,
    lf019_milestone_path: Path | None = None,
    output_path: Path | None = None,
) -> Gate4GFinalizationArtifacts:
    """Verify immutable LF-019 replay artifacts and emit Gate 4G exactly once."""

    run_a, report_a, manifest_a = _verify_one_run(
        paths,
        role="run_a",
        report_path=run_a_report_path,
    )
    run_b, report_b, manifest_b = _verify_one_run(
        paths,
        role="run_b",
        report_path=run_b_report_path,
    )
    if run_a.run_id == run_b.run_id:
        raise Gate4GFinalizationError("Gate-4G Run A and Run B must be distinct")
    if run_a.semantic_fingerprint != run_b.semantic_fingerprint:
        raise Gate4GFinalizationError("LF-019 semantic replay fingerprints differ")
    if manifest_a.execution.get("expected_semantic_fingerprint") is not None:
        raise Gate4GFinalizationError("LF-019 Run A unexpectedly binds a replay target")
    if manifest_b.execution.get("expected_semantic_fingerprint") != run_a.semantic_fingerprint:
        raise Gate4GFinalizationError("LF-019 Run B does not bind Run A's semantic fingerprint")
    if report_a.context_ids != report_b.context_ids:
        raise Gate4GFinalizationError("LF-019 replay runs used different contexts")
    for field in (
        "registry_hash",
        "config_hash",
        "configured_source_count",
        "accepted_source_count",
        "expected_failure_count",
        "generated_pair_count",
        "evidence_count",
        "smoke_label_count",
        "gold_label_count",
        "promoted_item_count",
    ):
        if getattr(report_a, field) != getattr(report_b, field):
            raise Gate4GFinalizationError(f"LF-019 replay runs disagree on {field}")
    for field in ("code_bundle_sha256", "code_tree_hash", "code_bundle_path"):
        if getattr(run_a, field) != getattr(run_b, field):
            raise Gate4GFinalizationError(f"LF-019 replay runs disagree on {field}")

    phase_file, phase_relative = _repo_file(
        paths,
        phase_report_path or _DEFAULT_PHASE_REPORT,
        label="Phase-4 milestone",
    )
    lf019_file, lf019_relative = _repo_file(
        paths,
        lf019_milestone_path or _DEFAULT_LF019_REPORT,
        label="LF-019 milestone",
    )
    _require_finalized_milestones(
        phase_path=phase_file,
        lf019_path=lf019_file,
        run_a=run_a,
        run_b=run_b,
    )

    checks: dict[str, Literal[True]] = dict.fromkeys(
        (*GATE_4G_SMOKE_CHECKS, *_FINALIZER_CHECKS),
        True,
    )
    report = Gate4GReport(
        date=manifest_b.created_at.date(),
        evidence=phase_relative,
        evidence_sha256=hash_file(phase_file),
        lf019_milestone=lf019_relative,
        lf019_milestone_sha256=hash_file(lf019_file),
        run_a=run_a,
        run_b=run_b,
        completed_checks=checks,
        notes=(
            "Gate 4G closes deterministic generation mechanics only.",
            "All bound LF-019 artifacts are smoke-only and barred from release, "
            "model selection, calibration, and scientific tables.",
            "Gate 4A remains open pending its independent blinded promotion audit.",
            "Gate 4B remains open pending its independent supervised-promotion routes.",
        ),
    )
    destination = output_path or paths.root / _DEFAULT_GATE_PATH
    destination_resolved = destination.resolve()
    if not destination_resolved.is_relative_to(paths.root.resolve()):
        raise Gate4GFinalizationError("Gate-4G report path must remain in the repository")
    payload = canonical_json_bytes(report.model_dump(mode="json")) + b"\n"
    if destination_resolved.exists():
        if destination_resolved.read_bytes() != payload:
            raise Gate4GFinalizationError(
                "Gate-4G report already exists with different bytes; archive or "
                "remove it explicitly before refinalizing"
            )
    else:
        destination_resolved.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination_resolved.open("xb") as handle:
                handle.write(payload)
        except FileExistsError as exc:
            raise Gate4GFinalizationError(
                "Gate-4G report appeared concurrently; refusing to overwrite"
            ) from exc
    return Gate4GFinalizationArtifacts(
        report_path=str(destination_resolved.relative_to(paths.root)),
        report_sha256=hash_file(destination_resolved),
        report=report,
    )


__all__ = [
    "GATE_4G_SMOKE_CHECKS",
    "Gate4GFinalizationArtifacts",
    "Gate4GFinalizationError",
    "Gate4GReport",
    "Gate4GRunBinding",
    "finalize_gate4g",
]
