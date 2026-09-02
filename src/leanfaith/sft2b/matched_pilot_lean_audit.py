"""Bounded, resumable Lean audit for the frozen SFT2B matched-500 pilot.

All artifact replay, schema validation, provenance joins, context reconstruction,
and content hashing happen before a Lean process is started.  The execution
order groups sources by render context, keeps one synchronous LeanInteract
backend alive for each context group, and writes one immutable source terminal
before appending its journal event.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import subprocess
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, ValidationError, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.host_resources import claim_resources, release_resources
from leanfaith.lean.leaninteract_backend import METHOD_VERSION
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import (
    ClosedExprBatchResult,
    ClosedExprFailure,
    ClosedExprInput,
    ClosedExprSourceMaterial,
    CompileContext,
    _parse_closed_expr_payloads,
    render_closed_expr_in_session,
)
from leanfaith.sft2b.durable import AppendOnlyJournal, immutable_write, write_json, write_jsonl
from leanfaith.sft2b.full_source_consumer import (
    FullSourceConsumerError,
    load_consumer_spec,
    verify_observed_pilot,
)
from leanfaith.sft2b.lean import (
    PropositionEndpoint,
    _failure_sentinel_expr_hash,
    _failure_sentinel_nat,
    _lean_name_list,
    _level_names,
    endpoint_cache_key,
    make_mathlib_backend,
)
from leanfaith.sft2b.pins import RuntimePins, verify_runtime_pins
from leanfaith.sft2b.schemas import (
    CandidateRecord,
    CompileStatus,
    EndpointCacheRecord,
    FormalizerAttempt,
    Sha256,
    SourceRecord,
    StableId,
    stable_id,
)

SCHEMA_VERSION = "sft2b_matched_pilot_lean_audit_v1"
RUN_SCHEMA_VERSION = "sft2b_matched_pilot_lean_audit_run_v2"
PREFLIGHT_SCHEMA_VERSION = "sft2b_matched_pilot_lean_preflight_v2"
TERMINAL_SCHEMA_VERSION: Literal["sft2b_matched_pilot_source_terminal_v2"] = (
    "sft2b_matched_pilot_source_terminal_v2"
)
MANIFEST_SCHEMA_VERSION = "sft2b_matched_pilot_lean_manifest_v2"

_SOURCE_CLASS_ORDER = (
    "library_docstring",
    "theorem_problem",
    "broader_public_synthetic",
    "specialist_high_difficulty",
)
_INFRASTRUCTURE_STATUSES = {
    LeanStatus.TIMEOUT,
    LeanStatus.CRASH,
    LeanStatus.SETUP_ERROR,
    LeanStatus.UNSUPPORTED,
    LeanStatus.INTERNAL_ERROR,
}
_FROZEN_EXPLICIT_LEVEL = re.compile(r"\bu_[0-9]+\b")
_LEGACY_FINSET_SUM_IN = re.compile(r"(?P<head>∑\s+[A-Za-z_][A-Za-z0-9_']*\s+)in(?=\s+)")


class MatchedPilotLeanAuditError(RuntimeError):
    """Raised when the audit cannot produce trustworthy terminal evidence."""


def _audit_level_names(proposition: str) -> tuple[str, ...]:
    """Include universe parameters appearing only in explicit constant levels."""

    return tuple(
        dict.fromkeys((*_level_names(proposition), *_FROZEN_EXPLICIT_LEVEL.findall(proposition)))
    )


class BundlePin(StrictModel):
    repo_id: str
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    path_prefix: str
    local_root: Path
    files: dict[str, Sha256]


class AuditThresholds(StrictModel):
    expected_sources: Literal[500]
    expected_requests: Literal[2000]
    expected_candidates: Literal[1242]
    expected_unique_signatures: Literal[1147]
    expected_render_contexts: Literal[35]
    expected_source_contexts: Literal[36]
    minimum_valid_candidates: Annotated[int, Field(ge=500)]
    minimum_valid_candidate_fraction_of_admitted: Annotated[float, Field(ge=0.4, le=1.0)]
    minimum_valid_candidate_fraction_of_requests: Annotated[float, Field(ge=0.25, le=1.0)]
    minimum_sources_with_valid_candidate: Annotated[int, Field(ge=250, le=500)]
    maximum_infrastructure_failure_fraction: Annotated[float, Field(gt=0.0, le=0.02)]


class ReferenceSyntaxMigration(StrictModel):
    source_id: StableId
    source_proposition_sha256: Sha256
    expected_replacements: Annotated[int, Field(ge=1)]


class MatchedPilotLeanAuditConfig(StrictModel):
    schema_version: Literal["sft2b_matched_pilot_lean_audit_v1"]
    owner_session: str
    consumer_config_path: Path
    helper_path: Path
    mathlib_project_path: Path
    mathlib_named_reference_catalog_path: Path
    mathlib_named_reference_catalog_sha256: Sha256
    explicit_reference_theorem_ids: tuple[str, ...]
    reference_syntax_migrations: tuple[ReferenceSyntaxMigration, ...]
    output_parent: Path
    input_bundle: BundlePin
    output_bundle: BundlePin
    thresholds: AuditThresholds
    lean_timeout_seconds: Annotated[float, Field(gt=0.0)]
    maximum_infrastructure_attempts: Annotated[int, Field(ge=1, le=3)]
    claimed_lean_rss_gib: Annotated[float, Field(ge=3.0, le=40.0)]


class ReferenceElaborationInput(StrictModel):
    method: Literal[
        "source_signature_pp",
        "pinned_sum_in_syntax_migration_v1",
        "frozen_reference_signature_explicit",
        "frozen_reference_constant_type",
    ]
    carrier: Annotated[str, Field(min_length=1)]
    raw_statement: Annotated[str, Field(min_length=1)]


class SourceAuditTerminal(StrictModel):
    schema_version: Literal["sft2b_matched_pilot_source_terminal_v2"]
    run_id: StableId
    source_id: StableId
    source_ordinal: Annotated[int, Field(ge=0, lt=500)]
    source_class: str
    source_context_id: str
    render_compile_context_id: str
    reference_elaboration_method: Literal[
        "source_signature_pp",
        "pinned_sum_in_syntax_migration_v1",
        "frozen_reference_signature_explicit",
        "frozen_reference_constant_type",
    ]
    reference_elaboration_sha256: Sha256
    reference_elaborated: bool
    candidate_ids: tuple[StableId, ...]
    candidate_elaborated: tuple[bool, ...]
    reference: EndpointCacheRecord
    candidates: tuple[EndpointCacheRecord, ...]
    request_hash: Sha256
    request_status: str
    elapsed_ms: Annotated[int, Field(ge=0)]
    raw_response_path: str | None = None
    raw_response_sha256: Sha256 | None = None
    infrastructure_attempts: Annotated[int, Field(ge=0)]
    backend_method_version: str
    peak_rss_bytes: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_terminal(self) -> SourceAuditTerminal:
        if (
            self.reference.endpoint_role != "reference"
            or self.reference.source_id != self.source_id
        ):
            raise ValueError("source terminal reference identity mismatch")
        if tuple(item.candidate_id for item in self.candidates) != self.candidate_ids:
            raise ValueError("source terminal candidate identity/order mismatch")
        if len(self.candidate_elaborated) != len(self.candidate_ids):
            raise ValueError("source terminal candidate elaboration vector mismatch")
        if any(item.source_id != self.source_id for item in self.candidates):
            raise ValueError("source terminal mixes source IDs")
        if self.reference.status == CompileStatus.VALID and not self.reference_elaborated:
            raise ValueError("valid reference representation lacks elaboration evidence")
        expected_reference_error = (
            "trusted_reference_repr_invalid"
            if self.reference_elaborated
            else "trusted_reference_elaboration_invalid"
        )
        if (
            self.reference.status == CompileStatus.INVALID
            and self.reference.error_class != expected_reference_error
        ):
            raise ValueError("invalid reference classification disagrees with elaboration evidence")
        for elaborated, candidate in zip(self.candidate_elaborated, self.candidates, strict=True):
            if candidate.status == CompileStatus.VALID and not elaborated:
                raise ValueError("valid candidate representation lacks elaboration evidence")
            expected_error = (
                "candidate_repr_invalid" if elaborated else "candidate_elaboration_invalid"
            )
            if (
                candidate.status == CompileStatus.INVALID
                and candidate.error_class != expected_error
            ):
                raise ValueError(
                    "invalid candidate classification disagrees with elaboration evidence"
                )
        return self


class _CapturingBackend:
    def __init__(self, backend: LeanBackend) -> None:
        self.backend = backend
        self.requests: list[LeanRequest] = []
        self.results: list[LeanResult] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        result = self.backend.run(request)
        self.results.append(result)
        return result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        raise MatchedPilotLeanAuditError("matched-pilot audit uses one request per source")

    def close(self) -> None:
        self.backend.close()


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MatchedPilotLeanAuditError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MatchedPilotLeanAuditError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _read_models[ModelT: StrictModel](path: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    rows: list[ModelT] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise MatchedPilotLeanAuditError(f"blank JSONL row at {path}:{line_number}")
            try:
                rows.append(model.model_validate_json(line))
            except Exception as exc:
                raise MatchedPilotLeanAuditError(
                    f"invalid {model.__name__} at {path}:{line_number}: {exc}"
                ) from exc
    return tuple(rows)


def load_config(path: Path) -> tuple[MatchedPilotLeanAuditConfig, str]:
    try:
        config = MatchedPilotLeanAuditConfig.model_validate(_json_object(path))
    except Exception as exc:
        raise MatchedPilotLeanAuditError(f"invalid matched-pilot audit config: {exc}") from exc
    return config, hash_file(path)


def _git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _run_identity(
    *, repo_root: Path, config_path: Path, config: MatchedPilotLeanAuditConfig
) -> tuple[str, Path, dict[str, str]]:
    module_path = Path(__file__).resolve()
    identity = {
        "schema_version": RUN_SCHEMA_VERSION,
        "config_sha256": hash_file(config_path),
        "module_sha256": hash_file(module_path),
        "repo_git_commit": _git_head(repo_root),
        "input_sources_sha256": config.input_bundle.files["sources.jsonl"],
        "output_candidates_sha256": config.output_bundle.files["candidates.jsonl"],
    }
    run_id = stable_id("sft2b_matched_lean_audit", identity)
    return run_id, config.output_parent / run_id.replace(":", "_"), identity


def _verify_file_pins(bundle: BundlePin) -> dict[str, str]:
    if not bundle.local_root.is_dir() or bundle.local_root.is_symlink():
        raise MatchedPilotLeanAuditError(f"bundle root is unavailable: {bundle.local_root}")
    observed: dict[str, str] = {}
    actual_names = {item.name for item in bundle.local_root.iterdir() if item.is_file()}
    if actual_names != set(bundle.files):
        raise MatchedPilotLeanAuditError(
            f"bundle file set drifted at {bundle.local_root}: {sorted(actual_names)}"
        )
    for name, expected in bundle.files.items():
        path = bundle.local_root / name
        digest = hash_file(path)
        if digest != expected:
            raise MatchedPilotLeanAuditError(f"bundle hash drifted: {path}")
        observed[name] = digest
    return dict(sorted(observed.items()))


def _helper_body(helper_path: Path, pins: RuntimePins) -> str:
    if hash_file(helper_path) != pins.sft2b_helper_hash:
        raise MatchedPilotLeanAuditError("SFT2B helper differs from verified runtime pins")
    return "\n".join(
        line
        for line in helper_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("import ")
    )


def _compile_context(source: SourceRecord, *, helper_body: str) -> CompileContext:
    record = source.compile_context
    context = CompileContext(
        project_id=record.project_id,
        project_revision=record.project_revision,
        lean_version=record.lean_version,
        import_header=record.import_header,
        command_preamble=helper_body,
        namespace_context=record.namespace_context,
        open_context=record.open_context,
        scoped_context=record.scoped_context,
        options=record.options,
    )
    if context.compile_context_id != record.render_compile_context_id:
        raise MatchedPilotLeanAuditError(
            f"render context does not replay for source {source.source_id}"
        )
    return context


def _source_classes(
    sources: Sequence[SourceRecord], source_manifest: dict[str, Any]
) -> tuple[str, ...]:
    source_mix = source_manifest.get("source_mix")
    if not isinstance(source_mix, dict) or not isinstance(source_mix.get("selected"), dict):
        raise MatchedPilotLeanAuditError("source manifest lacks selected source mix")
    selected = cast(dict[str, object], source_mix["selected"])
    expected = {
        "library_docstring": 175,
        "theorem_problem": 175,
        "broader_public_synthetic": 100,
        "specialist_high_difficulty": 50,
    }
    if selected != expected:
        raise MatchedPilotLeanAuditError(f"matched-pilot source mix drifted: {selected}")
    classes = tuple(name for name in _SOURCE_CLASS_ORDER for _ in range(expected[name]))
    if len(sources) != len(classes):
        raise MatchedPilotLeanAuditError("source class vector does not cover exact source order")
    for ordinal, (source, source_class) in enumerate(zip(sources, classes, strict=True)):
        location = f"{source.provenance.source_url} {source.provenance.source_path}"
        if source_class == "library_docstring" and "mathlib4" not in location.lower():
            raise MatchedPilotLeanAuditError(f"source class/order drift at ordinal {ordinal}")
        if source_class == "theorem_problem" and "sft_classic_numina" not in location:
            raise MatchedPilotLeanAuditError(f"source class/order drift at ordinal {ordinal}")
        if source_class in {"broader_public_synthetic", "specialist_high_difficulty"} and (
            "Lean-Workbook" not in location and "lean-workbook" not in location.lower()
        ):
            raise MatchedPilotLeanAuditError(f"source class/order drift at ordinal {ordinal}")
    return classes


def _verify_source_material(sources: Sequence[SourceRecord], repo_root: Path) -> dict[str, str]:
    observed: dict[str, str] = {}
    for raw_path, expected in sorted(
        {
            (item.compile_context.source_context_path, item.compile_context.source_context_sha256)
            for item in sources
        }
    ):
        path = Path(raw_path)
        old_root = Path("/localhome/milikic/LeanFaith")
        if path.is_relative_to(old_root):
            path = repo_root / path.relative_to(old_root)
        # Hugging Face snapshot entries are intentionally symlinks into its
        # content-addressed blob store.  Trust only the dereferenced bytes;
        # release bundles themselves remain protected by _verify_file_pins.
        if not path.is_file() or hash_file(path) != expected:
            raise MatchedPilotLeanAuditError(f"source-context material drifted: {path}")
        observed[str(path)] = expected
    return observed


def _migrated_reference_carrier(source: SourceRecord, migration: ReferenceSyntaxMigration) -> str:
    if migration.source_id != source.source_id:
        raise MatchedPilotLeanAuditError("reference syntax migration source identity drifted")
    if migration.source_proposition_sha256 != source.reference_proposition_sha256:
        raise MatchedPilotLeanAuditError(
            f"reference syntax migration source hash drifted: {source.source_id}"
        )
    carrier, replacements = _LEGACY_FINSET_SUM_IN.subn(r"\g<head>∈", source.reference_proposition)
    if replacements != migration.expected_replacements:
        raise MatchedPilotLeanAuditError(
            f"reference syntax migration count drifted: {source.source_id}"
        )
    return carrier


def _reference_elaboration_inputs(
    sources: Sequence[SourceRecord],
    source_classes: Sequence[str],
    source_manifest: dict[str, Any],
    *,
    named_catalog_path: Path,
    named_catalog_sha256: str,
    explicit_theorem_ids: frozenset[str],
    syntax_migrations: Sequence[ReferenceSyntaxMigration] = (),
) -> tuple[dict[str, ReferenceElaborationInput], dict[str, object]]:
    """Recover exact elaboration carriers without changing source records.

    Mathlib's frozen ``signature_pp`` is the source-facing proposition, but it
    is intentionally compact and can omit annotations needed to elaborate it
    without an expected type.  Even ``signature_explicit`` can contain the
    pretty-printer's proof-elision marker.  Cross-bind the compact text and
    frozen theorem identity byte-for-byte, then load the named constant's type
    from the exact project revision without re-elaborating either text view.
    Other source classes retain their source text.
    """

    migrations_by_source = {item.source_id: item for item in syntax_migrations}
    if len(migrations_by_source) != len(syntax_migrations):
        raise MatchedPilotLeanAuditError("reference syntax migrations contain duplicate source IDs")
    source_by_id = {item.source_id: item for item in sources}
    if not set(migrations_by_source).issubset(source_by_id):
        raise MatchedPilotLeanAuditError("reference syntax migration names an unselected source")

    catalogs = source_manifest.get("source_catalogs")
    if not isinstance(catalogs, dict):
        raise MatchedPilotLeanAuditError("source manifest lacks source catalogs")
    mathlib = catalogs.get("mathlib_docstrings")
    if not isinstance(mathlib, dict):
        raise MatchedPilotLeanAuditError("source manifest lacks Mathlib reference catalog")
    catalog_specs = (
        (
            "algebra",
            Path(str(mathlib.get("reference_catalog_path", ""))),
            str(mathlib.get("reference_catalog_sha256", "")),
        ),
        (
            "cross_domain",
            Path(str(mathlib.get("cross_domain_catalog_path", ""))),
            str(mathlib.get("cross_domain_catalog_sha256", "")),
        ),
    )
    mathlib_ids_by_family: dict[str, set[str]] = {name: set() for name, _, _ in catalog_specs}
    for source, source_class in zip(sources, source_classes, strict=True):
        if source_class != "library_docstring":
            continue
        source_family = source.provenance.source_family
        if source_family not in mathlib_ids_by_family:
            raise MatchedPilotLeanAuditError(
                f"unsupported Mathlib source family for reference recovery: {source_family}"
            )
        mathlib_ids_by_family[source_family].add(source.reference_theorem_id)

    rows: dict[str, dict[str, object]] = {}
    constant_rows: dict[str, dict[str, object]] = {}
    catalog_receipts: dict[str, object] = {}
    for family_key, catalog_path, catalog_hash in catalog_specs:
        if (
            not catalog_path.is_file()
            or len(catalog_hash) != 64
            or hash_file(catalog_path) != catalog_hash
        ):
            raise MatchedPilotLeanAuditError(
                f"frozen Mathlib {family_key} reference catalog drifted"
            )
        selected_ids = mathlib_ids_by_family[family_key]
        selected_rows: dict[str, dict[str, object]] = {}
        with catalog_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise MatchedPilotLeanAuditError(
                        f"blank reference-catalog row at {catalog_path}:{line_number}"
                    )
                try:
                    value: object = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MatchedPilotLeanAuditError(
                        f"invalid reference-catalog JSON at {catalog_path}:{line_number}"
                    ) from exc
                if not isinstance(value, dict):
                    raise MatchedPilotLeanAuditError("reference-catalog row is not an object")
                representation: object = (
                    value.get("representation") if family_key == "cross_domain" else value
                )
                if not isinstance(representation, dict):
                    raise MatchedPilotLeanAuditError(
                        f"Mathlib {family_key} reference representation is not an object"
                    )
                theorem_id = representation.get("theorem_id")
                if theorem_id not in selected_ids:
                    continue
                if not isinstance(theorem_id, str) or theorem_id in selected_rows:
                    raise MatchedPilotLeanAuditError(
                        f"Mathlib {family_key} catalog has duplicate selected theorem ID"
                    )
                selected_rows[theorem_id] = cast(dict[str, object], representation)
                if family_key == "cross_domain":
                    theorem: object = value.get("theorem")
                    if not isinstance(theorem, dict) or theorem.get("theorem_id") != theorem_id:
                        raise MatchedPilotLeanAuditError(
                            "Mathlib cross-domain theorem metadata is missing or mismatched"
                        )
                    constant_rows[theorem_id] = cast(dict[str, object], theorem)
        if set(selected_rows) != selected_ids:
            raise MatchedPilotLeanAuditError(
                f"Mathlib {family_key} reference catalog lacks selected theorem IDs"
            )
        duplicate_ids = set(rows).intersection(selected_rows)
        if duplicate_ids:
            raise MatchedPilotLeanAuditError(
                "Mathlib reference theorem IDs overlap source-family catalogs"
            )
        rows.update(selected_rows)
        catalog_receipts[family_key] = {
            "path": str(catalog_path),
            "sha256": catalog_hash,
            "selected_rows": len(selected_rows),
        }

    mathlib_ids = set().union(*mathlib_ids_by_family.values())
    if set(rows) != mathlib_ids:
        raise MatchedPilotLeanAuditError("Mathlib reference catalog union drifted")
    if not explicit_theorem_ids.issubset(mathlib_ids):
        raise MatchedPilotLeanAuditError(
            "explicit-reference exception contains an unselected theorem ID"
        )

    named_path = named_catalog_path
    named_hash = named_catalog_sha256
    if not named_path.is_file() or len(named_hash) != 64 or hash_file(named_path) != named_hash:
        raise MatchedPilotLeanAuditError("frozen Mathlib named-reference catalog drifted")
    algebra_ids = mathlib_ids_by_family["algebra"]
    selected_named_rows: dict[str, dict[str, object]] = {}
    with named_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise MatchedPilotLeanAuditError(
                    f"blank named-reference row at {named_path}:{line_number}"
                )
            try:
                named_value: object = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MatchedPilotLeanAuditError(
                    f"invalid named-reference JSON at {named_path}:{line_number}"
                ) from exc
            if not isinstance(named_value, dict):
                raise MatchedPilotLeanAuditError("named-reference row is not an object")
            named_theorem: object = named_value.get("theorem")
            if not isinstance(named_theorem, dict):
                raise MatchedPilotLeanAuditError("named-reference theorem is not an object")
            theorem_id = named_theorem.get("theorem_id")
            if theorem_id not in algebra_ids:
                continue
            if not isinstance(theorem_id, str) or theorem_id in selected_named_rows:
                raise MatchedPilotLeanAuditError(
                    "named-reference catalog has duplicate selected theorem ID"
                )
            selected_named_rows[theorem_id] = cast(dict[str, object], named_theorem)
    if set(selected_named_rows) != algebra_ids:
        raise MatchedPilotLeanAuditError("named-reference catalog lacks selected algebra IDs")
    constant_rows.update(selected_named_rows)
    if set(constant_rows) != mathlib_ids:
        raise MatchedPilotLeanAuditError("Mathlib named-reference catalog union drifted")

    result: dict[str, ReferenceElaborationInput] = {}
    method_counts: Counter[str] = Counter()
    for source, source_class in zip(sources, source_classes, strict=True):
        if source_class == "library_docstring":
            row = rows[source.reference_theorem_id]
            theorem = constant_rows[source.reference_theorem_id]
            compact = row.get("signature_pp")
            explicit = row.get("signature_explicit")
            if compact != source.reference_proposition:
                raise MatchedPilotLeanAuditError(
                    f"compact Mathlib reference drifted for {source.source_id}"
                )
            full_name = theorem.get("declaration_full_name")
            source_file = theorem.get("source_file")
            source_revision = theorem.get("source_revision")
            raw_statement = row.get("raw_proof_stripped")
            short_name = source.reference_declaration_name
            normalized_short_name = (
                short_name.removeprefix("_root_.") if short_name is not None else None
            )
            source_name_matches = short_name is None or (
                isinstance(full_name, str)
                and normalized_short_name is not None
                and (
                    full_name == normalized_short_name
                    or full_name.endswith(f".{normalized_short_name}")
                )
            )
            if (
                not isinstance(full_name, str)
                or not full_name.strip()
                or not source_name_matches
                or source_file != source.provenance.source_path
                or source_revision != source.provenance.source_revision
            ):
                raise MatchedPilotLeanAuditError(
                    f"named Mathlib reference drifted for {source.source_id}"
                )
            if not isinstance(raw_statement, str) or not raw_statement.strip():
                raise MatchedPilotLeanAuditError(
                    f"raw Mathlib reference is missing for {source.source_id}"
                )
            if not isinstance(explicit, str) or not explicit.strip():
                raise MatchedPilotLeanAuditError(
                    f"explicit Mathlib reference is missing for {source.source_id}"
                )
            # The named constant type is authoritative. A theorem enters the
            # explicit route only after a measured GoalV1 surface failure is
            # recorded by exact theorem ID in the config.
            use_explicit = source.reference_theorem_id in explicit_theorem_ids
            if use_explicit and "⋯" in explicit:
                raise MatchedPilotLeanAuditError(
                    f"explicit-reference exception contains proof elision: {source.source_id}"
                )
            PropositionEndpoint(
                endpoint_id="reference",
                endpoint_role="reference",
                proposition=explicit if use_explicit else source.reference_proposition,
                source_id=source.source_id,
            )
            item = ReferenceElaborationInput(
                method=(
                    "frozen_reference_signature_explicit"
                    if use_explicit
                    else "frozen_reference_constant_type"
                ),
                carrier=explicit if use_explicit else full_name,
                raw_statement=raw_statement,
            )
        else:
            migration = migrations_by_source.get(source.source_id)
            if migration is None:
                item = ReferenceElaborationInput(
                    method="source_signature_pp",
                    carrier=source.reference_proposition,
                    raw_statement=source.reference_proposition,
                )
            else:
                item = ReferenceElaborationInput(
                    method="pinned_sum_in_syntax_migration_v1",
                    carrier=_migrated_reference_carrier(source, migration),
                    raw_statement=source.reference_proposition,
                )
        result[source.source_id] = item
        method_counts[item.method] += 1
    return result, {
        "catalogs": catalog_receipts,
        "named_reference_catalog": {
            "path": str(named_path),
            "sha256": named_hash,
            "selected_rows": len(selected_named_rows),
        },
        "selected_mathlib_rows": len(mathlib_ids),
        "explicit_reference_theorem_ids": sorted(explicit_theorem_ids),
        "reference_syntax_migrations": [
            {
                **item.model_dump(mode="json"),
                "carrier_sha256": sha256_hex(result[item.source_id].carrier.encode("utf-8")),
            }
            for item in syntax_migrations
        ],
        "method_counts": dict(sorted(method_counts.items())),
    }


def _expected_candidate_order(attempts: Sequence[FormalizerAttempt]) -> tuple[str, ...]:
    return tuple(
        item.candidate_id
        for item in attempts
        if item.extraction_status == "candidate" and item.candidate_id is not None
    )


def prepare_preflight(
    *, repo_root: Path, config_path: Path, force_replay: bool = False
) -> tuple[dict[str, Any], Path]:
    """Replay all cheap evidence and write an immutable no-Lean preflight."""

    config, config_hash = load_config(config_path)
    run_id, run_root, identity = _run_identity(
        repo_root=repo_root, config_path=config_path, config=config
    )
    preflight_path = run_root / "preflight.json"
    input_hashes = _verify_file_pins(config.input_bundle)
    output_hashes = _verify_file_pins(config.output_bundle)
    if preflight_path.is_file() and not force_replay:
        preflight = _json_object(preflight_path)
        if (
            preflight.get("schema_version") != PREFLIGHT_SCHEMA_VERSION
            or preflight.get("run_id") != run_id
            or preflight.get("input_artifacts") != input_hashes
            or preflight.get("output_artifacts") != output_hashes
        ):
            raise MatchedPilotLeanAuditError("cached preflight identity drifted")
        return preflight, run_root

    consumer_path = repo_root / config.consumer_config_path
    consumer, consumer_hash = load_consumer_spec(consumer_path)
    try:
        observed = verify_observed_pilot(
            repo_root,
            consumer,
            artifact_root=config.output_bundle.local_root,
            pilot_input_root=config.input_bundle.local_root,
        )
    except FullSourceConsumerError as exc:
        raise MatchedPilotLeanAuditError(f"frozen pilot replay failed: {exc}") from exc

    sources = _read_models(config.input_bundle.local_root / "sources.jsonl", SourceRecord)
    candidates = _read_models(config.output_bundle.local_root / "candidates.jsonl", CandidateRecord)
    attempts = _read_models(
        config.output_bundle.local_root / "formalizer_attempts.jsonl", FormalizerAttempt
    )
    thresholds = config.thresholds
    if not (
        len(sources) == thresholds.expected_sources
        and len(attempts) == thresholds.expected_requests
        and len(candidates) == thresholds.expected_candidates
        and len({item.source_id for item in sources}) == len(sources)
        and len({item.candidate_id for item in candidates}) == len(candidates)
    ):
        raise MatchedPilotLeanAuditError("matched-pilot source/request/candidate counts drifted")
    expected_candidate_order = _expected_candidate_order(attempts)
    if tuple(item.candidate_id for item in candidates) != expected_candidate_order:
        raise MatchedPilotLeanAuditError("candidate rows differ from admitted attempt order")

    source_by_id = {item.source_id: item for item in sources}
    if any(
        item.source_id not in source_by_id
        or item.source_context_id != source_by_id[item.source_id].compile_context.source_context_id
        for item in candidates
    ):
        raise MatchedPilotLeanAuditError("candidate/source/context join drifted")
    if len({item.signature_sha256 for item in candidates}) != thresholds.expected_unique_signatures:
        raise MatchedPilotLeanAuditError("unique candidate-signature count drifted")

    source_manifest = _json_object(config.input_bundle.local_root / "source_manifest.json")
    source_classes = _source_classes(sources, source_manifest)
    _, reference_elaboration = _reference_elaboration_inputs(
        sources,
        source_classes,
        source_manifest,
        named_catalog_path=config.mathlib_named_reference_catalog_path,
        named_catalog_sha256=config.mathlib_named_reference_catalog_sha256,
        explicit_theorem_ids=frozenset(config.explicit_reference_theorem_ids),
        syntax_migrations=config.reference_syntax_migrations,
    )
    helper_path = repo_root / config.helper_path
    pins = verify_runtime_pins(repo_root, helper_path=helper_path)
    helper_body = _helper_body(helper_path, pins)
    contexts = [_compile_context(source, helper_body=helper_body) for source in sources]
    render_context_count = len({item.compile_context_id for item in contexts})
    source_context_count = len({item.compile_context.source_context_id for item in sources})
    if (
        render_context_count != thresholds.expected_render_contexts
        or source_context_count != thresholds.expected_source_contexts
    ):
        raise MatchedPilotLeanAuditError("matched-pilot context census drifted")
    if any(
        item.compile_context.project_path != str(config.mathlib_project_path)
        or item.compile_context.project_revision != contexts[0].project_revision
        or item.compile_context.lean_version != contexts[0].lean_version
        for item in sources
    ):
        raise MatchedPilotLeanAuditError("matched-pilot project/toolchain identity drifted")
    project_head = _git_head(config.mathlib_project_path)
    if project_head != contexts[0].project_revision:
        raise MatchedPilotLeanAuditError("mounted Mathlib revision differs from source records")
    context_material = _verify_source_material(sources, repo_root)

    candidate_count_by_source = Counter(item.source_id for item in candidates)
    candidate_count_histogram = Counter(candidate_count_by_source.values())
    candidate_count_histogram[0] = len(sources) - len(candidate_count_by_source)
    extraction_histogram = Counter(
        item.failure_class or item.extraction_status for item in attempts
    )
    source_class_by_id = {
        source.source_id: source_classes[index] for index, source in enumerate(sources)
    }
    class_candidate_counts = Counter(source_class_by_id[item.source_id] for item in candidates)
    context_source_counts = Counter(item.compile_context_id for item in contexts)
    context_candidate_counts = Counter(
        source_by_id[item.source_id].compile_context.render_compile_context_id
        for item in candidates
    )
    preflight = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "run_id": run_id,
        "identity": identity,
        "config_sha256": config_hash,
        "consumer_config_sha256": consumer_hash,
        "observed_pilot_evidence_binding_sha256": observed.evidence_binding_sha256,
        "published_input": {
            "repo_id": config.input_bundle.repo_id,
            "revision": config.input_bundle.revision,
            "path_prefix": config.input_bundle.path_prefix,
        },
        "published_output": {
            "repo_id": config.output_bundle.repo_id,
            "revision": config.output_bundle.revision,
            "path_prefix": config.output_bundle.path_prefix,
        },
        "input_artifacts": input_hashes,
        "output_artifacts": output_hashes,
        "context_material": context_material,
        "runtime_pins": pins.to_dict(),
        "counts": {
            "sources": len(sources),
            "requests": len(attempts),
            "candidates": len(candidates),
            "unique_signatures": len({item.signature_sha256 for item in candidates}),
            "render_contexts": render_context_count,
            "source_contexts": source_context_count,
        },
        "source_ids_sha256": hash_canonical([item.source_id for item in sources]),
        "candidate_ids_sha256": hash_canonical([item.candidate_id for item in candidates]),
        "source_class_counts": dict(sorted(Counter(source_classes).items())),
        "reference_elaboration": reference_elaboration,
        "class_candidate_counts": dict(sorted(class_candidate_counts.items())),
        "candidate_count_by_source_histogram": {
            str(key): value for key, value in sorted(candidate_count_histogram.items())
        },
        "extractor_histogram": dict(sorted(extraction_histogram.items())),
        "context_source_counts": dict(sorted(context_source_counts.items())),
        "context_candidate_counts": dict(sorted(context_candidate_counts.items())),
        "lean_started": False,
    }
    write_json(preflight_path, preflight)
    return preflight, run_root


def _endpoints(
    source: SourceRecord,
    candidates: Sequence[CandidateRecord],
    *,
    reference_proposition: str | None = None,
) -> tuple[PropositionEndpoint, ...]:
    reference = PropositionEndpoint(
        endpoint_id="reference",
        endpoint_role="reference",
        proposition=reference_proposition or source.reference_proposition,
        source_id=source.source_id,
    )
    candidate_endpoints = tuple(
        PropositionEndpoint(
            endpoint_id=item.candidate_id,
            endpoint_role="candidate",
            proposition=item.raw_proof_free_signature,
            source_id=source.source_id,
            candidate_id=item.candidate_id,
        )
        for item in candidates
    )
    if candidate_endpoints:
        return (reference, *candidate_endpoints)
    probe_id = stable_id("sft2b_reference_probe", {"source_id": source.source_id})
    return (
        reference,
        PropositionEndpoint(
            endpoint_id=probe_id,
            endpoint_role="candidate",
            proposition="True",
            source_id=source.source_id,
            candidate_id=probe_id,
        ),
    )


def _build_isolated_tolerant_session_body(
    endpoints: Sequence[PropositionEndpoint],
    *,
    render_scope_id: str,
    reference_constant_name: str | None = None,
) -> str:
    """Build a tolerant action whose rejected diagnostics cannot escape.

    ``TermElabM`` state and its message log are committed only for a clean
    candidate.  Deterministic candidate exceptions become sentinels, while
    interrupts and runtime failures are restored and rethrown for the Python
    infrastructure retry policy.
    """

    if len(endpoints) < 2 or endpoints[0].endpoint_role != "reference":
        raise ValueError("isolated tolerant session requires reference followed by candidates")
    if any(item.endpoint_role != "candidate" for item in endpoints[1:]):
        raise ValueError("isolated tolerant session has a non-candidate after the reference")
    reference = endpoints[0]
    lines = ["run_meta do"]
    if reference_constant_name is None:
        reference_source = json.dumps(reference.proposition, ensure_ascii=False)
        lines.append(
            "  let endpoint0 ← LeanFaith.SFT2B.Helper.elaborateProposition "
            f'"reference:reference" {reference_source} '
            f"{_lean_name_list(_audit_level_names(reference.proposition))}"
        )
    else:
        constant_literal = json.dumps(reference_constant_name, ensure_ascii=False)
        lines.extend(
            (
                "  let endpoint0 ←",
                f"    match (← Lean.getEnv).find? {constant_literal}.toName with",
                "    | some (.thmInfo info) =>",
                "        LeanFaith.SFT2B.Helper.checkedClosedProp "
                f'"reference:{reference_constant_name}" info.type',
                '    | some _ => throwError "trusted reference is not a theorem"',
                '    | none => throwError "trusted reference constant is not imported"',
            )
        )
    for index, endpoint in enumerate(endpoints[1:], start=1):
        source_literal = json.dumps(endpoint.proposition, ensure_ascii=False)
        origin_literal = json.dumps(f"candidate:{endpoint.endpoint_id}")
        sentinel = _failure_sentinel_nat(endpoint.endpoint_id)
        lines.extend(
            (
                f"  let endpoint{index}? ← Lean.Elab.Term.TermElabM.run' do",
                "    let saved ← Lean.Elab.Term.saveState",
                "    Lean.Core.resetMessageLog",
                "    try",
                "      let value ← LeanFaith.SFT2B.Helper.elaborateProposition "
                f"{origin_literal} {source_literal} "
                f"{_lean_name_list(_audit_level_names(endpoint.proposition))}",
                "      if (← Lean.MonadLog.hasErrors) then",
                "        Lean.restoreState saved",
                "        pure none",
                "      else",
                "        Lean.Core.setMessageLog "
                "(saved.meta.core.messages ++ (← Lean.Core.getMessageLog))",
                "        pure (some value)",
                "    catch ex =>",
                "      Lean.restoreState saved",
                "      if ex.isInterrupt || ex.isRuntime then",
                "        throw ex",
                "      pure none",
                f"  let endpoint{index} : Lean.Expr := endpoint{index}?.getD <| "
                "Lean.mkApp3 (Lean.mkConst ``Eq [.succ .zero]) "
                f"(Lean.mkConst ``Nat []) (.lit (.natVal {sentinel})) "
                f"(.lit (.natVal {sentinel + 1}))",
            )
        )
    scope_literal = json.dumps(render_scope_id)
    for index, endpoint in enumerate(endpoints):
        endpoint_literal = json.dumps(endpoint.endpoint_id)
        expr_origin = (
            "loaded_constant_type"
            if index == 0 and reference_constant_name is not None
            else "term_elaborated_proposition"
        )
        lines.append(
            "  LeanFaith.GoalV1.emitClosedProp "
            f'{endpoint_literal} {scope_literal} "{expr_origin}" endpoint{index}'
        )
    return "\n".join(lines)


def _render_propositions_isolated(
    backend: LeanBackend,
    *,
    endpoints: Sequence[PropositionEndpoint],
    compile_context: CompileContext,
    render_scope_id: str,
    request_id: str,
    timeout_seconds: float,
    reference_constant_name: str | None = None,
    reference_raw_statement: str | None = None,
) -> ClosedExprBatchResult:
    inputs: list[ClosedExprInput] = []
    for index, item in enumerate(endpoints):
        if index == 0 and reference_constant_name is not None:
            inputs.append(
                ClosedExprInput(
                    endpoint_id=item.endpoint_id,
                    endpoint_role=item.endpoint_role,  # type: ignore[arg-type]
                    expr_origin="loaded_constant_type",
                    source_material=ClosedExprSourceMaterial(
                        kind="raw_statement", raw_statement=reference_raw_statement
                    ),
                )
            )
        else:
            inputs.append(
                ClosedExprInput(
                    endpoint_id=item.endpoint_id,
                    endpoint_role=item.endpoint_role,  # type: ignore[arg-type]
                    expr_origin="term_elaborated_proposition",
                    source_material=ClosedExprSourceMaterial(
                        kind="proposition_text", proposition_text=item.proposition
                    ),
                )
            )
    rendered = render_closed_expr_in_session(
        backend,
        inputs=tuple(inputs),
        compile_context=compile_context,
        render_scope_id=render_scope_id,
        session_body=_build_isolated_tolerant_session_body(
            endpoints,
            render_scope_id=render_scope_id,
            reference_constant_name=reference_constant_name,
        ),
        request_id=request_id,
        timeout_seconds=timeout_seconds,
    )
    if rendered.failures:
        return rendered
    candidate_ids = {item.endpoint_id for item in endpoints[1:]}
    failures = {
        sidecar.record.endpoint_id: (
            "candidate proposition failed to parse, elaborate, or check as one closed Prop"
        )
        for sidecar in rendered.sidecars
        if sidecar.record.endpoint_id in candidate_ids
        and sidecar.record.provenance.expr_hash
        == _failure_sentinel_expr_hash(sidecar.record.endpoint_id)
    }
    if not failures:
        return rendered
    return ClosedExprBatchResult(
        sidecars=tuple(
            item for item in rendered.sidecars if item.record.endpoint_id not in failures
        ),
        failures=tuple(
            ClosedExprFailure(endpoint_id=endpoint_id, detail=detail)
            for endpoint_id, detail in sorted(failures.items())
        ),
        request_hash=rendered.request_hash,
        elapsed_ms=rendered.elapsed_ms,
        raw_response_path=rendered.raw_response_path,
        render_scope_id=rendered.render_scope_id,
    )


def _elaboration_statuses(
    result: LeanResult, endpoints: Sequence[PropositionEndpoint]
) -> tuple[bool, tuple[bool, ...]]:
    """Read closed-Prop success from emitted Expr payloads, not surface parsing."""

    if result.status != LeanStatus.VALID or result.sorries:
        return False, tuple(False for _ in endpoints[1:])
    endpoint_ids = {item.endpoint_id for item in endpoints}
    payloads, issues = _parse_closed_expr_payloads(result.messages, endpoint_ids)
    if issues:
        raise MatchedPilotLeanAuditError(
            "closed-Expr elaboration payloads are malformed: " + "; ".join(issues)
        )
    reference_elaborated = endpoints[0].endpoint_id in payloads
    candidate_elaborated: list[bool] = []
    for endpoint in endpoints[1:]:
        payload = payloads.get(endpoint.endpoint_id)
        expr_tree = payload.get("expr_tree") if payload is not None else None
        candidate_elaborated.append(
            expr_tree is not None
            and hash_canonical(expr_tree) != _failure_sentinel_expr_hash(endpoint.endpoint_id)
        )
    return reference_elaborated, tuple(candidate_elaborated)


def _endpoint_record(
    *,
    endpoint: PropositionEndpoint,
    source: SourceRecord,
    context: CompileContext,
    pins: RuntimePins,
    status: CompileStatus,
    sidecar: object | None = None,
    detail: str | None = None,
    error_class: str | None = None,
) -> EndpointCacheRecord:
    key = endpoint_cache_key(
        endpoint,
        source_context_id=source.compile_context.source_context_id,
        compile_context=context,
        pins=pins,
    )
    if status == CompileStatus.VALID:
        if sidecar is None:
            raise MatchedPilotLeanAuditError("valid endpoint lacks its REPR sidecar")
        sidecar_dict = cast(dict[str, object], sidecar.to_dict())  # type: ignore[attr-defined]
        goal = str(sidecar.core_text())  # type: ignore[attr-defined]
        return EndpointCacheRecord(
            endpoint_cache_key=key,
            endpoint_id=endpoint.endpoint_id,
            endpoint_role=endpoint.endpoint_role,  # type: ignore[arg-type]
            source_id=source.source_id,
            candidate_id=endpoint.candidate_id,
            proposition_sha256=endpoint.proposition_sha256,
            source_context_id=source.compile_context.source_context_id,
            render_compile_context_id=context.compile_context_id,
            project_revision=context.project_revision,
            lean_version=context.lean_version,
            helper_sha256=pins.sft2b_helper_hash,
            repr_spec_sha256=pins.repr_spec_hash,
            repr_implementation_set_sha256=pins.repr_implementation_set_hash,
            status=status,
            goal_v1=goal,
            goal_v1_sha256=sha256_hex(goal.encode("utf-8")),
            repr_sidecar=sidecar_dict,
        )
    if detail is None:
        raise MatchedPilotLeanAuditError("failed endpoint lacks an error detail")
    return EndpointCacheRecord(
        endpoint_cache_key=key,
        endpoint_id=endpoint.endpoint_id,
        endpoint_role=endpoint.endpoint_role,  # type: ignore[arg-type]
        source_id=source.source_id,
        candidate_id=endpoint.candidate_id,
        proposition_sha256=endpoint.proposition_sha256,
        source_context_id=source.compile_context.source_context_id,
        render_compile_context_id=context.compile_context_id,
        project_revision=context.project_revision,
        lean_version=context.lean_version,
        helper_sha256=pins.sft2b_helper_hash,
        repr_spec_sha256=pins.repr_spec_hash,
        repr_implementation_set_sha256=pins.repr_implementation_set_hash,
        status=status,
        error_class=error_class
        or (
            "trusted_reference_elaboration_invalid"
            if endpoint.endpoint_role == "reference"
            else "candidate_elaboration_invalid"
        ),
        error_detail=detail,
    )


def _valid_candidate_record_or_repr_failure(
    *,
    endpoint: PropositionEndpoint,
    source: SourceRecord,
    context: CompileContext,
    pins: RuntimePins,
    sidecar: object,
) -> EndpointCacheRecord:
    """Keep closed-Prop success distinct from strict model-render failure."""

    try:
        return _endpoint_record(
            endpoint=endpoint,
            source=source,
            context=context,
            pins=pins,
            status=CompileStatus.VALID,
            sidecar=sidecar,
        )
    except ValidationError:
        return _endpoint_record(
            endpoint=endpoint,
            source=source,
            context=context,
            pins=pins,
            status=CompileStatus.INVALID,
            error_class="candidate_repr_invalid",
            detail=(
                "candidate elaborated as one closed Prop, but its frozen GoalV1 sidecar failed "
                "strict endpoint validation"
            ),
        )


def _terminal_path(run_root: Path, source: SourceRecord) -> Path:
    return run_root / "terminals" / f"{source.source_id.split(':', 1)[1]}.json"


def _journal(run_root: Path, *, run_id: str, source: SourceRecord) -> AppendOnlyJournal:
    suffix = source.source_id.split(":", 1)[1]
    return AppendOnlyJournal(
        run_root / "journals" / f"{suffix}.jsonl", run_id=run_id, source_id=source.source_id
    )


def _rss_high_water_bytes() -> int:
    return int(
        max(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        )
        * 1024
    )


def _reference_proposition_for_audit(
    source: SourceRecord, reference_input: ReferenceElaborationInput
) -> str:
    if reference_input.method in {
        "frozen_reference_signature_explicit",
        "pinned_sum_in_syntax_migration_v1",
    }:
        return reference_input.carrier
    return source.reference_proposition


def _read_terminal(path: Path) -> SourceAuditTerminal:
    try:
        return SourceAuditTerminal.model_validate(_json_object(path))
    except Exception as exc:
        raise MatchedPilotLeanAuditError(f"invalid source audit terminal {path}: {exc}") from exc


def _execute_source(
    *,
    backend: _CapturingBackend,
    source: SourceRecord,
    source_ordinal: int,
    source_class: str,
    reference_input: ReferenceElaborationInput,
    candidates: tuple[CandidateRecord, ...],
    context: CompileContext,
    pins: RuntimePins,
    config: MatchedPilotLeanAuditConfig,
    run_id: str,
    run_root: Path,
) -> tuple[SourceAuditTerminal | None, int]:
    reference_proposition = _reference_proposition_for_audit(source, reference_input)
    endpoints = _endpoints(source, candidates, reference_proposition=reference_proposition)
    reference_constant_name = (
        reference_input.carrier
        if reference_input.method == "frozen_reference_constant_type"
        else None
    )
    infrastructure_attempts = 0
    rendered = None
    last_result: LeanResult | None = None
    request_count_before = len(backend.requests)
    result_count_before = len(backend.results)
    for attempt in range(1, config.maximum_infrastructure_attempts + 1):
        rendered = _render_propositions_isolated(
            backend,
            endpoints=endpoints,
            compile_context=context,
            render_scope_id=f"scope:{source.source_id}",
            request_id=(
                f"sft2b-matched-audit:{run_id.split(':', 1)[1]}:"
                f"{source_ordinal:03d}:attempt-{attempt}"
            ),
            timeout_seconds=config.lean_timeout_seconds,
            reference_constant_name=reference_constant_name,
            reference_raw_statement=reference_input.raw_statement,
        )
        last_result = backend.results[-1]
        if last_result.status not in _INFRASTRUCTURE_STATUSES:
            break
        infrastructure_attempts += 1
        backend.backend.reset_session()  # type: ignore[attr-defined]
    if rendered is None or last_result is None:
        raise AssertionError("source execution produced no Lean result")
    lean_requests = len(backend.requests) - request_count_before
    if last_result.status in _INFRASTRUCTURE_STATUSES:
        failure_path = (
            run_root / "infrastructure_failures" / (f"{source.source_id.split(':', 1)[1]}.json")
        )
        payload = {
            "schema_version": "sft2b_matched_pilot_infrastructure_failure_v1",
            "run_id": run_id,
            "source_id": source.source_id,
            "source_ordinal": source_ordinal,
            "attempts": infrastructure_attempts,
            "last_status": last_result.status.value,
            "last_error": last_result.infrastructure_error,
            "request_hash": last_result.request_hash,
            "raw_response_path": last_result.raw_response_path,
        }
        write_json(failure_path, payload)
        return None, lean_requests

    reference_elaborated, endpoint_candidate_elaborated = _elaboration_statuses(
        last_result, endpoints
    )
    candidate_elaborated = endpoint_candidate_elaborated[: len(candidates)]
    failures = {item.endpoint_id: item.detail for item in rendered.failures}
    sidecars = {item.record.endpoint_id: item for item in rendered.sidecars}
    reference_endpoint = endpoints[0]
    if "reference" in sidecars:
        if not reference_elaborated:
            raise MatchedPilotLeanAuditError(
                "trusted reference has a REPR sidecar without elaboration evidence"
            )
        reference = _endpoint_record(
            endpoint=reference_endpoint,
            source=source,
            context=context,
            pins=pins,
            status=CompileStatus.VALID,
            sidecar=sidecars["reference"],
        )
    else:
        detail = failures.get("reference") or (
            f"trusted reference session failed with status {last_result.status.value}"
        )
        reference = _endpoint_record(
            endpoint=reference_endpoint,
            source=source,
            context=context,
            pins=pins,
            status=CompileStatus.INVALID,
            detail=detail,
            error_class=(
                "trusted_reference_repr_invalid"
                if reference_elaborated
                else "trusted_reference_elaboration_invalid"
            ),
        )

    candidate_records: list[EndpointCacheRecord] = []
    for endpoint, elaborated in zip(
        endpoints[1 : 1 + len(candidates)], candidate_elaborated, strict=True
    ):
        sidecar = sidecars.get(endpoint.endpoint_id)
        if sidecar is not None:
            if not elaborated:
                raise MatchedPilotLeanAuditError(
                    "candidate has a REPR sidecar without elaboration evidence"
                )
            candidate_records.append(
                _valid_candidate_record_or_repr_failure(
                    endpoint=endpoint,
                    source=source,
                    context=context,
                    pins=pins,
                    sidecar=sidecar,
                )
            )
        else:
            candidate_records.append(
                _endpoint_record(
                    endpoint=endpoint,
                    source=source,
                    context=context,
                    pins=pins,
                    status=CompileStatus.INVALID,
                    detail=failures.get(endpoint.endpoint_id, "candidate lacks REPR or failure"),
                    error_class=(
                        "candidate_repr_invalid" if elaborated else "candidate_elaboration_invalid"
                    ),
                )
            )
    raw_path = Path(rendered.raw_response_path) if rendered.raw_response_path else None
    terminal = SourceAuditTerminal(
        schema_version=TERMINAL_SCHEMA_VERSION,
        run_id=run_id,
        source_id=source.source_id,
        source_ordinal=source_ordinal,
        source_class=source_class,
        source_context_id=source.compile_context.source_context_id,
        render_compile_context_id=context.compile_context_id,
        reference_elaboration_method=reference_input.method,
        reference_elaboration_sha256=sha256_hex(reference_input.carrier.encode("utf-8")),
        reference_elaborated=reference_elaborated,
        candidate_ids=tuple(item.candidate_id for item in candidates),
        candidate_elaborated=candidate_elaborated,
        reference=reference,
        candidates=tuple(candidate_records),
        request_hash=rendered.request_hash,
        request_status=last_result.status.value,
        elapsed_ms=sum(item.elapsed_ms for item in backend.results[result_count_before:]),
        raw_response_path=str(raw_path) if raw_path is not None else None,
        raw_response_sha256=(
            hash_file(raw_path) if raw_path is not None and raw_path.is_file() else None
        ),
        infrastructure_attempts=infrastructure_attempts,
        backend_method_version=METHOD_VERSION,
        peak_rss_bytes=_rss_high_water_bytes(),
    )
    return terminal, lean_requests


def _load_inputs(
    config: MatchedPilotLeanAuditConfig,
) -> tuple[
    tuple[SourceRecord, ...],
    tuple[CandidateRecord, ...],
    tuple[str, ...],
    dict[str, ReferenceElaborationInput],
]:
    sources = _read_models(config.input_bundle.local_root / "sources.jsonl", SourceRecord)
    candidates = _read_models(config.output_bundle.local_root / "candidates.jsonl", CandidateRecord)
    source_manifest = _json_object(config.input_bundle.local_root / "source_manifest.json")
    source_classes = _source_classes(sources, source_manifest)
    reference_inputs, _ = _reference_elaboration_inputs(
        sources,
        source_classes,
        source_manifest,
        named_catalog_path=config.mathlib_named_reference_catalog_path,
        named_catalog_sha256=config.mathlib_named_reference_catalog_sha256,
        explicit_theorem_ids=frozenset(config.explicit_reference_theorem_ids),
        syntax_migrations=config.reference_syntax_migrations,
    )
    return sources, candidates, source_classes, reference_inputs


def _compact(
    *,
    repo_root: Path,
    config_path: Path,
    config: MatchedPilotLeanAuditConfig,
    run_id: str,
    run_root: Path,
    sources: Sequence[SourceRecord],
    candidates: Sequence[CandidateRecord],
    source_classes: Sequence[str],
    pins: RuntimePins,
) -> dict[str, Any] | None:
    terminal_paths = [_terminal_path(run_root, item) for item in sources]
    if not all(path.is_file() for path in terminal_paths):
        return None
    terminals = [_read_terminal(path) for path in terminal_paths]
    if [item.source_id for item in terminals] != [item.source_id for item in sources]:
        raise MatchedPilotLeanAuditError("source terminal order/identity drifted")
    candidate_rows = []
    for terminal in terminals:
        for candidate, elaborated in zip(
            terminal.candidates, terminal.candidate_elaborated, strict=True
        ):
            candidate_rows.append(
                {
                    "source_id": terminal.source_id,
                    "source_class": terminal.source_class,
                    "source_context_id": terminal.source_context_id,
                    "render_compile_context_id": terminal.render_compile_context_id,
                    "elaboration_status": (
                        CompileStatus.VALID.value if elaborated else CompileStatus.INVALID.value
                    ),
                    **candidate.model_dump(mode="json"),
                }
            )
    if [item["candidate_id"] for item in candidate_rows] != [
        item.candidate_id for item in candidates
    ]:
        raise MatchedPilotLeanAuditError("compacted candidate order/identity drifted")
    source_rows: list[object] = [item.model_dump(mode="json") for item in terminals]
    candidate_output_rows: list[object] = list(candidate_rows)
    source_output = run_root / "source_compilation.jsonl"
    candidate_output = run_root / "candidate_compilation.jsonl"
    write_jsonl(source_output, source_rows)
    write_jsonl(candidate_output, candidate_output_rows)

    valid_candidates = sum(
        item["elaboration_status"] == CompileStatus.VALID.value for item in candidate_rows
    )
    invalid_candidates = len(candidate_rows) - valid_candidates
    representation_valid_candidates = sum(
        item["status"] == CompileStatus.VALID.value for item in candidate_rows
    )
    valid_references = sum(item.reference_elaborated for item in terminals)
    representation_valid_references = sum(
        item.reference.status == CompileStatus.VALID for item in terminals
    )
    sources_with_valid = sum(any(terminal.candidate_elaborated) for terminal in terminals)
    infrastructure_attempts = sum(item.infrastructure_attempts for item in terminals)
    total_attempts = len(terminals) + infrastructure_attempts
    infrastructure_fraction = infrastructure_attempts / total_attempts if total_attempts else 0.0
    valid_signature_count = len(
        {
            candidate.signature_sha256
            for candidate, row in zip(candidates, candidate_rows, strict=True)
            if row["elaboration_status"] == CompileStatus.VALID.value
        }
    )
    class_histogram: dict[str, dict[str, int]] = {}
    class_representation_histogram: dict[str, dict[str, int]] = {}
    for source_class in _SOURCE_CLASS_ORDER:
        rows = [item for item in candidate_rows if item["source_class"] == source_class]
        class_histogram[source_class] = dict(
            sorted(Counter(cast(str, item["elaboration_status"]) for item in rows).items())
        )
        class_representation_histogram[source_class] = dict(
            sorted(Counter(cast(str, item["status"]) for item in rows).items())
        )
    context_histogram: dict[str, dict[str, int]] = {}
    context_representation_histogram: dict[str, dict[str, int]] = {}
    for context_id in sorted({item.render_compile_context_id for item in terminals}):
        rows = [item for item in candidate_rows if item["render_compile_context_id"] == context_id]
        context_histogram[context_id] = dict(
            sorted(Counter(cast(str, item["elaboration_status"]) for item in rows).items())
        )
        context_representation_histogram[context_id] = dict(
            sorted(Counter(cast(str, item["status"]) for item in rows).items())
        )
    representation_failure_histogram = Counter(
        cast(str, item.get("error_class"))
        for item in candidate_rows
        if item["status"] != CompileStatus.VALID.value
    )
    elaboration_failure_histogram = Counter(
        cast(str, item.get("error_class"))
        for item in candidate_rows
        if item["elaboration_status"] != CompileStatus.VALID.value
    )
    reference_representation_failure_histogram = Counter(
        item.reference.error_class
        for item in terminals
        if item.reference.status != CompileStatus.VALID
    )
    thresholds = config.thresholds
    gate_checks = {
        "every_trusted_reference_elaborates": valid_references == thresholds.expected_sources,
        "minimum_valid_candidates": valid_candidates >= thresholds.minimum_valid_candidates,
        "minimum_40_percent_of_admitted": (
            valid_candidates / thresholds.expected_candidates
            >= thresholds.minimum_valid_candidate_fraction_of_admitted
        ),
        "minimum_25_percent_of_all_requests": (
            valid_candidates / thresholds.expected_requests
            >= thresholds.minimum_valid_candidate_fraction_of_requests
        ),
        "minimum_sources_with_valid_candidate": (
            sources_with_valid >= thresholds.minimum_sources_with_valid_candidate
        ),
        "infrastructure_failures_below_2_percent": (
            infrastructure_fraction < thresholds.maximum_infrastructure_failure_fraction
        ),
    }
    journal_hashes = {
        path.name: hash_file(path) for path in sorted((run_root / "journals").glob("*.jsonl"))
    }
    terminal_hashes = {
        path.name: hash_file(path) for path in sorted((run_root / "terminals").glob("*.json"))
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "repo_git_commit": _git_head(repo_root),
        "config_path": str(config_path),
        "config_sha256": hash_file(config_path),
        "module_sha256": hash_file(Path(__file__).resolve()),
        "preflight_sha256": hash_file(run_root / "preflight.json"),
        "runtime_pins": pins.to_dict(),
        "input_revision": config.input_bundle.revision,
        "output_revision": config.output_bundle.revision,
        "counts": {
            "sources": len(sources),
            "candidates": len(candidates),
            "valid_references": valid_references,
            "representation_valid_references": representation_valid_references,
            "valid_candidates": valid_candidates,
            "invalid_candidates": invalid_candidates,
            "representation_valid_candidates": representation_valid_candidates,
            "representation_invalid_candidates": len(candidate_rows)
            - representation_valid_candidates,
            "valid_unique_signatures": valid_signature_count,
            "sources_with_valid_candidate": sources_with_valid,
            "lean_requests_total": total_attempts,
            "infrastructure_attempts": infrastructure_attempts,
        },
        "rates": {
            "valid_fraction_of_admitted": valid_candidates / len(candidates),
            "valid_fraction_of_all_requests": valid_candidates / thresholds.expected_requests,
            "infrastructure_attempt_fraction": infrastructure_fraction,
        },
        "gate_checks": gate_checks,
        "gate_passed": all(gate_checks.values()),
        "source_class_counts": dict(sorted(Counter(source_classes).items())),
        "reference_elaboration_method_counts": dict(
            sorted(Counter(item.reference_elaboration_method for item in terminals).items())
        ),
        "class_candidate_elaboration_status_histogram": class_histogram,
        "class_candidate_representation_status_histogram": class_representation_histogram,
        "context_candidate_elaboration_status_histogram": context_histogram,
        "context_candidate_representation_status_histogram": context_representation_histogram,
        "candidate_elaboration_failure_histogram": dict(
            sorted(elaboration_failure_histogram.items())
        ),
        "candidate_representation_failure_histogram": dict(
            sorted(representation_failure_histogram.items())
        ),
        "reference_representation_failure_histogram": dict(
            sorted(reference_representation_failure_histogram.items())
        ),
        "extractor_histogram": _json_object(run_root / "preflight.json")["extractor_histogram"],
        "performance": {
            "total_elapsed_ms": sum(item.elapsed_ms for item in terminals),
            "maximum_source_elapsed_ms": max(item.elapsed_ms for item in terminals),
            "peak_rss_bytes": max(item.peak_rss_bytes for item in terminals),
        },
        "artifacts": {
            "source_compilation.jsonl": hash_file(source_output),
            "candidate_compilation.jsonl": hash_file(candidate_output),
            "terminal_ledger_sha256": hash_canonical(terminal_hashes),
            "journal_ledger_sha256": hash_canonical(journal_hashes),
        },
    }
    write_json(run_root / "manifest.json", manifest)
    return manifest


def run_audit(
    *, repo_root: Path, config_path: Path, max_sources: int | None = None
) -> dict[str, Any]:
    """Run pending context-grouped sources and compact only after all 500 terminals exist."""

    preflight, run_root = prepare_preflight(repo_root=repo_root, config_path=config_path)
    config, _ = load_config(config_path)
    run_id = cast(str, preflight["run_id"])
    sources, candidates, source_classes, reference_inputs = _load_inputs(config)
    helper_path = repo_root / config.helper_path
    pins = verify_runtime_pins(repo_root, helper_path=helper_path)
    helper_body = _helper_body(helper_path, pins)
    candidates_by_source: dict[str, list[CandidateRecord]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_source[candidate.source_id].append(candidate)

    indexed = list(enumerate(sources))
    context_order: list[str] = []
    by_context: dict[str, list[tuple[int, SourceRecord]]] = defaultdict(list)
    for item in indexed:
        context_id = item[1].compile_context.render_compile_context_id
        if context_id not in by_context:
            context_order.append(context_id)
        by_context[context_id].append(item)
    execution_order = [item for context_id in context_order for item in by_context[context_id]]
    pending = [item for item in execution_order if not _terminal_path(run_root, item[1]).is_file()]
    if max_sources is not None:
        if max_sources < 1:
            raise MatchedPilotLeanAuditError("max_sources must be positive")
        pending = pending[:max_sources]

    lean_requests = 0
    cache_hits = len(sources) - len(
        [item for item in sources if not _terminal_path(run_root, item).is_file()]
    )
    if pending:
        reservation = claim_resources(
            task="SFT2B",
            lean_workers=1,
            lean_rss_gib=config.claimed_lean_rss_gib,
            gpu=False,
            pid=os.getpid(),
            owner_session=config.owner_session,
            worktree=repo_root,
        )
        backend: _CapturingBackend | None = None
        current_context_id: str | None = None
        try:
            for source_ordinal, source in pending:
                context = _compile_context(source, helper_body=helper_body)
                if current_context_id != context.compile_context_id:
                    if backend is not None:
                        backend.close()
                    backend = _CapturingBackend(
                        make_mathlib_backend(
                            compile_context=context,
                            project_dir=config.mathlib_project_path,
                            raw_response_dir=run_root / "lean" / "raw_responses",
                        )
                    )
                    current_context_id = context.compile_context_id
                assert backend is not None
                terminal, requests = _execute_source(
                    backend=backend,
                    source=source,
                    source_ordinal=source_ordinal,
                    source_class=source_classes[source_ordinal],
                    reference_input=reference_inputs[source.source_id],
                    candidates=tuple(candidates_by_source[source.source_id]),
                    context=context,
                    pins=pins,
                    config=config,
                    run_id=run_id,
                    run_root=run_root,
                )
                lean_requests += requests
                if terminal is None:
                    raise MatchedPilotLeanAuditError(
                        f"infrastructure attempts exhausted for source {source.source_id}"
                    )
                terminal_path = _terminal_path(run_root, source)
                immutable_write(
                    terminal_path,
                    canonical_json_bytes(terminal.model_dump(mode="json")) + b"\n",
                )
                _journal(run_root, run_id=run_id, source=source).append(
                    stage="render_completed",
                    terminal_key=f"matched-pilot-render:{source.source_id}",
                    artifact_path=terminal_path,
                )
                if not terminal.reference_elaborated:
                    raise MatchedPilotLeanAuditError(
                        f"trusted reference failed deterministically: {source.source_id}"
                    )
        finally:
            if backend is not None:
                backend.close()
            released = release_resources(task="SFT2B")
            if released != reservation:
                raise MatchedPilotLeanAuditError("released Lean reservation differs from claim")

    # Repair the narrow crash window where a terminal was durable before its journal append.
    for source in sources:
        terminal_path = _terminal_path(run_root, source)
        if terminal_path.is_file():
            _read_terminal(terminal_path)
            _journal(run_root, run_id=run_id, source=source).append(
                stage="render_cache_hit",
                terminal_key=f"matched-pilot-render:{source.source_id}",
                artifact_path=terminal_path,
            )

    manifest = _compact(
        repo_root=repo_root,
        config_path=config_path,
        config=config,
        run_id=run_id,
        run_root=run_root,
        sources=sources,
        candidates=candidates,
        source_classes=source_classes,
        pins=pins,
    )
    completed = sum(_terminal_path(run_root, item).is_file() for item in sources)
    return {
        "run_id": run_id,
        "run_root": str(run_root),
        "completed_sources": completed,
        "remaining_sources": len(sources) - completed,
        "lean_requests_this_run": lean_requests,
        "preexisting_terminal_count": cache_hits,
        "manifest": manifest,
    }


def verify_completed_audit(*, repo_root: Path, config_path: Path) -> dict[str, Any]:
    preflight, run_root = prepare_preflight(repo_root=repo_root, config_path=config_path)
    config, _ = load_config(config_path)
    sources, _, _, _ = _load_inputs(config)
    missing = [item.source_id for item in sources if not _terminal_path(run_root, item).is_file()]
    if missing:
        raise MatchedPilotLeanAuditError(
            f"completed-audit verification found {len(missing)} missing source terminals"
        )
    if preflight.get("run_id") is None:
        raise MatchedPilotLeanAuditError("completed-audit preflight lacks its run identity")
    result = run_audit(repo_root=repo_root, config_path=config_path, max_sources=None)
    if result["remaining_sources"] != 0 or result["lean_requests_this_run"] != 0:
        raise MatchedPilotLeanAuditError("completed audit verification made a new Lean request")
    manifest = cast(dict[str, Any] | None, result["manifest"])
    if manifest is None:
        raise MatchedPilotLeanAuditError("completed audit lacks its manifest")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--force-replay", action="store_true")
    run = subparsers.add_parser("run")
    run.add_argument("--max-sources", type=int)
    subparsers.add_parser("verify")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    if args.command == "preflight":
        preflight, root = prepare_preflight(
            repo_root=repo_root,
            config_path=config_path.resolve(),
            force_replay=bool(args.force_replay),
        )
        output = {"run_root": str(root), "preflight": preflight}
    elif args.command == "run":
        output = run_audit(
            repo_root=repo_root,
            config_path=config_path.resolve(),
            max_sources=args.max_sources,
        )
    else:
        output = verify_completed_audit(repo_root=repo_root, config_path=config_path.resolve())
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
