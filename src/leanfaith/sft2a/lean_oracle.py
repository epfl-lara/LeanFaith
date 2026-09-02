"""Proof-free, cached signature elaboration through the frozen REPR Expr renderer."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanStatus
from leanfaith.representations import goal_v1
from leanfaith.representations.goal_v1 import (
    ClosedExprInput,
    ClosedExprSourceMaterial,
    CompileContext,
)
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.models import ProposerOutput

ORACLE_METHOD_VERSION = "sft2a_proof_free_signature_oracle_binder_hygiene_v1"
ORACLE_METHOD_VERSION_V2 = "sft2a_proof_free_signature_oracle_canonical_universes_v2"
# Identity of the v2 command skeleton around the elaborator body (scope ordering and layout).
COMMAND_TEMPLATE_VERSION_V2 = "sft2a_signature_command_namespaces_before_opens_v2_1"
# v3 (additive repair after shard 1): the proposer authoring view rendered from each certified
# closed Expr, candidate commands that carry imports/options/namespaces plus only individually
# validated `open scoped` entries (never the lossy flattened plain `open_context`), a preflighted
# effective-context identity, and prelude-versus-candidate attribution of every Lean failure.
ORACLE_METHOD_VERSION_V3 = "sft2a_proof_free_signature_oracle_authoring_view_v3"
COMMAND_TEMPLATE_VERSION_V3 = "sft2a_signature_command_effective_context_no_plain_opens_v3_1"
EFFECTIVE_CONTEXT_VERSION_V3 = "sft2a_effective_context_validated_scoped_v3_1"
AUTHORING_VIEW_VERSION_V3 = "sft2a_proposer_authoring_view_v3_1"
AUTHORING_VIEW_MARKER = "LFAUTHORINGV3 "
AUTHORING_VIEW_PROFILES: tuple[str, ...] = ("notation", "raw", "explicit")
INACCESSIBLE_NAME_MARK = "\u271d"
_FORBIDDEN_VIEW_MARKERS = ("[anonymous]", "\u22ef", "...", INACCESSIBLE_NAME_MARK)
_CACHE_VERSION_V1 = "v1"
_CACHE_VERSION_V2 = "v2"
_CACHE_VERSION_V3 = "v3"
CacheVersion = Literal["v1", "v2", "v3"]
EndpointRole = Literal["reference", "candidate", "authoring"]
LeanAttribution = Literal["context_prelude", "copied_inaccessible_name", "candidate_local"]
_CANONICAL_UNIVERSES = [f"u_{i}" for i in range(8)]
_SAFE_OPTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")
_INFRASTRUCTURE = {
    LeanStatus.TIMEOUT,
    LeanStatus.CRASH,
    LeanStatus.SETUP_ERROR,
    LeanStatus.UNSUPPORTED,
    LeanStatus.INTERNAL_ERROR,
}


class SignatureOracleError(RuntimeError):
    """A project, cache, request, or frozen-renderer invariant failed."""


@dataclass(frozen=True, slots=True)
class SignatureOracleResult:
    status: Literal["valid", "invalid", "infrastructure"]
    cache_key: str
    cache_hit: bool
    signature_sha256: str
    goal_v1: str | None
    sidecar: dict[str, object] | None
    lean_status: str
    request_hash: str | None
    elapsed_ms: int
    raw_response_path: str | None
    detail: str
    # v3 only: which layer an invalid result is attributable to (None for valid/infrastructure
    # results and for every v1/v2 result).
    attribution: LeanAttribution | None = None


@dataclass(frozen=True, slots=True)
class EffectiveContextV3:
    """Preflighted v3 elaboration context: raw census context minus plain opens, with only the
    individually validated ``open scoped`` entries retained."""

    raw: CompileContext
    context: CompileContext
    fingerprint: str
    record: dict[str, object]


@dataclass(frozen=True, slots=True)
class AuthoringViewResult:
    """Outcome of rendering and re-elaborating the SFT2A-only proposer authoring view."""

    status: Literal["validated", "unavailable"]
    declaration_name: str
    profile: str | None
    text: str | None
    closed_expr_hash: str | None
    canonical_level_params: tuple[str, ...]
    expected_closed_expr_hash: str
    expected_level_params: tuple[str, ...]
    cache_key: str | None
    cache_hit: bool
    lean_requests_executed: int
    detail: str
    attempts: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": AUTHORING_VIEW_VERSION_V3,
            "status": self.status,
            "declaration_name": self.declaration_name,
            "profile": self.profile,
            "text": self.text,
            "text_sha256": None if self.text is None else sha256_hex(self.text.encode("utf-8")),
            "closed_expr_hash": self.closed_expr_hash,
            "canonical_level_params": list(self.canonical_level_params),
            "expected_closed_expr_hash": self.expected_closed_expr_hash,
            "expected_level_params": list(self.expected_level_params),
            "identity_matched": self.status == "validated",
            "cache_key": self.cache_key,
            "cache_hit": self.cache_hit,
            "lean_requests_executed": self.lean_requests_executed,
            "detail": self.detail,
            "attempts": list(self.attempts),
        }


def compile_context(loaded: LoadedSFT2AConfig) -> CompileContext:
    source = loaded.config.root.compile_context
    return CompileContext(
        project_id=source.project_id,
        project_revision=source.project_revision,
        lean_version=source.lean_version,
        import_header=source.import_header,
        command_preamble=source.command_preamble,
        namespace_context=source.namespace_context,
        open_context=source.open_context,
        scoped_context=source.scoped_context,
        options=source.options,
    )


def elaborator_sha256(cache_version: CacheVersion) -> str:
    """Content identity of the Lean elaborator body plus command skeleton for one version."""

    if cache_version == "v1":
        return sha256_hex(_V1_NAMESPACE_BODY.encode("utf-8"))
    if cache_version == "v3":
        return sha256_hex((_V3_NAMESPACE_BODY + "\n" + COMMAND_TEMPLATE_VERSION_V3).encode("utf-8"))
    return sha256_hex((_V2_NAMESPACE_BODY + "\n" + COMMAND_TEMPLATE_VERSION_V2).encode("utf-8"))


def oracle_method_version(cache_version: CacheVersion) -> str:
    if cache_version == "v3":
        return ORACLE_METHOD_VERSION_V3
    if cache_version == "v2":
        return ORACLE_METHOD_VERSION_V2
    return ORACLE_METHOD_VERSION


def command_template_version(cache_version: CacheVersion) -> str | None:
    if cache_version == "v3":
        return COMMAND_TEMPLATE_VERSION_V3
    if cache_version == "v2":
        return COMMAND_TEMPLATE_VERSION_V2
    return None


def project_backend_context(context: CompileContext) -> CompileContext:
    """Project-level backend identity: imports, preamble, and options without root scopes."""

    return CompileContext(
        project_id=context.project_id,
        project_revision=context.project_revision,
        lean_version=context.lean_version,
        import_header=context.import_header,
        command_preamble=context.command_preamble,
        namespace_context=(),
        open_context=(),
        scoped_context=(),
        options=dict(context.options),
    )


def validate_signature_text(signature: str) -> str:
    """Apply the same strict proof/placeholder preflight to any signature source."""

    return ProposerOutput(
        schema_version=1,
        requested_polarity="preserving",
        mechanism="other",
        candidate_signature=signature,
        change_summary="signature preflight",
        judge_trap="not used",
        informative=True,
        proof_free=True,
    ).candidate_signature


def _option_value(value: str | int | float | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_V1_NAMESPACE_BODY = """
namespace LeanFaith.SFT2A.SignatureOracle

open Lean Elab Command Term Meta

private partial def freshDisplayedName
    (base : String) (used : Array String) (index : Nat := 0) : Name :=
  let base := if base.isEmpty then "x" else base
  let text := if index == 0 then base else s!"{base}_{index}"
  let candidate := Name.mkSimple text
  if used.contains candidate.toString then
    freshDisplayedName base used (index + 1)
  else
    candidate

/-- Erase parser macro scopes from named binder metadata without changing
    binder information, domains, bodies, bound-variable indices, or constants.
    This is a structural Expr pass, not a text renderer or re-elaborator. -/
private partial def canonicalizeBinderMetadata (used : Array String) : Expr → Expr
  | .forallE name domain body binderInfo =>
      let domain := canonicalizeBinderMetadata used domain
      let structuralArrow :=
        binderInfo == .default && !body.hasLooseBVar 0 && (name.isAnonymous || name.hasMacroScopes)
      if structuralArrow || name.isAnonymous then
        .forallE name domain (canonicalizeBinderMetadata used body) binderInfo
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .forallE name domain (canonicalizeBinderMetadata (used.push name.toString) body) binderInfo
  | .lam name domain body binderInfo =>
      let domain := canonicalizeBinderMetadata used domain
      if name.isAnonymous then
        .lam name domain (canonicalizeBinderMetadata used body) binderInfo
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .lam name domain (canonicalizeBinderMetadata (used.push name.toString) body) binderInfo
  | .letE name type value body nondep =>
      let type := canonicalizeBinderMetadata used type
      let value := canonicalizeBinderMetadata used value
      if name.isAnonymous then
        .letE name type value (canonicalizeBinderMetadata used body) nondep
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .letE name type value (canonicalizeBinderMetadata (used.push name.toString) body) nondep
  | .app fn arg =>
      .app (canonicalizeBinderMetadata used fn) (canonicalizeBinderMetadata used arg)
  | .proj typeName index base =>
      .proj typeName index (canonicalizeBinderMetadata used base)
  | .mdata data body => .mdata data (canonicalizeBinderMetadata used body)
  | e => e

/-- Elaborate one proof-free proposition term exactly once, then pass that same
    structural Expr, with parser scopes removed only from binder-name metadata,
    directly to the frozen REPR payload emitter. -/
elab "lfSft2aSignature" endpoint:str scope:str ":" signature:term : command => do
  liftTermElabM do
    let proposition ← Term.elabTerm signature (some (mkSort .zero))
    Term.synthesizeSyntheticMVarsNoPostponing
    let proposition ← instantiateMVars proposition
    let proposition := canonicalizeBinderMetadata #[] proposition
    LeanFaith.GoalV1.emitClosedProp
      endpoint.getString scope.getString "term_elaborated_proposition" proposition

end LeanFaith.SFT2A.SignatureOracle
""".strip()


_V2_NAMESPACE_BODY = """
namespace LeanFaith.SFT2A.SignatureOracle

open Lean Elab Command Term Meta

private partial def freshDisplayedName
    (base : String) (used : Array String) (index : Nat := 0) : Name :=
  let base := if base.isEmpty then "x" else base
  let text := if index == 0 then base else s!"{base}_{index}"
  let candidate := Name.mkSimple text
  if used.contains candidate.toString then
    freshDisplayedName base used (index + 1)
  else
    candidate

/-- Erase parser macro scopes from named binder metadata without changing
    binder information, domains, bodies, bound-variable indices, or constants.
    This is a structural Expr pass, not a text renderer or re-elaborator. -/
private partial def canonicalizeBinderMetadata (used : Array String) : Expr → Expr
  | .forallE name domain body binderInfo =>
      let domain := canonicalizeBinderMetadata used domain
      let structuralArrow :=
        binderInfo == .default && !body.hasLooseBVar 0 && (name.isAnonymous || name.hasMacroScopes)
      if structuralArrow || name.isAnonymous then
        .forallE name domain (canonicalizeBinderMetadata used body) binderInfo
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .forallE name domain (canonicalizeBinderMetadata (used.push name.toString) body) binderInfo
  | .lam name domain body binderInfo =>
      let domain := canonicalizeBinderMetadata used domain
      if name.isAnonymous then
        .lam name domain (canonicalizeBinderMetadata used body) binderInfo
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .lam name domain (canonicalizeBinderMetadata (used.push name.toString) body) binderInfo
  | .letE name type value body nondep =>
      let type := canonicalizeBinderMetadata used type
      let value := canonicalizeBinderMetadata used value
      if name.isAnonymous then
        .letE name type value (canonicalizeBinderMetadata used body) nondep
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .letE name type value (canonicalizeBinderMetadata (used.push name.toString) body) nondep
  | .app fn arg =>
      .app (canonicalizeBinderMetadata used fn) (canonicalizeBinderMetadata used arg)
  | .proj typeName index base =>
      .proj typeName index (canonicalizeBinderMetadata used base)
  | .mdata data body => .mdata data (canonicalizeBinderMetadata used body)
  | e => e

/-- Collect unassigned universe level metavariables in first-occurrence order. -/
private partial def collectLevelMVars (acc : Array LMVarId) : Level → Array LMVarId
  | .succ level => collectLevelMVars acc level
  | .max a b => collectLevelMVars (collectLevelMVars acc a) b
  | .imax a b => collectLevelMVars (collectLevelMVars acc a) b
  | .mvar mvarId => if acc.contains mvarId then acc else acc.push mvarId
  | _ => acc

/-- Traverse every Expr node that carries universe levels, including `Expr.sort`. -/
private partial def collectExprLevelMVars (acc : Array LMVarId) : Expr → Array LMVarId
  | .const _ levels => levels.foldl collectLevelMVars acc
  | .sort level => collectLevelMVars acc level
  | .app f a => collectExprLevelMVars (collectExprLevelMVars acc f) a
  | .forallE _ d b _ => collectExprLevelMVars (collectExprLevelMVars acc d) b
  | .lam _ d b _ => collectExprLevelMVars (collectExprLevelMVars acc d) b
  | .letE _ t v b _ =>
      collectExprLevelMVars (collectExprLevelMVars (collectExprLevelMVars acc t) v) b
  | .proj _ _ b => collectExprLevelMVars acc b
  | .mdata _ b => collectExprLevelMVars acc b
  | _ => acc

/-- Assign each remaining universe metavariable to a distinct canonical parameter
    `u_0`, `u_1`, ... in first-occurrence order. The frozen REPR renderer then never
    sees an unresolved universe metavariable, and independent universes stay distinct
    instead of collapsing into one level. -/
private def assignCanonicalUniverses (proposition : Expr) : MetaM Expr := do
  let proposition ← instantiateMVars proposition
  let pending := collectExprLevelMVars #[] proposition
  if pending.size > 8 then
    throwError "signature requires more than the eight canonical universes"
  let mut index := 0
  for mvarId in pending do
    unless (← isLevelMVarAssigned mvarId) do
      assignLevelMVar mvarId (.param (Name.mkSimple s!"u_{index}"))
    index := index + 1
  instantiateMVars proposition

/-- Elaborate one proof-free proposition term exactly once, assign remaining
    universe metavariables to distinct canonical parameters, then pass that same
    structural Expr to the frozen REPR payload emitter. -/
elab "lfSft2aSignatureV2" endpoint:str scope:str ":" signature:term : command => do
  liftTermElabM do
    let proposition ← Term.elabTerm signature (some (mkSort .zero))
    Term.synthesizeSyntheticMVarsNoPostponing
    let proposition ← assignCanonicalUniverses proposition
    let proposition := canonicalizeBinderMetadata #[] proposition
    LeanFaith.GoalV1.emitClosedProp
      endpoint.getString scope.getString "term_elaborated_proposition" proposition

end LeanFaith.SFT2A.SignatureOracle
""".strip()


_V3_NAMESPACE_BODY = """
namespace LeanFaith.SFT2A.SignatureOracle

open Lean Elab Command Term Meta

private partial def freshDisplayedName
    (base : String) (used : Array String) (index : Nat := 0) : Name :=
  let base := if base.isEmpty then "x" else base
  let text := if index == 0 then base else s!"{base}_{index}"
  let candidate := Name.mkSimple text
  if used.contains candidate.toString then
    freshDisplayedName base used (index + 1)
  else
    candidate

/-- Erase parser macro scopes from named binder metadata without changing
    binder information, domains, bodies, bound-variable indices, or constants.
    This is a structural Expr pass, not a text renderer or re-elaborator. -/
private partial def canonicalizeBinderMetadata (used : Array String) : Expr → Expr
  | .forallE name domain body binderInfo =>
      let domain := canonicalizeBinderMetadata used domain
      let structuralArrow :=
        binderInfo == .default && !body.hasLooseBVar 0 && (name.isAnonymous || name.hasMacroScopes)
      if structuralArrow || name.isAnonymous then
        .forallE name domain (canonicalizeBinderMetadata used body) binderInfo
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .forallE name domain (canonicalizeBinderMetadata (used.push name.toString) body) binderInfo
  | .lam name domain body binderInfo =>
      let domain := canonicalizeBinderMetadata used domain
      if name.isAnonymous then
        .lam name domain (canonicalizeBinderMetadata used body) binderInfo
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .lam name domain (canonicalizeBinderMetadata (used.push name.toString) body) binderInfo
  | .letE name type value body nondep =>
      let type := canonicalizeBinderMetadata used type
      let value := canonicalizeBinderMetadata used value
      if name.isAnonymous then
        .letE name type value (canonicalizeBinderMetadata used body) nondep
      else
        let name := freshDisplayedName name.eraseMacroScopes.toString used
        .letE name type value (canonicalizeBinderMetadata (used.push name.toString) body) nondep
  | .app fn arg =>
      .app (canonicalizeBinderMetadata used fn) (canonicalizeBinderMetadata used arg)
  | .proj typeName index base =>
      .proj typeName index (canonicalizeBinderMetadata used base)
  | .mdata data body => .mdata data (canonicalizeBinderMetadata used body)
  | e => e

/-- Collect unassigned universe level metavariables in first-occurrence order. -/
private partial def collectLevelMVars (acc : Array LMVarId) : Level → Array LMVarId
  | .succ level => collectLevelMVars acc level
  | .max a b => collectLevelMVars (collectLevelMVars acc a) b
  | .imax a b => collectLevelMVars (collectLevelMVars acc a) b
  | .mvar mvarId => if acc.contains mvarId then acc else acc.push mvarId
  | _ => acc

/-- Traverse every Expr node that carries universe levels, including `Expr.sort`. -/
private partial def collectExprLevelMVars (acc : Array LMVarId) : Expr → Array LMVarId
  | .const _ levels => levels.foldl collectLevelMVars acc
  | .sort level => collectLevelMVars acc level
  | .app f a => collectExprLevelMVars (collectExprLevelMVars acc f) a
  | .forallE _ d b _ => collectExprLevelMVars (collectExprLevelMVars acc d) b
  | .lam _ d b _ => collectExprLevelMVars (collectExprLevelMVars acc d) b
  | .letE _ t v b _ =>
      collectExprLevelMVars (collectExprLevelMVars (collectExprLevelMVars acc t) v) b
  | .proj _ _ b => collectExprLevelMVars acc b
  | .mdata _ b => collectExprLevelMVars acc b
  | _ => acc

/-- Assign each remaining universe metavariable to a distinct canonical parameter
    `u_0`, `u_1`, ... in first-occurrence order. -/
private def assignCanonicalUniverses (proposition : Expr) : MetaM Expr := do
  let proposition ← instantiateMVars proposition
  let pending := collectExprLevelMVars #[] proposition
  if pending.size > 8 then
    throwError "signature requires more than the eight canonical universes"
  let mut index := 0
  for mvarId in pending do
    unless (← isLevelMVarAssigned mvarId) do
      assignLevelMVar mvarId (.param (Name.mkSimple s!"u_{index}"))
    index := index + 1
  instantiateMVars proposition

/-- Elaborate one proof-free proposition syntax exactly once, assign remaining universe
    metavariables to distinct canonical parameters, canonicalize binder metadata, then pass
    that same structural Expr to the frozen REPR payload emitter. Shared by the candidate
    command and the authoring-view re-elaboration so both receive identical treatment. -/
private def emitProposition (endpoint scope origin : String) (stx : Syntax) : TermElabM Unit := do
  let proposition ← Term.elabTerm stx (some (mkSort .zero))
  Term.synthesizeSyntheticMVarsNoPostponing
  let proposition ← assignCanonicalUniverses proposition
  let proposition := canonicalizeBinderMetadata #[] proposition
  LeanFaith.GoalV1.emitClosedProp endpoint scope origin proposition

elab "lfSft2aSignatureV3" endpoint:str scope:str ":" signature:term : command => do
  liftTermElabM do
    emitProposition endpoint.getString scope.getString "term_elaborated_proposition" signature

/-- SFT2A-only proposer authoring view: the certified constant's closed type with canonical
    universes and every macro-scoped (inaccessible, `✝`-displayed) binder name alpha-renamed
    to a parseable display name, printed as one closed term with full names, then re-parsed
    and re-elaborated in this exact command scope so the emitted payload is the identity a
    candidate written from the view would receive. The frozen `goal_v1` renderer is untouched;
    this view is a sidecar, never a training-row or judge representation. -/
elab "lfSft2aAuthoringViewV3" endpoint:str scope:str name:str profile:str : command => do
  let constantName := name.getString.toName
  liftTermElabM do
    let some ci := (← getEnv).find? constantName
      | throwError m!"authoring_view_constant_not_found: {constantName}"
    let inputLevelParams := (collectLevelParams {} ci.type).params
    if inputLevelParams.size > 8 then
      throwError "authoring_view_requires_more_than_eight_canonical_universes"
    let canonicalNames := (List.range inputLevelParams.size).toArray.map
      fun i => Name.mkSimple s!"u_{i}"
    let renamed := canonicalizeBinderMetadata #[]
      (ci.type.instantiateLevelParamsArray inputLevelParams (canonicalNames.map Level.param))
    let base := LeanFaith.GoalV1.rendererOptions
      |>.setBool `pp.fullNames true
      |>.setBool `pp.funBinderTypes true
    -- `notation`: the frozen renderer's display choices with full names. `raw`: notation off
    -- (scoped notations need opens the census lost) but coercion arrows kept so instance paths
    -- survive. `explicit`: every implicit argument and universe printed, the last resort that
    -- reproduces the exact closed Expr when a shorter printing re-elaborates differently.
    let options :=
      if profile.getString == "raw" then
        base |>.setBool `pp.notation false
          |>.setBool `pp.proofs true |>.setBool `pp.deepTerms true
      else if profile.getString == "explicit" then
        base |>.setBool `pp.notation false |>.setBool `pp.explicit true
          |>.setBool `pp.universes true |>.setBool `pp.proofs true |>.setBool `pp.deepTerms true
      else
        base
    let text ← withOptions (fun _ => options) do
      return (← Meta.ppExpr renamed).pretty (width := 1000000)
    let view := Json.mkObj [
      ("schema_version", toJson 1),
      ("endpoint_id", Json.str endpoint.getString),
      ("constant_name", Json.str constantName.toString),
      ("profile", Json.str profile.getString),
      ("text", Json.str text),
      ("canonical_level_params", Json.arr (canonicalNames.map fun n => Json.str n.toString))
    ]
    IO.println s!"LFAUTHORINGV3 {view.compress}"
    match Lean.Parser.runParserCategory (← getEnv) `term text "<sft2a-authoring-view>" with
    | .error err => throwError m!"authoring_view_reparse_failed: {err}"
    | .ok stx =>
      emitProposition endpoint.getString scope.getString "authoring_view_reparsed_term" stx

end LeanFaith.SFT2A.SignatureOracle
""".strip()


def _command_keyword(cache_version: CacheVersion) -> str:
    if cache_version == "v3":
        return "lfSft2aSignatureV3"
    return "lfSft2aSignatureV2" if cache_version == "v2" else "lfSft2aSignature"


def _prelude_lines(context: CompileContext, cache_version: CacheVersion) -> list[str]:
    """Every command line before the live command: imports, frozen helper, elaborator body,
    preamble, options, and the root scope lines of the given version."""

    imports = [line.strip() for line in context.import_header.splitlines() if line.strip()]
    lines = ["import Lean", *(line for line in imports if line != "import Lean")]
    lines.append(goal_v1._helper_body())
    if cache_version in {"v2", "v3"}:
        lines.append("universe " + " ".join(_CANONICAL_UNIVERSES))
    namespace_body = {
        "v1": _V1_NAMESPACE_BODY,
        "v2": _V2_NAMESPACE_BODY,
        "v3": _V3_NAMESPACE_BODY,
    }[cache_version]
    lines.append(namespace_body)
    if context.command_preamble.strip():
        lines.append(context.command_preamble.rstrip())
    for option_name, value in sorted(context.options.items()):
        if _SAFE_OPTION_NAME.fullmatch(option_name) is None:
            raise SignatureOracleError(f"unsafe Lean option name {option_name!r}")
        lines.append(f"set_option {option_name} {_option_value(value)}")
    if cache_version == "v3":
        # v3 never emits the census's alphabetically flattened plain `open_context` (it lost the
        # source order and the `hiding`/explicit-open structure, so it poisoned otherwise valid
        # candidates). Only the individually validated `open scoped` entries of the preflighted
        # effective context are retained, inside the namespaces.
        if context.open_context:
            raise SignatureOracleError("v3 commands never emit plain open_context lines")
        lines.extend(f"namespace {name}" for name in context.namespace_context)
        lines.extend(f"open scoped {name}" for name in context.scoped_context)
    elif cache_version == "v2":
        # The census records `open` commands at the declaration offset, i.e. inside the enclosing
        # namespaces, where `open X` also resolves namespace-relative `N.X`. Emitting the opens
        # first made every candidate of such roots fail with `unknown namespace`.
        lines.extend(f"namespace {name}" for name in context.namespace_context)
        lines.extend(f"open {name}" for name in context.open_context)
        lines.extend(f"open scoped {name}" for name in context.scoped_context)
    else:
        lines.extend(f"open {name}" for name in context.open_context)
        lines.extend(f"open scoped {name}" for name in context.scoped_context)
        lines.extend(f"namespace {name}" for name in context.namespace_context)
    return lines


def prelude_line_count(context: CompileContext, cache_version: CacheVersion) -> int:
    """Number of physical lines before the live command of a signature command."""

    return sum(line.count("\n") + 1 for line in _prelude_lines(context, cache_version))


def _finish_command(
    lines: list[str], *, context: CompileContext, command_keyword: str, live_elaborators: int
) -> str:
    lines.extend(f"end {name}" for name in reversed(context.namespace_context))
    command = "\n".join(lines) + "\n"
    forbidden = ("sorry", "axiom", "admit")
    helper_start = command.index("namespace LeanFaith.GoalV1")
    live_suffix = command[command.rfind(command_keyword) :]
    if any(token in live_suffix for token in forbidden):
        raise SignatureOracleError("live signature command contains a forbidden proof token")
    if "Term.elabTerm" not in command or command.count("Term.elabTerm") != live_elaborators:
        raise SignatureOracleError("signature oracle must contain exactly one term elaborator")
    if command.count("LeanFaith.GoalV1.emitClosedProp") != 1:
        raise SignatureOracleError("signature oracle must call the frozen emitter exactly once")
    if helper_start < 0:
        raise SignatureOracleError("frozen GoalV1 helper was not injected")
    return command


def _signature_command(
    *,
    context: CompileContext,
    signature: str,
    endpoint_id: str,
    render_scope_id: str,
    cache_version: CacheVersion = "v1",
) -> str:
    command_keyword = _command_keyword(cache_version)
    lines = _prelude_lines(context, cache_version)
    lines.append(
        f"{command_keyword} {json.dumps(endpoint_id)} {json.dumps(render_scope_id)} : ({signature})"
    )
    return _finish_command(
        lines, context=context, command_keyword=command_keyword, live_elaborators=1
    )


def _prelude_command(context: CompileContext) -> str:
    """The v3 prelude alone (no live elaboration): used to preflight scope lines."""

    lines = _prelude_lines(context, "v3")
    lines.extend(f"end {name}" for name in reversed(context.namespace_context))
    command = "\n".join(lines) + "\n"
    if "lfSft2aSignatureV3" not in command or "namespace LeanFaith.GoalV1" not in command:
        raise SignatureOracleError("v3 prelude command lost its frozen helper or elaborator")
    return command


def _authoring_view_command(
    *,
    context: CompileContext,
    declaration_name: str,
    profile: str,
    endpoint_id: str,
    render_scope_id: str,
) -> str:
    if profile not in AUTHORING_VIEW_PROFILES:
        raise SignatureOracleError(f"unknown authoring view profile {profile!r}")
    if not declaration_name or any(char.isspace() for char in declaration_name):
        raise SignatureOracleError("authoring view declaration name is malformed")
    command_keyword = "lfSft2aAuthoringViewV3"
    lines = _prelude_lines(context, "v3")
    lines.append(
        f"{command_keyword} {json.dumps(endpoint_id)} {json.dumps(render_scope_id)} "
        f"{json.dumps(declaration_name)} {json.dumps(profile)}"
    )
    return _finish_command(
        lines, context=context, command_keyword=command_keyword, live_elaborators=1
    )


def _messages_on_prelude(
    messages: Sequence[Mapping[str, object]], *, prelude_lines: int
) -> list[dict[str, object]]:
    located: list[dict[str, object]] = []
    for message in messages:
        position = message.get("start_pos")
        line = position.get("line") if isinstance(position, Mapping) else None
        if isinstance(line, int) and not isinstance(line, bool) and line <= prelude_lines:
            located.append(dict(message))
    return located


def classify_lean_failure(
    *,
    signature: str,
    messages: Sequence[Mapping[str, object]],
    prelude_lines: int,
) -> LeanAttribution:
    """Attribute one invalid v3 elaboration to the prelude, a copied inaccessible name, or the
    candidate itself. Prelude attribution wins whenever any error sits on a prelude line."""

    prelude_errors = [
        message
        for message in _messages_on_prelude(messages, prelude_lines=prelude_lines)
        if message.get("severity") == "error"
    ]
    if prelude_errors:
        return "context_prelude"
    if INACCESSIBLE_NAME_MARK in signature:
        return "copied_inaccessible_name"
    return "candidate_local"


def effective_context_key(context: CompileContext) -> str:
    return hash_canonical(
        {
            "version": EFFECTIVE_CONTEXT_VERSION_V3,
            "raw_context": context.canonical_payload(),
            "command_template_version": COMMAND_TEMPLATE_VERSION_V3,
            "elaborator_sha256": elaborator_sha256("v3"),
        }
    )


def _atomic(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise SignatureOracleError(f"immutable Lean cache conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_cache(path: Path, expected_key: str) -> dict[str, object] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SignatureOracleError(f"Lean cache path is unsafe: {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SignatureOracleError(f"invalid Lean cache record {path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("cache_key") != expected_key:
        raise SignatureOracleError(f"Lean cache record key differs: {path}")
    if record.get("status") not in {"valid", "invalid"}:
        raise SignatureOracleError(f"Lean cache contains a nonterminal status: {path}")
    return record


class SignatureOracle:
    """One persistent backend plus an immutable content-addressed terminal cache."""

    def __init__(
        self,
        loaded: LoadedSFT2AConfig,
        *,
        backend: LeanBackend | None = None,
        cache_version: CacheVersion = "v1",
    ) -> None:
        self.loaded = loaded
        self.context = compile_context(loaded)
        self.staging_root = Path(loaded.config.staging_root)
        self.cache_root = self.staging_root / "lean_cache"
        self.cache_version = cache_version
        self.method_version = oracle_method_version(cache_version)
        self.elaborator_sha256 = elaborator_sha256(cache_version)
        self.command_template_version = command_template_version(cache_version)
        # v2/v3 bind the persistent backend to the project-level context (imports, preamble,
        # options) so one initialized Lean process serves every root of that project; the
        # root-local namespace/open/scoped lines stay in the command, cache key, and sidecar.
        self.backend_context = (
            project_backend_context(self.context) if cache_version in {"v2", "v3"} else self.context
        )
        self._effective: dict[str, EffectiveContextV3] = {}
        self.preflight_lean_requests = 0
        self._owns_backend = backend is None
        self._backend = backend or self._make_backend()

    def _make_backend(self) -> LeanBackend:
        source = self.loaded.config.root.compile_context
        project_dir = Path(source.project_dir).resolve(strict=True)
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=project_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or completed.stdout.strip() != source.project_revision:
            raise SignatureOracleError("Mathlib checkout differs from the frozen project revision")
        toolchain = (project_dir / "lean-toolchain").read_text(encoding="utf-8").strip()
        canonical_toolchain = toolchain.rsplit(":", maxsplit=1)[-1]
        if canonical_toolchain != source.lean_version:
            raise SignatureOracleError("Mathlib toolchain differs from the frozen Lean version")
        return LeanInteractBackend(
            BackendSettings(
                project_dir=project_dir,
                context_fingerprint=self.backend_context.fingerprint,
                environment_schema_version=source.environment_schema_version,
                raw_response_dir=self.staging_root / "lean_raw_responses",
                workers=source.workers,
                memory_hard_limit_mb=source.memory_hard_limit_mb,
                enable_parallel_elaboration=False,
            )
        )

    def rebind(self, loaded: LoadedSFT2AConfig) -> None:
        """Switch root-local context while retaining one initialized project backend."""

        current = self.loaded.config.root.compile_context
        target = loaded.config.root.compile_context
        identity_fields = (
            "project_id",
            "project_revision",
            "project_dir",
            "lean_version",
            "leaninteract_version",
            "repl_revision",
        )
        if any(getattr(current, field) != getattr(target, field) for field in identity_fields):
            raise SignatureOracleError("cannot rebind a persistent oracle across Lean projects")
        target_context = compile_context(loaded)
        if (
            self.cache_version in {"v2", "v3"}
            and project_backend_context(target_context) != self.backend_context
        ):
            raise SignatureOracleError(
                "cannot rebind a persistent v2/v3 oracle across import/option contexts"
            )
        if self.cache_version == "v1" and target_context != self.context:
            raise SignatureOracleError("v1 oracles bind one exact compile context per backend")
        self.loaded = loaded
        self.context = target_context

    # ---- v3: preflighted effective context -------------------------------------------------

    def _run_prelude(self, context: CompileContext, *, label: str) -> dict[str, object]:
        command = _prelude_command(context)
        request = LeanRequest(
            request_id=f"sft2a:prelude:{label}:{hash_canonical(context.canonical_payload())}",
            context_id=self.backend_context.compile_context_id,
            code=command,
            allow_sorry=False,
            timeout_seconds=300.0,
            metadata={"method_version": self.method_version, "stage": "effective_context"},
        )
        result = self._backend.run(request)
        self.preflight_lean_requests += 1
        if result.status in _INFRASTRUCTURE:
            raise SignatureOracleError(
                f"effective context preflight infrastructure failure: {result.status.value}"
            )
        return {
            "label": label,
            "diagnostics": [dict(message) for message in result.messages],
            "diagnostic_count": len(result.messages),
            "lean_status": result.status.value,
            "request_hash": result.request_hash,
            "raw_response_path": result.raw_response_path,
            "elapsed_ms": result.elapsed_ms,
        }

    def effective_context(self) -> EffectiveContextV3:
        """Resolve (and cache immutably) the v3 effective context of the bound root.

        Every ``open scoped`` entry of the census context is validated alone through the real
        prelude; entries that produce any diagnostic are dropped. The retained entries plus the
        namespaces are preflighted together and must produce zero diagnostics. Plain opens are
        always dropped. The record is content-addressed by the raw context, the v3 command
        template, and the elaborator identity.
        """

        if self.cache_version != "v3":
            raise SignatureOracleError("effective contexts exist only for the v3 oracle")
        raw = self.context
        key = effective_context_key(raw)
        cached = self._effective.get(key)
        if cached is not None:
            return cached
        cache_path = self.cache_root / "effective_context_v3" / key[:2] / f"{key}.json"
        record: dict[str, object] | None = None
        if cache_path.exists():
            if cache_path.is_symlink() or not cache_path.is_file():
                raise SignatureOracleError(f"effective context cache path is unsafe: {cache_path}")
            try:
                loaded_record = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SignatureOracleError(f"invalid effective context record: {exc}") from exc
            if not isinstance(loaded_record, dict) or loaded_record.get("cache_key") != key:
                raise SignatureOracleError("effective context record key differs")
            record = loaded_record
        if record is None:
            validated: list[str] = []
            dropped: list[dict[str, object]] = []
            probes: list[dict[str, object]] = []
            seen: set[str] = set()
            for entry in raw.scoped_context:
                if entry in seen:
                    continue
                seen.add(entry)
                probe_context = CompileContext(
                    project_id=raw.project_id,
                    project_revision=raw.project_revision,
                    lean_version=raw.lean_version,
                    import_header=raw.import_header,
                    command_preamble=raw.command_preamble,
                    namespace_context=raw.namespace_context,
                    open_context=(),
                    scoped_context=(entry,),
                    options=dict(raw.options),
                )
                probe = self._run_prelude(probe_context, label=f"scoped:{entry}")
                probes.append(probe)
                if (
                    probe["diagnostic_count"] == 0
                    and probe["lean_status"] == LeanStatus.VALID.value
                ):
                    validated.append(entry)
                else:
                    dropped.append(
                        {
                            "name": entry,
                            "detail": "; ".join(
                                str(message.get("data", ""))[:300]
                                for message in cast_messages(probe["diagnostics"])
                            ),
                        }
                    )
            effective = CompileContext(
                project_id=raw.project_id,
                project_revision=raw.project_revision,
                lean_version=raw.lean_version,
                import_header=raw.import_header,
                command_preamble=raw.command_preamble,
                namespace_context=raw.namespace_context,
                open_context=(),
                scoped_context=tuple(validated),
                options=dict(raw.options),
            )
            combined = self._run_prelude(effective, label="effective")
            if combined["diagnostic_count"] != 0 or combined["lean_status"] != "valid":
                raise SignatureOracleError(
                    "v3 effective context preflight produced diagnostics: "
                    + "; ".join(
                        str(message.get("data", ""))[:300]
                        for message in cast_messages(combined["diagnostics"])
                    )
                )
            effective_payload = {
                "version": EFFECTIVE_CONTEXT_VERSION_V3,
                "context": effective.canonical_payload(),
                "plain_opens_dropped": list(raw.open_context),
                "scoped_dropped": [item["name"] for item in dropped],
            }
            record = {
                "version": EFFECTIVE_CONTEXT_VERSION_V3,
                "cache_key": key,
                "raw_context": raw.canonical_payload(),
                "raw_fingerprint": raw.fingerprint,
                "effective_context": effective.canonical_payload(),
                "effective_payload": effective_payload,
                "effective_fingerprint": hash_canonical(effective_payload),
                "plain_opens_dropped": list(raw.open_context),
                "scoped_validated": validated,
                "scoped_dropped": dropped,
                "scoped_probes": probes,
                "combined_preflight": combined,
                "prelude_line_count": prelude_line_count(effective, "v3"),
                "command_template_version": COMMAND_TEMPLATE_VERSION_V3,
                "elaborator_sha256": self.elaborator_sha256,
                "lean_requests_executed": len(probes) + 1,
            }
            _atomic(cache_path, canonical_json_bytes(record) + b"\n")
        effective_context = CompileContext(
            project_id=raw.project_id,
            project_revision=raw.project_revision,
            lean_version=raw.lean_version,
            import_header=raw.import_header,
            command_preamble=raw.command_preamble,
            namespace_context=raw.namespace_context,
            open_context=(),
            scoped_context=tuple(str(item) for item in cast_list(record["scoped_validated"])),
            options=dict(raw.options),
        )
        resolved = EffectiveContextV3(
            raw=raw,
            context=effective_context,
            fingerprint=str(record["effective_fingerprint"]),
            record=record,
        )
        self._effective[key] = resolved
        return resolved

    # ---- v3: SFT2A-only proposer authoring view ----------------------------------------------

    def authoring_view(
        self,
        declaration_name: str,
        *,
        expected_closed_expr_hash: str,
        expected_level_params: Sequence[str],
        profiles: Sequence[str] = AUTHORING_VIEW_PROFILES,
    ) -> AuthoringViewResult:
        """Render, re-parse, and re-elaborate the authoring view of one certified constant.

        A profile is validated only when the re-elaborated closed Expr hash and canonical
        universe profile equal the certified reference's, the text carries no inaccessible name
        or placeholder marker, and the frozen emitter produced exactly one payload. Terminal
        outcomes are cached immutably per (effective context, declaration, profile).
        """

        if self.cache_version != "v3":
            raise SignatureOracleError("authoring views exist only for the v3 oracle")
        expected_levels = tuple(expected_level_params)
        effective = self.effective_context()
        attempts: list[dict[str, object]] = []
        executed = 0
        for profile in profiles:
            pins = self.loaded.config.root.compile_context
            key_payload: dict[str, object] = {
                "method_version": self.method_version,
                "stage": "authoring_view",
                "version": AUTHORING_VIEW_VERSION_V3,
                "declaration_name": declaration_name,
                "profile": profile,
                "effective_context": effective.record["effective_payload"],
                "expected_closed_expr_hash": expected_closed_expr_hash,
                "expected_level_params": list(expected_levels),
                "leaninteract_version": pins.leaninteract_version,
                "repl_revision": pins.repl_revision,
                "repr": self.loaded.config.repr.model_dump(mode="json"),
                "elaborator_sha256": self.elaborator_sha256,
                "command_template_version": COMMAND_TEMPLATE_VERSION_V3,
            }
            cache_key = hash_canonical(key_payload)
            cache_path = self.cache_root / "authoring_view_v3" / cache_key[:2] / f"{cache_key}.json"
            cached = _load_cache(cache_path, cache_key) if cache_path.exists() else None
            if cached is not None:
                terminal = cached
                cache_hit = True
            else:
                cache_hit = False
                endpoint_id = (
                    f"sft2a-authoring:{profile}:{hash_canonical([declaration_name, cache_key])}"
                )
                render_scope_id = "sft2a-signature-v3:" + effective.fingerprint
                command = _authoring_view_command(
                    context=effective.context,
                    declaration_name=declaration_name,
                    profile=profile,
                    endpoint_id=endpoint_id,
                    render_scope_id=render_scope_id,
                )
                request = LeanRequest(
                    request_id=f"sft2a:authoring:{cache_key}",
                    context_id=self.backend_context.compile_context_id,
                    code=command,
                    allow_sorry=False,
                    timeout_seconds=300.0,
                    metadata={"method_version": self.method_version, "stage": "authoring_view"},
                )
                result = self._backend.run(request)
                executed += 1
                if result.status in _INFRASTRUCTURE:
                    raise SignatureOracleError(
                        f"authoring view infrastructure failure: {result.status.value}"
                    )
                view = _parse_authoring_view(result.messages, endpoint_id)
                payloads, issues = goal_v1._parse_closed_expr_payloads(
                    result.messages, {endpoint_id}
                )
                payload = payloads.get(endpoint_id)
                text = None if view is None else str(view.get("text"))
                detail_parts: list[str] = []
                if result.status != LeanStatus.VALID or result.sorries:
                    detail_parts.append(
                        "; ".join(str(message.get("data", ""))[:300] for message in result.messages)
                        or result.status.value
                    )
                detail_parts.extend(issues)
                if view is None:
                    detail_parts.append("authoring view payload missing")
                elif any(marker in str(text) for marker in _FORBIDDEN_VIEW_MARKERS):
                    detail_parts.append(
                        "authoring view text contains an inaccessible name or placeholder"
                    )
                observed_hash: str | None = None
                observed_levels: list[str] = []
                if payload is None:
                    detail_parts.append("missing frozen REPR payload for the re-elaborated view")
                else:
                    tree = payload.get("expr_tree")
                    levels = payload.get("canonical_level_params")
                    if not isinstance(tree, dict) or not isinstance(levels, list):
                        detail_parts.append("malformed frozen REPR payload for the view")
                    else:
                        observed_hash = hash_canonical(tree)
                        observed_levels = [str(level) for level in levels]
                        if observed_hash != expected_closed_expr_hash:
                            detail_parts.append(
                                "re-elaborated view differs from the certified closed Expr"
                            )
                        if tuple(observed_levels) != expected_levels:
                            detail_parts.append(
                                "re-elaborated view changed the canonical universe profile"
                            )
                validated = not detail_parts and text is not None
                terminal = {
                    "version": "leanfaith_sft2a_authoring_view_cache_v1",
                    "cache_key": cache_key,
                    "key_payload": key_payload,
                    "status": "valid" if validated else "invalid",
                    "view_status": "validated" if validated else "unavailable",
                    "profile": profile,
                    "declaration_name": declaration_name,
                    "text": text,
                    "closed_expr_hash": observed_hash,
                    "canonical_level_params": observed_levels,
                    "goal_v1": None if payload is None else payload.get("goal_v1"),
                    "lean_status": result.status.value,
                    "request_hash": result.request_hash,
                    "elapsed_ms": result.elapsed_ms,
                    "raw_response_path": result.raw_response_path,
                    "detail": "; ".join(detail_parts)
                    if detail_parts
                    else "authoring view validated",
                    "effective_context_fingerprint": effective.fingerprint,
                }
                _atomic(cache_path, canonical_json_bytes(terminal) + b"\n")
            attempts.append(
                {
                    "profile": profile,
                    "view_status": terminal["view_status"],
                    "cache_key": cache_key,
                    "cache_hit": cache_hit,
                    "detail": str(terminal["detail"])[:500],
                    "elapsed_ms": terminal["elapsed_ms"],
                }
            )
            if terminal["view_status"] == "validated":
                return AuthoringViewResult(
                    status="validated",
                    declaration_name=declaration_name,
                    profile=profile,
                    text=str(terminal["text"]),
                    closed_expr_hash=str(terminal["closed_expr_hash"]),
                    canonical_level_params=tuple(
                        str(level) for level in cast_list(terminal["canonical_level_params"])
                    ),
                    expected_closed_expr_hash=expected_closed_expr_hash,
                    expected_level_params=expected_levels,
                    cache_key=cache_key,
                    cache_hit=cache_hit,
                    lean_requests_executed=executed,
                    detail=str(terminal["detail"]),
                    attempts=tuple(attempts),
                )
        return AuthoringViewResult(
            status="unavailable",
            declaration_name=declaration_name,
            profile=None,
            text=None,
            closed_expr_hash=None,
            canonical_level_params=(),
            expected_closed_expr_hash=expected_closed_expr_hash,
            expected_level_params=expected_levels,
            cache_key=None,
            cache_hit=all(bool(item["cache_hit"]) for item in attempts) if attempts else False,
            lean_requests_executed=executed,
            detail="no profile re-elaborated to the certified identity: "
            + " | ".join(f"{item['profile']}: {item['detail']}" for item in attempts),
            attempts=tuple(attempts),
        )

    def close(self) -> None:
        if self._owns_backend:
            self._backend.close()

    def elaborate(
        self,
        signature: str,
        *,
        endpoint_role: EndpointRole,
    ) -> SignatureOracleResult:
        signature = validate_signature_text(signature)
        signature_sha256 = sha256_hex(signature.encode("utf-8"))
        effective: EffectiveContextV3 | None = (
            self.effective_context() if self.cache_version == "v3" else None
        )
        command_context = self.context if effective is None else effective.context
        key_payload: dict[str, object] = {
            "method_version": self.method_version,
            "signature_sha256": signature_sha256,
            "endpoint_role": endpoint_role,
            "compile_context": (
                self.context.canonical_payload()
                if effective is None
                else effective.record["effective_payload"]
            ),
            "leaninteract_version": self.loaded.config.root.compile_context.leaninteract_version,
            "repl_revision": self.loaded.config.root.compile_context.repl_revision,
            "repr": self.loaded.config.repr.model_dump(mode="json"),
        }
        if self.cache_version == "v1":
            key_payload["oracle_source_sha256"] = hash_file(Path(__file__))
        else:
            # v2/v3 bind the Lean elaborator body (semantic identity) rather than this Python
            # file's bytes, so unrelated Python edits keep the cache while any change to the
            # elaboration semantics yields fresh keys.
            key_payload["elaborator_sha256"] = self.elaborator_sha256
        if effective is not None:
            key_payload["command_template_version"] = COMMAND_TEMPLATE_VERSION_V3
        cache_key = hash_canonical(key_payload)
        cache_path = self.cache_root / self.cache_version / cache_key[:2] / f"{cache_key}.json"
        cached = _load_cache(cache_path, cache_key)
        if cached is not None:
            sidecar = cached.get("sidecar")
            if sidecar is not None and not isinstance(sidecar, dict):
                raise SignatureOracleError("cached REPR sidecar is not an object")
            cached_goal = cached.get("goal_v1")
            if cached_goal is not None and not isinstance(cached_goal, str):
                raise SignatureOracleError("cached goal_v1 is not text")
            cached_status = cached.get("status")
            if cached_status not in {"valid", "invalid"}:
                raise SignatureOracleError("cached status is not terminal")
            cached_elapsed_ms = cached.get("elapsed_ms")
            if not isinstance(cached_elapsed_ms, int):
                raise SignatureOracleError("cached elapsed_ms is not an integer")
            cached_attribution = cached.get("attribution")
            return SignatureOracleResult(
                status=cached_status,
                cache_key=cache_key,
                cache_hit=True,
                signature_sha256=signature_sha256,
                goal_v1=cached_goal,
                sidecar=sidecar,
                lean_status=str(cached["lean_status"]),
                request_hash=str(cached["request_hash"]),
                elapsed_ms=cached_elapsed_ms,
                raw_response_path=(
                    None
                    if cached.get("raw_response_path") is None
                    else str(cached["raw_response_path"])
                ),
                detail=str(cached["detail"]),
                attribution=_attribution_literal(cached_attribution),
            )

        endpoint_id = f"sft2a-signature:{endpoint_role}:{signature_sha256}"
        render_scope_id = (
            "sft2a-signature:" + self.context.fingerprint
            if effective is None
            else "sft2a-signature-v3:" + effective.fingerprint
        )
        command = _signature_command(
            context=command_context,
            signature=signature,
            endpoint_id=endpoint_id,
            render_scope_id=render_scope_id,
            cache_version=self.cache_version,
        )
        request = LeanRequest(
            request_id=f"sft2a:{endpoint_role}:{signature_sha256}",
            context_id=self.backend_context.compile_context_id,
            code=command,
            allow_sorry=False,
            timeout_seconds=300.0,
            metadata={"method_version": self.method_version},
        )
        result = self._backend.run(request)
        detail = result.infrastructure_error or "; ".join(
            str(message.get("data", "")) for message in result.messages
        )
        if result.status in _INFRASTRUCTURE:
            return SignatureOracleResult(
                status="infrastructure",
                cache_key=cache_key,
                cache_hit=False,
                signature_sha256=signature_sha256,
                goal_v1=None,
                sidecar=None,
                lean_status=result.status.value,
                request_hash=result.request_hash,
                elapsed_ms=result.elapsed_ms,
                raw_response_path=result.raw_response_path,
                detail=detail or result.status.value,
            )

        payloads, issues = goal_v1._parse_closed_expr_payloads(result.messages, {endpoint_id})
        valid = result.status == LeanStatus.VALID and not result.sorries and not issues
        sidecar_dict: dict[str, object] | None = None
        goal: str | None = None
        if valid:
            payload = payloads.get(endpoint_id)
            if payload is None:
                valid = False
                detail = "missing frozen REPR payload"
            else:
                item = ClosedExprInput(
                    endpoint_id=endpoint_id,
                    endpoint_role="candidate" if endpoint_role == "authoring" else endpoint_role,
                    expr_origin="term_elaborated_proposition",
                    source_material=ClosedExprSourceMaterial(
                        kind="proposition_text",
                        proposition_text=signature,
                    ),
                )
                try:
                    sidecar = goal_v1._closed_expr_sidecar_from_payload(
                        payload=payload,
                        item=item,
                        compile_context=command_context,
                        render_scope_id=render_scope_id,
                        implementation_identity=goal_v1._implementation_identity(),
                    )
                except (goal_v1.GoalV1Error, ValueError) as exc:
                    valid = False
                    detail = f"frozen REPR payload rejected: {exc}"
                else:
                    sidecar_dict = sidecar.to_dict()
                    goal = sidecar.core_text()
        status: Literal["valid", "invalid"] = "valid" if valid else "invalid"
        if issues:
            detail = "; ".join(issues)
        if not detail:
            detail = (
                "proof-free signature elaborated and rendered" if valid else result.status.value
            )
        attribution: LeanAttribution | None = None
        prelude_lines: int | None = None
        if effective is not None:
            prelude_lines = int(cast_int(effective.record["prelude_line_count"]))
            if status == "invalid":
                attribution = classify_lean_failure(
                    signature=signature, messages=result.messages, prelude_lines=prelude_lines
                )
        terminal: dict[str, object] = {
            "version": "leanfaith_sft2a_signature_cache_v1",
            "cache_key": cache_key,
            "key_payload": key_payload,
            "status": status,
            "signature_sha256": signature_sha256,
            "goal_v1": goal,
            "sidecar": sidecar_dict,
            "lean_status": result.status.value,
            "request_hash": result.request_hash,
            "elapsed_ms": result.elapsed_ms,
            "raw_response_path": result.raw_response_path,
            "detail": detail,
        }
        if effective is not None:
            terminal["attribution"] = attribution
            terminal["prelude_line_count"] = prelude_lines
            terminal["effective_context_fingerprint"] = effective.fingerprint
            terminal["command_template_version"] = COMMAND_TEMPLATE_VERSION_V3
        _atomic(cache_path, canonical_json_bytes(terminal) + b"\n")
        return SignatureOracleResult(
            status=status,
            cache_key=cache_key,
            cache_hit=False,
            signature_sha256=signature_sha256,
            goal_v1=goal,
            sidecar=sidecar_dict,
            lean_status=result.status.value,
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=result.raw_response_path,
            detail=detail,
            attribution=attribution,
        )


def _attribution_literal(value: object) -> LeanAttribution | None:
    if value == "context_prelude":
        return "context_prelude"
    if value == "copied_inaccessible_name":
        return "copied_inaccessible_name"
    if value == "candidate_local":
        return "candidate_local"
    return None


def cast_messages(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def cast_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def cast_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SignatureOracleError("expected an integer cache field")
    return value


def _parse_authoring_view(
    messages: Sequence[Mapping[str, object]], endpoint_id: str
) -> dict[str, object] | None:
    selected: dict[str, object] | None = None
    for message in messages:
        for line in str(message.get("data", "")).splitlines():
            marker = line.find(AUTHORING_VIEW_MARKER)
            if marker < 0:
                continue
            try:
                payload = json.loads(line[marker + len(AUTHORING_VIEW_MARKER) :])
            except json.JSONDecodeError:
                continue
            if (
                isinstance(payload, dict)
                and payload.get("endpoint_id") == endpoint_id
                and isinstance(payload.get("text"), str)
            ):
                if selected is not None:
                    return None
                selected = payload
    return selected


__all__ = [
    "AUTHORING_VIEW_PROFILES",
    "AUTHORING_VIEW_VERSION_V3",
    "COMMAND_TEMPLATE_VERSION_V2",
    "COMMAND_TEMPLATE_VERSION_V3",
    "EFFECTIVE_CONTEXT_VERSION_V3",
    "INACCESSIBLE_NAME_MARK",
    "ORACLE_METHOD_VERSION",
    "ORACLE_METHOD_VERSION_V2",
    "ORACLE_METHOD_VERSION_V3",
    "AuthoringViewResult",
    "CacheVersion",
    "EffectiveContextV3",
    "EndpointRole",
    "LeanAttribution",
    "SignatureOracle",
    "SignatureOracleError",
    "SignatureOracleResult",
    "classify_lean_failure",
    "command_template_version",
    "compile_context",
    "effective_context_key",
    "elaborator_sha256",
    "oracle_method_version",
    "prelude_line_count",
    "project_backend_context",
    "validate_signature_text",
]
