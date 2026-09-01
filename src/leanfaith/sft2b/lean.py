"""Persistent, proof-free Lean elaboration and direct-Expr REPR rendering."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanBackend
from leanfaith.lean.session_policy import ServerMode
from leanfaith.representations.goal_v1 import (
    ClosedExprBatchResult,
    ClosedExprFailure,
    ClosedExprInput,
    ClosedExprSourceMaterial,
    CompileContext,
    render_closed_expr_in_session,
)
from leanfaith.sft2b.pins import RuntimePins

_SIMPLE_LEVEL = re.compile(r"\b(?:Type|Sort)\s+([A-Za-z_][A-Za-z0-9_']*)\b")
_FAILURE_SENTINEL_DOMAIN = "leanfaith.sft2b.elaboration_failure_sentinel.v1"


@dataclass(frozen=True, slots=True)
class PropositionEndpoint:
    endpoint_id: str
    endpoint_role: str
    proposition: str
    source_id: str
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        if self.endpoint_role not in {"reference", "candidate"}:
            raise ValueError("endpoint_role must be reference or candidate")
        if not self.endpoint_id.strip() or not self.proposition.strip():
            raise ValueError("endpoint ID and proposition must be nonempty")
        lowered = self.proposition.lower()
        if ":= by" in lowered or "sorry" in lowered or "axiom" in lowered:
            raise ValueError("SFT2B accepts proof-free proposition terms only")
        if self.endpoint_role == "reference" and self.candidate_id is not None:
            raise ValueError("reference endpoint cannot have candidate_id")
        if self.endpoint_role == "candidate" and self.candidate_id is None:
            raise ValueError("candidate endpoint requires candidate_id")

    @property
    def proposition_sha256(self) -> str:
        return sha256_hex(self.proposition.encode("utf-8"))


def _helper_body(path: Path, expected_hash: str) -> str:
    if hash_file(path) != expected_hash:
        raise RuntimeError("SFT2B Lean helper differs from its runtime pin")
    source = path.read_text(encoding="utf-8")
    return "\n".join(line for line in source.splitlines() if not line.startswith("import "))


def _load_source_context(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("source context must be a JSON object")
    required = {
        "context_id",
        "context_fingerprint",
        "project_revision",
        "lean_version",
        "header_text",
        "namespace_context",
        "open_context",
        "scoped_context",
        "options",
    }
    missing = required - value.keys()
    if missing:
        raise ValueError(f"source context lacks fields: {sorted(missing)}")
    if value["context_id"] != f"ctx:{value['context_fingerprint']}":
        raise ValueError("source context ID/fingerprint mismatch")
    return value


def compile_context_from_source(
    *, source_context_path: Path, helper_path: Path, pins: RuntimePins
) -> tuple[CompileContext, dict[str, object]]:
    """Bind the source context to the hash-pinned static SFT2B helper."""

    source = _load_source_context(source_context_path)
    sequence_fields: dict[str, tuple[str, ...]] = {}
    for key in ("namespace_context", "open_context", "scoped_context"):
        raw_sequence = source[key]
        if not isinstance(raw_sequence, list) or any(
            not isinstance(item, str) for item in raw_sequence
        ):
            raise ValueError(f"source context {key} must be a string list")
        sequence_fields[key] = tuple(raw_sequence)
    raw_options = source["options"]
    if not isinstance(raw_options, dict) or any(
        not isinstance(key, str) or not isinstance(value, (str, int, float, bool))
        for key, value in raw_options.items()
    ):
        raise ValueError("source context options must use supported scalar values")
    options = {
        str(key): value
        for key, value in raw_options.items()
        if isinstance(value, (str, int, float, bool))
    }
    context = CompileContext(
        project_id="mathlib",
        project_revision=str(source["project_revision"]),
        lean_version=str(source["lean_version"]),
        import_header=str(source["header_text"]),
        command_preamble=_helper_body(helper_path, pins.sft2b_helper_hash),
        namespace_context=sequence_fields["namespace_context"],
        open_context=sequence_fields["open_context"],
        scoped_context=sequence_fields["scoped_context"],
        options=options,
    )
    return context, source


def _level_names(proposition: str) -> tuple[str, ...]:
    ordered: list[str] = []
    for match in _SIMPLE_LEVEL.finditer(proposition):
        name = match.group(1)
        if name not in {"_", "max", "imax"} and name not in ordered:
            ordered.append(name)
    return tuple(ordered)


def _lean_name_list(names: Sequence[str]) -> str:
    return "[" + ", ".join(f"`{name}" for name in names) + "]"


def _failure_sentinel_nat(endpoint_id: str) -> int:
    digest = sha256_hex(f"{_FAILURE_SENTINEL_DOMAIN}|{endpoint_id}".encode())
    return int(digest, 16)


def _failure_sentinel_expr_hash(endpoint_id: str) -> str:
    value = _failure_sentinel_nat(endpoint_id)
    eq_nat = {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {
                "k": "app",
                "fn": {
                    "k": "const",
                    "name": "Eq",
                    "levels": [{"k": "succ", "level": {"k": "zero"}}],
                },
                "arg": {"k": "const", "name": "Nat", "levels": []},
            },
            "arg": {"k": "literal", "nat": str(value)},
        },
        "arg": {"k": "literal", "nat": str(value + 1)},
    }
    return hash_canonical(eq_nat)


def build_session_body(endpoints: Sequence[PropositionEndpoint], *, render_scope_id: str) -> str:
    """Build one Meta action; proposition strings are JSON-escaped literals."""

    if len(endpoints) < 2:
        raise ValueError("a render session needs reference plus candidate")
    lines = ["run_meta do"]
    for index, endpoint in enumerate(endpoints):
        source_literal = json.dumps(endpoint.proposition, ensure_ascii=False)
        origin_literal = json.dumps(f"{endpoint.endpoint_role}:{endpoint.endpoint_id}")
        lines.append(
            f"  let endpoint{index} ← LeanFaith.SFT2B.Helper.elaborateProposition "
            f"{origin_literal} {source_literal} "
            f"{_lean_name_list(_level_names(endpoint.proposition))}"
        )
    scope_literal = json.dumps(render_scope_id)
    for index, endpoint in enumerate(endpoints):
        endpoint_literal = json.dumps(endpoint.endpoint_id)
        lines.append(
            "  LeanFaith.GoalV1.emitClosedProp "
            f'{endpoint_literal} {scope_literal} "term_elaborated_proposition" endpoint{index}'
        )
    return "\n".join(lines)


def build_tolerant_session_body(
    endpoints: Sequence[PropositionEndpoint], *, render_scope_id: str
) -> str:
    """Build one action that isolates candidate failures without re-elaboration.

    A failed candidate is rendered transiently as a collision-resistant,
    endpoint-specific false proposition built directly as an Expr. Python
    identifies it by the frozen alpha-tree hash and discards it immediately;
    the sentinel is never cached or model-facing.
    """

    if len(endpoints) < 2 or endpoints[0].endpoint_role != "reference":
        raise ValueError("tolerant session requires reference followed by candidates")
    if any(item.endpoint_role != "candidate" for item in endpoints[1:]):
        raise ValueError("tolerant session contains a non-candidate after the reference")
    lines = ["run_meta do"]
    reference = endpoints[0]
    reference_source = json.dumps(reference.proposition, ensure_ascii=False)
    lines.append(
        "  let endpoint0 ← LeanFaith.SFT2B.Helper.elaborateProposition "
        f'"reference:reference" {reference_source} '
        f"{_lean_name_list(_level_names(reference.proposition))}"
    )
    for index, endpoint in enumerate(endpoints[1:], start=1):
        source_literal = json.dumps(endpoint.proposition, ensure_ascii=False)
        origin_literal = json.dumps(f"candidate:{endpoint.endpoint_id}")
        sentinel = _failure_sentinel_nat(endpoint.endpoint_id)
        lines.extend(
            (
                f"  let endpoint{index} : Lean.Expr ← try",
                "    LeanFaith.SFT2B.Helper.elaborateProposition "
                f"{origin_literal} {source_literal} "
                f"{_lean_name_list(_level_names(endpoint.proposition))}",
                "  catch ex =>",
                "    if ex.isInterrupt || ex.isRuntime then",
                "      throw ex",
                "    pure <| Lean.mkApp3 (Lean.mkConst ``Eq [.succ .zero]) "
                f"(Lean.mkConst ``Nat []) (.lit (.natVal {sentinel})) "
                f"(.lit (.natVal {sentinel + 1}))",
            )
        )
    scope_literal = json.dumps(render_scope_id)
    for index, endpoint in enumerate(endpoints):
        endpoint_literal = json.dumps(endpoint.endpoint_id)
        lines.append(
            "  LeanFaith.GoalV1.emitClosedProp "
            f'{endpoint_literal} {scope_literal} "term_elaborated_proposition" endpoint{index}'
        )
    return "\n".join(lines)


def render_propositions_tolerant(
    backend: LeanBackend,
    *,
    endpoints: Sequence[PropositionEndpoint],
    compile_context: CompileContext,
    render_scope_id: str,
    request_id: str,
    timeout_seconds: float = 300.0,
) -> ClosedExprBatchResult:
    """Elaborate one reference and N candidates once in one isolated request."""

    inputs = tuple(
        ClosedExprInput(
            endpoint_id=item.endpoint_id,
            endpoint_role=item.endpoint_role,  # type: ignore[arg-type]
            expr_origin="term_elaborated_proposition",
            source_material=ClosedExprSourceMaterial(
                kind="proposition_text", proposition_text=item.proposition
            ),
        )
        for item in endpoints
    )
    rendered = render_closed_expr_in_session(
        backend,
        inputs=inputs,
        compile_context=compile_context,
        render_scope_id=render_scope_id,
        session_body=build_tolerant_session_body(endpoints, render_scope_id=render_scope_id),
        request_id=request_id,
        timeout_seconds=timeout_seconds,
    )
    if rendered.failures:
        return rendered
    candidate_ids = {item.endpoint_id for item in endpoints if item.endpoint_role == "candidate"}
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
    retained = tuple(
        sidecar for sidecar in rendered.sidecars if sidecar.record.endpoint_id not in failures
    )
    return ClosedExprBatchResult(
        sidecars=retained,
        failures=tuple(
            ClosedExprFailure(endpoint_id=endpoint_id, detail=detail)
            for endpoint_id, detail in sorted(failures.items())
        ),
        request_hash=rendered.request_hash,
        elapsed_ms=rendered.elapsed_ms,
        raw_response_path=rendered.raw_response_path,
        render_scope_id=rendered.render_scope_id,
    )


def endpoint_cache_key(
    endpoint: PropositionEndpoint,
    *,
    source_context_id: str,
    compile_context: CompileContext,
    pins: RuntimePins,
) -> str:
    return hash_canonical(
        {
            "cache_schema": "sft2b_endpoint_cache_key_v1",
            "endpoint_role": endpoint.endpoint_role,
            "proposition_sha256": endpoint.proposition_sha256,
            "source_context_id": source_context_id,
            "render_compile_context_id": compile_context.compile_context_id,
            "project_revision": compile_context.project_revision,
            "lean_version": compile_context.lean_version,
            "helper_sha256": pins.sft2b_helper_hash,
            "repr_spec_sha256": pins.repr_spec_hash,
            "repr_implementation_set_sha256": pins.repr_implementation_set_hash,
            "repr_api_sha256": pins.repr_api_hash,
        }
    )


def render_propositions(
    backend: LeanBackend,
    *,
    endpoints: Sequence[PropositionEndpoint],
    compile_context: CompileContext,
    render_scope_id: str,
    request_id: str,
    timeout_seconds: float = 300.0,
) -> ClosedExprBatchResult:
    """Elaborate each proposition once and render the same live Expr once."""

    inputs = tuple(
        ClosedExprInput(
            endpoint_id=item.endpoint_id,
            endpoint_role=item.endpoint_role,  # type: ignore[arg-type]
            expr_origin="term_elaborated_proposition",
            source_material=ClosedExprSourceMaterial(
                kind="proposition_text", proposition_text=item.proposition
            ),
        )
        for item in endpoints
    )
    return render_closed_expr_in_session(
        backend,
        inputs=inputs,
        compile_context=compile_context,
        render_scope_id=render_scope_id,
        session_body=build_session_body(endpoints, render_scope_id=render_scope_id),
        request_id=request_id,
        timeout_seconds=timeout_seconds,
    )


def make_mathlib_backend(
    *,
    compile_context: CompileContext,
    project_dir: Path,
    raw_response_dir: Path,
) -> LeanInteractBackend:
    """Create one persistent synchronous backend for a context batch."""

    return LeanInteractBackend(
        BackendSettings(
            project_dir=project_dir,
            context_fingerprint=compile_context.fingerprint,
            environment_schema_version=1,
            raw_response_dir=raw_response_dir,
            server_mode=ServerMode.STABLE,
            workers=None,
            memory_hard_limit_mb=24576,
            enable_incremental_optimization=True,
            enable_parallel_elaboration=False,
            isolate_incremental_commands=True,
            confirm_invalid_on_fresh_process=True,
        )
    )
