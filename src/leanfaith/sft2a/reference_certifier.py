"""Authoritative, additive reference certification for SFT2A v5.2.

Imported library references are looked up by qualified constant name and their
actual theorem ``ConstantInfo.type`` is sent directly to frozen REPR. Only
compiler-data rows use proof-free proposition-term elaboration.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanStatus
from leanfaith.representations import goal_v1
from leanfaith.representations.goal_v1 import (
    ClosedExprInput,
    ClosedExprSourceMaterial,
    CompileContext,
)
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.models import OneRootConfig, SFT2AV52Config

CERTIFIER_VERSION = "sft2a_authoritative_reference_certifier_v5_2_1"
_FAILURE_MARKER = "LFSFT2AREFCERTJSON "
_SAFE_OPTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*$")
_INFRASTRUCTURE = {
    LeanStatus.TIMEOUT,
    LeanStatus.CRASH,
    LeanStatus.SETUP_ERROR,
    LeanStatus.UNSUPPORTED,
    LeanStatus.INTERNAL_ERROR,
}


class ReferenceCertifierError(RuntimeError):
    """A reference cache, project, context, REPR, or resource invariant failed."""


@dataclass(frozen=True, slots=True)
class ReferenceCertificationResult:
    status: Literal["valid", "invalid", "infrastructure"]
    taxonomy: str
    route: Literal["loaded_constant_type", "term_elaborated_proposition"]
    cache_key: str
    cache_path: Path
    cache_hit: bool
    goal_v1: str | None
    closed_expr_hash: str | None
    rendered_goal_hash: str | None
    sidecar: dict[str, object] | None
    sidecar_hash: str | None
    compile_context_id: str
    lean_status: str
    request_hash: str | None
    elapsed_ms: int
    measured_rss_peak_bytes: int
    raw_response_path: str | None
    detail: str


def _v5_2(loaded: LoadedSFT2AConfig) -> SFT2AV52Config:
    if not isinstance(loaded.config, SFT2AV52Config):
        raise ReferenceCertifierError("authoritative reference certification requires v5.2")
    return loaded.config


def _compile_context(root: OneRootConfig) -> CompileContext:
    source = root.compile_context
    authoritative_constant_lookup = root.source in {"mathlib", "physlib", "cslib"}
    return CompileContext(
        project_id=source.project_id,
        project_revision=source.project_revision,
        lean_version=source.lean_version,
        import_header=source.import_header,
        command_preamble=source.command_preamble,
        namespace_context=() if authoritative_constant_lookup else source.namespace_context,
        open_context=() if authoritative_constant_lookup else source.open_context,
        scoped_context=() if authoritative_constant_lookup else source.scoped_context,
        options=source.options,
    )


def _option_value(value: str | int | float | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _context_prefix(context: CompileContext) -> list[str]:
    imports = [line.strip() for line in context.import_header.splitlines() if line.strip()]
    lines = ["import Lean", *(line for line in imports if line != "import Lean")]
    lines.append(goal_v1._helper_body())
    if context.command_preamble.strip():
        lines.append(context.command_preamble.rstrip())
    for option_name, value in sorted(context.options.items()):
        if _SAFE_OPTION_NAME.fullmatch(option_name) is None:
            raise ReferenceCertifierError(f"unsafe Lean option name {option_name!r}")
        lines.append(f"set_option {option_name} {_option_value(value)}")
    return lines


def constant_lookup_command(
    *,
    context: CompileContext,
    declaration_name: str,
    endpoint_id: str,
    render_scope_id: str,
) -> str:
    """Build a command with no term elaboration or source-text type authority."""

    lines = _context_prefix(context)
    lines.append(
        """
namespace LeanFaith.SFT2A.ReferenceCertification

open Lean Elab Command Term Meta

private def emitFailure (name code detail : String) : IO Unit := do
  let payload := Json.mkObj [
    ("name", Json.str name),
    ("status", Json.str "invalid"),
    ("taxonomy", Json.str code),
    ("detail", Json.str detail)
  ]
  IO.println s!"LFSFT2AREFCERTJSON {payload.compress}"

elab "lfSft2aCertifyConstant " endpoint:str scope:str declaration:str : command => do
  let endpointId := endpoint.getString
  let renderScopeId := scope.getString
  let lookup := declaration.getString
  liftTermElabM do
    match (← getEnv).find? lookup.toName with
    | none =>
        emitFailure lookup "constant_not_found" "qualified declaration is not imported"
    | some (.thmInfo info) =>
        LeanFaith.GoalV1.emitClosedProp
          endpointId renderScopeId "loaded_constant_type" info.type
    | some _ =>
        emitFailure lookup "non_theorem_constant" "imported constant kind is not theorem"

end LeanFaith.SFT2A.ReferenceCertification
""".strip()
    )
    lines.append(
        "lfSft2aCertifyConstant "
        f"{json.dumps(endpoint_id, ensure_ascii=False)} "
        f"{json.dumps(render_scope_id, ensure_ascii=False)} "
        f"{json.dumps(declaration_name, ensure_ascii=False)}"
    )
    command = "\n".join(lines) + "\n"
    if "Term.elabTerm" in command:
        raise ReferenceCertifierError("constant lookup command must not elaborate source text")
    if command.count("LeanFaith.GoalV1.emitClosedProp") != 1:
        raise ReferenceCertifierError("constant lookup must call frozen REPR exactly once")
    return command


def term_elaboration_command(
    *,
    context: CompileContext,
    signature: str,
    endpoint_id: str,
    render_scope_id: str,
) -> str:
    """Build the compiler-data-only proof-free term elaboration route."""

    lines = _context_prefix(context)
    lines.append(
        """
namespace LeanFaith.SFT2A.ReferenceCertification

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
  | .mdata data body => canonicalizeBinderMetadata used body
  | e => e

elab "lfSft2aCertifyTerm " endpoint:str scope:str ":" signature:term : command => do
  liftTermElabM do
    let proposition ← Term.elabTerm signature (some (mkSort .zero))
    Term.synthesizeSyntheticMVarsNoPostponing
    let proposition ← instantiateMVars proposition
    let proposition := canonicalizeBinderMetadata #[] proposition
    LeanFaith.GoalV1.emitClosedProp
      endpoint.getString scope.getString "term_elaborated_proposition" proposition

end LeanFaith.SFT2A.ReferenceCertification
""".strip()
    )
    lines.extend(f"open {name}" for name in context.open_context)
    lines.extend(f"open scoped {name}" for name in context.scoped_context)
    lines.extend(f"namespace {name}" for name in context.namespace_context)
    lines.append(
        f"lfSft2aCertifyTerm {json.dumps(endpoint_id, ensure_ascii=False)} "
        f"{json.dumps(render_scope_id, ensure_ascii=False)} : "
        f"({signature})"
    )
    lines.extend(f"end {name}" for name in reversed(context.namespace_context))
    command = "\n".join(lines) + "\n"
    if command.count("Term.elabTerm") != 1:
        raise ReferenceCertifierError("compiler-data route must elaborate exactly one term")
    if command.count("LeanFaith.GoalV1.emitClosedProp") != 1:
        raise ReferenceCertifierError("compiler-data route must call frozen REPR exactly once")
    return command


def _atomic_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ReferenceCertifierError(f"immutable reference cache conflict: {path}")
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


def _cache_record(path: Path, expected_key: str) -> dict[str, object] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ReferenceCertifierError(f"unsafe reference cache path: {path}")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceCertifierError(f"invalid reference cache record {path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("cache_key") != expected_key:
        raise ReferenceCertifierError(f"reference cache key differs: {path}")
    if record.get("status") not in {"valid", "invalid"}:
        raise ReferenceCertifierError("reference cache contains a nonterminal result")
    return record


def _descendant_rss_bytes(root_pid: int) -> int:
    parents: dict[int, int] = {}
    rss: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        parent = 0
        resident = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                parent = int(line.split()[1])
            elif line.startswith("VmRSS:"):
                resident = int(line.split()[1]) * 1024
        parents[pid] = parent
        rss[pid] = resident
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return sum(rss.get(pid, 0) for pid in descendants)


class _RssSampler:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, _descendant_rss_bytes(self.pid))
            self._stop.wait(0.2)

    def __enter__(self) -> _RssSampler:
        self.peak = _descendant_rss_bytes(self.pid)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.peak = max(self.peak, _descendant_rss_bytes(self.pid))


def _failure_marker(messages: tuple[dict[str, object], ...]) -> dict[str, object] | None:
    found: list[dict[str, object]] = []
    for message in messages:
        data = str(message.get("data", ""))
        for line in data.splitlines():
            if not line.startswith(_FAILURE_MARKER):
                continue
            value = json.loads(line[len(_FAILURE_MARKER) :])
            if isinstance(value, dict):
                found.append(value)
    if len(found) > 1:
        raise ReferenceCertifierError("multiple reference-certification failure markers")
    return None if not found else found[0]


def _required_integer(record: dict[str, object], key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReferenceCertifierError(f"cached reference {key} is malformed")
    return value


def _result_from_cache(
    record: dict[str, object], *, cache_path: Path
) -> ReferenceCertificationResult:
    sidecar = record.get("sidecar")
    if sidecar is not None and not isinstance(sidecar, dict):
        raise ReferenceCertifierError("cached reference sidecar is malformed")
    status = record.get("status")
    if status not in {"valid", "invalid"}:
        raise ReferenceCertifierError("cached reference status is malformed")
    route = record.get("route")
    if route not in {"loaded_constant_type", "term_elaborated_proposition"}:
        raise ReferenceCertifierError("cached reference route is malformed")
    return ReferenceCertificationResult(
        status=status,
        taxonomy=str(record["taxonomy"]),
        route=route,
        cache_key=str(record["cache_key"]),
        cache_path=cache_path,
        cache_hit=True,
        goal_v1=None if record.get("goal_v1") is None else str(record["goal_v1"]),
        closed_expr_hash=(
            None if record.get("closed_expr_hash") is None else str(record["closed_expr_hash"])
        ),
        rendered_goal_hash=(
            None if record.get("rendered_goal_hash") is None else str(record["rendered_goal_hash"])
        ),
        sidecar=sidecar,
        sidecar_hash=None if record.get("sidecar_hash") is None else str(record["sidecar_hash"]),
        compile_context_id=str(record["compile_context_id"]),
        lean_status=str(record["lean_status"]),
        request_hash=None if record.get("request_hash") is None else str(record["request_hash"]),
        elapsed_ms=_required_integer(record, "elapsed_ms"),
        measured_rss_peak_bytes=_required_integer(record, "measured_rss_peak_bytes"),
        raw_response_path=(
            None if record.get("raw_response_path") is None else str(record["raw_response_path"])
        ),
        detail=str(record["detail"]),
    )


class AuthoritativeReferenceCertifier:
    """One persistent project backend with an additive terminal cache."""

    def __init__(
        self,
        loaded: LoadedSFT2AConfig,
        root: OneRootConfig,
        *,
        backend: LeanBackend | None = None,
    ) -> None:
        config = _v5_2(loaded)
        self.loaded = loaded
        self.root = root
        self.context = _compile_context(root)
        self.cache_root = Path(config.staging_root) / config.reference_certification.cache_subdir
        self._owns_backend = backend is None
        self._backend = backend

    def _backend_instance(self) -> LeanBackend:
        if self._backend is None:
            self._backend = self._make_backend()
        return self._backend

    def _make_backend(self) -> LeanBackend:
        source = self.root.compile_context
        project_dir = Path(source.project_dir).resolve(strict=True)
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=project_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or completed.stdout.strip() != source.project_revision:
            raise ReferenceCertifierError("reference project revision differs from its pin")
        toolchain = (project_dir / "lean-toolchain").read_text(encoding="utf-8").strip()
        if toolchain.rsplit(":", maxsplit=1)[-1] != source.lean_version:
            raise ReferenceCertifierError("reference project Lean toolchain differs from its pin")
        return LeanInteractBackend(
            BackendSettings(
                project_dir=project_dir,
                context_fingerprint=self.context.fingerprint,
                environment_schema_version=source.environment_schema_version,
                raw_response_dir=Path(self.loaded.config.staging_root)
                / "reference_certification_raw_responses",
                workers=1,
                memory_hard_limit_mb=source.memory_hard_limit_mb,
                enable_parallel_elaboration=False,
            )
        )

    def rebind(self, root: OneRootConfig) -> None:
        current = self.root.compile_context
        target = root.compile_context
        identity_fields = (
            "project_id",
            "project_revision",
            "project_dir",
            "lean_version",
            "leaninteract_version",
            "repl_revision",
        )
        if any(getattr(current, field) != getattr(target, field) for field in identity_fields):
            raise ReferenceCertifierError("cannot rebind a certifier across Lean projects")
        self.root = root
        self.context = _compile_context(root)

    def close(self) -> None:
        if self._owns_backend and self._backend is not None:
            self._backend.close()

    def certify(
        self,
        *,
        source_header: str,
        compiler_data_theorem_sha256: str | None,
    ) -> ReferenceCertificationResult:
        config = _v5_2(self.loaded)
        library = self.root.source in config.reference_certification.constant_lookup_sources
        route: Literal["loaded_constant_type", "term_elaborated_proposition"] = (
            "loaded_constant_type" if library else "term_elaborated_proposition"
        )
        identity = (
            {
                "qualified_declaration_name": self.root.declaration_name,
                "source_revision": self.root.source_revision,
            }
            if library
            else {
                "signature_sha256": sha256_hex(self.root.reference_signature.encode()),
                "compiler_data_theorem_sha256": compiler_data_theorem_sha256,
            }
        )
        key_payload = {
            "certifier_version": CERTIFIER_VERSION,
            "route": route,
            "source": self.root.source,
            "identity": identity,
            "source_header_sha256": sha256_hex(source_header.encode()),
            "compile_context": self.context.canonical_payload(),
            "leaninteract_version": self.root.compile_context.leaninteract_version,
            "repl_revision": self.root.compile_context.repl_revision,
            "repr": self.loaded.config.repr.model_dump(mode="json"),
        }
        cache_key = hash_canonical(key_payload)
        cache_path = self.cache_root / cache_key[:2] / f"{cache_key}.json"
        cached = _cache_record(cache_path, cache_key)
        if cached is not None:
            return _result_from_cache(cached, cache_path=cache_path)

        endpoint_id = f"sft2a-reference:{self.root.source}:{cache_key}"
        render_scope_id = "sft2a-reference-certification:" + self.context.fingerprint
        command = (
            constant_lookup_command(
                context=self.context,
                declaration_name=self.root.declaration_name,
                endpoint_id=endpoint_id,
                render_scope_id=render_scope_id,
            )
            if library
            else term_elaboration_command(
                context=self.context,
                signature=self.root.reference_signature,
                endpoint_id=endpoint_id,
                render_scope_id=render_scope_id,
            )
        )
        request = LeanRequest(
            request_id=f"sft2a-reference-certification:{cache_key}",
            context_id=self.context.compile_context_id,
            code=command,
            allow_sorry=False,
            timeout_seconds=float(config.reference_certification.timeout_seconds_per_reference),
            metadata={"certifier_version": CERTIFIER_VERSION, "route": route},
        )
        started = time.monotonic()
        with _RssSampler(os.getpid()) as sampler:
            result = self._backend_instance().run(request)
        elapsed_ms = max(result.elapsed_ms, int((time.monotonic() - started) * 1000))
        rss_peak = sampler.peak
        rss_limit = int(config.reference_certification.measured_rss_gib_maximum * 1024**3)
        detail = result.infrastructure_error or "; ".join(
            str(message.get("data", "")) for message in result.messages
        )
        if result.status in _INFRASTRUCTURE or rss_peak > rss_limit:
            taxonomy = (
                "measured_rss_ceiling_exceeded"
                if rss_peak > rss_limit
                else f"infrastructure_{result.status.value}"
            )
            return ReferenceCertificationResult(
                status="infrastructure",
                taxonomy=taxonomy,
                route=route,
                cache_key=cache_key,
                cache_path=cache_path,
                cache_hit=False,
                goal_v1=None,
                closed_expr_hash=None,
                rendered_goal_hash=None,
                sidecar=None,
                sidecar_hash=None,
                compile_context_id=self.context.compile_context_id,
                lean_status=result.status.value,
                request_hash=result.request_hash,
                elapsed_ms=elapsed_ms,
                measured_rss_peak_bytes=rss_peak,
                raw_response_path=result.raw_response_path,
                detail=detail or taxonomy,
            )

        marker = _failure_marker(result.messages)
        payloads, issues = goal_v1._parse_closed_expr_payloads(result.messages, {endpoint_id})
        sidecar_dict: dict[str, object] | None = None
        sidecar_hash: str | None = None
        goal: str | None = None
        expr_hash: str | None = None
        rendered_hash: str | None = None
        taxonomy = "valid"
        valid = result.status == LeanStatus.VALID and not result.sorries and marker is None
        if marker is not None:
            valid = False
            taxonomy = str(marker.get("taxonomy", "constant_lookup_invalid"))
            detail = str(marker.get("detail", taxonomy))
        elif result.status != LeanStatus.VALID or result.sorries:
            valid = False
            taxonomy = "term_elaboration_invalid" if not library else "lean_invalid_other"
        elif issues:
            valid = False
            taxonomy = "repr_payload_malformed"
            detail = "; ".join(issues)
        else:
            payload = payloads.get(endpoint_id)
            if payload is None:
                valid = False
                taxonomy = "repr_payload_missing"
                detail = "missing frozen REPR payload"
            else:
                source_material = (
                    ClosedExprSourceMaterial(kind="raw_statement", raw_statement=source_header)
                    if library
                    else ClosedExprSourceMaterial(
                        kind="proposition_text",
                        proposition_text=self.root.reference_signature,
                    )
                )
                item = ClosedExprInput(
                    endpoint_id=endpoint_id,
                    endpoint_role="reference",
                    expr_origin=route,
                    source_material=source_material,
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
                    taxonomy = "repr_rejected"
                    detail = str(exc)
                else:
                    sidecar_dict = sidecar.to_dict()
                    sidecar_hash = hash_canonical(sidecar_dict)
                    goal = sidecar.record.goal_v1
                    expr_hash = sidecar.record.provenance.expr_hash
                    rendered_hash = sidecar.record.rendered_goal_hash
                    detail = "authoritative reference rendered from actual closed Expr"

        status: Literal["valid", "invalid"] = "valid" if valid else "invalid"
        terminal: dict[str, object] = {
            "version": "leanfaith_sft2a_reference_certification_cache_v5_2",
            "certifier_version": CERTIFIER_VERSION,
            "cache_key": cache_key,
            "key_payload": key_payload,
            "status": status,
            "taxonomy": taxonomy,
            "route": route,
            "constant_kind": "theorem" if valid and library else None,
            "goal_v1": goal,
            "closed_expr_hash": expr_hash,
            "rendered_goal_hash": rendered_hash,
            "sidecar": sidecar_dict,
            "sidecar_hash": sidecar_hash,
            "compile_context_id": self.context.compile_context_id,
            "lean_status": result.status.value,
            "request_hash": result.request_hash,
            "elapsed_ms": elapsed_ms,
            "measured_rss_peak_bytes": rss_peak,
            "raw_response_path": result.raw_response_path,
            "detail": detail or taxonomy,
        }
        _atomic_exact(cache_path, canonical_json_bytes(terminal) + b"\n")
        return replace(_result_from_cache(terminal, cache_path=cache_path), cache_hit=False)


__all__ = [
    "CERTIFIER_VERSION",
    "AuthoritativeReferenceCertifier",
    "ReferenceCertificationResult",
    "ReferenceCertifierError",
    "constant_lookup_command",
    "term_elaboration_command",
]
