"""Proof-free, cached signature elaboration through the frozen REPR Expr renderer."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
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
_CACHE_VERSION_V1 = "v1"
_CACHE_VERSION_V2 = "v2"
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

/-- Assign any remaining universe level metavariables to a canonical parameter
    so that the frozen REPR renderer never sees an unresolved universe mvar. -/
private def assignLevelMVars (canonical : Level) : Level → MetaM Level
  | .succ level => return .succ (← assignLevelMVars canonical level)
  | .max a b => return .max (← assignLevelMVars canonical a) (← assignLevelMVars canonical b)
  | .imax a b => return .imax (← assignLevelMVars canonical a) (← assignLevelMVars canonical b)
  | .mvar mvarId =>
    unless (← mvarId.isAssigned) do
      mvarId.assign canonical
    pure (.mvar mvarId)
  | level => pure level

private partial def assignUnivMVars (canonical : Level) : Expr → MetaM Expr
  | .const declName levels =>
    let newLevels ← levels.mapM (assignLevelMVars canonical)
    pure (.const declName newLevels)
  | .sort level => return .sort (← assignLevelMVars canonical level)
  | .app f arg =>
    return .app (← assignUnivMVars canonical f) (← assignUnivMVars canonical arg)
  | .forallE n d b bi =>
    return .forallE n (← assignUnivMVars canonical d) (← assignUnivMVars canonical b) bi
  | .lam n d b bi =>
    return .lam n (← assignUnivMVars canonical d) (← assignUnivMVars canonical b) bi
  | .letE n t v b nd =>
    return .letE n (← assignUnivMVars canonical t) (← assignUnivMVars canonical v)
                 (← assignUnivMVars canonical b) nd
  | .proj t i b => return .proj t i (← assignUnivMVars canonical b)
  | .mdata m b => return .mdata m (← assignUnivMVars canonical b)
  | e => pure e

/-- Elaborate one proof-free proposition term exactly once, assign remaining
    universe metavariables to canonical parameters, then pass that same
    structural Expr to the frozen REPR payload emitter. -/
elab "lfSft2aSignatureV2" endpoint:str scope:str ":" signature:term : command => do
  liftTermElabM do
    let proposition ← Term.elabTerm signature (some (mkSort .zero))
    Term.synthesizeSyntheticMVarsNoPostponing
    let proposition ← instantiateMVars proposition
    let proposition ← assignUnivMVars (.param `u_0) proposition
    let proposition ← instantiateMVars proposition
    let proposition := canonicalizeBinderMetadata #[] proposition
    LeanFaith.GoalV1.emitClosedProp
      endpoint.getString scope.getString "term_elaborated_proposition" proposition

end LeanFaith.SFT2A.SignatureOracle
""".strip()


def _signature_command(
    *,
    context: CompileContext,
    signature: str,
    endpoint_id: str,
    render_scope_id: str,
    cache_version: Literal["v1", "v2"] = "v1",
) -> str:
    imports = [line.strip() for line in context.import_header.splitlines() if line.strip()]
    lines = ["import Lean", *(line for line in imports if line != "import Lean")]
    lines.append(goal_v1._helper_body())
    if cache_version == "v2":
        lines.append("universe " + " ".join(_CANONICAL_UNIVERSES))
    command_keyword = "lfSft2aSignatureV2" if cache_version == "v2" else "lfSft2aSignature"
    namespace_body = _V1_NAMESPACE_BODY if cache_version == "v1" else _V2_NAMESPACE_BODY
    lines.append(namespace_body)
    if context.command_preamble.strip():
        lines.append(context.command_preamble.rstrip())
    for option_name, value in sorted(context.options.items()):
        if _SAFE_OPTION_NAME.fullmatch(option_name) is None:
            raise SignatureOracleError(f"unsafe Lean option name {option_name!r}")
        lines.append(f"set_option {option_name} {_option_value(value)}")
    lines.extend(f"open {name}" for name in context.open_context)
    lines.extend(f"open scoped {name}" for name in context.scoped_context)
    lines.extend(f"namespace {name}" for name in context.namespace_context)
    lines.append(
        f"{command_keyword} {json.dumps(endpoint_id)} {json.dumps(render_scope_id)} : ({signature})"
    )
    lines.extend(f"end {name}" for name in reversed(context.namespace_context))
    command = "\n".join(lines) + "\n"
    forbidden = ("sorry", "axiom", "admit")
    helper_start = command.index("namespace LeanFaith.GoalV1")
    live_suffix = command[command.rfind(command_keyword) :]
    if any(token in live_suffix for token in forbidden):
        raise SignatureOracleError("live signature command contains a forbidden proof token")
    if "Term.elabTerm" not in command or command.count("Term.elabTerm") != 1:
        raise SignatureOracleError("signature oracle must contain exactly one term elaborator")
    if command.count("LeanFaith.GoalV1.emitClosedProp") != 1:
        raise SignatureOracleError("signature oracle must call the frozen emitter exactly once")
    if helper_start < 0:
        raise SignatureOracleError("frozen GoalV1 helper was not injected")
    return command


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
        cache_version: Literal["v1", "v2"] = "v1",
    ) -> None:
        self.loaded = loaded
        self.context = compile_context(loaded)
        self.staging_root = Path(loaded.config.staging_root)
        self.cache_root = self.staging_root / "lean_cache"
        self.cache_version = cache_version
        self.method_version = (
            ORACLE_METHOD_VERSION_V2 if cache_version == "v2" else ORACLE_METHOD_VERSION
        )
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
                context_fingerprint=self.context.fingerprint,
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
        self.loaded = loaded
        self.context = compile_context(loaded)

    def close(self) -> None:
        if self._owns_backend:
            self._backend.close()

    def elaborate(
        self,
        signature: str,
        *,
        endpoint_role: Literal["reference", "candidate"],
    ) -> SignatureOracleResult:
        signature = validate_signature_text(signature)
        signature_sha256 = sha256_hex(signature.encode("utf-8"))
        key_payload: dict[str, object] = {
            "method_version": self.method_version,
            "signature_sha256": signature_sha256,
            "endpoint_role": endpoint_role,
            "compile_context": self.context.canonical_payload(),
            "leaninteract_version": self.loaded.config.root.compile_context.leaninteract_version,
            "repl_revision": self.loaded.config.root.compile_context.repl_revision,
            "repr": self.loaded.config.repr.model_dump(mode="json"),
        }
        if self.cache_version == "v1":
            key_payload["oracle_source_sha256"] = hash_file(Path(__file__))
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
            )

        endpoint_id = f"sft2a-signature:{endpoint_role}:{signature_sha256}"
        render_scope_id = "sft2a-signature:" + self.context.fingerprint
        command = _signature_command(
            context=self.context,
            signature=signature,
            endpoint_id=endpoint_id,
            render_scope_id=render_scope_id,
            cache_version=self.cache_version,
        )
        request = LeanRequest(
            request_id=f"sft2a:{endpoint_role}:{signature_sha256}",
            context_id=self.context.compile_context_id,
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
                    endpoint_role=endpoint_role,
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
                        compile_context=self.context,
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
        )


__all__ = [
    "ORACLE_METHOD_VERSION",
    "ORACLE_METHOD_VERSION_V2",
    "SignatureOracle",
    "SignatureOracleError",
    "SignatureOracleResult",
    "compile_context",
    "validate_signature_text",
]
