"""Bounded, durable executor for SFT1 Wave 1 implementation readiness.

This module exposes only the user-authorized readiness surfaces: the four
positive success/rejection fixtures and N31 proposal resolution under the
frozen empty admission set.  It has no gate, row, production, or scale API.
Every Lean request reuses a caller-owned persistent backend.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.host_resources import (
    Reservation,
    claim_resources,
    list_reservations,
    release_resources,
)
from leanfaith.lean.cache import EvidenceCache
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import RetryPolicy, ServerMode, run_with_retries
from leanfaith.representations.goal_v1 import (
    ClosedExprInput,
    ClosedExprProvenance,
    ClosedExprRecord,
    ClosedExprSidecar,
    ClosedExprSourceMaterial,
    CompileContext,
    RendererImplementationIdentity,
)
from leanfaith.sft1.wave1_live_readiness import (
    DEFAULT_N31_PROPOSAL_RECEIPT_PATH,
    DEFAULT_POSITIVE_CHECKPOINT_RECEIPT_PATH,
    EXPECTED_OPERATION_IDS,
    EXPECTED_PROJECT_IDS,
    FixtureProjectContext,
    FixtureTemplate,
    LoadedWave1LiveReadiness,
    N31ResolutionProjectProposal,
    N31ResolutionProposalBundle,
    PositiveCheckpointCaseReceipt,
    PositiveLiveCheckpointReceipt,
    PositiveResolvedAnchorInput,
    assemble_runtime_preamble,
    build_fixture_compile_context,
    compute_n31_proposal_bank_template_hash,
    compute_positive_resolved_anchor_hash,
    load_wave1_live_readiness,
    n31_phase_receipt_id,
)
from leanfaith.sft1.wave1_meta_adapter import (
    RenderedWave1Pair,
    Wave1CentralCacheAdapter,
    bind_wave1_central_cache_key,
    make_wave1_audit_evidence,
    persist_wave1_sidecars,
    render_wave1_pair,
    runtime_endpoints_from_pair,
)
from leanfaith.sft1.wave1_readiness import Wave1CacheKey, compute_wave1_cache_key_hash
from leanfaith.sft1.wave1_runtime import (
    P01_CORRECTED_ENVELOPE_HASH,
    P01_POLICY_SEMANTIC_HASH,
    DedupCandidate,
    P01CapObservation,
    P01NameOnlyDelta,
    RuntimeChain,
    RuntimeEdge,
    RuntimeEndpoint,
    RuntimeRetentionBatch,
    RuntimeRetentionJournalRecord,
    RuntimeRetentionScopeManifest,
    TypedCertificateReceipt,
    Wave1RuntimeError,
    assert_post_orientation_unique,
    canonical_unordered_pair_key,
    compute_p01_binder_aware_fingerprint,
    compute_p01_selected_site_lineage_hash,
    deduplicate_unordered_pairs,
    load_and_validate_p01_runtime_binding,
    make_runtime_chain,
    p01_outer_binder_site_path,
    validate_p01_caps,
    validate_retention_batch,
    validate_runtime_chain,
)

TASK_RECEIPT_MARKER = "LFSFT1WAVE1JSON "
AUTHORIZED_POSITIVE_OPERATION_IDS = EXPECTED_OPERATION_IDS[:4]
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]+")
_DECIMAL_U64 = re.compile(r"^(?:0|[1-9][0-9]*)$")
_SUBEXPR_PATH = re.compile(r"^/(?:[0-3](?:/[0-3])*)?$")
_FORBIDDEN_DIRECT_BODY = re.compile(
    r"(?m)^\s*(?:theorem|lemma|axiom|opaque|example)\b|\bsorry\b|"
    r"LeanFaith\.GoalV1\.emitClosedProp"
)


class Wave1LiveRunnerError(RuntimeError):
    """Raised when a bounded readiness execution cannot be certified."""


def _json_no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise Wave1LiveRunnerError(f"duplicate task-receipt JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> float:
    raise Wave1LiveRunnerError(f"non-finite task-receipt JSON value: {value}")


def extract_task_receipt(
    messages: Sequence[Mapping[str, object]],
    *,
    receipt_id: str,
    receipt_kind: str,
) -> dict[str, object]:
    """Extract exactly one requested task marker from a Lean result."""

    selected: list[dict[str, object]] = []
    for message in messages:
        for line in str(message.get("data", "")).splitlines():
            marker = line.find(TASK_RECEIPT_MARKER)
            if marker < 0:
                continue
            encoded = line[marker + len(TASK_RECEIPT_MARKER) :]
            try:
                raw = json.loads(
                    encoded,
                    object_pairs_hook=_json_no_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
            except (json.JSONDecodeError, TypeError) as exc:
                raise Wave1LiveRunnerError("malformed task-owned Lean receipt") from exc
            if not isinstance(raw, dict):
                raise Wave1LiveRunnerError("task-owned Lean receipt is not a JSON object")
            if raw.get("receipt_id") == receipt_id and raw.get("receipt_kind") == receipt_kind:
                selected.append(cast(dict[str, object], raw))
    if len(selected) != 1:
        raise Wave1LiveRunnerError(
            f"expected one {receipt_kind} receipt {receipt_id}, found {len(selected)}"
        )
    return selected[0]


def _require_safe_id(value: str, field_name: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise Wave1LiveRunnerError(f"unsafe {field_name}: {value!r}")
    return value


def _fixture_for(
    loaded: LoadedWave1LiveReadiness,
    operation_id: str,
    fixture_kind: Literal["success", "adversarial_rejection"],
) -> FixtureTemplate:
    matches = tuple(
        item
        for item in loaded.fixtures.templates
        if item.operation_id == operation_id and item.fixture_kind == fixture_kind
    )
    if len(matches) != 1:
        raise Wave1LiveRunnerError("exact readiness fixture lookup failed")
    return matches[0]


def _project_for(loaded: LoadedWave1LiveReadiness, project_id: str) -> FixtureProjectContext:
    matches = tuple(
        item for item in loaded.fixtures.project_contexts if item.project_id == project_id
    )
    if len(matches) != 1:
        raise Wave1LiveRunnerError("exact readiness project lookup failed")
    return matches[0]


_POSITIVE_BUNDLE_EXPR = {
    "P01_ALPHA_RENAME_SINGLE_V1": ".p01AlphaRenameSingle",
    "P15_SWAP_IFF_SIDES_V1": ".p15SwapIffSides",
    "P18_SYMMETRIZE_EQUALITY_V1": ".p18SymmetrizeEquality",
    "P21_BETA_REDUCE_V1": ".p21BetaReduce",
}

_POSITIVE_OPERATION_CONSTRUCTOR = {
    "P01_ALPHA_RENAME_SINGLE_V1": "LeanFaith.SFT1.Wave1.PrimaryOperation.p01AlphaRenameSingle",
    "P15_SWAP_IFF_SIDES_V1": "LeanFaith.SFT1.Wave1.PrimaryOperation.p15SwapIffSides",
    "P18_SYMMETRIZE_EQUALITY_V1": "LeanFaith.SFT1.Wave1.PrimaryOperation.p18SymmetrizeEquality",
    "P21_BETA_REDUCE_V1": "LeanFaith.SFT1.Wave1.PrimaryOperation.p21BetaReduce",
}


def _selector_expr(template: FixtureTemplate) -> str:
    selector = template.selector
    if selector.kind == "outer_binder":
        if selector.binder_index is None:
            raise Wave1LiveRunnerError("outer-binder fixture lost its index")
        return f"(.outerBinder {selector.binder_index})"
    if selector.kind == "outer_target":
        return ".outerTarget"
    if selector.kind == "exact_expr_site":
        if selector.site_path != "/":
            raise Wave1LiveRunnerError("only the frozen root beta site is executable")
        return "(.subexpr .root)"
    raise Wave1LiveRunnerError("N31 proposal selector is not a positive selector")


def _source_elaboration_line(reference_term: str) -> str:
    if "\n" in reference_term or "\r" in reference_term:
        raise Wave1LiveRunnerError("readiness proposition must remain one frozen line")
    return (
        "  let sourceExpr ← "
        "LeanFaith.SFT1.RepresentationGate.elaborateReferenceProp\n"
        f"    (← `(term| {reference_term}))"
    )


def build_positive_symbol_resolution_session(
    *, project_id: str, operation_id: str, receipt_id: str
) -> str:
    """Build an independent typed symbol-resolution request for one bundle."""

    if project_id not in EXPECTED_PROJECT_IDS or operation_id not in (
        AUTHORIZED_POSITIVE_OPERATION_IDS
    ):
        raise Wave1LiveRunnerError("symbol resolution is outside authorized Wave 1 scope")
    _require_safe_id(receipt_id, "receipt ID")
    bundle = _POSITIVE_BUNDLE_EXPR[operation_id]
    constructor = _POSITIVE_OPERATION_CONSTRUCTOR[operation_id]
    return f"""run_meta do
  let bundle : LeanFaith.SFT1.Wave1Runtime.PositiveBundle := {bundle}
  let identity : LeanFaith.SFT1.Wave1Runtime.PositiveBundleIdentity := bundle.identity
  let _dispatchFn : LeanFaith.SFT1.Wave1.PrimaryOperation →
      LeanFaith.SFT1.Wave1.Selector → LeanFaith.SFT1.Wave1.DispatchContext →
      Lean.Expr → Lean.MetaM LeanFaith.SFT1.Wave1.ApplyResult :=
    LeanFaith.SFT1.Wave1.dispatchAt
  let _discoverFn : LeanFaith.SFT1.Wave1.PrimaryOperation →
      LeanFaith.SFT1.Wave1.DispatchContext → Lean.Expr →
      Lean.MetaM (Array LeanFaith.SFT1.Wave1.Candidate) :=
    LeanFaith.SFT1.Wave1.discover
  let _checkerFn : LeanFaith.SFT1.Wave1.DispatchContext → Lean.Expr → Lean.Expr →
      LeanFaith.SFT1.Wave1.Certificate → Lean.MetaM LeanFaith.SFT1.Wave1.ReplayResult :=
    LeanFaith.SFT1.Wave1.replayCertificate
  let environment ← Lean.getEnv
  let dispatchFound := environment.contains ``LeanFaith.SFT1.Wave1.dispatchAt
  let discoverFound := environment.contains ``LeanFaith.SFT1.Wave1.discover
  let checkerFound := environment.contains ``LeanFaith.SFT1.Wave1.replayCertificate
  let operationMatches := identity.operationId == {_lean_string(operation_id)} &&
    identity.operationConstructor == {_lean_string(constructor)}
  let symbolStringsMatch :=
    identity.dispatchSymbol == "LeanFaith.SFT1.Wave1.dispatchAt" &&
    identity.discoverSymbol == "LeanFaith.SFT1.Wave1.discover" &&
    identity.checkerSymbol == "LeanFaith.SFT1.Wave1.replayCertificate"
  let passed := dispatchFound && discoverFound && checkerFound && operationMatches &&
    symbolStringsMatch && identity.frozenEngineVersion == LeanFaith.SFT1.Wave1.sourceVersion
  unless passed do
    throwError "sft1_wave1_positive_symbol_resolution_failed"
  let payload := Lean.Json.mkObj [
    ("schema_version", toJson 1),
    ("receipt_kind", Lean.Json.str "positive_symbol_resolution"),
    ("receipt_id", Lean.Json.str {_lean_string(receipt_id)}),
    ("source_version", Lean.Json.str "sft1_wave1_runtime_readiness_v0_3_6"),
    ("project_id", Lean.Json.str {_lean_string(project_id)}),
    ("operation_id", Lean.Json.str identity.operationId),
    ("operation_constructor", Lean.Json.str identity.operationConstructor),
    ("dispatch_symbol", Lean.Json.str identity.dispatchSymbol),
    ("discover_symbol", Lean.Json.str identity.discoverSymbol),
    ("checker_symbol", Lean.Json.str identity.checkerSymbol),
    ("frozen_engine_version", Lean.Json.str identity.frozenEngineVersion),
    ("dispatch_declaration_found", Lean.Json.bool dispatchFound),
    ("discover_declaration_found", Lean.Json.bool discoverFound),
    ("checker_declaration_found", Lean.Json.bool checkerFound),
    ("typed_function_signatures_assigned", Lean.Json.bool true),
    ("bundle_identity_matches", Lean.Json.bool (operationMatches && symbolStringsMatch)),
    ("passed", Lean.Json.bool passed),
    ("candidate_constructed", Lean.Json.bool false),
    ("row_or_gate_emitted", Lean.Json.bool false)
  ]
  Lean.logInfo <| {_lean_string(TASK_RECEIPT_MARKER)} ++ payload.compress"""


def build_positive_success_session(
    loaded: LoadedWave1LiveReadiness,
    *,
    project_id: str,
    operation_id: str,
    receipt_id: str,
    render_scope_id: str,
) -> tuple[str, tuple[ClosedExprInput, ClosedExprInput]]:
    """Build one exact dispatch/replay/direct-Expr rendering request."""

    if project_id not in EXPECTED_PROJECT_IDS or operation_id not in (
        AUTHORIZED_POSITIVE_OPERATION_IDS
    ):
        raise Wave1LiveRunnerError("positive session is outside authorized Wave 1 scope")
    _require_safe_id(receipt_id, "receipt ID")
    _require_safe_id(render_scope_id, "render scope ID")
    fixture = _fixture_for(loaded, operation_id, "success")
    reference_id = f"{receipt_id}.reference"
    candidate_id = f"{receipt_id}.candidate"
    bundle = _POSITIVE_BUNDLE_EXPR[operation_id]
    selector = _selector_expr(fixture)
    body = f"""run_meta do
{_source_elaboration_line(fixture.reference_term)}
  let selected ← LeanFaith.SFT1.Wave1Runtime.emitPositiveSuccessReceipt
    "{receipt_id}" {bundle} {selector} sourceExpr
  LeanFaith.GoalV1.emitClosedProp
    "{reference_id}" "{render_scope_id}" "term_elaborated_proposition" sourceExpr
  LeanFaith.GoalV1.emitClosedProp
    "{candidate_id}" "{render_scope_id}" "sft1_transformed_expr" selected.candidate"""
    if body.count("LeanFaith.GoalV1.emitClosedProp") != 2:
        raise AssertionError("positive readiness body must unroll exactly two emitters")
    inputs = (
        ClosedExprInput(
            endpoint_id=reference_id,
            endpoint_role="reference",
            expr_origin="term_elaborated_proposition",
            source_material=ClosedExprSourceMaterial(
                kind="proposition_text", proposition_text=fixture.reference_term
            ),
        ),
        ClosedExprInput(
            endpoint_id=candidate_id,
            endpoint_role="candidate",
            expr_origin="sft1_transformed_expr",
            source_material=ClosedExprSourceMaterial(
                kind="constructed_expr_no_source_text",
                absence_reason=(
                    "candidate is the deterministic typed result of the hash-bound Wave 1 "
                    "dispatch and certificate replay"
                ),
            ),
        ),
    )
    return body, inputs


def build_positive_rejection_session(
    loaded: LoadedWave1LiveReadiness,
    *,
    project_id: str,
    operation_id: str,
    receipt_id: str,
) -> str:
    """Build a typed-not-applicable request with no candidate endpoint."""

    if project_id not in EXPECTED_PROJECT_IDS or operation_id not in (
        AUTHORIZED_POSITIVE_OPERATION_IDS
    ):
        raise Wave1LiveRunnerError("rejection session is outside authorized Wave 1 scope")
    _require_safe_id(receipt_id, "receipt ID")
    fixture = _fixture_for(loaded, operation_id, "adversarial_rejection")
    bundle = _POSITIVE_BUNDLE_EXPR[operation_id]
    selector = _selector_expr(fixture)
    body = f"""run_meta do
{_source_elaboration_line(fixture.reference_term)}
  LeanFaith.SFT1.Wave1Runtime.emitPositiveRejectionReceipt
    "{receipt_id}" {bundle} {selector} sourceExpr"""
    if _FORBIDDEN_DIRECT_BODY.search(body) is not None:
        raise Wave1LiveRunnerError("direct rejection request contains a forbidden endpoint token")
    return body


def _lean_option_value(value: str | int | float | bool) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def build_direct_meta_command(compile_context: CompileContext, session_body: str) -> str:
    """Build a non-rendering Meta request without copying the REPR helper."""

    if _FORBIDDEN_DIRECT_BODY.search(session_body) is not None:
        raise Wave1LiveRunnerError("direct Meta body contains a forbidden endpoint token")
    if len(re.findall(r"(?m)^\s*run_meta\s+do\b", session_body)) != 1:
        raise Wave1LiveRunnerError("direct Meta body must contain exactly one run_meta command")
    import_lines = [
        line.strip() for line in compile_context.import_header.splitlines() if line.strip()
    ]
    lines = ["import Lean", *(line for line in import_lines if line != "import Lean")]
    if compile_context.command_preamble.strip():
        lines.append(compile_context.command_preamble.rstrip())
    lines.extend(
        f"set_option {name} {_lean_option_value(value)}"
        for name, value in sorted(compile_context.options.items())
    )
    if compile_context.open_context:
        lines.append("open " + " ".join(compile_context.open_context))
    if compile_context.scoped_context:
        lines.append("open scoped " + " ".join(compile_context.scoped_context))
    lines.extend(f"namespace {name}" for name in compile_context.namespace_context)
    lines.append(session_body.rstrip())
    lines.extend(f"end {name}" for name in reversed(compile_context.namespace_context))
    return "\n".join(lines) + "\n"


def _lean_string(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise Wave1LiveRunnerError("Lean string input must remain one line")
    return json.dumps(value, ensure_ascii=True)


def _lean_name(value: str) -> str:
    parts = value.split(".")
    if not parts or any(not part or _SAFE_ID.fullmatch(part) is None for part in parts):
        raise Wave1LiveRunnerError(f"unsafe Lean declaration name: {value!r}")
    expression = f"Lean.Name.mkSimple {_lean_string(parts[0])}"
    for part in parts[1:]:
        expression = f"Lean.Name.str ({expression}) {_lean_string(part)}"
    return expression


def _generic_term_elaboration(binding: str, term: str) -> str:
    if "\n" in term or "\r" in term:
        raise Wave1LiveRunnerError("N31 elaboration term must remain one line")
    if _SAFE_ID.fullmatch(binding) is None:
        raise Wave1LiveRunnerError("unsafe N31 local binding")
    return f"""  let {binding} ← Lean.Elab.Term.TermElabM.run' do
    let expression ← Lean.Elab.Term.elabTerm (← `(term| {term})) none
    Lean.Elab.Term.synthesizeSyntheticMVarsNoPostponing
    Lean.Meta.instantiateMVars expression"""


def _n31_path(indices: Sequence[int]) -> str:
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in indices):
        raise Wave1LiveRunnerError("N31 application path is not a natural-number path")
    return "{ argumentIndices := #[" + ", ".join(str(item) for item in indices) + "] }"


def _n31_bank_expression(
    loaded: LoadedWave1LiveReadiness,
    *,
    project_id: str,
    resolved_lean_hash: str,
    resolution_receipt_hash: str,
) -> str:
    """Construct only the strict loader's exact nonactivated N31 proposal bank."""

    if project_id not in EXPECTED_PROJECT_IDS:
        raise Wave1LiveRunnerError("N31 proposal project is outside Wave 1 scope")
    bank = loaded.config.n31_proposal_bank
    if project_id not in bank.project_ids:
        raise Wave1LiveRunnerError("N31 proposal bank does not bind the requested project")
    for value, field_name in (
        (resolved_lean_hash, "resolved Lean hash"),
        (resolution_receipt_hash, "resolution receipt hash"),
    ):
        if value and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise Wave1LiveRunnerError(f"N31 {field_name} is not lowercase SHA256")

    entries: list[str] = []
    for entry in bank.entries:
        exact_zero = _n31_path((entry.guard_exact_zero_argument_index,))
        guard_roles = ", ".join(str(item) for item in entry.guard_role_argument_indices)
        guard_instances = ", ".join(
            str(item) for item in entry.guard_instance_or_type_argument_indices
        )
        target_roles = ", ".join(str(item) for item in entry.target_role_argument_indices)
        target_instances = ", ".join(
            str(item) for item in entry.target_instance_or_type_argument_indices
        )
        entries.append(
            "\n".join(
                (
                    "{",
                    f"      entryId := {_lean_string(entry.entry_id)}",
                    f"      guardShapeId := {_lean_string(entry.guard_shape_id)}",
                    f"      guardHeadName := {_lean_name(entry.guard_head_name)}",
                    f"      guardArgumentCount := {entry.guard_argument_count}",
                    f"      guardRoleArgumentIndices := #[{guard_roles}]",
                    f"      guardInstanceArgumentIndices := #[{guard_instances}]",
                    "      guardFixedHeads := #[]",
                    "      guardNestedHeadConstraints := #[]",
                    "      guardLiteralConstraints := #[]",
                    "      guardExactExprConstraints := #[{ "
                    f"path := {exact_zero}, expected := zeroExpr "
                    "}]",
                    f"      targetHeadName := {_lean_name(entry.target_head_name)}",
                    f"      targetArgumentCount := {entry.target_argument_count}",
                    f"      targetRoleArgumentIndices := #[{target_roles}]",
                    f"      targetInstanceArgumentIndices := #[{target_instances}]",
                    "      targetFixedHeads := #[]",
                    "      targetNestedHeadConstraints := #[]",
                    "      targetLiteralConstraints := #[]",
                    "      targetExactExprConstraints := #[]",
                    "    }",
                )
            )
        )

    retained: list[str] = []
    for pattern in bank.retained_contradiction_patterns:
        role_paths = ", ".join(_n31_path(item) for item in pattern.role_paths)
        instance_paths = ", ".join(_n31_path(item) for item in pattern.instance_or_type_paths)
        exact_paths = ", ".join(
            f"{{ path := {_n31_path(item)}, expected := zeroExpr }}"
            for item in pattern.exact_zero_paths
        )
        retained.append(
            "\n".join(
                (
                    "{",
                    f"      shapeId := {_lean_string(pattern.shape_id)}",
                    f"      headName := {_lean_name(pattern.head_name)}",
                    f"      argumentCount := {pattern.argument_count}",
                    f"      rolePaths := #[{role_paths}]",
                    f"      instancePaths := #[{instance_paths}]",
                    "      nestedHeadConstraints := #[]",
                    "      literalConstraints := #[]",
                    f"      exactExprConstraints := #[{exact_paths}]",
                    "    }",
                )
            )
        )
    implications = ", ".join(
        "{ premiseShapeId := "
        f"{_lean_string(item.premise_shape_id)}, conclusionShapeId := "
        f"{_lean_string(item.conclusion_shape_id)} }}"
        for item in bank.implications
    )
    contradictions = ", ".join(
        "{ retainedShapeId := "
        f"{_lean_string(item.retained_shape_id)}, removedShapeId := "
        f"{_lean_string(item.removed_shape_id)} }}"
        for item in bank.contradictions
    )
    return """{{
    identity := {{
      projectId := {}
      bankId := {}
      resolvedLeanHash := {}
      resolutionReceiptHash := {}
    }}
    entries := #[{}]
    retainedContradictionPatterns := #[{}]
    implications := #[{}]
    contradictions := #[{}]
  }}""".format(
        _lean_string(project_id),
        _lean_string(bank.bank_id),
        _lean_string(resolved_lean_hash),
        _lean_string(resolution_receipt_hash),
        ",\n    ".join(entries),
        ",\n    ".join(retained),
        implications,
        contradictions,
    )


def build_n31_phase_one_session(
    loaded: LoadedWave1LiveReadiness, *, project_id: str, receipt_id: str
) -> str:
    """Build the no-candidate name/arity/type resolution proposal request."""

    _require_safe_id(receipt_id, "receipt ID")
    bank = loaded.config.n31_proposal_bank
    bank_expression = _n31_bank_expression(
        loaded,
        project_id=project_id,
        resolved_lean_hash="",
        resolution_receipt_hash="",
    )
    return f"""run_meta do
{_generic_term_elaboration("zeroExpr", bank.zero_term)}
{_source_elaboration_line(bank.retained_witness_term).replace("sourceExpr", "retainedProposition")}
  let proposalBank : LeanFaith.SFT1.Wave1.N31TargetBank := {bank_expression}
  let retainedWitnesses :
      Array LeanFaith.SFT1.Wave1Runtime.N31RetainedPatternWitness := #[{{
    shapeId := {_lean_string(bank.retained_contradiction_patterns[0].shape_id)}
    proposition := retainedProposition
  }}]
  LeanFaith.SFT1.Wave1Runtime.emitN31ProposalResolutionReceipt
    {_lean_string(receipt_id)} proposalBank retainedWitnesses"""


def build_n31_phase_two_session(
    loaded: LoadedWave1LiveReadiness,
    *,
    project_id: str,
    receipt_id: str,
    resolved_lean_hash: str,
    resolution_receipt_hash: str,
) -> str:
    """Build the second request proving the proposed bank remains unadmitted."""

    _require_safe_id(receipt_id, "receipt ID")
    bank = loaded.config.n31_proposal_bank
    fixture = _fixture_for(loaded, "N31_DROP_REQUIRED_GUARD_RUBRIC_V1", "success")
    bank_expression = _n31_bank_expression(
        loaded,
        project_id=project_id,
        resolved_lean_hash=resolved_lean_hash,
        resolution_receipt_hash=resolution_receipt_hash,
    )
    assignments = "\n".join(
        _generic_term_elaboration(f"reachabilityAssignment{index}", term)
        for index, term in enumerate(bank.reachability_assignment_terms)
    )
    assignment_array = ", ".join(
        f"reachabilityAssignment{index}" for index in range(len(bank.reachability_assignment_terms))
    )
    return f"""run_meta do
{_generic_term_elaboration("zeroExpr", bank.zero_term)}
{_source_elaboration_line(bank.retained_witness_term).replace("sourceExpr", "retainedProposition")}
{_source_elaboration_line(fixture.reference_term)}
{assignments}
  let proposalBank : LeanFaith.SFT1.Wave1.N31TargetBank := {bank_expression}
  let retainedWitnesses :
      Array LeanFaith.SFT1.Wave1Runtime.N31RetainedPatternWitness := #[{{
    shapeId := {_lean_string(bank.retained_contradiction_patterns[0].shape_id)}
    proposition := retainedProposition
  }}]
  let reachability : LeanFaith.SFT1.Wave1.N31ReachabilityEvidence := {{
    modeId := {_lean_string(bank.reachability_mode_id)}
    guardOrdinal := {bank.phase_two_selector_guard_ordinal}
    assignments := #[{assignment_array}]
  }}
  LeanFaith.SFT1.Wave1Runtime.emitN31FrozenNonActivationReceipt
    {_lean_string(receipt_id)} sourceExpr
    (.requiredGuard {bank.phase_two_selector_guard_ordinal} .root
      {_lean_string(bank.phase_two_selector_bank_entry_id)})
    proposalBank reachability {_lean_string(resolved_lean_hash)}
    {_lean_string(resolution_receipt_hash)} retainedWitnesses"""


@dataclass(slots=True)
class RetryingCapturingBackend:
    """Infrastructure-only retries while preserving the final raw result."""

    delegate: LeanBackend
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    last_result: LeanResult | None = None
    attempt_results: tuple[LeanResult, ...] = ()

    def run(self, request: LeanRequest) -> LeanResult:
        outcome = run_with_retries(self.delegate.run, request, self.retry_policy)
        self.last_result = outcome.result
        self.attempt_results = outcome.attempts
        return outcome.result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class PositiveSuccessExecution:
    project_id: str
    operation_id: str
    receipt_id: str
    compile_context: CompileContext
    pair: RenderedWave1Pair
    result: LeanResult
    task_receipt: dict[str, object]
    attempt_request_hashes: tuple[str, ...]
    persistent_session_id: str


def _mapping(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise Wave1LiveRunnerError(f"task receipt {field_name} is not an object")
    return cast(dict[str, object], value)


def _exact_keys(value: Mapping[str, object], expected: set[str], field_name: str) -> None:
    if set(value) != expected:
        raise Wave1LiveRunnerError(f"task receipt {field_name} field inventory drift")


def _natural(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Wave1LiveRunnerError(f"task receipt {field_name} is not a natural number")
    return value


def _decimal_u64(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DECIMAL_U64.fullmatch(value) is None:
        raise Wave1LiveRunnerError(f"task receipt {field_name} is not decimal UInt64 evidence")
    if int(value) >= 2**64:
        raise Wave1LiveRunnerError(f"task receipt {field_name} exceeds UInt64")
    return value


def _string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise Wave1LiveRunnerError(f"task receipt {field_name} is not a string list")
    return cast(list[str], value)


def _expected_positive_selector(operation_id: str) -> dict[str, object]:
    if operation_id == "P01_ALPHA_RENAME_SINGLE_V1":
        return {"kind": "outerBinder", "ordinal": 0}
    if operation_id in {"P15_SWAP_IFF_SIDES_V1", "P18_SYMMETRIZE_EQUALITY_V1"}:
        return {"kind": "outerTarget"}
    if operation_id == "P21_BETA_REDUCE_V1":
        return {"kind": "subexpr", "position": "/", "position_nat": "1"}
    raise Wave1LiveRunnerError("unknown positive operation receipt")


def _validate_positive_certificate(operation_id: str, certificate: Mapping[str, object]) -> str:
    if operation_id == "P01_ALPHA_RENAME_SINGLE_V1":
        _exact_keys(
            certificate,
            {
                "kind",
                "binder_ordinal",
                "binder_site",
                "binder_site_nat",
                "source_name",
                "candidate_name",
                "binder_info",
            },
            "certificate",
        )
        if certificate.get("kind") != "p01" or certificate.get("binder_ordinal") != 0:
            raise Wave1LiveRunnerError("P01 certificate constructor/ordinal drift")
        path_field, nat_field = "binder_site", "binder_site_nat"
        if (
            certificate.get("binder_info") != "default"
            or not isinstance(certificate.get("source_name"), str)
            or not isinstance(certificate.get("candidate_name"), str)
            or certificate.get("source_name") == certificate.get("candidate_name")
        ):
            raise Wave1LiveRunnerError("P01 certificate name/BinderInfo drift")
    elif operation_id in {"P15_SWAP_IFF_SIDES_V1", "P18_SYMMETRIZE_EQUALITY_V1"}:
        _exact_keys(certificate, {"kind", "target_site", "target_site_nat"}, "certificate")
        expected_kind = "p15" if operation_id.startswith("P15") else "p18"
        if certificate.get("kind") != expected_kind:
            raise Wave1LiveRunnerError("binary-rewrite certificate constructor drift")
        path_field, nat_field = "target_site", "target_site_nat"
    elif operation_id == "P21_BETA_REDUCE_V1":
        _exact_keys(certificate, {"kind", "redex_site", "redex_site_nat"}, "certificate")
        if certificate.get("kind") != "p21":
            raise Wave1LiveRunnerError("P21 certificate constructor drift")
        path_field, nat_field = "redex_site", "redex_site_nat"
    else:
        raise Wave1LiveRunnerError("unknown positive certificate operation")
    path = certificate.get(path_field)
    if not isinstance(path, str) or _SUBEXPR_PATH.fullmatch(path) is None:
        raise Wave1LiveRunnerError("certificate site is not a canonical SubExpr.Pos path")
    position_nat = _decimal_u64(certificate.get(nat_field), f"certificate.{nat_field}")
    if path == "/" and position_nat != "1":
        raise Wave1LiveRunnerError("SubExpr.Pos root must use the canonical asNat value 1")
    return path


_INSTANCE_EVIDENCE_KEYS = {
    "basis_id",
    "endpoint_role",
    "application_path",
    "path_semantics",
    "head_kind",
    "head_name",
    "head_expr_hash",
    "application_expr_hash",
    "argument_index",
    "argument_expr_hash",
    "expected_type_hash",
    "declaration_type_hash",
    "binder_info",
    "typing_evidence_class",
    "exact_instance_implicit",
    "conservative_possible_instance",
}


def _validate_instance_evidence(value: object, endpoint_role: str) -> dict[str, object]:
    item = _mapping(value, "synthesized instance evidence")
    _exact_keys(item, _INSTANCE_EVIDENCE_KEYS, "synthesized instance evidence")
    path = item.get("application_path")
    if not isinstance(path, list) or any(
        not isinstance(step, int) or isinstance(step, bool) or step < 0 for step in path
    ):
        raise Wave1LiveRunnerError("instance evidence application path drift")
    if (
        item.get("basis_id") != "sft1_wave1_typed_inst_implicit_inventory_v0_3_6"
        or item.get("endpoint_role") != endpoint_role
        or item.get("path_semantics") != "flattened_app_head_0_args_i_plus_1_other_expr_children_v1"
        or not isinstance(item.get("head_kind"), str)
        or not (item.get("head_name") is None or isinstance(item.get("head_name"), str))
    ):
        raise Wave1LiveRunnerError("instance evidence identity drift")
    for field_name in (
        "head_expr_hash",
        "application_expr_hash",
        "argument_expr_hash",
    ):
        _decimal_u64(item.get(field_name), f"instance evidence {field_name}")
    for field_name in ("expected_type_hash", "declaration_type_hash"):
        field = item.get(field_name)
        if field is not None:
            _decimal_u64(field, f"instance evidence {field_name}")
    _natural(item.get("argument_index"), "instance evidence argument_index")
    exact = item.get("exact_instance_implicit")
    conservative = item.get("conservative_possible_instance")
    binder_info = item.get("binder_info")
    evidence_class = item.get("typing_evidence_class")
    if exact is True and conservative is False:
        if (
            binder_info != "instImplicit"
            or item.get("expected_type_hash") is None
            or item.get("declaration_type_hash") is None
            or evidence_class != "instImplicit_binder_with_instantiated_expected_type"
        ):
            raise Wave1LiveRunnerError("exact instance evidence is incomplete")
    elif exact is False and conservative is True:
        if (
            binder_info is not None
            or evidence_class != "checked_closed_prop_endpoint_conservative_application_argument"
        ):
            raise Wave1LiveRunnerError("conservative instance evidence is incoherent")
    else:
        raise Wave1LiveRunnerError("instance evidence class booleans are contradictory")
    return item


def _validate_endpoint_inventory(
    value: object, *, endpoint_role: str, endpoint_expr_hash: object
) -> tuple[dict[str, object], list[dict[str, object]]]:
    inventory = _mapping(value, f"{endpoint_role} instance inventory")
    _exact_keys(
        inventory,
        {
            "endpoint_role",
            "endpoint_expr_hash",
            "checked_closed_prop",
            "structurally_complete",
            "exact_instance_implicit_count",
            "conservative_possible_instance_count",
            "evidence_count",
            "empty_inventory_proved",
            "evidence",
        },
        f"{endpoint_role} instance inventory",
    )
    evidence_raw = inventory.get("evidence")
    if not isinstance(evidence_raw, list):
        raise Wave1LiveRunnerError("endpoint instance evidence is not a list")
    evidence = [_validate_instance_evidence(item, endpoint_role) for item in evidence_raw]
    exact_count = sum(item["exact_instance_implicit"] is True for item in evidence)
    conservative_count = sum(item["conservative_possible_instance"] is True for item in evidence)
    if (
        inventory.get("endpoint_role") != endpoint_role
        or inventory.get("endpoint_expr_hash") != endpoint_expr_hash
        or inventory.get("checked_closed_prop") is not True
        or inventory.get("evidence_count") != len(evidence)
        or inventory.get("exact_instance_implicit_count") != exact_count
        or inventory.get("conservative_possible_instance_count") != conservative_count
    ):
        raise Wave1LiveRunnerError("endpoint instance inventory accounting drift")
    empty = inventory.get("empty_inventory_proved")
    structurally_complete = inventory.get("structurally_complete")
    if empty is not (not evidence and structurally_complete is True):
        raise Wave1LiveRunnerError("endpoint empty-instance proof is incoherent")
    return inventory, evidence


def validate_positive_success_task_receipt(
    raw: Mapping[str, object], *, receipt_id: str, operation_id: str
) -> dict[str, object]:
    """Strictly replay the complete helper JSON before any certificate/cache use."""

    receipt = dict(raw)
    _exact_keys(
        receipt,
        {
            "schema_version",
            "receipt_kind",
            "receipt_id",
            "source_version",
            "operation_id",
            "selector",
            "certificate",
            "source_expr_hash",
            "candidate_expr_hash",
            "structural_expr_fingerprint_id",
            "operation_matches",
            "selector_matches",
            "frozen_replay",
            "certificate_constructor_matches",
            "deterministic_candidate_equality",
            "deterministic_certificate_equality",
            "discovered_candidate_count",
            "selected_selector_rediscovery_count",
            "selected_candidate_and_certificate_exact_count",
            "selected_site_uniquely_rediscovered",
            "synthesized_instance_inventory",
            "p01_exact_delta",
            "candidate_exposed_to_caller_for_same_request_repr",
            "row_or_gate_emitted",
        },
        "positive success",
    )
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "positive_success"
        or receipt.get("receipt_id") != receipt_id
        or receipt.get("source_version") != "sft1_wave1_runtime_readiness_v0_3_6"
        or receipt.get("operation_id") != operation_id
        or receipt.get("structural_expr_fingerprint_id") != "lean_hashable_expr_uint64_decimal_v1"
    ):
        raise Wave1LiveRunnerError("positive task receipt identity drift")
    for field_name in (
        "operation_matches",
        "selector_matches",
        "certificate_constructor_matches",
        "deterministic_candidate_equality",
        "deterministic_certificate_equality",
        "selected_site_uniquely_rediscovered",
        "candidate_exposed_to_caller_for_same_request_repr",
    ):
        if receipt.get(field_name) is not True:
            raise Wave1LiveRunnerError(f"positive task receipt {field_name} failed")
    if receipt.get("row_or_gate_emitted") is not False:
        raise Wave1LiveRunnerError("positive task receipt exposed a row or gate")
    _decimal_u64(receipt.get("source_expr_hash"), "source_expr_hash")
    _decimal_u64(receipt.get("candidate_expr_hash"), "candidate_expr_hash")
    selector = _mapping(receipt.get("selector"), "selector")
    if selector != _expected_positive_selector(operation_id):
        raise Wave1LiveRunnerError("positive task receipt selector drift")
    certificate = _mapping(receipt.get("certificate"), "certificate")
    certificate_path = _validate_positive_certificate(operation_id, certificate)
    replay = _mapping(receipt.get("frozen_replay"), "frozen replay")
    _exact_keys(replay, {"passed", "operation_id", "reason"}, "frozen replay")
    if replay != {"passed": True, "operation_id": operation_id, "reason": None}:
        raise Wave1LiveRunnerError("frozen certificate replay receipt failed")
    if (
        _natural(receipt.get("discovered_candidate_count"), "discovered_candidate_count") < 1
        or receipt.get("selected_selector_rediscovery_count") != 1
        or receipt.get("selected_candidate_and_certificate_exact_count") != 1
    ):
        raise Wave1LiveRunnerError("positive unique rediscovery accounting drift")
    inventory = _mapping(receipt.get("synthesized_instance_inventory"), "inventory")
    _exact_keys(
        inventory,
        {
            "basis_id",
            "structural_expr_fingerprint_id",
            "hash_input_contract",
            "ordering",
            "scope",
            "source",
            "candidate",
            "ordered_cache_hash_preimage_count",
            "ordered_cache_hash_preimages",
            "empty_inventory_proved",
            "cache_hash_basis_adequate",
        },
        "synthesized instance inventory",
    )
    if (
        inventory.get("basis_id") != "sft1_wave1_typed_inst_implicit_inventory_v0_3_6"
        or inventory.get("structural_expr_fingerprint_id") != "lean_hashable_expr_uint64_decimal_v1"
        or inventory.get("hash_input_contract")
        != "python_sha256_canonical_json_per_ordered_item_v1"
        or inventory.get("ordering") != "source_preorder_then_candidate_preorder_v1"
        or inventory.get("scope")
        != "all_exact_instImplicit_arguments_plus_conservative_unclassified_arguments"
        or inventory.get("cache_hash_basis_adequate") is not True
    ):
        raise Wave1LiveRunnerError("synthesized instance inventory identity drift")
    source_inventory, source_evidence = _validate_endpoint_inventory(
        inventory.get("source"),
        endpoint_role="source",
        endpoint_expr_hash=receipt.get("source_expr_hash"),
    )
    candidate_inventory, candidate_evidence = _validate_endpoint_inventory(
        inventory.get("candidate"),
        endpoint_role="candidate",
        endpoint_expr_hash=receipt.get("candidate_expr_hash"),
    )
    ordered = inventory.get("ordered_cache_hash_preimages")
    expected_ordered = [*source_evidence, *candidate_evidence]
    if (
        ordered != expected_ordered
        or inventory.get("ordered_cache_hash_preimage_count") != len(expected_ordered)
        or inventory.get("empty_inventory_proved")
        is not (
            not expected_ordered
            and source_inventory.get("empty_inventory_proved") is True
            and candidate_inventory.get("empty_inventory_proved") is True
        )
    ):
        raise Wave1LiveRunnerError("ordered instance-cache preimage replay failed")
    delta = receipt.get("p01_exact_delta")
    if operation_id == "P01_ALPHA_RENAME_SINGLE_V1":
        p01 = _mapping(delta, "p01_exact_delta")
        _exact_keys(
            p01,
            {
                "operation_id",
                "source_expr_hash",
                "candidate_expr_hash",
                "binder_ordinal",
                "binder_site",
                "binder_site_nat",
                "source_name",
                "candidate_name",
                "binder_info",
                "source_domain_hash",
                "candidate_domain_hash",
                "source_body_hash",
                "candidate_body_hash",
                "binder_site_matches_certificate",
                "source_name_matches_certificate",
                "candidate_name_matches_certificate",
                "binder_info_matches_certificate",
                "names_differ",
                "domains_exactly_equal",
                "bodies_exactly_equal",
                "source_candidate_alpha_equivalent",
                "source_candidate_exactly_different",
                "deterministic_candidate_replay_exact",
                "frozen_certificate_replay_passed",
                "exact_name_only_delta_passed",
            },
            "p01 exact delta",
        )
        true_fields = {
            "binder_site_matches_certificate",
            "source_name_matches_certificate",
            "candidate_name_matches_certificate",
            "binder_info_matches_certificate",
            "names_differ",
            "domains_exactly_equal",
            "bodies_exactly_equal",
            "source_candidate_alpha_equivalent",
            "source_candidate_exactly_different",
            "deterministic_candidate_replay_exact",
            "frozen_certificate_replay_passed",
            "exact_name_only_delta_passed",
        }
        if any(p01.get(field_name) is not True for field_name in true_fields) or (
            p01.get("operation_id") != operation_id
            or p01.get("source_expr_hash") != receipt.get("source_expr_hash")
            or p01.get("candidate_expr_hash") != receipt.get("candidate_expr_hash")
            or p01.get("binder_ordinal") != certificate.get("binder_ordinal")
            or p01.get("binder_site") != certificate_path
            or p01.get("binder_site_nat") != certificate.get("binder_site_nat")
            or p01.get("source_name") != certificate.get("source_name")
            or p01.get("candidate_name") != certificate.get("candidate_name")
            or p01.get("binder_info") != certificate.get("binder_info")
        ):
            raise Wave1LiveRunnerError("P01 exact delta/certificate cross-link failed")
        for field_name in (
            "source_domain_hash",
            "candidate_domain_hash",
            "source_body_hash",
            "candidate_body_hash",
        ):
            _decimal_u64(p01.get(field_name), f"p01 exact delta {field_name}")
    elif delta is not None:
        raise Wave1LiveRunnerError("non-P01 receipt invented a P01 exact delta")
    return receipt


def validate_positive_rejection_task_receipt(
    raw: Mapping[str, object], *, receipt_id: str, operation_id: str, expected_reason: str
) -> dict[str, object]:
    receipt = dict(raw)
    _exact_keys(
        receipt,
        {
            "schema_version",
            "receipt_kind",
            "receipt_id",
            "source_version",
            "operation_id",
            "selector",
            "source_expr_hash",
            "structural_expr_fingerprint_id",
            "terminal",
            "reason",
            "candidate_constructed",
            "candidate_serialized",
            "row_or_gate_emitted",
        },
        "positive rejection",
    )
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "positive_typed_not_applicable"
        or receipt.get("receipt_id") != receipt_id
        or receipt.get("source_version") != "sft1_wave1_runtime_readiness_v0_3_6"
        or receipt.get("operation_id") != operation_id
        or receipt.get("selector") != _expected_positive_selector(operation_id)
        or receipt.get("structural_expr_fingerprint_id") != "lean_hashable_expr_uint64_decimal_v1"
        or receipt.get("terminal") != "typedNotApplicable"
        or receipt.get("reason") != expected_reason
        or receipt.get("candidate_constructed") is not False
        or receipt.get("candidate_serialized") is not False
        or receipt.get("row_or_gate_emitted") is not False
    ):
        raise Wave1LiveRunnerError("positive rejection receipt contract drift")
    _decimal_u64(receipt.get("source_expr_hash"), "rejection source_expr_hash")
    return receipt


def synthesized_instance_hashes(task_receipt: Mapping[str, object]) -> tuple[str, ...]:
    """Replay the exact ordered typed instance-inventory cache preimages."""

    receipt_id = task_receipt.get("receipt_id")
    operation_id = task_receipt.get("operation_id")
    if not isinstance(receipt_id, str) or not isinstance(operation_id, str):
        raise Wave1LiveRunnerError("instance inventory lacks its task receipt identity")
    task_receipt = validate_positive_success_task_receipt(
        task_receipt, receipt_id=receipt_id, operation_id=operation_id
    )
    inventory = _mapping(task_receipt.get("synthesized_instance_inventory"), "inventory")
    if (
        inventory.get("basis_id") != "sft1_wave1_typed_inst_implicit_inventory_v0_3_6"
        or inventory.get("hash_input_contract")
        != "python_sha256_canonical_json_per_ordered_item_v1"
        or inventory.get("ordering") != "source_preorder_then_candidate_preorder_v1"
        or inventory.get("cache_hash_basis_adequate") is not True
    ):
        raise Wave1LiveRunnerError("typed instance-inventory contract drift")
    preimages = inventory.get("ordered_cache_hash_preimages")
    if not isinstance(preimages, list) or inventory.get("ordered_cache_hash_preimage_count") != len(
        preimages
    ):
        raise Wave1LiveRunnerError("typed instance-inventory count drift")
    if not preimages and inventory.get("empty_inventory_proved") is not True:
        raise Wave1LiveRunnerError("empty synthesized-instance inventory lacks typed proof")
    if preimages and inventory.get("empty_inventory_proved") is not False:
        raise Wave1LiveRunnerError("nonempty synthesized-instance inventory claims emptiness")
    return tuple(hash_canonical(item) for item in preimages)


def _selected_site_path(certificate: Mapping[str, object]) -> str:
    kind = certificate.get("kind")
    fields = {
        "p01": "binder_site",
        "p15": "target_site",
        "p18": "target_site",
        "p21": "redex_site",
    }
    field_name = fields.get(cast(str, kind))
    if field_name is None:
        raise Wave1LiveRunnerError("positive certificate constructor drift")
    value = certificate.get(field_name)
    if not isinstance(value, str) or _SUBEXPR_PATH.fullmatch(value) is None:
        raise Wave1LiveRunnerError("positive certificate site is absent")
    return value


def typed_certificate_from_execution(
    execution: PositiveSuccessExecution,
) -> TypedCertificateReceipt:
    """Bind the Lean-emitted exact certificate to complete REPR endpoints."""

    if not execution.persistent_session_id.startswith("sft1-wave1-session:"):
        raise Wave1LiveRunnerError("positive execution lacks persistent-session evidence")
    receipt = execution.task_receipt
    certificate = _mapping(receipt.get("certificate"), "certificate")
    selector = _mapping(receipt.get("selector"), "selector")
    replay = _mapping(receipt.get("frozen_replay"), "frozen_replay")
    if replay.get("passed") is not True or replay.get("operation_id") != execution.operation_id:
        raise Wave1LiveRunnerError("Lean certificate replay was not successful")
    site_path = _selected_site_path(certificate)
    endpoints = runtime_endpoints_from_pair(execution.pair)
    p01_delta: P01NameOnlyDelta | None = None
    source_fingerprint_payload: dict[str, object] = {
        "structural_expr_fingerprint_id": receipt.get("structural_expr_fingerprint_id"),
        "expr_hash": receipt.get("source_expr_hash"),
        "certificate": certificate,
        "endpoint_role": "source",
    }
    candidate_fingerprint_payload: dict[str, object] = {
        "structural_expr_fingerprint_id": receipt.get("structural_expr_fingerprint_id"),
        "expr_hash": receipt.get("candidate_expr_hash"),
        "certificate": certificate,
        "endpoint_role": "candidate",
    }
    if execution.operation_id == "P01_ALPHA_RENAME_SINGLE_V1":
        delta = _mapping(receipt.get("p01_exact_delta"), "p01_exact_delta")
        required_true = (
            "binder_site_matches_certificate",
            "source_name_matches_certificate",
            "candidate_name_matches_certificate",
            "binder_info_matches_certificate",
            "names_differ",
            "domains_exactly_equal",
            "bodies_exactly_equal",
            "source_candidate_alpha_equivalent",
            "source_candidate_exactly_different",
            "deterministic_candidate_replay_exact",
            "frozen_certificate_replay_passed",
            "exact_name_only_delta_passed",
        )
        if any(delta.get(field_name) is not True for field_name in required_true):
            raise Wave1LiveRunnerError("P01 exact name-only delta receipt failed")
        old_name = delta.get("source_name")
        new_name = delta.get("candidate_name")
        binder_info = delta.get("binder_info")
        ordinal = delta.get("binder_ordinal")
        if (
            not isinstance(old_name, str)
            or not isinstance(new_name, str)
            or binder_info != "default"
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
        ):
            raise Wave1LiveRunnerError("P01 delta identity fields drift")
        p01_delta = P01NameOnlyDelta(
            old_name=old_name,
            new_name=new_name,
            binder_info="default",
            selected_site_ordinal=ordinal,
            selected_site_rediscovery_count=1,
            domains_unchanged=True,
            bodies_unchanged_except_selected_name=True,
            bound_variable_indices_unchanged=True,
            universes_unchanged=True,
            metadata_unchanged=True,
            other_binders_unchanged=True,
            binder_info_unchanged=True,
        )
        if site_path != p01_outer_binder_site_path(ordinal):
            raise Wave1LiveRunnerError("P01 Lean binder ordinal/path identity drift")
    elif receipt.get("p01_exact_delta") is not None:
        raise Wave1LiveRunnerError("non-P01 success invented a P01 delta")

    if p01_delta is not None:
        source_fingerprint = compute_p01_binder_aware_fingerprint(
            endpoint_role="source",
            closed_expr_hash=endpoints[0].closed_expr_hash,
            sidecar_sha256=endpoints[0].complete_sidecar_sha256,
            selected_site_path=site_path,
            selected_site_ordinal=p01_delta.selected_site_ordinal,
            binder_name=p01_delta.old_name,
            binder_info=p01_delta.binder_info,
        )
        candidate_fingerprint = compute_p01_binder_aware_fingerprint(
            endpoint_role="candidate",
            closed_expr_hash=endpoints[1].closed_expr_hash,
            sidecar_sha256=endpoints[1].complete_sidecar_sha256,
            selected_site_path=site_path,
            selected_site_ordinal=p01_delta.selected_site_ordinal,
            binder_name=p01_delta.new_name,
            binder_info=p01_delta.binder_info,
        )
    else:
        source_fingerprint = hash_canonical(source_fingerprint_payload)
        candidate_fingerprint = hash_canonical(candidate_fingerprint_payload)
    selected_lineage_hash = (
        compute_p01_selected_site_lineage_hash(
            selected_site_path=site_path,
            selected_site_ordinal=p01_delta.selected_site_ordinal,
        )
        if p01_delta is not None
        else hash_canonical(
            {
                "operation_id": execution.operation_id,
                "selector": selector,
                "certificate_site_path": site_path,
            }
        )
    )
    return TypedCertificateReceipt(
        operation_id=execution.operation_id,
        source_closed_expr_hash=endpoints[0].closed_expr_hash,
        candidate_closed_expr_hash=endpoints[1].closed_expr_hash,
        source_sidecar_sha256=endpoints[0].complete_sidecar_sha256,
        candidate_sidecar_sha256=endpoints[1].complete_sidecar_sha256,
        render_request_hash=execution.pair.request_hash,
        replay_request_hash=execution.pair.request_hash,
        selected_site_path=site_path,
        selected_site_path_fingerprint=hash_canonical(site_path),
        selected_site_lineage_hash=selected_lineage_hash,
        binder_aware_source_fingerprint=source_fingerprint,
        binder_aware_candidate_fingerprint=candidate_fingerprint,
        selected_site_uniquely_rediscovered=True,
        replayed_in_persistent_meta=True,
        certificate_replay_passed=True,
        candidate_is_exact_deterministic_replay_result=True,
        p01_name_only_delta=p01_delta,
    )


def runtime_chain_from_execution(
    loaded: LoadedWave1LiveReadiness,
    execution: PositiveSuccessExecution,
) -> tuple[RuntimeChain, RuntimeEdge]:
    """Construct and replay the real composition-runtime boundary."""

    binding = next(
        item for item in loaded.config.operations if item.operation_id == execution.operation_id
    )
    fixture = _fixture_for(loaded, execution.operation_id, "success")
    certificate = typed_certificate_from_execution(execution)
    edge = RuntimeEdge(
        operation_id=execution.operation_id,
        mechanism_superclass=binding.mechanism_superclass,
        inverse_token=binding.inverse_token,
        registry_entry_hash=binding.registry_entry_hash,
        anchor_hash=binding.anchor_hash,
        operation_bank_entry_hash=binding.operation_bank_entry_hash,
        certificate_payload_hash=hash_canonical(certificate.model_dump(mode="json")),
        certificate=certificate,
    )
    chain = make_runtime_chain(
        root_ancestry_id=f"readiness-fixture:{execution.project_id}:{fixture.template_id}",
        source_identity_hash=hash_canonical(
            {
                "fixture_set_hash": loaded.fixture_hash,
                "project_id": execution.project_id,
                "template": fixture.model_dump(mode="json"),
                "compile_context_id": execution.compile_context.compile_context_id,
            }
        ),
        polarity="positive",
        label=1,
        endpoints=runtime_endpoints_from_pair(execution.pair),
        edges=(edge,),
    )
    validate_runtime_chain(chain)
    return chain, edge


@dataclass(frozen=True, slots=True)
class P01RuntimeReplayEvidence:
    receipt: dict[str, object]
    receipt_hash: str
    receipt_path: Path
    receipt_file_sha256: str
    retention_manifest_path: Path
    retention_manifest_file_sha256: str
    retention_journal_path: Path
    retention_journal_file_sha256: str


@dataclass(frozen=True, slots=True)
class P01RetentionScopeEvidence:
    batch: RuntimeRetentionBatch
    result: dict[str, object]
    manifest_path: Path
    journal_path: Path


def _p01_readiness_retention_scope(
    chain: RuntimeChain,
    *,
    project_id: str,
    evidence_root: Path,
) -> P01RetentionScopeEvidence:
    """Persist and replay one complete, explicitly non-row readiness fixture scope."""

    source, renamed = chain.endpoints
    composed_final = _runtime_endpoint_variant(renamed, f"{project_id}:composed-final")
    composed_edge = _runtime_edge_for_replay(
        operation_id="P15_SWAP_IFF_SIDES_V1",
        source=renamed,
        candidate=composed_final,
        path="/1",
        lineage_token=f"{project_id}:composed-p15",
        p01_delta=None,
    )
    composed = make_runtime_chain(
        root_ancestry_id=f"{chain.root_ancestry_id}:composed-contract-fixture",
        source_identity_hash=hash_canonical(
            [chain.source_identity_hash, "composed-contract-fixture"]
        ),
        polarity="positive",
        label=1,
        endpoints=(source, renamed, composed_final),
        edges=(chain.edges[0], composed_edge),
    )
    chains: list[RuntimeChain] = [chain, composed]
    for index in range(798):
        filler_source = _runtime_endpoint_variant(
            source, f"{project_id}:retention-filler:{index}:source"
        )
        filler_candidate = _runtime_endpoint_variant(
            renamed, f"{project_id}:retention-filler:{index}:candidate"
        )
        filler_edge = _runtime_edge_for_replay(
            operation_id="P15_SWAP_IFF_SIDES_V1",
            source=filler_source,
            candidate=filler_candidate,
            path="/1",
            lineage_token=f"{project_id}:retention-filler:{index}",
            p01_delta=None,
        )
        chains.append(
            make_runtime_chain(
                root_ancestry_id=f"readiness-contract:{project_id}:filler:{index}",
                source_identity_hash=hash_canonical(
                    [project_id, "retention-filler", index]
                ),
                polarity="positive",
                label=1,
                endpoints=(filler_source, filler_candidate),
                edges=(filler_edge,),
            )
        )
    scope_id = f"p01-readiness-complete:{project_id}:{chain.stable_row_hash[:16]}"
    resolved_root = evidence_root.resolve()
    relative_base = Path(project_id) / "p01" / "retention_scope"
    journal_relative = relative_base / "complete_scope.journal.jsonl"
    manifest_relative = relative_base / "complete_scope.manifest.json"
    journal_path = resolved_root / journal_relative
    manifest_path = resolved_root / manifest_relative
    previous = "0" * 64
    records: list[dict[str, object]] = []
    for sequence, retained_chain in enumerate(chains):
        core: dict[str, object] = {
            "schema_version": 1,
            "sequence": sequence,
            "previous_chain_hash": previous,
            "retention_scope_id": scope_id,
            "event": "prospective_chain_bound",
            "stable_row_hash": retained_chain.stable_row_hash,
        }
        record = RuntimeRetentionJournalRecord.model_validate(
            {**core, "chain_hash": hash_canonical(core)}
        )
        records.append(record.model_dump(mode="json"))
        previous = record.chain_hash
    journal_payload = b"".join(canonical_json_bytes(item) + b"\n" for item in records)
    journal_sha = _install_immutable(journal_path, journal_payload)
    manifest_core: dict[str, object] = {
        "schema_version": 1,
        "manifest_kind": "durable_complete_prospective_retention_manifest_v1",
        "scope_purpose": "bounded_readiness_contract_fixture_scope_v1",
        "retention_scope_id": scope_id,
        "evidence_root_path": str(resolved_root),
        "scope_manifest_relative_path": manifest_relative.as_posix(),
        "scope_journal_relative_path": journal_relative.as_posix(),
        "scope_journal_file_sha256": journal_sha,
        "complete_scope": True,
        "scope_record_count": len(chains),
        "scope_journal_final_chain_hash": previous,
        "chains": [item.model_dump(mode="json") for item in chains],
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
        "production_admission_changed": False,
    }
    manifest = RuntimeRetentionScopeManifest.model_validate(
        {**manifest_core, "manifest_hash": hash_canonical(manifest_core)}
    )
    manifest_sha = install_immutable_json(manifest_path, manifest.model_dump(mode="json"))
    batch = RuntimeRetentionBatch(
        retention_scope_id=scope_id,
        evidence_root_path=str(resolved_root),
        scope_manifest_relative_path=manifest_relative.as_posix(),
        scope_manifest_path=str(manifest_path),
        scope_manifest_file_sha256=manifest_sha,
        scope_manifest_hash=manifest.manifest_hash,
        scope_journal_relative_path=journal_relative.as_posix(),
        scope_journal_path=str(journal_path),
        scope_journal_file_sha256=journal_sha,
        scope_journal_final_chain_hash=previous,
    )
    result = validate_retention_batch(batch)
    if (
        len(result.retained_stable_row_hashes) != 800
        or result.suppressed_duplicate_stable_row_hashes
        or result.p01_cap_observation.retained_semantic_pair_count != 800
        or result.p01_cap_observation.p01_pair_count != 2
        or result.p01_cap_observation.positive_p01_pair_count != 2
        or result.p01_cap_observation.negative_p01_pair_count != 0
        or result.p01_cap_observation.direct_p01_pair_count != 1
        or result.p01_cap_observation.composed_p01_pair_count != 1
    ):
        raise Wave1LiveRunnerError("P01 readiness retention scope accounting drift")
    return P01RetentionScopeEvidence(
        batch=batch,
        result=result.model_dump(mode="json"),
        manifest_path=manifest_path,
        journal_path=journal_path,
    )


_P01_NAMED_REJECTION_REASONS = {
    "missing_certificate": "invalid_runtime_chain_receipt",
    "failed_certificate": "invalid_runtime_chain_receipt",
    "equal_render": "render_hash_cycle",
    "equal_model_text": "model_facing_text_cycle",
    "wrong_operation_edge": "closed_expr_hash_cycle_without_p01",
    "nonadjacent_repeat": "nonadjacent_or_wrong_operation_edge_repeat",
    "third_occurrence": "third_closed_expr_hash_occurrence",
    "multiple_p01_hops": "multiple_p01_hops",
}


def _runtime_endpoint_variant(
    endpoint: RuntimeEndpoint,
    token: str,
    *,
    closed_expr_hash: str | None = None,
) -> RuntimeEndpoint:
    return endpoint.model_copy(
        update={
            "closed_expr_hash": closed_expr_hash or hash_canonical([token, "closed_expr"]),
            "render_hash": hash_canonical([token, "render"]),
            "core_text_sha256": hash_canonical([token, "core_text"]),
            "complete_sidecar_sha256": hash_canonical([token, "sidecar"]),
        }
    )


def _runtime_edge_for_replay(
    *,
    operation_id: str,
    source: RuntimeEndpoint,
    candidate: RuntimeEndpoint,
    path: str,
    lineage_token: str,
    p01_delta: P01NameOnlyDelta | None,
) -> RuntimeEdge:
    binding = next(
        item
        for item in load_and_validate_p01_runtime_binding().operations
        if item.operation_id == operation_id
    )
    if p01_delta is not None:
        expected_path = p01_outer_binder_site_path(p01_delta.selected_site_ordinal)
        if path != expected_path:
            raise Wave1LiveRunnerError("synthetic P01 replay ordinal/path drift")
        source_fingerprint = compute_p01_binder_aware_fingerprint(
            endpoint_role="source",
            closed_expr_hash=source.closed_expr_hash,
            sidecar_sha256=source.complete_sidecar_sha256,
            selected_site_path=path,
            selected_site_ordinal=p01_delta.selected_site_ordinal,
            binder_name=p01_delta.old_name,
            binder_info=p01_delta.binder_info,
        )
        candidate_fingerprint = compute_p01_binder_aware_fingerprint(
            endpoint_role="candidate",
            closed_expr_hash=candidate.closed_expr_hash,
            sidecar_sha256=candidate.complete_sidecar_sha256,
            selected_site_path=path,
            selected_site_ordinal=p01_delta.selected_site_ordinal,
            binder_name=p01_delta.new_name,
            binder_info=p01_delta.binder_info,
        )
    else:
        source_fingerprint = hash_canonical([lineage_token, "source"])
        candidate_fingerprint = hash_canonical([lineage_token, "candidate"])
    certificate = TypedCertificateReceipt(
        operation_id=operation_id,
        source_closed_expr_hash=source.closed_expr_hash,
        candidate_closed_expr_hash=candidate.closed_expr_hash,
        source_sidecar_sha256=source.complete_sidecar_sha256,
        candidate_sidecar_sha256=candidate.complete_sidecar_sha256,
        render_request_hash=source.render_request_hash,
        replay_request_hash=source.render_request_hash,
        selected_site_path=path,
        selected_site_path_fingerprint=hash_canonical(path),
        selected_site_lineage_hash=(
            compute_p01_selected_site_lineage_hash(
                selected_site_path=path,
                selected_site_ordinal=p01_delta.selected_site_ordinal,
            )
            if p01_delta is not None
            else hash_canonical([lineage_token, "lineage"])
        ),
        binder_aware_source_fingerprint=source_fingerprint,
        binder_aware_candidate_fingerprint=candidate_fingerprint,
        selected_site_uniquely_rediscovered=True,
        replayed_in_persistent_meta=True,
        certificate_replay_passed=True,
        candidate_is_exact_deterministic_replay_result=True,
        p01_name_only_delta=p01_delta,
    )
    return RuntimeEdge(
        operation_id=operation_id,
        mechanism_superclass=binding.mechanism_superclass,
        inverse_token=binding.inverse_token,
        registry_entry_hash=binding.registry_entry_hash,
        anchor_hash=binding.anchor_hash,
        operation_bank_entry_hash=binding.operation_bank_entry_hash,
        certificate_payload_hash=hash_canonical(certificate.model_dump(mode="json")),
        certificate=certificate,
    )


def _derived_runtime_chain(
    source: RuntimeChain,
    endpoints: tuple[RuntimeEndpoint, ...],
    edges: tuple[RuntimeEdge, ...],
) -> RuntimeChain:
    return make_runtime_chain(
        root_ancestry_id=source.root_ancestry_id,
        source_identity_hash=source.source_identity_hash,
        polarity="positive",
        label=1,
        endpoints=endpoints,
        edges=edges,
    )


def _capture_p01_runtime_rejection(chain: RuntimeChain, expected_reason: str) -> str:
    try:
        validate_runtime_chain(chain)
    except Wave1RuntimeError as exc:
        observed = str(exc)
        if expected_reason not in observed:
            raise Wave1LiveRunnerError(
                f"P01 runtime rejection drift: expected {expected_reason}, observed {observed}"
            ) from exc
        return observed
    raise Wave1LiveRunnerError(f"P01 runtime adversary was accepted: {expected_reason}")


def _p01_runtime_adversaries(chain: RuntimeChain) -> dict[str, str]:
    source, renamed = chain.endpoints
    edge = chain.edges[0]
    delta = edge.certificate.p01_name_only_delta
    if delta is None:
        raise Wave1LiveRunnerError("live P01 chain lacks its exact name-only certificate")

    missing_certificate = edge.certificate.model_copy(update={"p01_name_only_delta": None})
    missing_edge = edge.model_copy(
        update={
            "certificate": missing_certificate,
            "certificate_payload_hash": hash_canonical(missing_certificate.model_dump(mode="json")),
        }
    )
    failed_certificate = edge.certificate.model_copy(update={"certificate_replay_passed": False})
    failed_edge = edge.model_copy(
        update={
            "certificate": failed_certificate,
            "certificate_payload_hash": hash_canonical(failed_certificate.model_dump(mode="json")),
        }
    )
    p15_edge = _runtime_edge_for_replay(
        operation_id="P15_SWAP_IFF_SIDES_V1",
        source=source,
        candidate=renamed,
        path="/0",
        lineage_token="p01-wrong-edge",
        p01_delta=None,
    )
    wrong_edge = _derived_runtime_chain(chain, (source, renamed), (p15_edge,))

    middle = _runtime_endpoint_variant(renamed, "p01-nonadjacent-middle")
    final = _runtime_endpoint_variant(
        source,
        "p01-nonadjacent-final",
        closed_expr_hash=source.closed_expr_hash,
    )
    nonadjacent_p01 = _runtime_edge_for_replay(
        operation_id="P01_ALPHA_RENAME_SINGLE_V1",
        source=source,
        candidate=middle,
        path="/",
        lineage_token="p01-nonadjacent-first",
        p01_delta=delta,
    )
    nonadjacent_p15 = _runtime_edge_for_replay(
        operation_id="P15_SWAP_IFF_SIDES_V1",
        source=middle,
        candidate=final,
        path="/0",
        lineage_token="p01-nonadjacent-second",
        p01_delta=None,
    )
    nonadjacent = _derived_runtime_chain(
        chain,
        (source, middle, final),
        (nonadjacent_p01, nonadjacent_p15),
    )

    third = _runtime_endpoint_variant(
        source,
        "p01-third-final",
        closed_expr_hash=source.closed_expr_hash,
    )
    third_p15 = _runtime_edge_for_replay(
        operation_id="P15_SWAP_IFF_SIDES_V1",
        source=renamed,
        candidate=third,
        path="/0",
        lineage_token="p01-third-second",
        p01_delta=None,
    )
    third_occurrence = _derived_runtime_chain(
        chain,
        (source, renamed, third),
        (edge, third_p15),
    )

    final_distinct = _runtime_endpoint_variant(renamed, "p01-multiple-final")
    second_p01 = _runtime_edge_for_replay(
        operation_id="P01_ALPHA_RENAME_SINGLE_V1",
        source=renamed,
        candidate=final_distinct,
        path="/1",
        lineage_token="p01-multiple-second",
        p01_delta=delta.model_copy(update={"selected_site_ordinal": 1}),
    )
    multiple = _derived_runtime_chain(
        chain,
        (source, renamed, final_distinct),
        (edge, second_p01),
    )

    mutations = {
        "missing_certificate": chain.model_copy(update={"edges": (missing_edge,)}),
        "failed_certificate": chain.model_copy(update={"edges": (failed_edge,)}),
        "equal_render": chain.model_copy(
            update={
                "endpoints": (
                    source,
                    renamed.model_copy(update={"render_hash": source.render_hash}),
                )
            }
        ),
        "equal_model_text": chain.model_copy(
            update={
                "endpoints": (
                    source,
                    renamed.model_copy(update={"core_text_sha256": source.core_text_sha256}),
                )
            }
        ),
        "wrong_operation_edge": wrong_edge,
        "nonadjacent_repeat": nonadjacent,
        "third_occurrence": third_occurrence,
        "multiple_p01_hops": multiple,
    }
    return {
        name: _capture_p01_runtime_rejection(mutations[name], _P01_NAMED_REJECTION_REASONS[name])
        for name in _P01_NAMED_REJECTION_REASONS
    }


def _capture_contract_rejection(action: Callable[[], None], expected_reason: str) -> str:
    try:
        action()
    except Wave1RuntimeError as exc:
        observed = str(exc)
        if expected_reason not in observed:
            raise Wave1LiveRunnerError(
                f"runtime contract rejection drift: expected {expected_reason}, observed {observed}"
            ) from exc
        return observed
    raise Wave1LiveRunnerError(f"runtime contract adversary was accepted: {expected_reason}")


def build_p01_runtime_replay_receipt(
    chain: RuntimeChain,
    *,
    project_id: str,
    retention_scope: P01RetentionScopeEvidence | None = None,
) -> dict[str, object]:
    """Replay the live P01 chain plus exact static cap/dedup rejection contract."""

    validation = validate_runtime_chain(chain)
    if not validation.p01_present or not validation.exception_used:
        raise Wave1LiveRunnerError("P01 runtime replay did not exercise its identity exception")
    binding = load_and_validate_p01_runtime_binding()
    adversarial_rejections = _p01_runtime_adversaries(chain)

    cap_observation = P01CapObservation(
        retained_semantic_pair_count=2000,
        p01_pair_count=5,
        p01_procedure_pair_count=5,
        p01_pairs_by_root={f"static-root-{index}": 1 for index in range(5)},
        positive_p01_pair_count=3,
        negative_p01_pair_count=2,
        direct_p01_pair_count=2,
        composed_p01_pair_count=3,
    )
    validate_p01_caps(cap_observation)
    cap_rejections = {
        "per_root": _capture_contract_rejection(
            lambda: validate_p01_caps(
                cap_observation.model_copy(update={"p01_pairs_by_root": {"root": 5}})
            ),
            "p01_per_root_cap_exceeded",
        ),
        "procedure_share": _capture_contract_rejection(
            lambda: validate_p01_caps(
                cap_observation.model_copy(update={"retained_semantic_pair_count": 1999})
            ),
            "p01_procedure_share_cap_exceeded",
        ),
    }

    reference_hash = chain.endpoints[0].render_hash
    candidate_hash = chain.endpoints[-1].render_hash
    duplicate_hash = hash_canonical([chain.stable_row_hash, "duplicate"])
    duplicate_result = deduplicate_unordered_pairs(
        (
            DedupCandidate(
                stable_row_hash=chain.stable_row_hash,
                reference_render_hash=reference_hash,
                candidate_render_hash=candidate_hash,
                label=1,
            ),
            DedupCandidate(
                stable_row_hash=duplicate_hash,
                reference_render_hash=candidate_hash,
                candidate_render_hash=reference_hash,
                label=1,
            ),
        )
    )
    conflict_result = deduplicate_unordered_pairs(
        (
            DedupCandidate(
                stable_row_hash=chain.stable_row_hash,
                reference_render_hash=reference_hash,
                candidate_render_hash=candidate_hash,
                label=1,
            ),
            DedupCandidate(
                stable_row_hash=duplicate_hash,
                reference_render_hash=candidate_hash,
                candidate_render_hash=reference_hash,
                label=0,
            ),
        )
    )
    post_orientation_rejections = {
        "duplicate": _capture_contract_rejection(
            lambda: assert_post_orientation_unique(
                (
                    DedupCandidate(
                        stable_row_hash=chain.stable_row_hash,
                        reference_render_hash=reference_hash,
                        candidate_render_hash=candidate_hash,
                        label=1,
                    ),
                    DedupCandidate(
                        stable_row_hash=duplicate_hash,
                        reference_render_hash=candidate_hash,
                        candidate_render_hash=reference_hash,
                        label=1,
                    ),
                )
            ),
            "post_orientation_duplicate_class",
        ),
        "conflicting_label": _capture_contract_rejection(
            lambda: assert_post_orientation_unique(
                (
                    DedupCandidate(
                        stable_row_hash=chain.stable_row_hash,
                        reference_render_hash=reference_hash,
                        candidate_render_hash=candidate_hash,
                        label=1,
                    ),
                    DedupCandidate(
                        stable_row_hash=duplicate_hash,
                        reference_render_hash=candidate_hash,
                        candidate_render_hash=reference_hash,
                        label=0,
                    ),
                )
            ),
            "post_orientation_conflicting_label_class",
        ),
    }
    repository_root = Path(__file__).resolve().parents[3]
    runtime_path = Path(__file__).with_name("wave1_runtime.py").resolve()
    runtime_test_path = repository_root / "tests/unit/sft1/test_wave1_runtime.py"
    core: dict[str, object] = {
        "schema_version": 1,
        "receipt_kind": "p01_composition_dedup_runtime_replay",
        "project_id": project_id,
        "operation_id": "P01_ALPHA_RENAME_SINGLE_V1",
        "required_policy_semantic_hash": P01_POLICY_SEMANTIC_HASH,
        "corrected_envelope_semantic_hash": P01_CORRECTED_ENVELOPE_HASH,
        "runtime_binding": binding.model_dump(mode="json"),
        "runtime_binding_hash": hash_canonical(binding.model_dump(mode="json")),
        "runtime_source_path": str(runtime_path.relative_to(repository_root)),
        "runtime_source_sha256": hash_file(runtime_path),
        "live_runtime_chain": chain.model_dump(mode="json"),
        "live_runtime_chain_hash": hash_canonical(chain.model_dump(mode="json")),
        "live_acceptance": {
            "p01_present": validation.p01_present,
            "identity_exception_used": validation.exception_used,
            "repeated_closed_expr_hash": validation.repeated_closed_expr_hash,
            "exact_typed_certificate_replayed": True,
        },
        "named_adversarial_rejections": adversarial_rejections,
        "cap_contract": {
            "evidence_kind": "static_arithmetic_contract_not_row_evidence",
            "observation": cap_observation.model_dump(mode="json"),
            "covers_both_polarities": True,
            "covers_direct_and_composed_p01": True,
            "complete_retention_scope_executed": retention_scope is not None,
            "live_negative_p01_evidence_claimed": False,
            "live_negative_p01_unavailable_reason": (
                "N31 remains inactive pending exact user admission"
            ),
            "rejections": cap_rejections,
        },
        "durable_readiness_retention_scope": (
            {
                "batch": retention_scope.batch.model_dump(mode="json"),
                "result": retention_scope.result,
                "manifest_path": str(retention_scope.manifest_path.resolve()),
                "manifest_file_sha256": hash_file(retention_scope.manifest_path),
                "journal_path": str(retention_scope.journal_path.resolve()),
                "journal_file_sha256": hash_file(retention_scope.journal_path),
                "real_chain_dedup_cap_post_orientation_replay_passed": True,
                "scope_is_explicitly_non_row_contract_fixture": True,
                "negative_p01_retained_count": 0,
            }
            if retention_scope is not None
            else None
        ),
        "dedup_conflict_contract": {
            "canonical_unordered_orientation_equal": (
                canonical_unordered_pair_key(reference_hash, candidate_hash)
                == canonical_unordered_pair_key(candidate_hash, reference_hash)
            ),
            "duplicate_result": duplicate_result.model_dump(mode="json"),
            "conflict_result": conflict_result.model_dump(mode="json"),
            "post_orientation_rejections": post_orientation_rejections,
        },
        "composition_regression": {
            "status": "static_contract_regression_bound_not_live_composed_fixture",
            "accepted_test_id": (
                "test_p01_binder_name_slot_composes_with_strict_descendant_expr_site"
            ),
            "rejected_overlap_test_id": (
                "test_p01_binder_name_slot_rejects_whole_expr_site_at_same_node"
            ),
            "test_source_path": str(runtime_test_path.relative_to(repository_root)),
            "test_source_sha256": hash_file(runtime_test_path),
        },
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
        "production_admission_changed": False,
    }
    return {**core, "receipt_hash": hash_canonical(core)}


def validate_p01_runtime_replay_receipt(payload: Mapping[str, object]) -> dict[str, object]:
    expected_keys = {
        "schema_version",
        "receipt_kind",
        "project_id",
        "operation_id",
        "required_policy_semantic_hash",
        "corrected_envelope_semantic_hash",
        "runtime_binding",
        "runtime_binding_hash",
        "runtime_source_path",
        "runtime_source_sha256",
        "live_runtime_chain",
        "live_runtime_chain_hash",
        "live_acceptance",
        "named_adversarial_rejections",
        "cap_contract",
        "durable_readiness_retention_scope",
        "dedup_conflict_contract",
        "composition_regression",
        "wave1_gate_executed",
        "model_facing_rows_emitted",
        "production_admission_changed",
        "receipt_hash",
    }
    core = dict(payload)
    observed_hash = core.pop("receipt_hash", None)
    binding = load_and_validate_p01_runtime_binding()
    try:
        chain = RuntimeChain.model_validate(payload.get("live_runtime_chain"))
    except ValueError as exc:
        raise Wave1LiveRunnerError("P01 runtime receipt chain is invalid") from exc
    validation = validate_runtime_chain(chain)
    repository_root = Path(__file__).resolve().parents[3]
    composition = payload.get("composition_regression")
    if not isinstance(composition, dict):
        raise Wave1LiveRunnerError("P01 runtime receipt lost composition evidence")
    if (
        set(payload) != expected_keys
        or observed_hash != hash_canonical(core)
        or payload.get("schema_version") != 1
        or payload.get("receipt_kind") != "p01_composition_dedup_runtime_replay"
        or payload.get("operation_id") != "P01_ALPHA_RENAME_SINGLE_V1"
        or payload.get("required_policy_semantic_hash") != P01_POLICY_SEMANTIC_HASH
        or payload.get("corrected_envelope_semantic_hash") != P01_CORRECTED_ENVELOPE_HASH
        or payload.get("runtime_binding") != binding.model_dump(mode="json")
        or payload.get("runtime_binding_hash") != hash_canonical(binding.model_dump(mode="json"))
        or payload.get("runtime_source_path") != "src/leanfaith/sft1/wave1_runtime.py"
        or payload.get("runtime_source_sha256")
        != hash_file(Path(__file__).with_name("wave1_runtime.py"))
        or payload.get("live_runtime_chain_hash") != hash_canonical(chain.model_dump(mode="json"))
        or not validation.p01_present
        or not validation.exception_used
        or payload.get("named_adversarial_rejections") is None
        or set(cast(dict[str, object], payload["named_adversarial_rejections"]))
        != set(_P01_NAMED_REJECTION_REASONS)
        or composition.get("status") != "static_contract_regression_bound_not_live_composed_fixture"
        or composition.get("accepted_test_id")
        != "test_p01_binder_name_slot_composes_with_strict_descendant_expr_site"
        or composition.get("rejected_overlap_test_id")
        != "test_p01_binder_name_slot_rejects_whole_expr_site_at_same_node"
        or composition.get("test_source_sha256")
        != hash_file(repository_root / "tests/unit/sft1/test_wave1_runtime.py")
        or payload.get("wave1_gate_executed") is not False
        or payload.get("model_facing_rows_emitted") is not False
        or payload.get("production_admission_changed") is not False
    ):
        raise Wave1LiveRunnerError("P01 runtime replay receipt drift")
    project_id = payload.get("project_id")
    durable = payload.get("durable_readiness_retention_scope")
    retention_scope: P01RetentionScopeEvidence | None = None
    if durable is not None:
        if not isinstance(durable, dict):
            raise Wave1LiveRunnerError("P01 durable retention scope is not an object")
        try:
            batch = RuntimeRetentionBatch.model_validate(durable.get("batch"))
            replayed_result = validate_retention_batch(batch)
        except ValueError as exc:
            raise Wave1LiveRunnerError("P01 durable retention scope replay failed") from exc
        manifest_path = Path(cast(str, durable.get("manifest_path")))
        journal_path = Path(cast(str, durable.get("journal_path")))
        if (
            durable.get("manifest_file_sha256") != hash_file(manifest_path)
            or durable.get("journal_file_sha256") != hash_file(journal_path)
            or durable.get("result") != replayed_result.model_dump(mode="json")
            or durable.get("real_chain_dedup_cap_post_orientation_replay_passed") is not True
            or durable.get("scope_is_explicitly_non_row_contract_fixture") is not True
            or durable.get("negative_p01_retained_count") != 0
        ):
            raise Wave1LiveRunnerError("P01 durable retention scope binding drift")
        retention_scope = P01RetentionScopeEvidence(
            batch=batch,
            result=replayed_result.model_dump(mode="json"),
            manifest_path=manifest_path,
            journal_path=journal_path,
        )
    if not isinstance(project_id, str) or dict(payload) != build_p01_runtime_replay_receipt(
        chain, project_id=project_id, retention_scope=retention_scope
    ):
        raise Wave1LiveRunnerError("P01 runtime replay receipt is not an exact replay")
    return dict(payload)


def persist_p01_runtime_replay(
    chain: RuntimeChain,
    *,
    project_id: str,
    evidence_root: Path,
) -> P01RuntimeReplayEvidence:
    retention_scope = _p01_readiness_retention_scope(
        chain,
        project_id=project_id,
        evidence_root=evidence_root,
    )
    receipt = validate_p01_runtime_replay_receipt(
        build_p01_runtime_replay_receipt(
            chain,
            project_id=project_id,
            retention_scope=retention_scope,
        )
    )
    path = evidence_root / project_id / "p01" / "runtime_contract_replay.json"
    file_sha = install_immutable_json(path, receipt)
    return P01RuntimeReplayEvidence(
        receipt=receipt,
        receipt_hash=cast(str, receipt["receipt_hash"]),
        receipt_path=path,
        receipt_file_sha256=file_sha,
        retention_manifest_path=retention_scope.manifest_path,
        retention_manifest_file_sha256=hash_file(retention_scope.manifest_path),
        retention_journal_path=retention_scope.journal_path,
        retention_journal_file_sha256=hash_file(retention_scope.journal_path),
    )


@dataclass(frozen=True, slots=True)
class PositiveEvidenceReplay:
    chain: RuntimeChain
    edge: RuntimeEdge
    task_receipt_path: Path
    task_receipt_sha256: str
    typed_replay_path: Path
    typed_replay_sha256: str
    raw_response_path: Path
    raw_response_sha256: str
    reference_sidecar_path: Path
    candidate_sidecar_path: Path
    wave1_cache_key: Wave1CacheKey
    wave1_cache_key_hash: str
    central_cache_key_hash: str
    central_cache_entry_path: Path
    central_cache_entry_sha256: str
    symbol_resolution_receipt_hash: str
    symbol_resolution_receipt_path: Path
    symbol_resolution_receipt_file_sha256: str
    symbol_resolution_raw_response_path: Path
    symbol_resolution_raw_response_sha256: str
    positive_resolved_anchor_hash: str
    p01_runtime_replay: P01RuntimeReplayEvidence | None


def persist_and_cache_positive_execution(
    loaded: LoadedWave1LiveReadiness,
    execution: PositiveSuccessExecution,
    *,
    evidence_root: Path,
    sidecar_root: Path,
    cache_root: Path,
    symbol_resolution: PositiveSymbolResolutionEvidence,
    created_at: datetime | None = None,
) -> PositiveEvidenceReplay:
    """Persist complete evidence and prove central-cache immutable replay."""

    chain, edge = runtime_chain_from_execution(loaded, execution)
    task_receipt_path = (
        evidence_root / execution.project_id / f"{execution.receipt_id}.lean_receipt.json"
    )
    task_receipt_sha256 = install_immutable_json(task_receipt_path, execution.task_receipt)
    persisted = persist_wave1_sidecars(execution.pair, sidecar_root)
    operation = next(
        item for item in loaded.config.operations if item.operation_id == execution.operation_id
    )
    if (
        symbol_resolution.project_id != execution.project_id
        or symbol_resolution.operation_id != execution.operation_id
        or symbol_resolution.task_receipt_hash != hash_canonical(symbol_resolution.task_receipt)
        or hash_file(symbol_resolution.task_receipt_path)
        != symbol_resolution.task_receipt_file_sha256
        or hash_file(symbol_resolution.raw_response_path) != symbol_resolution.raw_response_sha256
    ):
        raise Wave1LiveRunnerError("positive symbol-resolution evidence binding drift")
    helper_sha = next(
        item.file_sha256
        for item in loaded.config.source_bindings
        if item.role == "lean_runtime_helper"
    )
    frozen_engine_sha = next(
        item.file_sha256
        for item in loaded.config.source_bindings
        if item.role == "frozen_wave1_engine"
    )
    symbol_resolution_receipt_hash = symbol_resolution.task_receipt_hash
    positive_resolved_anchor_hash = compute_positive_resolved_anchor_hash(
        PositiveResolvedAnchorInput(
            operation_id=cast(
                Literal[
                    "P01_ALPHA_RENAME_SINGLE_V1",
                    "P15_SWAP_IFF_SIDES_V1",
                    "P18_SYMMETRIZE_EQUALITY_V1",
                    "P21_BETA_REDUCE_V1",
                ],
                execution.operation_id,
            ),
            project_id=cast(
                Literal["compiler_data", "cslib", "mathlib", "physlib"],
                execution.project_id,
            ),
            toolchain_revision=execution.compile_context.lean_version,
            frozen_wave1_source_sha256=frozen_engine_sha,
            runtime_helper_sha256=helper_sha,
            operation_constructor=operation.operation_constructor,
            dispatch_symbol=operation.dispatch_symbol,
            checker_symbol=operation.checker_symbol,
            anchor_hash=operation.anchor_hash,
            symbol_resolution_receipt_hash=symbol_resolution_receipt_hash,
        )
    )
    environment = loaded.config.lean_environment_contract
    wave1_key = Wave1CacheKey(
        source_closed_expr_hash=chain.endpoints[0].closed_expr_hash,
        candidate_closed_expr_hash=chain.endpoints[-1].closed_expr_hash,
        canonical_universe_profile_id="goal_v1_first_occurrence_u_i_v1",
        canonical_universe_profile_hash=(
            "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
        ),
        source_expr_builder_version="sft1_fixture_reference_elaboration_v0_3_6",
        candidate_expr_builder_version="sft1_wave1_runtime_readiness_v0_3_6",
        lean_version=execution.compile_context.lean_version,
        project_id=execution.compile_context.project_id,
        project_revision=execution.compile_context.project_revision,
        toolchain_revision=execution.compile_context.lean_version,
        imports_hash=sha256_hex(execution.compile_context.import_header.encode("utf-8")),
        options_hash=hash_canonical(dict(sorted(execution.compile_context.options.items()))),
        synthesized_instance_hashes=synthesized_instance_hashes(execution.task_receipt),
        operation_id=execution.operation_id,
        operation_registry_entry_hash=operation.registry_entry_hash,
        schema_lemma_procedure_hash=operation.anchor_hash,
        evidence_certificate_payload_hash=edge.certificate_payload_hash,
        bank_resolved_lean_hash=positive_resolved_anchor_hash,
        transparency=operation.transparency,
        allowed_axiom_profile=operation.allowed_axiom_profile,
        typed_meta_validator_version="sft1_wave1_typed_meta_validator_v0_3_6",
        evidence_replay_version="sft1_wave1_certificate_replay_v0_3_6",
        evaluation_blocklist_sha256=(
            "8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7"
        ),
        repr_replacement_commit="176a783842c5a73b84413dfa8347670608b615d9",
        render_context_id="goal_v1_render_context_v1",
        render_context_hash=("5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"),
        renderer_api_hash=("c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"),
        repr_spec_hash="68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8",
        environment_fingerprint_hash=execution.compile_context.fingerprint,
        policy_config_hash=loaded.config_hash,
    )
    binding = bind_wave1_central_cache_key(
        wave1_key,
        pair=execution.pair,
        environment_schema_version=environment.environment_schema_version,
        lean_interact_version=environment.lean_interact_version,
        repl_revision=environment.repl_revision,
        timeout_seconds=float(environment.timeout_seconds_per_request),
    )
    typed_replay_path = (
        evidence_root / execution.project_id / f"{execution.receipt_id}.typed_replay.json"
    )
    typed_replay_sha256 = install_immutable_json(
        typed_replay_path, edge.certificate.model_dump(mode="json")
    )
    if execution.result.raw_response_path is None:
        raise Wave1LiveRunnerError("positive execution lacks a persisted raw Lean response")
    if execution.pair.raw_response_path != execution.result.raw_response_path:
        raise Wave1LiveRunnerError("rendered pair and Lean result raw-response paths diverge")
    raw_response_artifact_path = cast(str, binding.raw_response_path)
    raw_response_path = Path(raw_response_artifact_path)
    if not raw_response_path.is_file() or raw_response_path.is_symlink():
        raise Wave1LiveRunnerError("positive raw Lean response is unavailable or unsafe")
    raw_response_sha256 = hash_file(raw_response_path)
    p01_runtime_replay = (
        persist_p01_runtime_replay(
            chain,
            project_id=execution.project_id,
            evidence_root=evidence_root,
        )
        if execution.operation_id == "P01_ALPHA_RENAME_SINGLE_V1"
        else None
    )
    evidence = make_wave1_audit_evidence(
        binding,
        checks={
            "typed_meta_validation": True,
            "typed_certificate_replay": True,
            "same_request_repr": True,
            "sidecars_persisted": True,
        },
        violation_codes=(),
        typed_replay_artifact_path=str(typed_replay_path.resolve()),
        typed_replay_artifact_sha256=typed_replay_sha256,
        raw_response_artifact_path=raw_response_artifact_path,
        raw_response_artifact_sha256=raw_response_sha256,
        created_at=created_at or datetime.now(UTC),
    )
    if evidence.metadata.get("typed_replay_artifact_sha256") != typed_replay_sha256:
        raise Wave1LiveRunnerError("typed replay evidence hash differs from its artifact")
    central_cache = EvidenceCache(cache_root, artifact_root=Path("/"))
    adapter = Wave1CentralCacheAdapter(central_cache)
    artifact_hashes = {
        str(task_receipt_path.resolve()): task_receipt_sha256,
        str(persisted.reference_path.resolve()): persisted.reference_sha256,
        str(persisted.candidate_path.resolve()): persisted.candidate_sha256,
        str(typed_replay_path.resolve()): typed_replay_sha256,
        str(raw_response_path): raw_response_sha256,
        str(symbol_resolution.task_receipt_path.resolve()): (
            symbol_resolution.task_receipt_file_sha256
        ),
        str(symbol_resolution.raw_response_path.resolve()): (symbol_resolution.raw_response_sha256),
    }
    if p01_runtime_replay is not None:
        artifact_hashes[str(p01_runtime_replay.receipt_path.resolve())] = (
            p01_runtime_replay.receipt_file_sha256
        )
        artifact_hashes[str(p01_runtime_replay.retention_manifest_path.resolve())] = (
            p01_runtime_replay.retention_manifest_file_sha256
        )
        artifact_hashes[str(p01_runtime_replay.retention_journal_path.resolve())] = (
            p01_runtime_replay.retention_journal_file_sha256
        )
    request_hashes = tuple(
        dict.fromkeys((*execution.attempt_request_hashes, execution.pair.request_hash))
    )
    entry = adapter.put(
        binding,
        evidence,
        lean_request_hashes=request_hashes,
        certificate_dependency_hash=edge.certificate_payload_hash,
        artifact_hashes=artifact_hashes,
    )
    replayed = adapter.get_after_replay(
        binding,
        replay_receipt=edge.certificate,
        replay_artifact_sha256=typed_replay_sha256,
    )
    if replayed != entry:
        raise Wave1LiveRunnerError("central cache replay differs from immutable write")
    cache_path = central_cache.entry_path(binding.central_key)
    return PositiveEvidenceReplay(
        chain=chain,
        edge=edge,
        task_receipt_path=task_receipt_path,
        task_receipt_sha256=task_receipt_sha256,
        typed_replay_path=typed_replay_path,
        typed_replay_sha256=typed_replay_sha256,
        raw_response_path=raw_response_path,
        raw_response_sha256=raw_response_sha256,
        reference_sidecar_path=persisted.reference_path,
        candidate_sidecar_path=persisted.candidate_path,
        wave1_cache_key=wave1_key,
        wave1_cache_key_hash=binding.wave1_key_hash,
        central_cache_key_hash=entry.cache_key_hash,
        central_cache_entry_path=cache_path,
        central_cache_entry_sha256=hash_file(cache_path),
        symbol_resolution_receipt_hash=symbol_resolution_receipt_hash,
        symbol_resolution_receipt_path=symbol_resolution.task_receipt_path,
        symbol_resolution_receipt_file_sha256=(symbol_resolution.task_receipt_file_sha256),
        symbol_resolution_raw_response_path=symbol_resolution.raw_response_path,
        symbol_resolution_raw_response_sha256=symbol_resolution.raw_response_sha256,
        positive_resolved_anchor_hash=positive_resolved_anchor_hash,
        p01_runtime_replay=p01_runtime_replay,
    )


def execute_positive_success(
    loaded: LoadedWave1LiveReadiness,
    backend: LeanBackend,
    *,
    project_id: str,
    operation_id: str,
    receipt_id: str,
    assembled_preamble: str,
    timeout_seconds: float,
    persistent_session_id: str,
) -> PositiveSuccessExecution:
    """Execute one bounded positive fixture in one persistent Meta request."""

    project = _project_for(loaded, project_id)
    context = build_fixture_compile_context(project, assembled_preamble=assembled_preamble)
    render_scope_id = f"sft1-wave1-readiness:{receipt_id}"
    body, inputs = build_positive_success_session(
        loaded,
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
        render_scope_id=render_scope_id,
    )
    capturing = RetryingCapturingBackend(
        backend,
        retry_policy=RetryPolicy(
            max_attempts=2,
            retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR}),
        ),
    )
    pair = render_wave1_pair(
        capturing,
        reference=inputs[0],
        candidate=inputs[1],
        compile_context=context,
        render_scope_id=render_scope_id,
        session_body=body,
        request_id=f"sft1-wave1-readiness:{hash_canonical({'receipt_id': receipt_id})}",
        timeout_seconds=timeout_seconds,
        require_distinct=True,
    )
    if capturing.last_result is None:
        raise Wave1LiveRunnerError("positive rendering returned no Lean result")
    task_receipt = validate_positive_success_task_receipt(
        extract_task_receipt(
            capturing.last_result.messages,
            receipt_id=receipt_id,
            receipt_kind="positive_success",
        ),
        receipt_id=receipt_id,
        operation_id=operation_id,
    )
    if (
        task_receipt.get("operation_id") != operation_id
        or task_receipt.get("selected_site_uniquely_rediscovered") is not True
        or task_receipt.get("deterministic_candidate_equality") is not True
        or task_receipt.get("deterministic_certificate_equality") is not True
        or task_receipt.get("candidate_exposed_to_caller_for_same_request_repr") is not True
        or task_receipt.get("row_or_gate_emitted") is not False
    ):
        raise Wave1LiveRunnerError("positive task receipt failed its typed replay contract")
    return PositiveSuccessExecution(
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
        compile_context=context,
        pair=pair,
        result=capturing.last_result,
        task_receipt=task_receipt,
        attempt_request_hashes=tuple(item.request_hash for item in capturing.attempt_results),
        persistent_session_id=persistent_session_id,
    )


@dataclass(frozen=True, slots=True)
class DirectReceiptExecution:
    project_id: str
    operation_id: str
    receipt_id: str
    compile_context: CompileContext
    result: LeanResult
    task_receipt: dict[str, object]
    attempt_request_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PositiveSymbolResolutionEvidence:
    project_id: str
    operation_id: str
    receipt_id: str
    task_receipt: dict[str, object]
    task_receipt_hash: str
    task_receipt_path: Path
    task_receipt_file_sha256: str
    raw_response_path: Path
    raw_response_sha256: str
    request_hash: str
    elapsed_ms: int
    compile_context_id: str
    compile_context_fingerprint: str
    assembled_preamble_sha256: str
    runtime_config_file_sha256: str
    runtime_config_hash: str
    runtime_fixture_file_sha256: str
    runtime_fixture_hash: str
    runtime_loader_file_sha256: str
    runtime_helper_sha256: str
    storage_root: Path


def validate_positive_symbol_resolution_receipt(
    raw: Mapping[str, object], *, project_id: str, operation_id: str, receipt_id: str
) -> dict[str, object]:
    receipt = dict(raw)
    _exact_keys(
        receipt,
        {
            "schema_version",
            "receipt_kind",
            "receipt_id",
            "source_version",
            "project_id",
            "operation_id",
            "operation_constructor",
            "dispatch_symbol",
            "discover_symbol",
            "checker_symbol",
            "frozen_engine_version",
            "dispatch_declaration_found",
            "discover_declaration_found",
            "checker_declaration_found",
            "typed_function_signatures_assigned",
            "bundle_identity_matches",
            "passed",
            "candidate_constructed",
            "row_or_gate_emitted",
        },
        "positive symbol resolution",
    )
    required_true = (
        "dispatch_declaration_found",
        "discover_declaration_found",
        "checker_declaration_found",
        "typed_function_signatures_assigned",
        "bundle_identity_matches",
        "passed",
    )
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "positive_symbol_resolution"
        or receipt.get("receipt_id") != receipt_id
        or receipt.get("source_version") != "sft1_wave1_runtime_readiness_v0_3_6"
        or receipt.get("project_id") != project_id
        or receipt.get("operation_id") != operation_id
        or receipt.get("operation_constructor") != _POSITIVE_OPERATION_CONSTRUCTOR[operation_id]
        or receipt.get("dispatch_symbol") != "LeanFaith.SFT1.Wave1.dispatchAt"
        or receipt.get("discover_symbol") != "LeanFaith.SFT1.Wave1.discover"
        or receipt.get("checker_symbol") != "LeanFaith.SFT1.Wave1.replayCertificate"
        or receipt.get("frozen_engine_version") != "sft1_wave1_expr_engine_v0_3_4"
        or any(receipt.get(field_name) is not True for field_name in required_true)
        or receipt.get("candidate_constructed") is not False
        or receipt.get("row_or_gate_emitted") is not False
    ):
        raise Wave1LiveRunnerError("positive typed symbol-resolution receipt failed")
    return receipt


def execute_positive_symbol_resolution(
    loaded: LoadedWave1LiveReadiness,
    backend: LeanBackend,
    *,
    project_id: str,
    operation_id: str,
    assembled_preamble: str,
    timeout_seconds: float,
    evidence_root: Path,
) -> PositiveSymbolResolutionEvidence:
    """Resolve one operation-project bundle independently of either fixture."""

    if timeout_seconds != 300:
        raise Wave1LiveRunnerError("symbol-resolution request budget must remain 300 seconds")
    receipt_id = f"{project_id}.{operation_id}.symbols"
    project = _project_for(loaded, project_id)
    context = build_fixture_compile_context(project, assembled_preamble=assembled_preamble)
    if context.options.get("Elab.async") is not False:
        raise Wave1LiveRunnerError("symbol-resolution context enables asynchronous elaboration")
    session = build_positive_symbol_resolution_session(
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
    )
    request = LeanRequest(
        request_id=f"sft1-wave1-readiness:{hash_canonical({'receipt_id': receipt_id})}",
        context_id=context.compile_context_id,
        code=build_direct_meta_command(context, session),
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={
            "sft1_wave1_readiness": "positive_symbol_resolution",
            "operation_id": operation_id,
            "fixture_independent": "true",
            "row_or_gate_authorized": "false",
        },
    )
    outcome = run_with_retries(
        backend.run,
        request,
        RetryPolicy(
            max_attempts=2,
            retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR}),
        ),
    )
    result = outcome.result
    if (
        result.status != LeanStatus.VALID
        or result.sorries
        or result.context_id != context.compile_context_id
        or result.context_fingerprint != context.fingerprint
    ):
        raise Wave1LiveRunnerError("positive symbol-resolution request did not compile exactly")
    task_receipt = validate_positive_symbol_resolution_receipt(
        extract_task_receipt(
            result.messages,
            receipt_id=receipt_id,
            receipt_kind="positive_symbol_resolution",
        ),
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
    )
    task_receipt_path = evidence_root / project_id / "symbols" / f"{operation_id}.json"
    task_file_sha = install_immutable_json(task_receipt_path, task_receipt)
    raw_path, raw_sha = _validated_raw_response(result)
    _reject_symlink_components(task_receipt_path)
    _reject_symlink_components(raw_path)
    storage_root = evidence_root.resolve().parent
    if task_receipt_path.resolve() != (
        storage_root / "evidence" / project_id / "symbols" / f"{operation_id}.json"
    ) or not raw_path.resolve().is_relative_to(storage_root / "raw" / "positive" / project_id):
        raise Wave1LiveRunnerError("symbol-resolution artifacts escaped their task-owned roots")
    helper_sha = next(
        item.file_sha256
        for item in loaded.config.source_bindings
        if item.role == "lean_runtime_helper"
    )
    return PositiveSymbolResolutionEvidence(
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
        task_receipt=task_receipt,
        task_receipt_hash=hash_canonical(task_receipt),
        task_receipt_path=task_receipt_path,
        task_receipt_file_sha256=task_file_sha,
        raw_response_path=raw_path,
        raw_response_sha256=raw_sha,
        request_hash=result.request_hash,
        elapsed_ms=max(1, result.elapsed_ms),
        compile_context_id=context.compile_context_id,
        compile_context_fingerprint=context.fingerprint,
        assembled_preamble_sha256=sha256_hex(assembled_preamble.encode("utf-8")),
        runtime_config_file_sha256=loaded.config_file_sha256,
        runtime_config_hash=loaded.config_hash,
        runtime_fixture_file_sha256=loaded.fixture_file_sha256,
        runtime_fixture_hash=loaded.fixture_hash,
        runtime_loader_file_sha256=hash_file(Path(__file__).with_name("wave1_live_readiness.py")),
        runtime_helper_sha256=helper_sha,
        storage_root=storage_root,
    )


def _symbol_resolution_checkpoint_payload(
    evidence: PositiveSymbolResolutionEvidence,
) -> dict[str, object]:
    core = {
        "schema_version": 1,
        "project_id": evidence.project_id,
        "operation_id": evidence.operation_id,
        "receipt_id": evidence.receipt_id,
        "task_receipt": evidence.task_receipt,
        "task_receipt_hash": evidence.task_receipt_hash,
        "task_receipt_path": str(evidence.task_receipt_path.resolve()),
        "task_receipt_file_sha256": evidence.task_receipt_file_sha256,
        "raw_response_path": str(evidence.raw_response_path.resolve()),
        "raw_response_sha256": evidence.raw_response_sha256,
        "request_hash": evidence.request_hash,
        "elapsed_ms": evidence.elapsed_ms,
        "compile_context_id": evidence.compile_context_id,
        "compile_context_fingerprint": evidence.compile_context_fingerprint,
        "assembled_preamble_sha256": evidence.assembled_preamble_sha256,
        "runtime_config_file_sha256": evidence.runtime_config_file_sha256,
        "runtime_config_hash": evidence.runtime_config_hash,
        "runtime_fixture_file_sha256": evidence.runtime_fixture_file_sha256,
        "runtime_fixture_hash": evidence.runtime_fixture_hash,
        "runtime_loader_file_sha256": evidence.runtime_loader_file_sha256,
        "runtime_helper_sha256": evidence.runtime_helper_sha256,
        "storage_root": str(evidence.storage_root.resolve()),
    }
    return {**core, "checkpoint_hash": hash_canonical(core)}


def _load_symbol_resolution_checkpoint(
    path: Path,
    *,
    loaded: LoadedWave1LiveReadiness,
    project_id: str,
    operation_id: str,
    assembled_preamble: str,
    storage_root: Path,
) -> PositiveSymbolResolutionEvidence:
    payload = _read_json_object(path)
    core = dict(payload)
    observed = core.pop("checkpoint_hash", None)
    receipt_id = f"{project_id}.{operation_id}.symbols"
    task_receipt = payload.get("task_receipt")
    task_path_value = payload.get("task_receipt_path")
    raw_path_value = payload.get("raw_response_path")
    if not isinstance(task_path_value, str) or not isinstance(raw_path_value, str):
        raise Wave1LiveRunnerError("positive symbol-resolution artifact path is malformed")
    task_path = Path(task_path_value)
    raw_path = Path(raw_path_value)
    _reject_symlink_components(task_path)
    _reject_symlink_components(raw_path)
    expected_context = build_fixture_compile_context(
        _project_for(loaded, project_id), assembled_preamble=assembled_preamble
    )
    helper_sha = next(
        item.file_sha256
        for item in loaded.config.source_bindings
        if item.role == "lean_runtime_helper"
    )
    expected_keys = {
        "schema_version",
        "project_id",
        "operation_id",
        "receipt_id",
        "task_receipt",
        "task_receipt_hash",
        "task_receipt_path",
        "task_receipt_file_sha256",
        "raw_response_path",
        "raw_response_sha256",
        "request_hash",
        "elapsed_ms",
        "compile_context_id",
        "compile_context_fingerprint",
        "assembled_preamble_sha256",
        "runtime_config_file_sha256",
        "runtime_config_hash",
        "runtime_fixture_file_sha256",
        "runtime_fixture_hash",
        "runtime_loader_file_sha256",
        "runtime_helper_sha256",
        "storage_root",
        "checkpoint_hash",
    }
    if (
        set(payload) != expected_keys
        or payload.get("schema_version") != 1
        or payload.get("project_id") != project_id
        or payload.get("operation_id") != operation_id
        or payload.get("receipt_id") != receipt_id
        or observed != hash_canonical(core)
        or not isinstance(task_receipt, dict)
        or payload.get("task_receipt_hash") != hash_canonical(task_receipt)
        or task_path.is_symlink()
        or not task_path.is_file()
        or payload.get("task_receipt_file_sha256") != hash_file(task_path)
        or raw_path.is_symlink()
        or not raw_path.is_file()
        or payload.get("raw_response_sha256") != hash_file(raw_path)
        or payload.get("storage_root") != str(storage_root.resolve())
        or task_path.resolve()
        != storage_root.resolve() / "evidence" / project_id / "symbols" / f"{operation_id}.json"
        or not raw_path.resolve().is_relative_to(
            storage_root.resolve() / "raw" / "positive" / project_id
        )
        or payload.get("compile_context_id") != expected_context.compile_context_id
        or payload.get("compile_context_fingerprint") != expected_context.fingerprint
        or payload.get("assembled_preamble_sha256")
        != sha256_hex(assembled_preamble.encode("utf-8"))
        or payload.get("runtime_config_file_sha256") != loaded.config_file_sha256
        or payload.get("runtime_config_hash") != loaded.config_hash
        or payload.get("runtime_fixture_file_sha256") != loaded.fixture_file_sha256
        or payload.get("runtime_fixture_hash") != loaded.fixture_hash
        or payload.get("runtime_loader_file_sha256")
        != hash_file(Path(__file__).with_name("wave1_live_readiness.py"))
        or payload.get("runtime_helper_sha256") != helper_sha
    ):
        raise Wave1LiveRunnerError("positive symbol-resolution checkpoint replay failed")
    validate_positive_symbol_resolution_receipt(
        task_receipt,
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
    )
    request_hash = payload.get("request_hash")
    elapsed_ms = payload.get("elapsed_ms")
    if (
        not isinstance(request_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
        or not isinstance(elapsed_ms, int)
        or isinstance(elapsed_ms, bool)
        or elapsed_ms <= 0
    ):
        raise Wave1LiveRunnerError("positive symbol-resolution execution evidence drift")
    return PositiveSymbolResolutionEvidence(
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
        task_receipt=task_receipt,
        task_receipt_hash=cast(str, payload["task_receipt_hash"]),
        task_receipt_path=task_path.resolve(),
        task_receipt_file_sha256=cast(str, payload["task_receipt_file_sha256"]),
        raw_response_path=raw_path.resolve(),
        raw_response_sha256=cast(str, payload["raw_response_sha256"]),
        request_hash=request_hash,
        elapsed_ms=elapsed_ms,
        compile_context_id=expected_context.compile_context_id,
        compile_context_fingerprint=expected_context.fingerprint,
        assembled_preamble_sha256=cast(str, payload["assembled_preamble_sha256"]),
        runtime_config_file_sha256=cast(str, payload["runtime_config_file_sha256"]),
        runtime_config_hash=cast(str, payload["runtime_config_hash"]),
        runtime_fixture_file_sha256=cast(str, payload["runtime_fixture_file_sha256"]),
        runtime_fixture_hash=cast(str, payload["runtime_fixture_hash"]),
        runtime_loader_file_sha256=cast(str, payload["runtime_loader_file_sha256"]),
        runtime_helper_sha256=cast(str, payload["runtime_helper_sha256"]),
        storage_root=storage_root.resolve(),
    )


def execute_positive_rejection(
    loaded: LoadedWave1LiveReadiness,
    backend: LeanBackend,
    *,
    project_id: str,
    operation_id: str,
    receipt_id: str,
    assembled_preamble: str,
    timeout_seconds: float,
) -> DirectReceiptExecution:
    """Execute one no-candidate adversarial rejection on a persistent backend."""

    project = _project_for(loaded, project_id)
    context = build_fixture_compile_context(project, assembled_preamble=assembled_preamble)
    session = build_positive_rejection_session(
        loaded,
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
    )
    request = LeanRequest(
        request_id=f"sft1-wave1-readiness:{hash_canonical({'receipt_id': receipt_id})}",
        context_id=context.compile_context_id,
        code=build_direct_meta_command(context, session),
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={"sft1_wave1_readiness": "positive_rejection"},
    )
    outcome = run_with_retries(
        backend.run,
        request,
        RetryPolicy(
            max_attempts=2,
            retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR}),
        ),
    )
    if outcome.result.status != LeanStatus.VALID or outcome.result.sorries:
        raise Wave1LiveRunnerError("positive adversarial rejection request did not compile")
    fixture = _fixture_for(loaded, operation_id, "adversarial_rejection")
    if fixture.expected_engine_reason is None:
        raise Wave1LiveRunnerError("positive rejection fixture lost its expected reason")
    task_receipt = validate_positive_rejection_task_receipt(
        extract_task_receipt(
            outcome.result.messages,
            receipt_id=receipt_id,
            receipt_kind="positive_typed_not_applicable",
        ),
        receipt_id=receipt_id,
        operation_id=operation_id,
        expected_reason=fixture.expected_engine_reason,
    )
    if (
        task_receipt.get("operation_id") != operation_id
        or task_receipt.get("terminal") != fixture.expected_engine_terminal
        or task_receipt.get("reason") != fixture.expected_engine_reason
        or task_receipt.get("candidate_constructed") is not False
        or task_receipt.get("candidate_serialized") is not False
        or task_receipt.get("row_or_gate_emitted") is not False
    ):
        raise Wave1LiveRunnerError("positive rejection receipt differs from exact fixture")
    return DirectReceiptExecution(
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
        compile_context=context,
        result=outcome.result,
        task_receipt=task_receipt,
        attempt_request_hashes=tuple(item.request_hash for item in outcome.attempts),
    )


@dataclass(frozen=True, slots=True)
class N31PhaseExecution:
    project_id: str
    receipt_id: str
    receipt_kind: Literal["n31_proposal_resolution", "n31_frozen_nonactivation"]
    compile_context: CompileContext
    result: LeanResult
    task_receipt: dict[str, object]
    attempt_request_hashes: tuple[str, ...]
    raw_response_path: Path
    raw_response_sha256: str


def _validated_raw_response(result: LeanResult) -> tuple[Path, str]:
    if result.raw_response_path is None:
        raise Wave1LiveRunnerError("Lean result lacks its persisted raw response")
    path = Path(result.raw_response_path)
    if path.is_symlink() or not path.is_file():
        raise Wave1LiveRunnerError("Lean raw response artifact is unavailable or unsafe")
    resolved = path.resolve()
    return resolved, hash_file(resolved)


def _execute_n31_phase(
    loaded: LoadedWave1LiveReadiness,
    backend: LeanBackend,
    *,
    project_id: str,
    receipt_id: str,
    assembled_preamble: str,
    timeout_seconds: float,
    receipt_kind: Literal["n31_proposal_resolution", "n31_frozen_nonactivation"],
    session_body: str,
) -> N31PhaseExecution:
    if timeout_seconds != 300:
        raise Wave1LiveRunnerError("N31 request budget must remain exactly 300 seconds")
    project = _project_for(loaded, project_id)
    context = build_fixture_compile_context(project, assembled_preamble=assembled_preamble)
    if context.options.get("Elab.async") is not False:
        raise Wave1LiveRunnerError("N31 compile context did not disable asynchronous elaboration")
    request = LeanRequest(
        request_id=f"sft1-wave1-readiness:{hash_canonical({'receipt_id': receipt_id})}",
        context_id=context.compile_context_id,
        code=build_direct_meta_command(context, session_body),
        allow_sorry=False,
        timeout_seconds=timeout_seconds,
        metadata={
            "sft1_wave1_readiness": "n31_proposal_nonactivation",
            "n31_receipt_kind": receipt_kind,
            "n31_activation_authorized": "false",
            "row_or_gate_authorized": "false",
        },
    )
    outcome = run_with_retries(
        backend.run,
        request,
        RetryPolicy(
            max_attempts=2,
            retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR}),
        ),
    )
    result = outcome.result
    if (
        result.status != LeanStatus.VALID
        or result.sorries
        or result.context_id != context.compile_context_id
        or result.context_fingerprint != context.fingerprint
    ):
        raise Wave1LiveRunnerError(f"N31 {receipt_kind} request did not compile exactly")
    receipt = extract_task_receipt(
        result.messages,
        receipt_id=receipt_id,
        receipt_kind=receipt_kind,
    )
    if (
        receipt.get("candidate_constructed") is not False
        or receipt.get("candidate_exposed") is not False
        or receipt.get("row_or_gate_emitted") is not False
        or receipt.get("semantic_conformance_performed") is not False
    ):
        raise Wave1LiveRunnerError("N31 proposal phase crossed its frozen authorization boundary")
    raw_path, raw_sha = _validated_raw_response(result)
    return N31PhaseExecution(
        project_id=project_id,
        receipt_id=receipt_id,
        receipt_kind=receipt_kind,
        compile_context=context,
        result=result,
        task_receipt=receipt,
        attempt_request_hashes=tuple(item.request_hash for item in outcome.attempts),
        raw_response_path=raw_path,
        raw_response_sha256=raw_sha,
    )


@dataclass(frozen=True, slots=True)
class N31ProposalExecution:
    proposal: N31ResolutionProjectProposal
    phase_one: N31PhaseExecution
    phase_two: N31PhaseExecution


def _n31_phase_checkpoint_payload(phase: N31PhaseExecution) -> dict[str, object]:
    core = {
        "schema_version": 1,
        "project_id": phase.project_id,
        "receipt_id": phase.receipt_id,
        "receipt_kind": phase.receipt_kind,
        "compile_context_id": phase.compile_context.compile_context_id,
        "compile_context_fingerprint": phase.compile_context.fingerprint,
        "request_hash": phase.result.request_hash,
        "attempt_request_hashes": list(phase.attempt_request_hashes),
        "task_receipt": phase.task_receipt,
        "task_receipt_hash": hash_canonical(phase.task_receipt),
        "raw_response_path": str(phase.raw_response_path),
        "raw_response_sha256": phase.raw_response_sha256,
        "elapsed_ms": max(1, phase.result.elapsed_ms),
        "candidate_constructed": False,
        "semantic_conformance_performed": False,
        "row_or_gate_emitted": False,
    }
    return {**core, "checkpoint_hash": hash_canonical(core)}


def _load_n31_phase_checkpoint(
    loaded: LoadedWave1LiveReadiness,
    *,
    path: Path,
    project_id: str,
    assembled_preamble: str,
    receipt_kind: Literal["n31_proposal_resolution", "n31_frozen_nonactivation"],
) -> N31PhaseExecution:
    payload = _read_json_object(path)
    core = dict(payload)
    observed = core.pop("checkpoint_hash", None)
    phase_name: Literal["phase_one", "phase_two"] = (
        "phase_one" if receipt_kind == "n31_proposal_resolution" else "phase_two"
    )
    receipt_id = n31_phase_receipt_id(
        cast(Literal["compiler_data", "cslib", "mathlib", "physlib"], project_id),
        phase_name,
    )
    task_receipt = payload.get("task_receipt")
    raw_path = Path(cast(str, payload.get("raw_response_path")))
    context = build_fixture_compile_context(
        _project_for(loaded, project_id), assembled_preamble=assembled_preamble
    )
    if (
        payload.get("schema_version") != 1
        or payload.get("project_id") != project_id
        or payload.get("receipt_id") != receipt_id
        or payload.get("receipt_kind") != receipt_kind
        or observed != hash_canonical(core)
        or payload.get("compile_context_id") != context.compile_context_id
        or payload.get("compile_context_fingerprint") != context.fingerprint
        or not isinstance(task_receipt, dict)
        or task_receipt.get("receipt_id") != receipt_id
        or task_receipt.get("receipt_kind") != receipt_kind
        or payload.get("task_receipt_hash") != hash_canonical(task_receipt)
        or raw_path.is_symlink()
        or not raw_path.is_file()
        or hash_file(raw_path) != payload.get("raw_response_sha256")
        or payload.get("candidate_constructed") is not False
        or payload.get("semantic_conformance_performed") is not False
        or payload.get("row_or_gate_emitted") is not False
    ):
        raise Wave1LiveRunnerError("N31 phase checkpoint replay failed")
    request_hash = payload.get("request_hash")
    attempt_hashes = payload.get("attempt_request_hashes")
    elapsed_ms = payload.get("elapsed_ms")
    if (
        not isinstance(request_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
        or not isinstance(attempt_hashes, list)
        or not all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in attempt_hashes
        )
        or not isinstance(elapsed_ms, int)
        or isinstance(elapsed_ms, bool)
        or elapsed_ms <= 0
    ):
        raise Wave1LiveRunnerError("N31 phase checkpoint request evidence drift")
    result = LeanResult(
        request_id=receipt_id,
        request_hash=request_hash,
        context_id=context.compile_context_id,
        context_fingerprint=context.fingerprint,
        status=LeanStatus.VALID,
        elapsed_ms=elapsed_ms,
        raw_response_path=str(raw_path),
    )
    return N31PhaseExecution(
        project_id=project_id,
        receipt_id=receipt_id,
        receipt_kind=receipt_kind,
        compile_context=context,
        result=result,
        task_receipt=task_receipt,
        attempt_request_hashes=tuple(cast(list[str], attempt_hashes)),
        raw_response_path=raw_path.resolve(),
        raw_response_sha256=cast(str, payload["raw_response_sha256"]),
    )


def execute_n31_resolution_proposal_evidence(
    loaded: LoadedWave1LiveReadiness,
    backend: LeanBackend,
    *,
    project_id: str,
    assembled_preamble: str,
    timeout_seconds: float,
    measured_peak_rss_bytes: int,
    phase_checkpoint_root: Path | None = None,
) -> N31ProposalExecution:
    """Resolve exact bank identities in two requests and prove nonactivation.

    This function cannot run the private semantic checker and never obtains a
    candidate.  Its only terminal output is a ``proposed_not_admitted`` bank
    identity suitable for the user's separate exact admission decision.
    """

    if project_id not in EXPECTED_PROJECT_IDS:
        raise Wave1LiveRunnerError("N31 project is outside the exact four-project proposal scope")
    phase_one_id = n31_phase_receipt_id(project_id, "phase_one")
    phase_one_path = (
        phase_checkpoint_root / project_id / "phase_one.json"
        if phase_checkpoint_root is not None
        else None
    )
    if phase_one_path is not None and phase_one_path.exists():
        phase_one = _load_n31_phase_checkpoint(
            loaded,
            path=phase_one_path,
            project_id=project_id,
            assembled_preamble=assembled_preamble,
            receipt_kind="n31_proposal_resolution",
        )
    else:
        phase_one = _execute_n31_phase(
            loaded,
            backend,
            project_id=project_id,
            receipt_id=phase_one_id,
            assembled_preamble=assembled_preamble,
            timeout_seconds=timeout_seconds,
            receipt_kind="n31_proposal_resolution",
            session_body=build_n31_phase_one_session(
                loaded, project_id=project_id, receipt_id=phase_one_id
            ),
        )
        if phase_one_path is not None:
            install_immutable_json(phase_one_path, _n31_phase_checkpoint_payload(phase_one))
    bank_payload = phase_one.task_receipt.get("bank_fingerprint_payload")
    receipt_preimage = phase_one.task_receipt.get("resolution_receipt_hash_preimage_payload")
    if not isinstance(bank_payload, dict) or not isinstance(receipt_preimage, dict):
        raise Wave1LiveRunnerError("N31 phase one omitted its external hash preimages")
    resolved_lean_hash = hash_canonical(bank_payload)
    resolution_receipt_hash = hash_canonical(receipt_preimage)
    phase_two_id = n31_phase_receipt_id(project_id, "phase_two")
    phase_two_path = (
        phase_checkpoint_root / project_id / "phase_two.json"
        if phase_checkpoint_root is not None
        else None
    )
    if phase_two_path is not None and phase_two_path.exists():
        phase_two = _load_n31_phase_checkpoint(
            loaded,
            path=phase_two_path,
            project_id=project_id,
            assembled_preamble=assembled_preamble,
            receipt_kind="n31_frozen_nonactivation",
        )
    else:
        phase_two = _execute_n31_phase(
            loaded,
            backend,
            project_id=project_id,
            receipt_id=phase_two_id,
            assembled_preamble=assembled_preamble,
            timeout_seconds=timeout_seconds,
            receipt_kind="n31_frozen_nonactivation",
            session_body=build_n31_phase_two_session(
                loaded,
                project_id=project_id,
                receipt_id=phase_two_id,
                resolved_lean_hash=resolved_lean_hash,
                resolution_receipt_hash=resolution_receipt_hash,
            ),
        )
        if phase_two_path is not None:
            install_immutable_json(phase_two_path, _n31_phase_checkpoint_payload(phase_two))
    core: dict[str, object] = {
        "project_id": project_id,
        "compile_context_id": phase_one.compile_context.compile_context_id,
        "compile_context_fingerprint": phase_one.compile_context.fingerprint,
        "bank_id": loaded.config.n31_proposal_bank.bank_id,
        "bank_template_hash": compute_n31_proposal_bank_template_hash(
            loaded.config.n31_proposal_bank
        ),
        "resolved_lean_hash": resolved_lean_hash,
        "resolution_receipt_hash": resolution_receipt_hash,
        "phase_one_request_hash": phase_one.result.request_hash,
        "phase_two_request_hash": phase_two.result.request_hash,
        "phase_one_raw_response_sha256": phase_one.raw_response_sha256,
        "phase_two_raw_response_sha256": phase_two.raw_response_sha256,
        "phase_one_task_receipt": phase_one.task_receipt,
        "phase_two_task_receipt": phase_two.task_receipt,
        "phase_one_task_receipt_hash": hash_canonical(phase_one.task_receipt),
        "phase_two_task_receipt_hash": hash_canonical(phase_two.task_receipt),
        "exact_name_arity_type_instance_resolution_passed": True,
        "frozen_nonactivation_replayed": True,
        "runtime_activated": False,
        "semantic_success_conformance_performed": False,
        "semantic_adversarial_conformance_performed": False,
        "candidate_constructed": False,
        "row_or_gate_emitted": False,
        "elapsed_ms": max(1, phase_one.result.elapsed_ms + phase_two.result.elapsed_ms),
        "measured_peak_rss_bytes": measured_peak_rss_bytes,
    }
    try:
        proposal = N31ResolutionProjectProposal.model_validate(
            {**core, "project_receipt_hash": hash_canonical(core)}
        )
    except ValidationError as exc:
        raise Wave1LiveRunnerError("N31 two-phase proposal receipt failed strict replay") from exc
    return N31ProposalExecution(proposal=proposal, phase_one=phase_one, phase_two=phase_two)


def execute_n31_resolution_proposal(
    loaded: LoadedWave1LiveReadiness,
    backend: LeanBackend,
    *,
    project_id: str,
    assembled_preamble: str,
    timeout_seconds: float,
    measured_peak_rss_bytes: int,
    phase_checkpoint_root: Path | None = None,
) -> N31ResolutionProjectProposal:
    """Compatibility surface returning only the strict project proposal."""

    return execute_n31_resolution_proposal_evidence(
        loaded,
        backend,
        project_id=project_id,
        assembled_preamble=assembled_preamble,
        timeout_seconds=timeout_seconds,
        measured_peak_rss_bytes=measured_peak_rss_bytes,
        phase_checkpoint_root=phase_checkpoint_root,
    ).proposal


def _install_immutable(path: Path, payload: bytes) -> str:
    digest = sha256_hex(payload)
    _prepare_parent_directory(path)
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise Wave1LiveRunnerError(f"immutable artifact conflict: {path}")
        return digest
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o444)
    try:
        os.link(temporary, path)
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise Wave1LiveRunnerError(f"immutable artifact conflict: {path}") from None
    finally:
        temporary.unlink(missing_ok=True)
    return digest


def install_immutable_json(path: Path, payload: object) -> str:
    return _install_immutable(path, canonical_json_bytes(payload) + b"\n")


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise Wave1LiveRunnerError(f"symlinked durable path component: {current}")


def _prepare_parent_directory(path: Path) -> None:
    _reject_symlink_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path.parent)
    if not path.parent.is_dir():
        raise Wave1LiveRunnerError(f"durable parent is not a directory: {path.parent}")


def _journal_records(payload: bytes) -> tuple[int, str]:
    previous = "0" * 64
    count = 0
    if payload and not payload.endswith(b"\n"):
        raise Wave1LiveRunnerError("journal has a torn final record")
    for line_number, encoded in enumerate(payload.splitlines(), start=1):
        try:
            record = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_json_no_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise Wave1LiveRunnerError(f"malformed journal record at line {line_number}") from exc
        if not isinstance(record, dict) or set(record) != {
            "schema_version",
            "sequence",
            "previous_event_hash",
            "event",
            "event_hash",
        }:
            raise Wave1LiveRunnerError("journal record field inventory drift")
        event_hash = record.pop("event_hash")
        if (
            record.get("schema_version") != 1
            or record.get("sequence") != count
            or record.get("previous_event_hash") != previous
            or not isinstance(record.get("event"), dict)
            or event_hash != hash_canonical(record)
        ):
            raise Wave1LiveRunnerError(f"journal hash chain mismatch at line {line_number}")
        previous = cast(str, event_hash)
        count += 1
    return count, previous


@dataclass(slots=True)
class HashChainJournal:
    path: Path
    _sequence: int = 0
    _last_hash: str = field(default_factory=lambda: "0" * 64)

    def __post_init__(self) -> None:
        if self.path.exists():
            if self.path.is_symlink():
                raise Wave1LiveRunnerError("journal path must not be a symlink")
            sequence, digest = replay_hash_chain_journal(self.path)
            self._sequence = sequence
            self._last_hash = digest

    def append(self, event: Mapping[str, object]) -> str:
        _prepare_parent_directory(self.path)
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise Wave1LiveRunnerError(f"cannot open durable journal: {self.path}") from exc
        try:
            with os.fdopen(descriptor, "r+b", closefd=True) as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise Wave1LiveRunnerError("journal is not a regular file")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.seek(0)
                sequence, previous = _journal_records(handle.read())
                if sequence != self._sequence or previous != self._last_hash:
                    raise Wave1LiveRunnerError("journal changed outside this locked writer")
                core = {
                    "schema_version": 1,
                    "sequence": sequence,
                    "previous_event_hash": previous,
                    "event": dict(event),
                }
                event_hash = hash_canonical(core)
                record = {**core, "event_hash": event_hash}
                handle.seek(0, os.SEEK_END)
                handle.write(canonical_json_bytes(record) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except BaseException:
            # fdopen owns and closes the descriptor after successful construction.
            raise
        self._sequence = sequence + 1
        self._last_hash = event_hash
        return event_hash

    @property
    def final_chain_hash(self) -> str:
        return self._last_hash


def replay_hash_chain_journal(path: Path) -> tuple[int, str]:
    if path.is_symlink():
        raise Wave1LiveRunnerError("journal path must not be a symlink")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise Wave1LiveRunnerError(f"cannot open durable journal: {path}") from exc
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Wave1LiveRunnerError("journal is not a regular file")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        payload = handle.read()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return _journal_records(payload)


def _process_parent_map() -> dict[int, int]:
    parents: dict[int, int] = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text(encoding="utf-8").split()
            parents[int(path.name)] = int(fields[3])
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
    if not parents:
        raise Wave1LiveRunnerError("/proc RSS measurement is unavailable")
    return parents


def measured_process_tree_rss_bytes(root_pid: int) -> int:
    parents = _process_parent_map()
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in selected and pid not in selected:
                selected.add(pid)
                changed = True
    page_size = os.sysconf("SC_PAGE_SIZE")
    total = 0
    for pid in selected:
        try:
            resident_pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
        except (FileNotFoundError, PermissionError, IndexError, ValueError) as exc:
            if pid == root_pid:
                raise Wave1LiveRunnerError("runner RSS measurement failed") from exc
            continue
        total += resident_pages * page_size
    return total


@dataclass(slots=True)
class PeakRssSampler:
    root_pid: int = field(default_factory=os.getpid)
    interval_seconds: float = 0.1
    maximum_bytes: int = 40 * 1024**3
    peak_bytes: int = 0
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _failure: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise Wave1LiveRunnerError("RSS sampler already started")

        def sample() -> None:
            try:
                while not self._stop.is_set():
                    observed = measured_process_tree_rss_bytes(self.root_pid)
                    self.peak_bytes = max(self.peak_bytes, observed)
                    if observed > self.maximum_bytes:
                        raise Wave1LiveRunnerError("combined measured RSS ceiling exceeded")
                    self._stop.wait(self.interval_seconds)
            except BaseException as exc:  # propagated on stop
                self._failure = exc
                self._stop.set()

        self._thread = threading.Thread(target=sample, name="sft1-wave1-rss", daemon=True)
        self._thread.start()

    def stop(self) -> int:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 5))
        if self._failure is not None:
            raise Wave1LiveRunnerError("RSS sampler failed") from self._failure
        if self.peak_bytes <= 0:
            raise Wave1LiveRunnerError("RSS sampler recorded no positive measurement")
        return self.peak_bytes

    def check(self) -> int:
        if self._failure is not None:
            raise Wave1LiveRunnerError("RSS sampler failed") from self._failure
        if self.peak_bytes > self.maximum_bytes:
            raise Wave1LiveRunnerError("combined measured RSS ceiling exceeded")
        return self.peak_bytes


def runner_process_identity(pid: int | None = None) -> dict[str, object]:
    """Bind heartbeats to one host process incarnation, not merely a PID."""

    selected_pid = pid or os.getpid()
    if selected_pid <= 0:
        raise Wave1LiveRunnerError("runner process PID must be positive")
    try:
        stat_fields = Path(f"/proc/{selected_pid}/stat").read_text(encoding="utf-8").split()
        start_ticks = int(stat_fields[21])
        command = Path(f"/proc/{selected_pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, IndexError, ValueError) as exc:
        raise Wave1LiveRunnerError("runner process identity is unavailable") from exc
    return {
        "schema_version": 1,
        "hostname": socket.gethostname(),
        "pid": selected_pid,
        "proc_start_ticks": start_ticks,
        "cmdline_sha256": sha256_hex(command),
    }


def write_durable_heartbeat(
    path: Path,
    *,
    run_spec_hash: str,
    state: str,
    project_id: str | None,
    case_id: str | None,
    rss_bytes: int,
    process_identity: Mapping[str, object] | None = None,
) -> str:
    """Atomically refresh a hash-bound liveness record without following links."""

    if re.fullmatch(r"[0-9a-f]{64}", run_spec_hash) is None:
        raise Wave1LiveRunnerError("heartbeat run-spec hash is malformed")
    if rss_bytes < 0 or rss_bytes > 40 * 1024**3:
        raise Wave1LiveRunnerError("heartbeat RSS is outside the fixed ceiling")
    _prepare_parent_directory(path)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise Wave1LiveRunnerError("heartbeat target is not a safe regular file")
    core = {
        "schema_version": 1,
        "run_spec_hash": run_spec_hash,
        "state": state,
        "project_id": project_id,
        "case_id": case_id,
        "rss_bytes": rss_bytes,
        "process_identity": dict(process_identity or runner_process_identity()),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    payload = canonical_json_bytes({**core, "heartbeat_hash": hash_canonical(core)}) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    if temporary.exists():
        raise Wave1LiveRunnerError("stale heartbeat temporary file exists")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return sha256_hex(payload)


@dataclass(slots=True)
class DurableHeartbeatEmitter:
    """Refresh liveness while a bounded Lean request is in flight."""

    path: Path
    run_spec_hash: str
    rss_supplier: Callable[[], int]
    interval_seconds: float = 30.0
    process_identity: Mapping[str, object] = field(default_factory=runner_process_identity)
    _state: str = "initializing"
    _project_id: str | None = None
    _case_id: str | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _failure: BaseException | None = None

    def update(self, *, state: str, project_id: str | None, case_id: str | None) -> None:
        with self._lock:
            self._state = state
            self._project_id = project_id
            self._case_id = case_id
        self._write_once()

    def _write_once(self) -> None:
        with self._lock:
            state = self._state
            project_id = self._project_id
            case_id = self._case_id
        write_durable_heartbeat(
            self.path,
            run_spec_hash=self.run_spec_hash,
            state=state,
            project_id=project_id,
            case_id=case_id,
            rss_bytes=self.rss_supplier(),
            process_identity=self.process_identity,
        )

    def start(self) -> None:
        if self._thread is not None or self.interval_seconds <= 0:
            raise Wave1LiveRunnerError("heartbeat emitter start contract failed")
        self._write_once()

        def emit() -> None:
            try:
                while not self._stop.wait(self.interval_seconds):
                    self._write_once()
            except BaseException as exc:
                self._failure = exc
                self._stop.set()

        self._thread = threading.Thread(
            target=emit,
            name="sft1-wave1-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def check(self) -> None:
        if self._failure is not None:
            raise Wave1LiveRunnerError("durable heartbeat emitter failed") from self._failure

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, min(5.0, self.interval_seconds)))
        self.check()


def claim_single_wave1_worker(
    *, owner_session: str, worktree: Path, lean_rss_gib: float = 24.0
) -> Reservation:
    """Atomically reserve the initial one-worker budget for this runner PID."""

    if not owner_session.strip() or not (0 < lean_rss_gib <= 40):
        raise Wave1LiveRunnerError("invalid SFT1 resource-claim parameters")
    existing = [item for item in list_reservations() if item.task == "SFT1"]
    if existing:
        reservation = existing[0]
        if (
            reservation.pid != os.getpid()
            or reservation.lean_workers != 1
            or reservation.lean_rss_gib != lean_rss_gib
            or Path(reservation.worktree) != worktree.resolve()
        ):
            raise Wave1LiveRunnerError("foreign or stale SFT1 resource reservation exists")
        return reservation
    return claim_resources(
        task="SFT1",
        lean_workers=1,
        lean_rss_gib=lean_rss_gib,
        gpu=False,
        pid=os.getpid(),
        owner_session=owner_session,
        worktree=worktree,
    )


def reservation_snapshot_hash(reservation: Reservation) -> str:
    return sha256_hex(reservation.to_json().encode("utf-8"))


def _release_sft1_worker() -> Reservation:
    return release_resources(task="SFT1")


@dataclass(frozen=True, slots=True)
class OrchestratorDependencies:
    """Injectable process boundaries; production defaults are the central backend."""

    prepare_backend: Callable[[BackendSettings], None] = LeanInteractBackend.prepare_environment
    make_backend: Callable[[BackendSettings], LeanBackend] = LeanInteractBackend
    claim_worker: Callable[..., Reservation] = claim_single_wave1_worker
    release_worker: Callable[[], Reservation] = _release_sft1_worker
    sampler_factory: Callable[[], PeakRssSampler] = PeakRssSampler
    verify_implementation_identity: Callable[[Path, str, str], dict[str, object]] = field(
        default_factory=lambda: verify_clean_git_implementation_identity
    )
    symbol_resolution_executor: Callable[..., PositiveSymbolResolutionEvidence] = (
        execute_positive_symbol_resolution
    )
    positive_success_executor: Callable[..., PositiveSuccessExecution] = execute_positive_success
    positive_rejection_executor: Callable[..., DirectReceiptExecution] = execute_positive_rejection
    positive_persister: Callable[..., PositiveEvidenceReplay] = persist_and_cache_positive_execution
    n31_executor: Callable[..., N31ProposalExecution] = execute_n31_resolution_proposal_evidence


def _validate_execution_contract(loaded: LoadedWave1LiveReadiness) -> None:
    resources = loaded.config.resource_contract
    environment = loaded.config.lean_environment_contract
    if (
        resources.initial_persistent_lean_workers != 1
        or resources.maximum_concurrent_lean_workers != 2
        or resources.maximum_combined_measured_rss_gib != 40
        or resources.memory_hard_limit_mb_per_worker != 24576
        or resources.elab_async is not False
        or resources.per_row_process_spawn_allowed is not False
        or resources.compile_corpus_allowed is not False
        or environment.server_mode != "stable"
        or environment.timeout_seconds_per_request != 300
        or environment.infrastructure_retry_max_attempts != 2
    ):
        raise Wave1LiveRunnerError("Wave 1 bounded execution contract drift")
    for project in loaded.fixtures.project_contexts:
        if project.options.get("Elab.async") is not False:
            raise Wave1LiveRunnerError("a Wave 1 project context enables Elab.async")


def _git_object_id(value: str, field_name: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise Wave1LiveRunnerError(f"{field_name} is not a lowercase Git object ID")
    return value


def verify_clean_git_implementation_identity(
    worktree: Path,
    implementation_commit: str,
    implementation_tree: str,
) -> dict[str, object]:
    """Bind a readiness run to the actual clean checkout before any Lean work."""

    expected_commit = _git_object_id(implementation_commit, "implementation commit")
    expected_tree = _git_object_id(implementation_tree, "implementation tree")
    resolved_worktree = worktree.resolve()

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(resolved_worktree), *arguments),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise Wave1LiveRunnerError(
                f"Git implementation identity query failed: {' '.join(arguments)}"
            )
        return completed.stdout.strip()

    observed_top_level = Path(git("rev-parse", "--show-toplevel")).resolve()
    if observed_top_level != resolved_worktree:
        raise Wave1LiveRunnerError("implementation worktree is not the Git top level")
    observed_commit = git("rev-parse", "--verify", "HEAD")
    observed_tree = git("rev-parse", "--verify", "HEAD^{tree}")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    if observed_commit != expected_commit or observed_tree != expected_tree:
        raise Wave1LiveRunnerError("caller Git IDs differ from the checked-out implementation")
    if status:
        raise Wave1LiveRunnerError("implementation worktree is not clean")
    core: dict[str, object] = {
        "schema_version": 1,
        "worktree": str(resolved_worktree),
        "implementation_commit": observed_commit,
        "implementation_tree": observed_tree,
        "status_porcelain_sha256": sha256_hex(status.encode("utf-8")),
        "worktree_clean": True,
        "verified_before_resource_claim": True,
    }
    return {**core, "verification_hash": hash_canonical(core)}


def _validate_git_identity_receipt(
    payload: Mapping[str, object],
    *,
    worktree: Path,
    implementation_commit: str,
    implementation_tree: str,
) -> dict[str, object]:
    expected_keys = {
        "schema_version",
        "worktree",
        "implementation_commit",
        "implementation_tree",
        "status_porcelain_sha256",
        "worktree_clean",
        "verified_before_resource_claim",
        "verification_hash",
    }
    core = dict(payload)
    observed_hash = core.pop("verification_hash", None)
    if (
        set(payload) != expected_keys
        or payload.get("schema_version") != 1
        or payload.get("worktree") != str(worktree.resolve())
        or payload.get("implementation_commit") != implementation_commit
        or payload.get("implementation_tree") != implementation_tree
        or payload.get("status_porcelain_sha256") != sha256_hex(b"")
        or payload.get("worktree_clean") is not True
        or payload.get("verified_before_resource_claim") is not True
        or observed_hash != hash_canonical(core)
    ):
        raise Wave1LiveRunnerError("Git implementation identity receipt drift")
    return dict(payload)


def _positive_case_ids() -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (project_id, operation_id, fixture_kind)
        for project_id in EXPECTED_PROJECT_IDS
        for operation_id in AUTHORIZED_POSITIVE_OPERATION_IDS
        for fixture_kind in ("success", "adversarial_rejection")
    )


_RUN_SPEC_FIELDS = {
    "schema_version",
    "run_kind",
    "stage",
    "implementation_commit",
    "implementation_tree",
    "implementation_identity_receipt",
    "implementation_identity_receipt_hash",
    "worktree",
    "storage_root",
    "runtime_config_path",
    "runtime_config_file_sha256",
    "runtime_config_hash",
    "runtime_fixture_path",
    "runtime_fixture_file_sha256",
    "runtime_fixture_hash",
    "runtime_loader_file_sha256",
    "live_runner_file_sha256",
    "assembled_preamble_sha256",
    "positive_checkpoint_receipt_hash",
    "backend_bindings",
    "server_mode",
    "persistent_worker_count",
    "maximum_concurrent_worker_count",
    "memory_hard_limit_mb",
    "maximum_combined_measured_rss_bytes",
    "request_timeout_seconds",
    "elab_async",
    "per_row_process_spawned",
    "corpus_compiled",
    "positive_case_order",
    "n31_project_order",
    "n31_runtime_activation_authorized",
    "n31_semantic_conformance_authorized",
    "wave1_gate_authorized",
    "row_emission_authorized",
    "resume_command",
    "terminal_stop",
}


def _run_spec_payload_fields() -> frozenset[str]:
    return frozenset(_RUN_SPEC_FIELDS)


def _run_spec_payload(
    loaded: LoadedWave1LiveReadiness,
    *,
    stage: Literal["positive_checkpoint", "n31_resolution_proposal"],
    assembled_preamble: str,
    implementation_commit: str,
    implementation_tree: str,
    worktree: Path,
    storage_root: Path,
    resume_command: str,
    implementation_identity_receipt: Mapping[str, object],
    positive_checkpoint_receipt_hash: str | None = None,
) -> dict[str, object]:
    _validate_execution_contract(loaded)
    _git_object_id(implementation_commit, "implementation commit")
    _git_object_id(implementation_tree, "implementation tree")
    identity = _validate_git_identity_receipt(
        implementation_identity_receipt,
        worktree=worktree,
        implementation_commit=implementation_commit,
        implementation_tree=implementation_tree,
    )
    if not resume_command.strip() or "\n" in resume_command or "\r" in resume_command:
        raise Wave1LiveRunnerError("resume command must be one nonempty line")
    preamble_sha = sha256_hex(assembled_preamble.encode("utf-8"))
    if preamble_sha != loaded.config.preamble_contract.assembled_preamble_sha256:
        raise Wave1LiveRunnerError("assembled preamble hash drift")
    if stage == "n31_resolution_proposal":
        if (
            positive_checkpoint_receipt_hash is None
            or re.fullmatch(r"[0-9a-f]{64}", positive_checkpoint_receipt_hash) is None
        ):
            raise Wave1LiveRunnerError("N31 stage lacks the positive checkpoint receipt hash")
    elif positive_checkpoint_receipt_hash is not None:
        raise Wave1LiveRunnerError("positive stage cannot bind a future N31 prerequisite")
    backend_bindings = [item.model_dump(mode="json") for item in loaded.config.backend_projects]
    return {
        "schema_version": 1,
        "run_kind": "sft1_wave1_implementation_readiness_v0_3_6",
        "stage": stage,
        "implementation_commit": implementation_commit,
        "implementation_tree": implementation_tree,
        "implementation_identity_receipt": identity,
        "implementation_identity_receipt_hash": identity["verification_hash"],
        "worktree": str(worktree.resolve()),
        "storage_root": str(storage_root.resolve()),
        "runtime_config_path": str(loaded.config_path.resolve()),
        "runtime_config_file_sha256": loaded.config_file_sha256,
        "runtime_config_hash": loaded.config_hash,
        "runtime_fixture_path": str(loaded.fixture_path.resolve()),
        "runtime_fixture_file_sha256": loaded.fixture_file_sha256,
        "runtime_fixture_hash": loaded.fixture_hash,
        "runtime_loader_file_sha256": hash_file(
            Path(__file__).with_name("wave1_live_readiness.py")
        ),
        "live_runner_file_sha256": runner_implementation_hash(),
        "assembled_preamble_sha256": preamble_sha,
        "positive_checkpoint_receipt_hash": positive_checkpoint_receipt_hash,
        "backend_bindings": backend_bindings,
        "server_mode": "stable",
        "persistent_worker_count": 1,
        "maximum_concurrent_worker_count": 2,
        "memory_hard_limit_mb": 24576,
        "maximum_combined_measured_rss_bytes": 40 * 1024**3,
        "request_timeout_seconds": 300,
        "elab_async": False,
        "per_row_process_spawned": False,
        "corpus_compiled": False,
        "positive_case_order": [list(item) for item in _positive_case_ids()],
        "n31_project_order": list(EXPECTED_PROJECT_IDS),
        "n31_runtime_activation_authorized": False,
        "n31_semantic_conformance_authorized": False,
        "wave1_gate_authorized": False,
        "row_emission_authorized": False,
        "resume_command": resume_command,
        "terminal_stop": (
            "positive_checkpoint_only"
            if stage == "positive_checkpoint"
            else "stopped_for_exact_n31_user_admission"
        ),
    }


def _install_run_spec(root: Path, payload: Mapping[str, object]) -> tuple[Path, str]:
    core = dict(payload)
    spec_hash = hash_canonical(core)
    path = root / "run_specs" / f"{core['stage']}.json"
    install_immutable_json(path, {**core, "run_spec_hash": spec_hash})
    return path, spec_hash


def _backend_settings_for_project(
    loaded: LoadedWave1LiveReadiness,
    *,
    project_id: str,
    assembled_preamble: str,
    raw_response_dir: Path,
) -> BackendSettings:
    project = _project_for(loaded, project_id)
    context = build_fixture_compile_context(project, assembled_preamble=assembled_preamble)
    matches = tuple(
        item for item in loaded.config.backend_projects if project_id in item.source_project_ids
    )
    if len(matches) != 1:
        raise Wave1LiveRunnerError("project did not resolve to exactly one backend binding")
    binding = matches[0]
    if (
        binding.project_revision != project.compile_project_revision
        or binding.lean_version != project.lean_version
    ):
        raise Wave1LiveRunnerError("backend/fixture project identity drift")
    return BackendSettings(
        project_dir=Path(binding.project_dir),
        context_fingerprint=context.fingerprint,
        environment_schema_version=(
            loaded.config.lean_environment_contract.environment_schema_version
        ),
        raw_response_dir=raw_response_dir,
        server_mode=ServerMode.STABLE,
        workers=None,
        memory_hard_limit_mb=24576,
        enable_incremental_optimization=True,
        enable_parallel_elaboration=False,
        isolate_incremental_commands=False,
        confirm_invalid_on_fresh_process=False,
        environment_is_prepared=False,
    )


def _case_completion_payload(
    *,
    run_spec_hash: str,
    project_id: str,
    operation_id: str,
    fixture_kind: str,
    request_hashes: Sequence[str],
    task_receipt: Mapping[str, object],
    artifact_hashes: Mapping[str, str],
    symbol_resolution: PositiveSymbolResolutionEvidence,
    typed_replay_performed: bool,
    cache_write_and_readback_replayed: bool,
    elapsed_ms: int,
    reference_complete_sidecar_path: Path | None,
    candidate_complete_sidecar_path: Path | None,
    positive_replay: PositiveEvidenceReplay | None,
    p01_runtime_replay: P01RuntimeReplayEvidence | None,
) -> dict[str, object]:
    measured_elapsed_ms = max(1, elapsed_ms)
    reference_path = (
        str(reference_complete_sidecar_path.resolve())
        if reference_complete_sidecar_path is not None
        else None
    )
    candidate_path = (
        str(candidate_complete_sidecar_path.resolve())
        if candidate_complete_sidecar_path is not None
        else None
    )
    core = {
        "schema_version": 1,
        "run_spec_hash": run_spec_hash,
        "project_id": project_id,
        "operation_id": operation_id,
        "fixture_kind": fixture_kind,
        "case_id": f"{project_id}.{operation_id}.{fixture_kind}",
        "request_hashes": list(request_hashes),
        "task_receipt": dict(task_receipt),
        "task_receipt_hash": hash_canonical(task_receipt),
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "symbol_resolution_receipt_hash": symbol_resolution.task_receipt_hash,
        "symbol_resolution_receipt_path": str(symbol_resolution.task_receipt_path.resolve()),
        "symbol_resolution_receipt_file_sha256": (symbol_resolution.task_receipt_file_sha256),
        "symbol_resolution_raw_response_path": str(symbol_resolution.raw_response_path.resolve()),
        "symbol_resolution_raw_response_sha256": (symbol_resolution.raw_response_sha256),
        "symbol_resolution_request_hash": symbol_resolution.request_hash,
        "typed_replay_performed": typed_replay_performed,
        "cache_write_and_readback_replayed": cache_write_and_readback_replayed,
        "runtime_chain": (
            positive_replay.chain.model_dump(mode="json") if positive_replay is not None else None
        ),
        "runtime_chain_hash": (
            hash_canonical(positive_replay.chain.model_dump(mode="json"))
            if positive_replay is not None
            else None
        ),
        "typed_replay_path": (
            str(positive_replay.typed_replay_path.resolve())
            if positive_replay is not None
            else None
        ),
        "typed_replay_file_sha256": (
            positive_replay.typed_replay_sha256 if positive_replay is not None else None
        ),
        "raw_response_path": (
            str(positive_replay.raw_response_path.resolve())
            if positive_replay is not None
            else None
        ),
        "raw_response_file_sha256": (
            positive_replay.raw_response_sha256 if positive_replay is not None else None
        ),
        "wave1_cache_key": (
            positive_replay.wave1_cache_key.model_dump(mode="json")
            if positive_replay is not None
            else None
        ),
        "wave1_cache_key_hash": (
            positive_replay.wave1_cache_key_hash if positive_replay is not None else None
        ),
        "central_cache_key_hash": (
            positive_replay.central_cache_key_hash if positive_replay is not None else None
        ),
        "central_cache_entry_path": (
            str(positive_replay.central_cache_entry_path.resolve())
            if positive_replay is not None
            else None
        ),
        "central_cache_entry_file_sha256": (
            positive_replay.central_cache_entry_sha256 if positive_replay is not None else None
        ),
        "elapsed_ms": measured_elapsed_ms,
        "measured_lean_seconds": measured_elapsed_ms / 1000,
        "reference_complete_sidecar_path": reference_path,
        "reference_complete_sidecar_bytes": (
            reference_complete_sidecar_path.stat().st_size
            if reference_complete_sidecar_path is not None
            else None
        ),
        "reference_complete_sidecar_sha256": (
            hash_file(reference_complete_sidecar_path)
            if reference_complete_sidecar_path is not None
            else None
        ),
        "candidate_complete_sidecar_path": candidate_path,
        "candidate_complete_sidecar_bytes": (
            candidate_complete_sidecar_path.stat().st_size
            if candidate_complete_sidecar_path is not None
            else None
        ),
        "candidate_complete_sidecar_sha256": (
            hash_file(candidate_complete_sidecar_path)
            if candidate_complete_sidecar_path is not None
            else None
        ),
        "p01_runtime_replay_path": (
            str(p01_runtime_replay.receipt_path.resolve())
            if p01_runtime_replay is not None
            else None
        ),
        "p01_runtime_replay_file_sha256": (
            p01_runtime_replay.receipt_file_sha256 if p01_runtime_replay is not None else None
        ),
        "p01_runtime_replay_receipt_hash": (
            p01_runtime_replay.receipt_hash if p01_runtime_replay is not None else None
        ),
        "n31_activation_performed": False,
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
    }
    return {**core, "completion_hash": hash_canonical(core)}


def _task_owned_artifact_path(raw_path: object, *, storage_root: Path, artifact_kind: str) -> Path:
    if not isinstance(raw_path, str):
        raise Wave1LiveRunnerError(f"{artifact_kind} path is not a string")
    path = Path(raw_path)
    resolved = path.resolve()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or not resolved.is_relative_to(storage_root.resolve())
    ):
        raise Wave1LiveRunnerError(f"{artifact_kind} escaped its task-owned evidence root")
    return resolved


def _read_canonical_json_object(path: Path, *, artifact_kind: str) -> dict[str, object]:
    payload = path.read_bytes()
    value = _read_json_object(path)
    if payload != canonical_json_bytes(value) + b"\n":
        raise Wave1LiveRunnerError(f"{artifact_kind} is not canonical JSON")
    return value


def _require_exact_mapping(
    value: object, *, keys: set[str], artifact_kind: str
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise Wave1LiveRunnerError(f"{artifact_kind} field inventory drift")
    return cast(dict[str, object], value)


def _closed_expr_sidecar_from_artifact(
    path: Path,
    *,
    endpoint_role: Literal["reference", "candidate"],
    artifact_kind: str,
) -> ClosedExprSidecar:
    payload = _read_canonical_json_object(path, artifact_kind=artifact_kind)
    _require_exact_mapping(
        payload,
        keys={"record", "source_material", "compile_context"},
        artifact_kind=artifact_kind,
    )
    record = _require_exact_mapping(
        payload["record"],
        keys={
            "representation_id",
            "goal_v1",
            "goal_v1_source",
            "renderer_version",
            "spec_hash",
            "compile_context_id",
            "endpoint_id",
            "endpoint_role",
            "source_material_hash",
            "rendered_goal_hash",
            "provenance",
            "implementation_identity",
            "typed_alpha_fingerprint",
            "warnings",
        },
        artifact_kind=f"{artifact_kind} record",
    )
    provenance = _require_exact_mapping(
        record["provenance"],
        keys={
            "expr_hash",
            "expr_hash_algorithm",
            "input_level_params",
            "canonical_level_params",
            "universe_profile_id",
            "universe_profile_hash",
            "render_scope_id",
            "render_context_id",
            "render_context_hash",
            "route_id",
            "expr_origin",
        },
        artifact_kind=f"{artifact_kind} provenance",
    )
    implementation = _require_exact_mapping(
        record["implementation_identity"],
        keys={
            "renderer_semantic_hash",
            "lean_renderer_sha256",
            "injected_helper_sha256",
            "python_module_sha256",
            "config_file_sha256",
            "implementation_set_hash",
        },
        artifact_kind=f"{artifact_kind} implementation identity",
    )
    source = _require_exact_mapping(
        payload["source_material"],
        keys={"kind", "raw_statement", "proposition_text", "absence_reason"},
        artifact_kind=f"{artifact_kind} source material",
    )
    context = _require_exact_mapping(
        payload["compile_context"],
        keys={
            "schema_version",
            "project_id",
            "project_revision",
            "lean_version",
            "import_header",
            "command_preamble",
            "namespace_context",
            "open_context",
            "scoped_context",
            "options",
        },
        artifact_kind=f"{artifact_kind} compile context",
    )
    list_fields = (
        provenance.get("input_level_params"),
        provenance.get("canonical_level_params"),
        record.get("warnings"),
        context.get("namespace_context"),
        context.get("open_context"),
        context.get("scoped_context"),
    )
    if any(
        not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        for value in list_fields
    ):
        raise Wave1LiveRunnerError(f"{artifact_kind} string-list field drift")
    if context.get("schema_version") != 1 or not isinstance(context.get("options"), dict):
        raise Wave1LiveRunnerError(f"{artifact_kind} compile-context schema drift")
    try:
        compile_context = CompileContext(
            project_id=cast(str, context["project_id"]),
            project_revision=cast(str, context["project_revision"]),
            lean_version=cast(str, context["lean_version"]),
            import_header=cast(str, context["import_header"]),
            command_preamble=cast(str, context["command_preamble"]),
            namespace_context=tuple(cast(list[str], context["namespace_context"])),
            open_context=tuple(cast(list[str], context["open_context"])),
            scoped_context=tuple(cast(list[str], context["scoped_context"])),
            options=cast(dict[str, str | int | float | bool], context["options"]),
        )
        source_material = ClosedExprSourceMaterial(
            kind=cast(
                Literal[
                    "raw_statement",
                    "proposition_text",
                    "constructed_expr_no_source_text",
                ],
                source["kind"],
            ),
            raw_statement=cast(str | None, source["raw_statement"]),
            proposition_text=cast(str | None, source["proposition_text"]),
            absence_reason=cast(str | None, source["absence_reason"]),
        )
        implementation_identity = RendererImplementationIdentity(
            renderer_semantic_hash=cast(str, implementation["renderer_semantic_hash"]),
            lean_renderer_sha256=cast(str, implementation["lean_renderer_sha256"]),
            injected_helper_sha256=cast(str, implementation["injected_helper_sha256"]),
            python_module_sha256=cast(str, implementation["python_module_sha256"]),
            config_file_sha256=cast(str, implementation["config_file_sha256"]),
            implementation_set_hash=cast(str, implementation["implementation_set_hash"]),
        )
        closed_provenance = ClosedExprProvenance(
            expr_hash=cast(str, provenance["expr_hash"]),
            expr_hash_algorithm=cast(str, provenance["expr_hash_algorithm"]),
            input_level_params=tuple(cast(list[str], provenance["input_level_params"])),
            canonical_level_params=tuple(cast(list[str], provenance["canonical_level_params"])),
            universe_profile_id=cast(str, provenance["universe_profile_id"]),
            universe_profile_hash=cast(str, provenance["universe_profile_hash"]),
            render_scope_id=cast(str, provenance["render_scope_id"]),
            render_context_id=cast(str, provenance["render_context_id"]),
            render_context_hash=cast(str, provenance["render_context_hash"]),
            route_id=cast(str, provenance["route_id"]),
            expr_origin=cast(
                Literal[
                    "loaded_constant_type",
                    "term_elaborated_proposition",
                    "sft1_transformed_expr",
                ],
                provenance["expr_origin"],
            ),
        )
        closed_record = ClosedExprRecord(
            representation_id=cast(str, record["representation_id"]),
            goal_v1=cast(str, record["goal_v1"]),
            goal_v1_source=cast(Literal["closed_prop_expr"], record["goal_v1_source"]),
            renderer_version=cast(str, record["renderer_version"]),
            spec_hash=cast(str, record["spec_hash"]),
            compile_context_id=cast(str, record["compile_context_id"]),
            endpoint_id=cast(str, record["endpoint_id"]),
            endpoint_role=cast(Literal["reference", "candidate"], record["endpoint_role"]),
            source_material_hash=cast(str, record["source_material_hash"]),
            rendered_goal_hash=cast(str, record["rendered_goal_hash"]),
            provenance=closed_provenance,
            implementation_identity=implementation_identity,
            typed_alpha_fingerprint=cast(str | None, record["typed_alpha_fingerprint"]),
            warnings=tuple(cast(list[str], record["warnings"])),
        )
        sidecar = ClosedExprSidecar(
            record=closed_record,
            source_material=source_material,
            compile_context=compile_context,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Wave1LiveRunnerError(f"{artifact_kind} failed typed reconstruction") from exc
    text = sidecar.core_text()
    if (
        sidecar.record.endpoint_role != endpoint_role
        or sidecar.record.goal_v1_source != "closed_prop_expr"
        or sidecar.record.provenance.route_id != "closed_expr_in_session"
        or sidecar.record.rendered_goal_hash != sha256_hex(text.encode("utf-8"))
        or text.count("⊢") != 1
        or "[anonymous]" in text
        or "⋯" in text
    ):
        raise Wave1LiveRunnerError(f"{artifact_kind} closed-Expr renderer contract drift")
    representation_payload = {
        "renderer_version": sidecar.record.renderer_version,
        "spec_hash": sidecar.record.spec_hash,
        "goal_v1_source": sidecar.record.goal_v1_source,
        "goal_v1": sidecar.record.goal_v1,
        "rendered_goal_hash": sidecar.record.rendered_goal_hash,
        "endpoint_id": sidecar.record.endpoint_id,
        "endpoint_role": sidecar.record.endpoint_role,
        "source_material_hash": sidecar.record.source_material_hash,
        "compile_context_id": sidecar.record.compile_context_id,
        "provenance": sidecar.record.provenance.to_dict(),
        "implementation_identity": sidecar.record.implementation_identity.to_dict(),
    }
    if sidecar.record.representation_id != "repr:" + hash_canonical(representation_payload):
        raise Wave1LiveRunnerError(f"{artifact_kind} representation identity drift")
    return sidecar


def _validate_positive_success_artifact_closure(
    payload: Mapping[str, object],
    *,
    loaded: LoadedWave1LiveReadiness,
    storage_root: Path,
    project_id: str,
    operation_id: str,
    task_receipt: Mapping[str, object],
    artifacts: Mapping[str, object],
) -> None:
    def bound_path(path_field: str, hash_field: str, kind: str) -> Path:
        path = _task_owned_artifact_path(
            payload.get(path_field), storage_root=storage_root, artifact_kind=kind
        )
        expected_hash = payload.get(hash_field)
        if expected_hash != hash_file(path) or artifacts.get(str(path)) != expected_hash:
            raise Wave1LiveRunnerError(f"{kind} explicit/content inventory binding drift")
        return path

    typed_path = bound_path(
        "typed_replay_path", "typed_replay_file_sha256", "positive typed replay"
    )
    raw_path = bound_path(
        "raw_response_path", "raw_response_file_sha256", "positive raw response"
    )
    reference_path = bound_path(
        "reference_complete_sidecar_path",
        "reference_complete_sidecar_sha256",
        "positive reference complete sidecar",
    )
    candidate_path = bound_path(
        "candidate_complete_sidecar_path",
        "candidate_complete_sidecar_sha256",
        "positive candidate complete sidecar",
    )
    cache_path = bound_path(
        "central_cache_entry_path",
        "central_cache_entry_file_sha256",
        "positive central-cache entry",
    )
    typed_bytes = typed_path.read_bytes()
    try:
        certificate = TypedCertificateReceipt.model_validate_json(typed_bytes)
    except ValueError as exc:
        raise Wave1LiveRunnerError("positive typed replay artifact is invalid") from exc
    if typed_bytes != canonical_json_bytes(certificate.model_dump(mode="json")) + b"\n":
        raise Wave1LiveRunnerError("positive typed replay artifact is not canonical JSON")
    try:
        chain = RuntimeChain.model_validate(payload.get("runtime_chain"))
        validation = validate_runtime_chain(chain)
        wave1_key = Wave1CacheKey.model_validate(payload.get("wave1_cache_key"))
    except ValueError as exc:
        raise Wave1LiveRunnerError("positive runtime-chain/cache-key replay failed") from exc
    if (
        len(chain.edges) != 1
        or chain.edges[0].certificate != certificate
        or chain.edges[0].certificate_payload_hash
        != hash_canonical(certificate.model_dump(mode="json"))
        or payload.get("runtime_chain_hash")
        != hash_canonical(chain.model_dump(mode="json"))
        or chain.polarity != "positive"
        or chain.label != 1
        or (operation_id == "P01_ALPHA_RENAME_SINGLE_V1")
        != (validation.p01_present and validation.exception_used)
    ):
        raise Wave1LiveRunnerError("positive runtime-chain/certificate cross-binding drift")
    reference = _closed_expr_sidecar_from_artifact(
        reference_path,
        endpoint_role="reference",
        artifact_kind="positive reference complete sidecar",
    )
    candidate = _closed_expr_sidecar_from_artifact(
        candidate_path,
        endpoint_role="candidate",
        artifact_kind="positive candidate complete sidecar",
    )
    expected_context = build_fixture_compile_context(
        _project_for(loaded, project_id),
        assembled_preamble=reference.compile_context.command_preamble,
    )
    receipt_id = cast(str, payload["case_id"])
    if (
        reference.record.endpoint_id != f"{receipt_id}.reference"
        or candidate.record.endpoint_id != f"{receipt_id}.candidate"
        or reference.record.provenance.expr_origin != "term_elaborated_proposition"
        or candidate.record.provenance.expr_origin != "sft1_transformed_expr"
        or reference.compile_context != candidate.compile_context
        or reference.compile_context != expected_context
        or sha256_hex(reference.compile_context.command_preamble.encode("utf-8"))
        != loaded.config.preamble_contract.assembled_preamble_sha256
    ):
        raise Wave1LiveRunnerError("positive complete-sidecar endpoint/context binding drift")
    pair = RenderedWave1Pair(
        reference=reference,
        candidate=candidate,
        request_hash=certificate.render_request_hash,
        elapsed_ms=cast(int, payload["elapsed_ms"]),
        raw_response_path=str(raw_path),
        render_scope_id=reference.record.provenance.render_scope_id,
        reference_sidecar_sha256=cast(str, payload["reference_complete_sidecar_sha256"]),
        candidate_sidecar_sha256=cast(str, payload["candidate_complete_sidecar_sha256"]),
    )
    if runtime_endpoints_from_pair(pair) != chain.endpoints:
        raise Wave1LiveRunnerError("positive runtime endpoints differ from complete sidecars")
    operation = next(item for item in loaded.config.operations if item.operation_id == operation_id)
    if (
        compute_wave1_cache_key_hash(wave1_key) != payload.get("wave1_cache_key_hash")
        or wave1_key.operation_id != operation_id
        or wave1_key.operation_registry_entry_hash != operation.registry_entry_hash
        or wave1_key.schema_lemma_procedure_hash != operation.anchor_hash
        or wave1_key.evidence_certificate_payload_hash != chain.edges[0].certificate_payload_hash
        or wave1_key.source_closed_expr_hash != chain.endpoints[0].closed_expr_hash
        or wave1_key.candidate_closed_expr_hash != chain.endpoints[1].closed_expr_hash
        or wave1_key.synthesized_instance_hashes != synthesized_instance_hashes(task_receipt)
        or wave1_key.policy_config_hash != loaded.config_hash
        or wave1_key.environment_fingerprint_hash != reference.compile_context.fingerprint
    ):
        raise Wave1LiveRunnerError("positive Wave1 cache-key semantic binding drift")
    environment = loaded.config.lean_environment_contract
    binding = bind_wave1_central_cache_key(
        wave1_key,
        pair=pair,
        environment_schema_version=environment.environment_schema_version,
        lean_interact_version=environment.lean_interact_version,
        repl_revision=environment.repl_revision,
        timeout_seconds=float(environment.timeout_seconds_per_request),
    )
    if (
        binding.wave1_key_hash != payload.get("wave1_cache_key_hash")
        or payload.get("central_cache_key_hash") != cache_path.stem
        or cache_path.parent.name != cache_path.stem[:2]
        or cache_path.parent.parent.name != "v1"
    ):
        raise Wave1LiveRunnerError("positive central-cache path/key binding drift")
    cache_root = cache_path.parents[2]
    central_cache = EvidenceCache(cache_root, artifact_root=Path("/"))
    expected_cache_path = central_cache.entry_path(binding.central_key).resolve()
    if expected_cache_path != cache_path:
        raise Wave1LiveRunnerError("positive central-cache entry path is swappable")
    replayed = Wave1CentralCacheAdapter(central_cache).get_after_replay(
        binding,
        replay_receipt=certificate,
        replay_artifact_sha256=cast(str, payload["typed_replay_file_sha256"]),
    )
    if (
        replayed is None
        or replayed.cache_key_hash != payload.get("central_cache_key_hash")
        or hash_file(cache_path) != payload.get("central_cache_entry_file_sha256")
        or binding.raw_response_path != str(raw_path)
    ):
        raise Wave1LiveRunnerError("positive central-cache immutable readback drift")


def _validate_case_completion(
    payload: object,
    *,
    loaded: LoadedWave1LiveReadiness,
    run_spec_hash: str,
    expected_identity: tuple[str, str, str],
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise Wave1LiveRunnerError("positive case completion is not an object")
    try:
        payload = PositiveCheckpointCaseReceipt.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise Wave1LiveRunnerError("positive case completion typed schema drift") from exc
    expected_keys = {
        "schema_version",
        "run_spec_hash",
        "project_id",
        "operation_id",
        "fixture_kind",
        "case_id",
        "request_hashes",
        "task_receipt",
        "task_receipt_hash",
        "artifact_hashes",
        "symbol_resolution_receipt_hash",
        "symbol_resolution_receipt_path",
        "symbol_resolution_receipt_file_sha256",
        "symbol_resolution_raw_response_path",
        "symbol_resolution_raw_response_sha256",
        "symbol_resolution_request_hash",
        "typed_replay_performed",
        "cache_write_and_readback_replayed",
        "runtime_chain",
        "runtime_chain_hash",
        "typed_replay_path",
        "typed_replay_file_sha256",
        "raw_response_path",
        "raw_response_file_sha256",
        "wave1_cache_key",
        "wave1_cache_key_hash",
        "central_cache_key_hash",
        "central_cache_entry_path",
        "central_cache_entry_file_sha256",
        "elapsed_ms",
        "measured_lean_seconds",
        "reference_complete_sidecar_path",
        "reference_complete_sidecar_bytes",
        "reference_complete_sidecar_sha256",
        "candidate_complete_sidecar_path",
        "candidate_complete_sidecar_bytes",
        "candidate_complete_sidecar_sha256",
        "p01_runtime_replay_path",
        "p01_runtime_replay_file_sha256",
        "p01_runtime_replay_receipt_hash",
        "n31_activation_performed",
        "wave1_gate_executed",
        "model_facing_rows_emitted",
        "completion_hash",
    }
    if set(payload) != expected_keys:
        raise Wave1LiveRunnerError("positive case completion field inventory drift")
    core = dict(payload)
    observed_hash = core.pop("completion_hash")
    project_id, operation_id, fixture_kind = expected_identity
    storage_root = Path(loaded.config.persistence_contract.root).resolve()
    if (
        payload.get("schema_version") != 1
        or payload.get("run_spec_hash") != run_spec_hash
        or (
            payload.get("project_id"),
            payload.get("operation_id"),
            payload.get("fixture_kind"),
        )
        != expected_identity
        or payload.get("case_id") != f"{project_id}.{operation_id}.{fixture_kind}"
        or observed_hash != hash_canonical(core)
        or payload.get("n31_activation_performed") is not False
        or payload.get("wave1_gate_executed") is not False
        or payload.get("model_facing_rows_emitted") is not False
        or not isinstance(payload.get("elapsed_ms"), int)
        or cast(int, payload.get("elapsed_ms")) <= 0
        or payload.get("measured_lean_seconds") != cast(int, payload.get("elapsed_ms")) / 1000
    ):
        raise Wave1LiveRunnerError("positive case completion identity/hash drift")
    task_receipt = payload.get("task_receipt")
    if not isinstance(task_receipt, dict) or payload.get("task_receipt_hash") != hash_canonical(
        task_receipt
    ):
        raise Wave1LiveRunnerError("positive case task receipt replay failed")
    if fixture_kind == "success":
        validate_positive_success_task_receipt(
            task_receipt,
            receipt_id=f"{project_id}.{operation_id}.success",
            operation_id=operation_id,
        )
        if (
            payload.get("typed_replay_performed") is not True
            or payload.get("cache_write_and_readback_replayed") is not True
        ):
            raise Wave1LiveRunnerError("positive success lost typed/cache replay")
        for path_key, size_key in (
            ("reference_complete_sidecar_path", "reference_complete_sidecar_bytes"),
            ("candidate_complete_sidecar_path", "candidate_complete_sidecar_bytes"),
        ):
            raw_path = payload.get(path_key)
            observed_size = payload.get(size_key)
            if (
                not isinstance(raw_path, str)
                or not isinstance(observed_size, int)
                or observed_size <= 0
            ):
                raise Wave1LiveRunnerError("positive success lost sidecar byte measurement")
            sidecar_path = _task_owned_artifact_path(
                raw_path, storage_root=storage_root, artifact_kind="positive sidecar"
            )
            if sidecar_path.stat().st_size != observed_size:
                raise Wave1LiveRunnerError("positive success sidecar byte replay failed")
        if operation_id == "P01_ALPHA_RENAME_SINGLE_V1":
            p01_path_value = payload.get("p01_runtime_replay_path")
            if not isinstance(p01_path_value, str):
                raise Wave1LiveRunnerError("P01 success lost runtime-contract replay")
            p01_path = _task_owned_artifact_path(
                p01_path_value,
                storage_root=storage_root,
                artifact_kind="P01 runtime replay",
            )
            p01_receipt = validate_p01_runtime_replay_receipt(_read_json_object(p01_path))
            cap_contract = p01_receipt.get("cap_contract")
            durable_scope = p01_receipt.get("durable_readiness_retention_scope")
            if (
                payload.get("p01_runtime_replay_file_sha256") != hash_file(p01_path)
                or payload.get("p01_runtime_replay_receipt_hash")
                != p01_receipt.get("receipt_hash")
                or not isinstance(cap_contract, dict)
                or cap_contract.get("complete_retention_scope_executed") is not True
                or not isinstance(durable_scope, dict)
            ):
                raise Wave1LiveRunnerError("P01 runtime-contract artifact binding drift")
            for field_name in ("manifest_path", "journal_path"):
                _task_owned_artifact_path(
                    durable_scope.get(field_name),
                    storage_root=storage_root,
                    artifact_kind=f"P01 retention {field_name}",
                )
        elif any(
            payload.get(key) is not None
            for key in (
                "p01_runtime_replay_path",
                "p01_runtime_replay_file_sha256",
                "p01_runtime_replay_receipt_hash",
            )
        ):
            raise Wave1LiveRunnerError("non-P01 success invented P01 runtime replay")
    else:
        fixture = _fixture_for(
            loaded,
            operation_id,
            "adversarial_rejection",
        )
        if fixture.expected_engine_reason is None:
            raise Wave1LiveRunnerError("rejection fixture lost its reason")
        validate_positive_rejection_task_receipt(
            task_receipt,
            receipt_id=f"{project_id}.{operation_id}.adversarial_rejection",
            operation_id=operation_id,
            expected_reason=fixture.expected_engine_reason,
        )
        if (
            payload.get("typed_replay_performed") is not False
            or payload.get("cache_write_and_readback_replayed") is not False
            or payload.get("reference_complete_sidecar_path") is not None
            or payload.get("reference_complete_sidecar_bytes") is not None
            or payload.get("candidate_complete_sidecar_path") is not None
            or payload.get("candidate_complete_sidecar_bytes") is not None
            or payload.get("p01_runtime_replay_path") is not None
            or payload.get("p01_runtime_replay_file_sha256") is not None
            or payload.get("p01_runtime_replay_receipt_hash") is not None
            or payload.get("runtime_chain") is not None
            or payload.get("runtime_chain_hash") is not None
            or payload.get("typed_replay_path") is not None
            or payload.get("typed_replay_file_sha256") is not None
            or payload.get("raw_response_path") is not None
            or payload.get("raw_response_file_sha256") is not None
            or payload.get("wave1_cache_key") is not None
            or payload.get("wave1_cache_key_hash") is not None
            or payload.get("central_cache_key_hash") is not None
            or payload.get("central_cache_entry_path") is not None
            or payload.get("central_cache_entry_file_sha256") is not None
            or payload.get("reference_complete_sidecar_sha256") is not None
            or payload.get("candidate_complete_sidecar_sha256") is not None
        ):
            raise Wave1LiveRunnerError("positive rejection invented typed/cache replay")
    artifacts = payload.get("artifact_hashes")
    if not isinstance(artifacts, dict) or not artifacts:
        raise Wave1LiveRunnerError("positive case completion has no artifacts")
    for raw_path, expected_hash in artifacts.items():
        if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
            raise Wave1LiveRunnerError("positive artifact inventory is malformed")
        path = _task_owned_artifact_path(
            raw_path, storage_root=storage_root, artifact_kind="positive completion artifact"
        )
        if hash_file(path) != expected_hash:
            raise Wave1LiveRunnerError("positive completion artifact replay failed")
    if fixture_kind == "success":
        _validate_positive_success_artifact_closure(
            payload,
            loaded=loaded,
            storage_root=storage_root,
            project_id=project_id,
            operation_id=operation_id,
            task_receipt=task_receipt,
            artifacts=artifacts,
        )
    symbol_path = _task_owned_artifact_path(
        payload.get("symbol_resolution_receipt_path"),
        storage_root=storage_root,
        artifact_kind="symbol-resolution receipt",
    )
    symbol_raw_path = _task_owned_artifact_path(
        payload.get("symbol_resolution_raw_response_path"),
        storage_root=storage_root,
        artifact_kind="symbol-resolution raw response",
    )
    symbol_receipt = _read_json_object(symbol_path)
    if (
        payload.get("symbol_resolution_receipt_hash") != hash_canonical(symbol_receipt)
        or payload.get("symbol_resolution_receipt_file_sha256") != hash_file(symbol_path)
        or payload.get("symbol_resolution_raw_response_sha256") != hash_file(symbol_raw_path)
        or artifacts.get(str(symbol_path.resolve())) != hash_file(symbol_path)
        or artifacts.get(str(symbol_raw_path.resolve())) != hash_file(symbol_raw_path)
    ):
        raise Wave1LiveRunnerError("positive completion symbol-resolution replay failed")
    validate_positive_symbol_resolution_receipt(
        symbol_receipt,
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=f"{project_id}.{operation_id}.symbols",
    )
    return cast(dict[str, object], payload)


def _read_json_object(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise Wave1LiveRunnerError(f"durable JSON artifact is unavailable or unsafe: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_no_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise Wave1LiveRunnerError(f"malformed durable JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise Wave1LiveRunnerError(f"durable JSON artifact is not an object: {path}")
    return cast(dict[str, object], value)


_POSITIVE_CHECKPOINT_RECEIPT_FIELDS = {
    "schema_version",
    "receipt_id",
    "run_spec_hash",
    "run_spec_path",
    "run_spec_file_sha256",
    "runtime_config_file_sha256",
    "runtime_config_hash",
    "runtime_fixture_file_sha256",
    "runtime_fixture_hash",
    "runtime_loader_file_sha256",
    "live_runner_file_sha256",
    "implementation_commit",
    "implementation_tree",
    "implementation_identity_receipt",
    "implementation_identity_receipt_hash",
    "assembled_preamble_sha256",
    "resource_claim_id",
    "resource_claim_snapshot",
    "resource_claim_snapshot_hash",
    "resource_released",
    "persistent_worker_count",
    "measured_combined_peak_rss_bytes",
    "measured_case_lean_milliseconds",
    "measured_symbol_resolution_lean_milliseconds",
    "measured_total_lean_milliseconds",
    "measured_total_lean_seconds",
    "measured_total_complete_sidecar_bytes",
    "p01_runtime_policy_semantic_hash",
    "p01_runtime_source_sha256",
    "p01_live_acceptance_project_count",
    "p01_runtime_replay_receipt_hashes",
    "p01_complete_scope_cap_execution_performed",
    "elab_async",
    "per_row_process_spawned",
    "corpus_compiled",
    "positive_case_count",
    "cases",
    "project_journals",
    "journal_is_durable_log",
    "heartbeat_path",
    "heartbeat_file_sha256",
    "all_positive_cases_completed",
    "all_success_sidecars_typed_replay_and_cache_readback_bound",
    "all_rejections_candidate_free",
    "n31_resolution_started",
    "n31_activation_performed",
    "wave1_gate_executed",
    "model_facing_rows_emitted",
    "terminal_marker_path",
    "terminal_marker_preimage_hash",
    "terminal_status",
    "receipt_hash",
}


def _positive_checkpoint_core_is_valid(payload: Mapping[str, object]) -> bool:
    core = dict(payload)
    observed = core.pop("receipt_hash", None)
    return (
        set(payload) == _POSITIVE_CHECKPOINT_RECEIPT_FIELDS
        and payload.get("schema_version") == 1
        and payload.get("receipt_id") == "sft1_wave1_positive_live_checkpoint_v0_3_6"
        and payload.get("positive_case_count") == 32
        and payload.get("all_positive_cases_completed") is True
        and payload.get("n31_resolution_started") is False
        and payload.get("n31_activation_performed") is False
        and payload.get("wave1_gate_executed") is False
        and payload.get("model_facing_rows_emitted") is False
        and payload.get("resource_released") is True
        and payload.get("terminal_status") == "positive_checkpoint_complete_n31_not_started"
        and observed == hash_canonical(core)
    )


def validate_positive_checkpoint_receipt(
    loaded: LoadedWave1LiveReadiness, payload: Mapping[str, object]
) -> dict[str, object]:
    """Replay the strict 32-case positive-only checkpoint."""

    try:
        typed = PositiveLiveCheckpointReceipt.model_validate(payload)
    except ValidationError as exc:
        raise Wave1LiveRunnerError("positive-only checkpoint typed schema drift") from exc
    payload = typed.model_dump(mode="json")
    if not _positive_checkpoint_core_is_valid(payload):
        raise Wave1LiveRunnerError("positive-only checkpoint core/hash drift")
    run_spec_path_value = payload.get("run_spec_path")
    if not isinstance(run_spec_path_value, str):
        raise Wave1LiveRunnerError("positive-only checkpoint lost its run-spec path")
    storage_root = Path(loaded.config.persistence_contract.root).resolve()
    run_spec_path = _task_owned_artifact_path(
        run_spec_path_value, storage_root=storage_root, artifact_kind="positive run spec"
    )
    run_spec_payload = _read_json_object(run_spec_path)
    run_spec_core = dict(run_spec_payload)
    installed_run_spec_hash = run_spec_core.pop("run_spec_hash", None)
    implementation_commit = payload.get("implementation_commit")
    implementation_tree = payload.get("implementation_tree")
    if (
        set(run_spec_payload) != set(_run_spec_payload_fields()) | {"run_spec_hash"}
        or payload.get("run_spec_file_sha256") != hash_file(run_spec_path)
        or installed_run_spec_hash != hash_canonical(run_spec_core)
        or installed_run_spec_hash != payload.get("run_spec_hash")
        or run_spec_payload.get("stage") != "positive_checkpoint"
        or run_spec_payload.get("storage_root") != str(storage_root)
        or run_spec_payload.get("implementation_commit") != implementation_commit
        or run_spec_payload.get("implementation_tree") != implementation_tree
        or run_spec_payload.get("assembled_preamble_sha256")
        != payload.get("assembled_preamble_sha256")
        or run_spec_payload.get("runtime_config_file_sha256") != loaded.config_file_sha256
        or run_spec_payload.get("runtime_config_hash") != loaded.config_hash
        or run_spec_payload.get("runtime_fixture_file_sha256") != loaded.fixture_file_sha256
        or run_spec_payload.get("runtime_fixture_hash") != loaded.fixture_hash
        or run_spec_payload.get("runtime_loader_file_sha256")
        != hash_file(Path(__file__).with_name("wave1_live_readiness.py"))
        or run_spec_payload.get("live_runner_file_sha256") != runner_implementation_hash()
        or run_spec_payload.get("positive_checkpoint_receipt_hash") is not None
        or run_spec_payload.get("server_mode") != "stable"
        or run_spec_payload.get("persistent_worker_count") != 1
        or run_spec_payload.get("maximum_concurrent_worker_count") != 2
        or run_spec_payload.get("memory_hard_limit_mb") != 24576
        or run_spec_payload.get("maximum_combined_measured_rss_bytes") != 40 * 1024**3
        or run_spec_payload.get("request_timeout_seconds") != 300
        or run_spec_payload.get("elab_async") is not False
        or run_spec_payload.get("per_row_process_spawned") is not False
        or run_spec_payload.get("corpus_compiled") is not False
        or run_spec_payload.get("positive_case_order")
        != [list(item) for item in _positive_case_ids()]
        or run_spec_payload.get("n31_runtime_activation_authorized") is not False
        or run_spec_payload.get("n31_semantic_conformance_authorized") is not False
        or run_spec_payload.get("wave1_gate_authorized") is not False
        or run_spec_payload.get("row_emission_authorized") is not False
        or run_spec_payload.get("terminal_stop") != "positive_checkpoint_only"
    ):
        raise Wave1LiveRunnerError("positive-only checkpoint run-spec replay failed")
    if not isinstance(implementation_commit, str) or not isinstance(implementation_tree, str):
        raise Wave1LiveRunnerError("positive-only checkpoint lost implementation IDs")
    _git_object_id(implementation_commit, "implementation commit")
    _git_object_id(implementation_tree, "implementation tree")
    identity = payload.get("implementation_identity_receipt")
    if not isinstance(identity, dict):
        raise Wave1LiveRunnerError("positive-only checkpoint lost Git identity evidence")
    _validate_git_identity_receipt(
        identity,
        worktree=Path(cast(str, run_spec_payload.get("worktree"))),
        implementation_commit=implementation_commit,
        implementation_tree=implementation_tree,
    )
    resource_snapshot = payload.get("resource_claim_snapshot")
    resource_snapshot_hash = payload.get("resource_claim_snapshot_hash")
    live_runner_binding_sha = next(
        item.file_sha256
        for item in loaded.config.source_bindings
        if item.role == "live_readiness_runner"
    )
    if (
        payload.get("runtime_config_file_sha256") != loaded.config_file_sha256
        or payload.get("runtime_config_hash") != loaded.config_hash
        or payload.get("runtime_fixture_file_sha256") != loaded.fixture_file_sha256
        or payload.get("runtime_fixture_hash") != loaded.fixture_hash
        or payload.get("runtime_loader_file_sha256")
        != hash_file(Path(__file__).with_name("wave1_live_readiness.py"))
        or payload.get("live_runner_file_sha256") != runner_implementation_hash()
        or payload.get("live_runner_file_sha256") != live_runner_binding_sha
        or payload.get("implementation_identity_receipt_hash") != identity.get("verification_hash")
        or payload.get("assembled_preamble_sha256")
        != loaded.config.preamble_contract.assembled_preamble_sha256
        or not isinstance(payload.get("resource_claim_id"), str)
        or not cast(str, payload.get("resource_claim_id")).startswith("SFT1:")
        or not isinstance(payload.get("resource_claim_snapshot"), dict)
        or sha256_hex(
            (
                json.dumps(
                    payload.get("resource_claim_snapshot"),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        != payload.get("resource_claim_snapshot_hash")
        or cast(dict[str, object], resource_snapshot).get("task") != "SFT1"
        or cast(dict[str, object], resource_snapshot).get("lean_workers") != 1
        or cast(dict[str, object], resource_snapshot).get("lean_rss_gib") != 24.0
        or cast(dict[str, object], resource_snapshot).get("gpu") is not False
        or cast(dict[str, object], resource_snapshot).get("worktree")
        != run_spec_payload.get("worktree")
        or payload.get("resource_claim_id") != f"SFT1:{cast(str, resource_snapshot_hash)[:24]}"
        or re.fullmatch(r"[0-9a-f]{64}", cast(str, payload.get("resource_claim_snapshot_hash")))
        is None
        or payload.get("persistent_worker_count") != 1
        or payload.get("elab_async") is not False
        or payload.get("per_row_process_spawned") is not False
        or payload.get("corpus_compiled") is not False
        or payload.get("measured_combined_peak_rss_bytes", 0) <= 0
        or payload.get("measured_combined_peak_rss_bytes", 0) > 40 * 1024**3
        or not isinstance(payload.get("measured_total_lean_milliseconds"), int)
        or cast(int, payload.get("measured_total_lean_milliseconds")) <= 0
        or payload.get("measured_total_lean_seconds")
        != cast(int, payload.get("measured_total_lean_milliseconds")) / 1000
        or payload.get("journal_is_durable_log") is not True
        or payload.get("all_success_sidecars_typed_replay_and_cache_readback_bound") is not True
        or payload.get("all_rejections_candidate_free") is not True
    ):
        raise Wave1LiveRunnerError("positive-only checkpoint execution identity drift")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != 32:
        raise Wave1LiveRunnerError("positive-only checkpoint case count drift")
    observed_order: list[tuple[object, object, object]] = []
    symbol_hashes: dict[tuple[str, str], set[str]] = {}
    run_spec_hash = payload.get("run_spec_hash")
    if not isinstance(run_spec_hash, str):
        raise Wave1LiveRunnerError("positive-only checkpoint lost its run spec")
    for item, expected in zip(cases, _positive_case_ids(), strict=True):
        validated = _validate_case_completion(
            item,
            loaded=loaded,
            run_spec_hash=run_spec_hash,
            expected_identity=expected,
        )
        observed_order.append(
            (
                validated["project_id"],
                validated["operation_id"],
                validated["fixture_kind"],
            )
        )
        symbol_hash = validated.get("symbol_resolution_receipt_hash")
        if not isinstance(symbol_hash, str):
            raise Wave1LiveRunnerError("positive case lost symbol-resolution identity")
        symbol_hashes.setdefault((expected[0], expected[1]), set()).add(symbol_hash)
    if tuple(observed_order) != _positive_case_ids():
        raise Wave1LiveRunnerError("positive-only checkpoint case order drift")
    if len(symbol_hashes) != 16 or any(len(values) != 1 for values in symbol_hashes.values()):
        raise Wave1LiveRunnerError("positive success/rejection symbol-resolution identity diverged")
    measured_case_ms = sum(cast(int, item["elapsed_ms"]) for item in cases)
    measured_sidecar_bytes = sum(
        cast(int, item["reference_complete_sidecar_bytes"])
        + cast(int, item["candidate_complete_sidecar_bytes"])
        for item in cases
        if item["fixture_kind"] == "success"
    )
    symbol_ms = payload.get("measured_symbol_resolution_lean_milliseconds")
    if (
        payload.get("measured_case_lean_milliseconds") != measured_case_ms
        or not isinstance(symbol_ms, int)
        or symbol_ms <= 0
        or payload.get("measured_total_lean_milliseconds") != measured_case_ms + symbol_ms
        or payload.get("measured_total_complete_sidecar_bytes") != measured_sidecar_bytes
        or measured_sidecar_bytes <= 0
    ):
        raise Wave1LiveRunnerError("positive-only checkpoint measurement replay failed")
    p01_hashes = tuple(
        cast(str, item["p01_runtime_replay_receipt_hash"])
        for item in cases
        if item["operation_id"] == "P01_ALPHA_RENAME_SINGLE_V1"
        and item["fixture_kind"] == "success"
    )
    if (
        payload.get("p01_runtime_policy_semantic_hash") != P01_POLICY_SEMANTIC_HASH
        or payload.get("p01_runtime_source_sha256")
        != hash_file(Path(__file__).with_name("wave1_runtime.py"))
        or payload.get("p01_live_acceptance_project_count") != len(EXPECTED_PROJECT_IDS)
        or payload.get("p01_runtime_replay_receipt_hashes") != list(p01_hashes)
        or len(p01_hashes) != len(EXPECTED_PROJECT_IDS)
        or payload.get("p01_complete_scope_cap_execution_performed") is not True
    ):
        raise Wave1LiveRunnerError("positive-only checkpoint P01 runtime replay drift")
    journals = payload.get("project_journals")
    if not isinstance(journals, list) or len(journals) != len(EXPECTED_PROJECT_IDS):
        raise Wave1LiveRunnerError("positive-only checkpoint journal inventory drift")
    for item, project_id in zip(journals, EXPECTED_PROJECT_IDS, strict=True):
        if not isinstance(item, dict) or item.get("project_id") != project_id:
            raise Wave1LiveRunnerError("positive-only checkpoint journal identity drift")
        path = _task_owned_artifact_path(
            item.get("path"), storage_root=storage_root, artifact_kind="positive project journal"
        )
        if hash_file(path) != item.get("file_sha256") or replay_hash_chain_journal(path)[
            1
        ] != item.get("final_chain_hash"):
            raise Wave1LiveRunnerError("positive-only checkpoint journal replay failed")
    heartbeat_path = _task_owned_artifact_path(
        payload.get("heartbeat_path"),
        storage_root=storage_root,
        artifact_kind="positive heartbeat",
    )
    if hash_file(heartbeat_path) != payload.get("heartbeat_file_sha256"):
        raise Wave1LiveRunnerError("positive-only checkpoint heartbeat replay failed")
    marker_path = _task_owned_artifact_path(
        payload.get("terminal_marker_path"),
        storage_root=storage_root,
        artifact_kind="positive terminal marker",
    )
    marker = _read_json_object(marker_path)
    if set(marker) != {
        "schema_version",
        "terminal_status",
        "run_spec_hash",
        "positive_case_count",
        "n31_resolution_started",
        "resource_released",
        "positive_checkpoint_receipt_hash",
    }:
        raise Wave1LiveRunnerError("positive-only checkpoint terminal marker fields drift")
    marker_core = dict(marker)
    marker_receipt_hash = marker_core.pop("positive_checkpoint_receipt_hash", None)
    if (
        marker_receipt_hash != payload.get("receipt_hash")
        or hash_canonical(marker_core) != payload.get("terminal_marker_preimage_hash")
        or marker_core.get("run_spec_hash") != payload.get("run_spec_hash")
        or marker_core.get("resource_released") is not True
    ):
        raise Wave1LiveRunnerError("positive-only checkpoint terminal marker replay failed")
    return dict(payload)


def _persist_positive_case(
    loaded: LoadedWave1LiveReadiness,
    dependencies: OrchestratorDependencies,
    backend: LeanBackend,
    *,
    project_id: str,
    operation_id: str,
    fixture_kind: str,
    assembled_preamble: str,
    run_spec_hash: str,
    storage_root: Path,
    session_id: str,
    symbol_resolution: PositiveSymbolResolutionEvidence,
) -> dict[str, object]:
    receipt_id = f"{project_id}.{operation_id}.{fixture_kind}"
    if fixture_kind == "success":
        execution = dependencies.positive_success_executor(
            loaded,
            backend,
            project_id=project_id,
            operation_id=operation_id,
            receipt_id=receipt_id,
            assembled_preamble=assembled_preamble,
            timeout_seconds=300,
            persistent_session_id=session_id,
        )
        replay = dependencies.positive_persister(
            loaded,
            execution,
            evidence_root=storage_root / "evidence",
            sidecar_root=storage_root / "sidecars",
            cache_root=storage_root / "cache",
            symbol_resolution=symbol_resolution,
        )
        cache_path = (
            storage_root
            / "cache"
            / "v1"
            / replay.central_cache_key_hash[:2]
            / f"{replay.central_cache_key_hash}.json"
        )
        artifacts = {
            str(replay.task_receipt_path.resolve()): replay.task_receipt_sha256,
            str(replay.typed_replay_path.resolve()): replay.typed_replay_sha256,
            str(replay.raw_response_path.resolve()): replay.raw_response_sha256,
            str(replay.reference_sidecar_path.resolve()): hash_file(replay.reference_sidecar_path),
            str(replay.candidate_sidecar_path.resolve()): hash_file(replay.candidate_sidecar_path),
            str(cache_path.resolve()): replay.central_cache_entry_sha256,
            str(symbol_resolution.task_receipt_path.resolve()): (
                symbol_resolution.task_receipt_file_sha256
            ),
            str(symbol_resolution.raw_response_path.resolve()): (
                symbol_resolution.raw_response_sha256
            ),
        }
        if replay.p01_runtime_replay is not None:
            artifacts[str(replay.p01_runtime_replay.receipt_path.resolve())] = (
                replay.p01_runtime_replay.receipt_file_sha256
            )
            artifacts[str(replay.p01_runtime_replay.retention_manifest_path.resolve())] = (
                replay.p01_runtime_replay.retention_manifest_file_sha256
            )
            artifacts[str(replay.p01_runtime_replay.retention_journal_path.resolve())] = (
                replay.p01_runtime_replay.retention_journal_file_sha256
            )
        return _case_completion_payload(
            run_spec_hash=run_spec_hash,
            project_id=project_id,
            operation_id=operation_id,
            fixture_kind=fixture_kind,
            request_hashes=execution.attempt_request_hashes,
            task_receipt=execution.task_receipt,
            artifact_hashes=artifacts,
            symbol_resolution=symbol_resolution,
            typed_replay_performed=True,
            cache_write_and_readback_replayed=True,
            elapsed_ms=execution.result.elapsed_ms,
            reference_complete_sidecar_path=replay.reference_sidecar_path,
            candidate_complete_sidecar_path=replay.candidate_sidecar_path,
            positive_replay=replay,
            p01_runtime_replay=replay.p01_runtime_replay,
        )
    rejection = dependencies.positive_rejection_executor(
        loaded,
        backend,
        project_id=project_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
        assembled_preamble=assembled_preamble,
        timeout_seconds=300,
    )
    task_path = storage_root / "evidence" / project_id / f"{receipt_id}.lean_receipt.json"
    task_sha = install_immutable_json(task_path, rejection.task_receipt)
    raw_path, raw_sha = _validated_raw_response(rejection.result)
    symbol_artifacts = {
        str(symbol_resolution.task_receipt_path.resolve()): (
            symbol_resolution.task_receipt_file_sha256
        ),
        str(symbol_resolution.raw_response_path.resolve()): (symbol_resolution.raw_response_sha256),
    }
    return _case_completion_payload(
        run_spec_hash=run_spec_hash,
        project_id=project_id,
        operation_id=operation_id,
        fixture_kind=fixture_kind,
        request_hashes=rejection.attempt_request_hashes,
        task_receipt=rejection.task_receipt,
        artifact_hashes={
            str(task_path.resolve()): task_sha,
            str(raw_path): raw_sha,
            **symbol_artifacts,
        },
        symbol_resolution=symbol_resolution,
        typed_replay_performed=False,
        cache_write_and_readback_replayed=False,
        elapsed_ms=rejection.result.elapsed_ms,
        reference_complete_sidecar_path=None,
        candidate_complete_sidecar_path=None,
        positive_replay=None,
        p01_runtime_replay=None,
    )


def _run_positive_readiness_checkpoint(
    loaded: LoadedWave1LiveReadiness,
    *,
    assembled_preamble: str,
    implementation_commit: str,
    implementation_tree: str,
    worktree: Path,
    storage_root: Path,
    owner_session: str,
    resume_command: str,
    repository_receipt_path: Path | None,
    dependencies: OrchestratorDependencies,
) -> dict[str, object]:
    implementation_identity_receipt = dependencies.verify_implementation_identity(
        worktree, implementation_commit, implementation_tree
    )
    run_spec = _run_spec_payload(
        loaded,
        stage="positive_checkpoint",
        assembled_preamble=assembled_preamble,
        implementation_commit=implementation_commit,
        implementation_tree=implementation_tree,
        worktree=worktree,
        storage_root=storage_root,
        resume_command=resume_command,
        implementation_identity_receipt=implementation_identity_receipt,
    )
    run_spec_path, run_spec_hash = _install_run_spec(storage_root, run_spec)
    durable_receipt_path = storage_root / "receipts" / "wave1_positive_live_checkpoint_v0_3_6.json"
    if durable_receipt_path.exists():
        replayed = validate_positive_checkpoint_receipt(
            loaded, _read_json_object(durable_receipt_path)
        )
        if replayed.get("run_spec_hash") != run_spec_hash:
            raise Wave1LiveRunnerError("positive checkpoint belongs to another run spec")
        if repository_receipt_path is not None:
            install_immutable_json(repository_receipt_path, replayed)
        return replayed
    process_identity = runner_process_identity()
    reservation: Reservation | None = None
    sampler: PeakRssSampler | None = None
    heartbeat: DurableHeartbeatEmitter | None = None
    active_backend: LeanBackend | None = None
    project_journals: dict[str, HashChainJournal] = {}
    case_receipts: list[dict[str, object]] = []
    total_elapsed_ms = 0
    peak_rss = 0
    released = False
    try:
        # This is intentionally before both environment preparation and backend construction.
        reservation = dependencies.claim_worker(
            owner_session=owner_session,
            worktree=worktree,
            lean_rss_gib=24.0,
        )
        sampler = dependencies.sampler_factory()
        sampler.start()
        heartbeat = DurableHeartbeatEmitter(
            path=storage_root / "heartbeats" / "positive.json",
            run_spec_hash=run_spec_hash,
            rss_supplier=lambda: sampler.peak_bytes if sampler is not None else peak_rss,
            interval_seconds=loaded.config.lean_environment_contract.heartbeat_seconds,
            process_identity=process_identity,
        )
        heartbeat.start()
        for project_id in EXPECTED_PROJECT_IDS:
            journal = HashChainJournal(storage_root / "journals" / f"positive.{project_id}.jsonl")
            project_journals[project_id] = journal
            settings = _backend_settings_for_project(
                loaded,
                project_id=project_id,
                assembled_preamble=assembled_preamble,
                raw_response_dir=storage_root / "raw" / "positive" / project_id,
            )
            journal.append(
                {
                    "event": "project_session_prepare",
                    "run_spec_hash": run_spec_hash,
                    "project_id": project_id,
                    "process_identity": process_identity,
                    "backend_settings_hash": hash_canonical(
                        {
                            "project_dir": str(settings.project_dir),
                            "context_fingerprint": settings.context_fingerprint,
                            "server_mode": settings.server_mode.value,
                            "memory_hard_limit_mb": settings.memory_hard_limit_mb,
                            "enable_parallel_elaboration": settings.enable_parallel_elaboration,
                        }
                    ),
                }
            )
            dependencies.prepare_backend(settings)
            prepared = replace(settings, environment_is_prepared=True)
            active_backend = dependencies.make_backend(prepared)
            session_id = f"sft1-wave1-session:{run_spec_hash}:{project_id}:positive"
            symbol_evidence_by_operation: dict[str, PositiveSymbolResolutionEvidence] = {}
            try:
                for expected in (item for item in _positive_case_ids() if item[0] == project_id):
                    _, operation_id, fixture_kind = expected
                    symbol_resolution = symbol_evidence_by_operation.get(operation_id)
                    if symbol_resolution is None:
                        symbol_checkpoint = (
                            storage_root
                            / "checkpoints"
                            / "symbols"
                            / project_id
                            / f"{operation_id}.json"
                        )
                        if symbol_checkpoint.exists():
                            symbol_resolution = _load_symbol_resolution_checkpoint(
                                symbol_checkpoint,
                                loaded=loaded,
                                project_id=project_id,
                                operation_id=operation_id,
                                assembled_preamble=assembled_preamble,
                                storage_root=storage_root,
                            )
                            journal.append(
                                {
                                    "event": "symbol_resolution_resume_suppressed",
                                    "project_id": project_id,
                                    "operation_id": operation_id,
                                    "symbol_resolution_receipt_hash": (
                                        symbol_resolution.task_receipt_hash
                                    ),
                                }
                            )
                        else:
                            heartbeat.update(
                                state="resolving_positive_bundle_symbols",
                                project_id=project_id,
                                case_id=f"{project_id}.{operation_id}.symbols",
                            )
                            symbol_resolution = dependencies.symbol_resolution_executor(
                                loaded,
                                active_backend,
                                project_id=project_id,
                                operation_id=operation_id,
                                assembled_preamble=assembled_preamble,
                                timeout_seconds=300,
                                evidence_root=storage_root / "evidence",
                            )
                            install_immutable_json(
                                symbol_checkpoint,
                                _symbol_resolution_checkpoint_payload(symbol_resolution),
                            )
                            journal.append(
                                {
                                    "event": "symbol_resolution_completed",
                                    "project_id": project_id,
                                    "operation_id": operation_id,
                                    "symbol_resolution_receipt_hash": (
                                        symbol_resolution.task_receipt_hash
                                    ),
                                }
                            )
                        total_elapsed_ms += symbol_resolution.elapsed_ms
                        symbol_evidence_by_operation[operation_id] = symbol_resolution
                    case_id = f"{project_id}.{operation_id}.{fixture_kind}"
                    completion_path = storage_root / "checkpoints" / "positive" / f"{case_id}.json"
                    sampler.check()
                    heartbeat.check()
                    heartbeat.update(
                        state="executing_positive_fixture",
                        project_id=project_id,
                        case_id=case_id,
                    )
                    if completion_path.exists():
                        completion = _validate_case_completion(
                            _read_json_object(completion_path),
                            loaded=loaded,
                            run_spec_hash=run_spec_hash,
                            expected_identity=expected,
                        )
                        journal.append({"event": "case_resume_suppressed", "case_id": case_id})
                    else:
                        journal.append({"event": "case_started", "case_id": case_id})
                        completion = _persist_positive_case(
                            loaded,
                            dependencies,
                            active_backend,
                            project_id=project_id,
                            operation_id=operation_id,
                            fixture_kind=fixture_kind,
                            assembled_preamble=assembled_preamble,
                            run_spec_hash=run_spec_hash,
                            storage_root=storage_root,
                            session_id=session_id,
                            symbol_resolution=symbol_resolution,
                        )
                        install_immutable_json(completion_path, completion)
                        completion = _validate_case_completion(
                            completion,
                            loaded=loaded,
                            run_spec_hash=run_spec_hash,
                            expected_identity=expected,
                        )
                        journal.append(
                            {
                                "event": "case_completed",
                                "case_id": case_id,
                                "completion_hash": completion["completion_hash"],
                            }
                        )
                    total_elapsed_ms += cast(int, completion["elapsed_ms"])
                    case_receipts.append(completion)
                journal.append({"event": "project_session_completed", "project_id": project_id})
            finally:
                active_backend.close()
                active_backend = None
        heartbeat.update(
            state="positive_checkpoint_complete",
            project_id=None,
            case_id=None,
        )
        heartbeat.stop()
        heartbeat = None
        peak_rss = sampler.stop()
        sampler = None
        dependencies.release_worker()
        released = True
    except BaseException as exc:
        if active_backend is not None:
            active_backend.close()
            active_backend = None
        if heartbeat is not None:
            with contextlib.suppress(BaseException):
                heartbeat.stop()
        if sampler is not None:
            with contextlib.suppress(BaseException):
                peak_rss = max(peak_rss, sampler.stop())
        if reservation is not None and not released:
            try:
                dependencies.release_worker()
                released = True
            except BaseException:
                pass
        failure = {
            "schema_version": 1,
            "run_spec_hash": run_spec_hash,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "resource_release_attempted": reservation is not None,
            "resource_released": released,
            "process_identity": process_identity,
            "failed_at": datetime.now(UTC).isoformat(),
        }
        install_immutable_json(
            storage_root / "terminal" / f"positive.failed.{time.time_ns()}.json", failure
        )
        raise
    if reservation is None or not released or len(case_receipts) != 32:
        raise Wave1LiveRunnerError("positive checkpoint did not close its exact bounded scope")
    journal_receipts = []
    for project_id in EXPECTED_PROJECT_IDS:
        journal = project_journals[project_id]
        journal_receipts.append(
            {
                "project_id": project_id,
                "path": str(journal.path.resolve()),
                "file_sha256": hash_file(journal.path),
                "final_chain_hash": journal.final_chain_hash,
            }
        )
    marker_core = {
        "schema_version": 1,
        "terminal_status": "positive_checkpoint_complete_n31_not_started",
        "run_spec_hash": run_spec_hash,
        "positive_case_count": 32,
        "n31_resolution_started": False,
        "resource_released": True,
    }
    measured_case_lean_milliseconds = sum(cast(int, case["elapsed_ms"]) for case in case_receipts)
    measured_complete_sidecar_bytes = sum(
        cast(int, case["reference_complete_sidecar_bytes"])
        + cast(int, case["candidate_complete_sidecar_bytes"])
        for case in case_receipts
        if case["fixture_kind"] == "success"
    )
    p01_runtime_replay_receipt_hashes = [
        cast(str, case["p01_runtime_replay_receipt_hash"])
        for case in case_receipts
        if case["operation_id"] == "P01_ALPHA_RENAME_SINGLE_V1"
        and case["fixture_kind"] == "success"
    ]
    receipt_core: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": "sft1_wave1_positive_live_checkpoint_v0_3_6",
        "run_spec_hash": run_spec_hash,
        "run_spec_path": str(run_spec_path.resolve()),
        "run_spec_file_sha256": hash_file(run_spec_path),
        "runtime_config_file_sha256": loaded.config_file_sha256,
        "runtime_config_hash": loaded.config_hash,
        "runtime_fixture_file_sha256": loaded.fixture_file_sha256,
        "runtime_fixture_hash": loaded.fixture_hash,
        "runtime_loader_file_sha256": hash_file(
            Path(__file__).with_name("wave1_live_readiness.py")
        ),
        "live_runner_file_sha256": runner_implementation_hash(),
        "implementation_commit": implementation_commit,
        "implementation_tree": implementation_tree,
        "implementation_identity_receipt": implementation_identity_receipt,
        "implementation_identity_receipt_hash": implementation_identity_receipt[
            "verification_hash"
        ],
        "assembled_preamble_sha256": sha256_hex(assembled_preamble.encode("utf-8")),
        "resource_claim_id": f"SFT1:{reservation_snapshot_hash(reservation)[:24]}",
        "resource_claim_snapshot": json.loads(reservation.to_json()),
        "resource_claim_snapshot_hash": reservation_snapshot_hash(reservation),
        "resource_released": True,
        "persistent_worker_count": 1,
        "measured_combined_peak_rss_bytes": peak_rss,
        "measured_case_lean_milliseconds": measured_case_lean_milliseconds,
        "measured_symbol_resolution_lean_milliseconds": (
            total_elapsed_ms - measured_case_lean_milliseconds
        ),
        "measured_total_lean_milliseconds": total_elapsed_ms,
        "measured_total_lean_seconds": total_elapsed_ms / 1000,
        "measured_total_complete_sidecar_bytes": measured_complete_sidecar_bytes,
        "p01_runtime_policy_semantic_hash": P01_POLICY_SEMANTIC_HASH,
        "p01_runtime_source_sha256": hash_file(Path(__file__).with_name("wave1_runtime.py")),
        "p01_live_acceptance_project_count": len(p01_runtime_replay_receipt_hashes),
        "p01_runtime_replay_receipt_hashes": p01_runtime_replay_receipt_hashes,
        "p01_complete_scope_cap_execution_performed": True,
        "elab_async": False,
        "per_row_process_spawned": False,
        "corpus_compiled": False,
        "positive_case_count": 32,
        "cases": case_receipts,
        "project_journals": journal_receipts,
        "journal_is_durable_log": True,
        "heartbeat_path": str((storage_root / "heartbeats" / "positive.json").resolve()),
        "heartbeat_file_sha256": hash_file(storage_root / "heartbeats" / "positive.json"),
        "all_positive_cases_completed": True,
        "all_success_sidecars_typed_replay_and_cache_readback_bound": True,
        "all_rejections_candidate_free": True,
        "n31_resolution_started": False,
        "n31_activation_performed": False,
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
        "terminal_marker_path": str(
            (storage_root / "terminal" / "positive_checkpoint_complete.json").resolve()
        ),
        "terminal_marker_preimage_hash": hash_canonical(marker_core),
        "terminal_status": "positive_checkpoint_complete_n31_not_started",
    }
    receipt = {**receipt_core, "receipt_hash": hash_canonical(receipt_core)}
    if not _positive_checkpoint_core_is_valid(receipt):
        raise Wave1LiveRunnerError("constructed positive checkpoint failed its core replay")
    install_immutable_json(
        storage_root / "terminal" / "positive_checkpoint_complete.json",
        {**marker_core, "positive_checkpoint_receipt_hash": receipt["receipt_hash"]},
    )
    validate_positive_checkpoint_receipt(loaded, receipt)
    install_immutable_json(durable_receipt_path, receipt)
    if repository_receipt_path is not None:
        install_immutable_json(repository_receipt_path, receipt)
    return receipt


def run_positive_readiness_checkpoint(
    loaded: LoadedWave1LiveReadiness,
    *,
    assembled_preamble: str,
    implementation_commit: str,
    implementation_tree: str,
    worktree: Path,
    owner_session: str,
    resume_command: str,
    repository_receipt_path: Path | None = None,
    dependencies: OrchestratorDependencies | None = None,
) -> dict[str, object]:
    """Execute only the 32 positive readiness cases and release resources.

    The policy storage root is not caller-selectable.  N31 is not started by
    this function, and the 40-case live-readiness receipt is never written.
    """

    if repository_receipt_path is not None:
        mirror = repository_receipt_path.resolve()
        expected_mirror = (worktree.resolve() / DEFAULT_POSITIVE_CHECKPOINT_RECEIPT_PATH).resolve()
        if mirror != expected_mirror:
            raise Wave1LiveRunnerError("positive checkpoint mirror is not its repo-owned path")
    return _run_positive_readiness_checkpoint(
        loaded,
        assembled_preamble=assembled_preamble,
        implementation_commit=implementation_commit,
        implementation_tree=implementation_tree,
        worktree=worktree,
        storage_root=Path(loaded.config.persistence_contract.root),
        owner_session=owner_session,
        resume_command=resume_command,
        repository_receipt_path=repository_receipt_path,
        dependencies=dependencies or OrchestratorDependencies(),
    )


def _n31_completion_payload(
    *,
    run_spec_hash: str,
    execution: N31ProposalExecution,
    artifact_hashes: Mapping[str, str],
) -> dict[str, object]:
    proposal = execution.proposal.model_dump(mode="json")
    core = {
        "schema_version": 1,
        "run_spec_hash": run_spec_hash,
        "project_id": execution.proposal.project_id,
        "proposal": proposal,
        "project_receipt_hash": execution.proposal.project_receipt_hash,
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "runtime_activated": False,
        "semantic_conformance_performed": False,
        "candidate_constructed": False,
        "row_or_gate_emitted": False,
    }
    return {**core, "completion_hash": hash_canonical(core)}


def _validate_n31_completion(
    payload: object,
    *,
    run_spec_hash: str,
    project_id: str,
    storage_root: Path,
) -> tuple[dict[str, object], N31ResolutionProjectProposal]:
    if not isinstance(payload, dict):
        raise Wave1LiveRunnerError("N31 completion is not an object")
    core = dict(payload)
    observed = core.pop("completion_hash", None)
    if (
        payload.get("schema_version") != 1
        or payload.get("run_spec_hash") != run_spec_hash
        or payload.get("project_id") != project_id
        or observed != hash_canonical(core)
        or payload.get("runtime_activated") is not False
        or payload.get("semantic_conformance_performed") is not False
        or payload.get("candidate_constructed") is not False
        or payload.get("row_or_gate_emitted") is not False
    ):
        raise Wave1LiveRunnerError("N31 completion identity/hash drift")
    try:
        proposal = N31ResolutionProjectProposal.model_validate(payload.get("proposal"))
    except ValidationError as exc:
        raise Wave1LiveRunnerError("N31 completion proposal replay failed") from exc
    if proposal.project_receipt_hash != payload.get("project_receipt_hash"):
        raise Wave1LiveRunnerError("N31 completion project receipt hash drift")
    artifacts = payload.get("artifact_hashes")
    if not isinstance(artifacts, dict) or len(artifacts) < 5:
        raise Wave1LiveRunnerError("N31 completion artifact inventory is incomplete")
    for raw_path, expected_hash in artifacts.items():
        if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
            raise Wave1LiveRunnerError("N31 completion artifact inventory is malformed")
        path = _task_owned_artifact_path(
            raw_path,
            storage_root=storage_root,
            artifact_kind="N31 completion artifact",
        )
        if hash_file(path) != expected_hash:
            raise Wave1LiveRunnerError("N31 completion artifact replay failed")
    return dict(payload), proposal


def _persist_n31_execution(
    execution: N31ProposalExecution, *, storage_root: Path, run_spec_hash: str
) -> dict[str, object]:
    project_id = execution.proposal.project_id
    base = storage_root / "evidence" / project_id / "n31"
    phase_one_receipt = base / "phase_one.task_receipt.json"
    phase_two_receipt = base / "phase_two.task_receipt.json"
    proposal_path = base / "project_proposal.json"
    artifacts = {
        str(phase_one_receipt.resolve()): install_immutable_json(
            phase_one_receipt, execution.phase_one.task_receipt
        ),
        str(phase_two_receipt.resolve()): install_immutable_json(
            phase_two_receipt, execution.phase_two.task_receipt
        ),
        str(proposal_path.resolve()): install_immutable_json(
            proposal_path, execution.proposal.model_dump(mode="json")
        ),
        str(execution.phase_one.raw_response_path.resolve()): (
            execution.phase_one.raw_response_sha256
        ),
        str(execution.phase_two.raw_response_path.resolve()): (
            execution.phase_two.raw_response_sha256
        ),
    }
    return _n31_completion_payload(
        run_spec_hash=run_spec_hash,
        execution=execution,
        artifact_hashes=artifacts,
    )


def validate_n31_proposal_checkpoint(
    loaded: LoadedWave1LiveReadiness,
    payload: Mapping[str, object],
) -> N31ResolutionProposalBundle:
    """Replay the entire proposal-only N31 artifact closure without Lean."""

    try:
        bundle = N31ResolutionProposalBundle.model_validate(payload)
    except ValidationError as exc:
        raise Wave1LiveRunnerError("N31 proposal checkpoint typed schema drift") from exc
    storage_root = Path(loaded.config.persistence_contract.root).resolve()
    run_spec_path = _task_owned_artifact_path(
        bundle.run_spec_path,
        storage_root=storage_root,
        artifact_kind="N31 run spec",
    )
    run_spec = _read_canonical_json_object(run_spec_path, artifact_kind="N31 run spec")
    run_spec_core = dict(run_spec)
    observed_run_spec_hash = run_spec_core.pop("run_spec_hash", None)
    if (
        set(run_spec) != set(_run_spec_payload_fields()) | {"run_spec_hash"}
        or hash_file(run_spec_path) != bundle.run_spec_file_sha256
        or observed_run_spec_hash != hash_canonical(run_spec_core)
        or observed_run_spec_hash != bundle.run_spec_hash
        or run_spec.get("stage") != "n31_resolution_proposal"
        or run_spec.get("storage_root") != str(storage_root)
        or run_spec.get("positive_checkpoint_receipt_hash")
        != bundle.positive_checkpoint_receipt_hash
        or run_spec.get("implementation_commit") != bundle.implementation_commit
        or run_spec.get("implementation_tree") != bundle.implementation_tree
        or run_spec.get("assembled_preamble_sha256") != bundle.assembled_preamble_sha256
        or run_spec.get("runtime_config_file_sha256") != loaded.config_file_sha256
        or run_spec.get("runtime_config_hash") != loaded.config_hash
        or run_spec.get("runtime_fixture_file_sha256") != loaded.fixture_file_sha256
        or run_spec.get("runtime_fixture_hash") != loaded.fixture_hash
        or run_spec.get("runtime_loader_file_sha256")
        != hash_file(Path(__file__).with_name("wave1_live_readiness.py"))
        or run_spec.get("live_runner_file_sha256") != runner_implementation_hash()
        or run_spec.get("n31_runtime_activation_authorized") is not False
        or run_spec.get("n31_semantic_conformance_authorized") is not False
        or run_spec.get("wave1_gate_authorized") is not False
        or run_spec.get("row_emission_authorized") is not False
        or run_spec.get("terminal_stop") != "stopped_for_exact_n31_user_admission"
    ):
        raise Wave1LiveRunnerError("N31 proposal run-spec replay failed")
    _validate_git_identity_receipt(
        bundle.implementation_identity_receipt.model_dump(mode="json"),
        worktree=Path(cast(str, run_spec["worktree"])),
        implementation_commit=bundle.implementation_commit,
        implementation_tree=bundle.implementation_tree,
    )
    positive_path = _task_owned_artifact_path(
        bundle.positive_checkpoint_receipt_path,
        storage_root=storage_root,
        artifact_kind="N31 prerequisite positive checkpoint",
    )
    positive_payload = _read_canonical_json_object(
        positive_path, artifact_kind="N31 prerequisite positive checkpoint"
    )
    if (
        hash_file(positive_path) != bundle.positive_checkpoint_receipt_file_sha256
        or positive_payload.get("receipt_hash") != bundle.positive_checkpoint_receipt_hash
        or positive_payload.get("implementation_commit") != bundle.implementation_commit
        or positive_payload.get("implementation_tree") != bundle.implementation_tree
    ):
        raise Wave1LiveRunnerError("N31 positive-checkpoint prerequisite binding drift")
    validate_positive_checkpoint_receipt(loaded, positive_payload)
    if (
        bundle.runtime_config_file_sha256 != loaded.config_file_sha256
        or bundle.runtime_config_hash != loaded.config_hash
        or bundle.runtime_fixture_file_sha256 != loaded.fixture_file_sha256
        or bundle.runtime_fixture_hash != loaded.fixture_hash
        or bundle.runtime_loader_file_sha256
        != hash_file(Path(__file__).with_name("wave1_live_readiness.py"))
        or bundle.live_runner_file_sha256 != runner_implementation_hash()
        or bundle.implementation_identity_receipt_hash
        != bundle.implementation_identity_receipt.verification_hash
        or bundle.resource_claim_id
        != f"SFT1:{bundle.resource_claim_snapshot_hash[:24]}"
        or bundle.resource_claim_snapshot.worktree != cast(str, run_spec["worktree"])
        or bundle.resource_released is not True
    ):
        raise Wave1LiveRunnerError("N31 implementation/resource identity drift")
    proposals: list[N31ResolutionProjectProposal] = []
    for binding, project_id in zip(
        bundle.project_completions, EXPECTED_PROJECT_IDS, strict=True
    ):
        path = _task_owned_artifact_path(
            binding.path,
            storage_root=storage_root,
            artifact_kind="N31 project completion",
        )
        completion = _read_canonical_json_object(path, artifact_kind="N31 project completion")
        if (
            binding.project_id != project_id
            or hash_file(path) != binding.file_sha256
            or completion.get("completion_hash") != binding.completion_hash
        ):
            raise Wave1LiveRunnerError("N31 project completion binding drift")
        _, proposal = _validate_n31_completion(
            completion,
            run_spec_hash=bundle.run_spec_hash,
            project_id=project_id,
            storage_root=storage_root,
        )
        proposals.append(proposal)
    if tuple(proposals) != bundle.proposals:
        raise Wave1LiveRunnerError("N31 completion/proposal inventory is swappable")
    for journal, project_id in zip(bundle.project_journals, EXPECTED_PROJECT_IDS, strict=True):
        path = _task_owned_artifact_path(
            journal.path,
            storage_root=storage_root,
            artifact_kind="N31 project journal",
        )
        if (
            journal.project_id != project_id
            or hash_file(path) != journal.file_sha256
            or replay_hash_chain_journal(path)[1] != journal.final_chain_hash
        ):
            raise Wave1LiveRunnerError("N31 project journal replay failed")
    heartbeat_path = _task_owned_artifact_path(
        bundle.heartbeat_path,
        storage_root=storage_root,
        artifact_kind="N31 heartbeat",
    )
    heartbeat = _read_canonical_json_object(heartbeat_path, artifact_kind="N31 heartbeat")
    heartbeat_core = dict(heartbeat)
    heartbeat_hash = heartbeat_core.pop("heartbeat_hash", None)
    if (
        hash_file(heartbeat_path) != bundle.heartbeat_file_sha256
        or heartbeat_hash != hash_canonical(heartbeat_core)
        or heartbeat.get("run_spec_hash") != bundle.run_spec_hash
        or heartbeat.get("state") != "stopping_for_exact_n31_user_admission"
    ):
        raise Wave1LiveRunnerError("N31 terminal heartbeat replay failed")
    marker_path = _task_owned_artifact_path(
        bundle.terminal_marker_path,
        storage_root=storage_root,
        artifact_kind="N31 terminal marker",
    )
    marker = _read_canonical_json_object(marker_path, artifact_kind="N31 terminal marker")
    marker_core = dict(marker)
    marker_bundle_hash = marker_core.pop("n31_proposal_bundle_receipt_hash", None)
    if (
        marker_bundle_hash != bundle.receipt_hash
        or hash_canonical(marker_core) != bundle.terminal_marker_preimage_hash
        or marker_core.get("run_spec_hash") != bundle.run_spec_hash
        or marker_core.get("positive_checkpoint_receipt_hash")
        != bundle.positive_checkpoint_receipt_hash
        or marker_core.get("resource_released") is not True
        or marker_core.get("n31_activation_performed") is not False
        or marker_core.get("semantic_conformance_performed") is not False
        or marker_core.get("wave1_gate_executed") is not False
        or marker_core.get("model_facing_rows_emitted") is not False
    ):
        raise Wave1LiveRunnerError("N31 terminal marker replay failed")
    return bundle


def _run_n31_resolution_proposals(
    loaded: LoadedWave1LiveReadiness,
    *,
    assembled_preamble: str,
    implementation_commit: str,
    implementation_tree: str,
    worktree: Path,
    storage_root: Path,
    owner_session: str,
    resume_command: str,
    positive_checkpoint_receipt: Mapping[str, object],
    repository_receipt_path: Path | None,
    dependencies: OrchestratorDependencies,
) -> N31ResolutionProposalBundle:
    positive = validate_positive_checkpoint_receipt(loaded, positive_checkpoint_receipt)
    positive_hash = cast(str, positive["receipt_hash"])
    positive_durable_path = (
        storage_root / "receipts" / "wave1_positive_live_checkpoint_v0_3_6.json"
    ).resolve()
    if (
        not positive_durable_path.is_file()
        or positive_durable_path.is_symlink()
        or _read_canonical_json_object(
            positive_durable_path,
            artifact_kind="N31 prerequisite positive checkpoint",
        )
        != positive
    ):
        raise Wave1LiveRunnerError(
            "N31 requires the canonical task-root positive checkpoint prerequisite"
        )
    implementation_identity_receipt = dependencies.verify_implementation_identity(
        worktree, implementation_commit, implementation_tree
    )
    run_spec = _run_spec_payload(
        loaded,
        stage="n31_resolution_proposal",
        assembled_preamble=assembled_preamble,
        implementation_commit=implementation_commit,
        implementation_tree=implementation_tree,
        worktree=worktree,
        storage_root=storage_root,
        resume_command=resume_command,
        implementation_identity_receipt=implementation_identity_receipt,
        positive_checkpoint_receipt_hash=positive_hash,
    )
    run_spec_path, run_spec_hash = _install_run_spec(storage_root, run_spec)
    durable_receipt_path = storage_root / "receipts" / "wave1_n31_resolution_proposal_v0_3_6.json"
    if durable_receipt_path.exists():
        replayed_bundle = validate_n31_proposal_checkpoint(
            loaded, _read_canonical_json_object(durable_receipt_path, artifact_kind="N31 bundle")
        )
        if replayed_bundle.run_spec_hash != run_spec_hash:
            raise Wave1LiveRunnerError("durable N31 proposal belongs to another run spec")
        if repository_receipt_path is not None:
            install_immutable_json(repository_receipt_path, replayed_bundle.model_dump(mode="json"))
        return replayed_bundle
    process_identity = runner_process_identity()
    reservation: Reservation | None = None
    sampler: PeakRssSampler | None = None
    heartbeat: DurableHeartbeatEmitter | None = None
    active_backend: LeanBackend | None = None
    proposals: list[N31ResolutionProjectProposal] = []
    journals: dict[str, HashChainJournal] = {}
    total_elapsed_ms = 0
    peak_rss = 0
    released = False
    try:
        reservation = dependencies.claim_worker(
            owner_session=owner_session,
            worktree=worktree,
            lean_rss_gib=24.0,
        )
        sampler = dependencies.sampler_factory()
        sampler.start()
        heartbeat = DurableHeartbeatEmitter(
            path=storage_root / "heartbeats" / "n31.json",
            run_spec_hash=run_spec_hash,
            rss_supplier=lambda: sampler.peak_bytes if sampler is not None else peak_rss,
            interval_seconds=loaded.config.lean_environment_contract.heartbeat_seconds,
            process_identity=process_identity,
        )
        heartbeat.start()
        for project_id in EXPECTED_PROJECT_IDS:
            journal = HashChainJournal(storage_root / "journals" / f"n31.{project_id}.jsonl")
            journals[project_id] = journal
            completion_path = storage_root / "checkpoints" / "n31" / f"{project_id}.json"
            if completion_path.exists():
                _, proposal = _validate_n31_completion(
                    _read_json_object(completion_path),
                    run_spec_hash=run_spec_hash,
                    project_id=project_id,
                    storage_root=storage_root,
                )
                journal.append(
                    {
                        "event": "n31_project_resume_suppressed",
                        "project_id": project_id,
                        "project_receipt_hash": proposal.project_receipt_hash,
                    }
                )
                proposals.append(proposal)
                total_elapsed_ms += proposal.elapsed_ms
                continue
            settings = _backend_settings_for_project(
                loaded,
                project_id=project_id,
                assembled_preamble=assembled_preamble,
                raw_response_dir=storage_root / "raw" / "n31" / project_id,
            )
            journal.append(
                {
                    "event": "n31_project_prepare",
                    "run_spec_hash": run_spec_hash,
                    "project_id": project_id,
                    "process_identity": process_identity,
                }
            )
            dependencies.prepare_backend(settings)
            active_backend = dependencies.make_backend(
                replace(settings, environment_is_prepared=True)
            )
            try:
                sampler.peak_bytes = max(
                    sampler.peak_bytes, measured_process_tree_rss_bytes(os.getpid())
                )
                sampler.check()
                heartbeat.check()
                heartbeat.update(
                    state="resolving_n31_proposal_without_activation",
                    project_id=project_id,
                    case_id=f"{project_id}.N31.two_phase",
                )
                execution = dependencies.n31_executor(
                    loaded,
                    active_backend,
                    project_id=project_id,
                    assembled_preamble=assembled_preamble,
                    timeout_seconds=300,
                    measured_peak_rss_bytes=sampler.peak_bytes,
                    phase_checkpoint_root=storage_root / "checkpoints" / "n31_phases",
                )
                completion = _persist_n31_execution(
                    execution, storage_root=storage_root, run_spec_hash=run_spec_hash
                )
                install_immutable_json(completion_path, completion)
                _, proposal = _validate_n31_completion(
                    completion,
                    run_spec_hash=run_spec_hash,
                    project_id=project_id,
                    storage_root=storage_root,
                )
                journal.append(
                    {
                        "event": "n31_project_proposed_not_admitted",
                        "project_id": project_id,
                        "project_receipt_hash": proposal.project_receipt_hash,
                        "resolved_lean_hash": proposal.resolved_lean_hash,
                        "resolution_receipt_hash": proposal.resolution_receipt_hash,
                        "activation_performed": False,
                    }
                )
                proposals.append(proposal)
                total_elapsed_ms += proposal.elapsed_ms
            finally:
                active_backend.close()
                active_backend = None
        heartbeat.update(
            state="stopping_for_exact_n31_user_admission",
            project_id=None,
            case_id=None,
        )
        heartbeat.stop()
        heartbeat = None
        peak_rss = sampler.stop()
        sampler = None
        dependencies.release_worker()
        released = True
    except BaseException as exc:
        if active_backend is not None:
            active_backend.close()
        if heartbeat is not None:
            with contextlib.suppress(BaseException):
                heartbeat.stop()
        if sampler is not None:
            with contextlib.suppress(BaseException):
                peak_rss = max(peak_rss, sampler.stop())
        if reservation is not None and not released:
            try:
                dependencies.release_worker()
                released = True
            except BaseException:
                pass
        install_immutable_json(
            storage_root / "terminal" / f"n31.failed.{time.time_ns()}.json",
            {
                "schema_version": 1,
                "run_spec_hash": run_spec_hash,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "resource_released": released,
                "activation_performed": False,
                "row_or_gate_emitted": False,
                "failed_at": datetime.now(UTC).isoformat(),
            },
        )
        raise
    if (
        reservation is None
        or not released
        or tuple(item.project_id for item in proposals) != (EXPECTED_PROJECT_IDS)
    ):
        raise Wave1LiveRunnerError("N31 proposal stage did not close its exact project scope")
    journal_bindings = [
        {
            "project_id": project_id,
            "path": str(journals[project_id].path.resolve()),
            "file_sha256": hash_file(journals[project_id].path),
            "final_chain_hash": journals[project_id].final_chain_hash,
        }
        for project_id in EXPECTED_PROJECT_IDS
    ]
    completion_bindings = []
    for project_id in EXPECTED_PROJECT_IDS:
        completion_path = (
            storage_root / "checkpoints" / "n31" / f"{project_id}.json"
        ).resolve()
        completion = _read_canonical_json_object(
            completion_path, artifact_kind="N31 project completion"
        )
        completion_bindings.append(
            {
                "project_id": project_id,
                "path": str(completion_path),
                "file_sha256": hash_file(completion_path),
                "completion_hash": completion["completion_hash"],
            }
        )
    heartbeat_path = (storage_root / "heartbeats" / "n31.json").resolve()
    marker_path = (
        storage_root / "terminal" / "stopped_for_exact_n31_user_admission.json"
    ).resolve()
    exact_user_decision = [
        item.model_dump(
            include={
                "project_id",
                "bank_id",
                "resolved_lean_hash",
                "resolution_receipt_hash",
            },
            mode="json",
        )
        for item in proposals
    ]
    marker_core = {
        "schema_version": 1,
        "terminal_status": "stopped_for_exact_n31_user_admission",
        "run_spec_hash": run_spec_hash,
        "positive_checkpoint_receipt_hash": positive_hash,
        "project_journals": journal_bindings,
        "resource_released": True,
        "n31_activation_performed": False,
        "semantic_conformance_performed": False,
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
        "exact_user_decision_required": exact_user_decision,
    }
    bundle_core: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": "sft1_wave1_n31_resolution_proposal_v0_3_6",
        "run_spec_hash": run_spec_hash,
        "run_spec_path": str(run_spec_path.resolve()),
        "run_spec_file_sha256": hash_file(run_spec_path),
        "positive_checkpoint_receipt_hash": positive_hash,
        "positive_checkpoint_receipt_path": str(positive_durable_path),
        "positive_checkpoint_receipt_file_sha256": hash_file(positive_durable_path),
        "runtime_config_file_sha256": loaded.config_file_sha256,
        "runtime_config_hash": loaded.config_hash,
        "runtime_fixture_file_sha256": loaded.fixture_file_sha256,
        "runtime_fixture_hash": loaded.fixture_hash,
        "runtime_loader_file_sha256": hash_file(
            Path(__file__).with_name("wave1_live_readiness.py")
        ),
        "live_runner_file_sha256": runner_implementation_hash(),
        "implementation_commit": implementation_commit,
        "implementation_tree": implementation_tree,
        "implementation_identity_receipt": implementation_identity_receipt,
        "implementation_identity_receipt_hash": implementation_identity_receipt[
            "verification_hash"
        ],
        "assembled_preamble_sha256": sha256_hex(assembled_preamble.encode("utf-8")),
        "resource_claim_id": f"SFT1:{reservation_snapshot_hash(reservation)[:24]}",
        "resource_claim_snapshot": json.loads(reservation.to_json()),
        "resource_claim_snapshot_hash": reservation_snapshot_hash(reservation),
        "resource_released": True,
        "persistent_worker_count": 1,
        "measured_combined_peak_rss_bytes": peak_rss,
        "measured_total_lean_seconds": max(0.001, total_elapsed_ms / 1000),
        "elab_async": False,
        "per_row_process_spawned": False,
        "corpus_compiled": False,
        "proposals": [item.model_dump(mode="json") for item in proposals],
        "project_completions": completion_bindings,
        "project_journals": journal_bindings,
        "journal_is_durable_log": True,
        "heartbeat_path": str(heartbeat_path),
        "heartbeat_file_sha256": hash_file(heartbeat_path),
        "n31_activation_performed": False,
        "semantic_success_conformance_performed": False,
        "semantic_adversarial_conformance_performed": False,
        "wave1_gate_executed": False,
        "model_facing_rows_emitted": False,
        "terminal_status": "stopped_for_exact_n31_user_admission",
        "exact_user_admission_fields": [
            "project_id",
            "bank_id",
            "resolved_lean_hash",
            "resolution_receipt_hash",
        ],
        "terminal_marker_path": str(marker_path),
        "terminal_marker_preimage_hash": hash_canonical(marker_core),
    }
    try:
        bundle = N31ResolutionProposalBundle.model_validate(
            {**bundle_core, "receipt_hash": hash_canonical(bundle_core)}
        )
    except ValidationError as exc:
        raise Wave1LiveRunnerError("N31 proposal bundle failed strict replay") from exc
    install_immutable_json(
        marker_path,
        {
            **marker_core,
            "n31_proposal_bundle_receipt_hash": bundle.receipt_hash,
        },
    )
    validate_n31_proposal_checkpoint(loaded, bundle.model_dump(mode="json"))
    install_immutable_json(durable_receipt_path, bundle.model_dump(mode="json"))
    if repository_receipt_path is not None:
        install_immutable_json(repository_receipt_path, bundle.model_dump(mode="json"))
    return bundle


def run_n31_resolution_proposals(
    loaded: LoadedWave1LiveReadiness,
    *,
    assembled_preamble: str,
    implementation_commit: str,
    implementation_tree: str,
    worktree: Path,
    owner_session: str,
    resume_command: str,
    positive_checkpoint_receipt_path: Path,
    repository_receipt_path: Path | None = None,
    dependencies: OrchestratorDependencies | None = None,
) -> N31ResolutionProposalBundle:
    """Resolve, freeze, and stop on four unadmitted N31 bank proposals."""

    if repository_receipt_path is not None:
        mirror = repository_receipt_path.resolve()
        expected_mirror = (worktree.resolve() / DEFAULT_N31_PROPOSAL_RECEIPT_PATH).resolve()
        if mirror != expected_mirror:
            raise Wave1LiveRunnerError("N31 proposal mirror is not its repo-owned path")
    return _run_n31_resolution_proposals(
        loaded,
        assembled_preamble=assembled_preamble,
        implementation_commit=implementation_commit,
        implementation_tree=implementation_tree,
        worktree=worktree,
        storage_root=Path(loaded.config.persistence_contract.root),
        owner_session=owner_session,
        resume_command=resume_command,
        positive_checkpoint_receipt=_read_json_object(positive_checkpoint_receipt_path),
        repository_receipt_path=repository_receipt_path,
        dependencies=dependencies or OrchestratorDependencies(),
    )


def wait_for_heartbeat(deadline: float, *, interval_seconds: float = 0.1) -> None:
    """Small interruptible wait used only by bounded heartbeat tests."""

    while time.monotonic() < deadline:
        time.sleep(min(interval_seconds, max(0.0, deadline - time.monotonic())))


def runner_implementation_hash() -> str:
    return hash_file(Path(__file__).resolve())


def main(argv: Sequence[str] | None = None) -> int:
    """Run one explicitly selected readiness stage; never a gate or row job."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("positive-checkpoint", "n31-resolution-proposals"))
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--implementation-tree", required=True)
    parser.add_argument("--owner-session", required=True)
    parser.add_argument("--resume-command", required=True)
    parser.add_argument("--repository-receipt", type=Path)
    parser.add_argument("--positive-checkpoint-receipt", type=Path)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    loaded = load_wave1_live_readiness(repo_root)
    preamble = assemble_runtime_preamble(repo_root, loaded.config.source_bindings)
    common = {
        "assembled_preamble": preamble.text,
        "implementation_commit": args.implementation_commit,
        "implementation_tree": args.implementation_tree,
        "worktree": repo_root,
        "owner_session": args.owner_session,
        "resume_command": args.resume_command,
        "repository_receipt_path": args.repository_receipt,
    }
    if args.stage == "positive-checkpoint":
        if args.positive_checkpoint_receipt is not None:
            parser.error("positive-checkpoint does not accept --positive-checkpoint-receipt")
        receipt = run_positive_readiness_checkpoint(loaded, **common)
        print(receipt["receipt_hash"])
        return 0
    positive_path = args.positive_checkpoint_receipt or (
        Path(loaded.config.persistence_contract.root)
        / "receipts"
        / "wave1_positive_live_checkpoint_v0_3_6.json"
    )
    bundle = run_n31_resolution_proposals(
        loaded,
        positive_checkpoint_receipt_path=positive_path,
        **common,
    )
    print(bundle.receipt_hash)
    return 0


__all__ = [
    "AUTHORIZED_POSITIVE_OPERATION_IDS",
    "TASK_RECEIPT_MARKER",
    "DirectReceiptExecution",
    "HashChainJournal",
    "N31PhaseExecution",
    "N31ProposalExecution",
    "OrchestratorDependencies",
    "P01RuntimeReplayEvidence",
    "PeakRssSampler",
    "PositiveEvidenceReplay",
    "PositiveSuccessExecution",
    "PositiveSymbolResolutionEvidence",
    "RetryingCapturingBackend",
    "Wave1LiveRunnerError",
    "build_direct_meta_command",
    "build_n31_phase_one_session",
    "build_n31_phase_two_session",
    "build_p01_runtime_replay_receipt",
    "build_positive_rejection_session",
    "build_positive_success_session",
    "build_positive_symbol_resolution_session",
    "claim_single_wave1_worker",
    "execute_n31_resolution_proposal",
    "execute_n31_resolution_proposal_evidence",
    "execute_positive_rejection",
    "execute_positive_success",
    "execute_positive_symbol_resolution",
    "extract_task_receipt",
    "install_immutable_json",
    "measured_process_tree_rss_bytes",
    "persist_p01_runtime_replay",
    "replay_hash_chain_journal",
    "reservation_snapshot_hash",
    "run_n31_resolution_proposals",
    "run_positive_readiness_checkpoint",
    "runner_implementation_hash",
    "runner_process_identity",
    "validate_p01_runtime_replay_receipt",
    "validate_positive_checkpoint_receipt",
    "validate_positive_symbol_resolution_receipt",
    "verify_clean_git_implementation_identity",
    "write_durable_heartbeat",
]


if __name__ == "__main__":
    raise SystemExit(main())
