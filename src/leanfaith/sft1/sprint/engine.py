"""Python side of the compact sprint engine: compile context, request bodies,
evidence parsing, and the frozen render route binding.

The Lean engine runs inside one persistent Mathlib request.  A *process*
request applies every enabled operation to a batch of roots and emits one
typed evidence line per root.  A *render* request rebuilds the retained pairs
and renders both endpoints through the frozen ``LeanFaith.GoalV1.emitClosedProp``
route; Python binds the two requests through the engine's structural hashes
and the exact pre-rendered texts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import (
    ClosedExprBatchResult,
    ClosedExprInput,
    ClosedExprSourceMaterial,
    CompileContext,
    _closed_expr_command,
    render_closed_expr_in_session,
)

ENGINE_RELATIVE_PATH = Path("LeanFaith/Meta/SFT1/Sprint.lean")
EVIDENCE_MARKER = "LFSFT1SPRINTJSON "
RENDER_SCOPE_PREFIX = "sft1-sprint"

OPERATIONS: tuple[str, ...] = (
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P23_CURRY_PROP_PAIR_V1",
    "N25_TOGGLE_EQ_NE_PROOF_V1",
    "N32_SWAP_ROLE_ORDER_PROOF_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    "P_NE_SYMMETRIZE_V1",
    "P_DROP_REDUNDANT_GUARD_PROOF_V1",
)
SPRINT_V1_OPERATIONS: tuple[str, ...] = OPERATIONS[:7]
OPERATION_BITS: dict[str, int] = {operation: index for index, operation in enumerate(OPERATIONS)}
POSITIVE_OPERATIONS: frozenset[str] = frozenset(op for op in OPERATIONS if op.startswith("P"))
OPERATION_MECHANISM: dict[str, str] = {
    "P15_SWAP_IFF_SIDES_V1": "P15",
    "P18_SYMMETRIZE_EQUALITY_V1": "P18",
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1": "P14",
    "P23_CURRY_PROP_PAIR_V1": "P23",
    "N25_TOGGLE_EQ_NE_PROOF_V1": "N25",
    "N32_SWAP_ROLE_ORDER_PROOF_V1": "N32",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1": "N31",
    "P_NE_SYMMETRIZE_V1": "PNE",
    "P_DROP_REDUNDANT_GUARD_PROOF_V1": "PDRG",
    "SQUARE_N25_SYMMETRY_V1": "SQ25",
}


def cacheable_status(status: object) -> bool:
    """Only deterministic terminals may enter the semantic cache.

    Request failures (``error``) depend on the engine text, the worker, and host
    state, so they are never written to nor served from the cache.
    """
    return status != "error"


def mechanism_of(operation: str) -> str:
    """Mechanism label of an operation; exact table, never a prefix split."""

    try:
        return OPERATION_MECHANISM[operation]
    except KeyError as exc:
        raise SprintEngineError(f"unknown operation {operation!r}") from exc


NEGATIVE_OPERATIONS: frozenset[str] = frozenset(op for op in OPERATIONS if op.startswith("N"))
ALL_OPERATIONS_MASK = (1 << len(OPERATIONS)) - 1

_SEMANTIC_VERSION = re.compile(r'^def engineSemanticVersion : String := "([^"]+)"', re.MULTILINE)


class SprintEngineError(RuntimeError):
    """Fail-closed engine adapter error."""


@dataclass(frozen=True, slots=True)
class ProjectPins:
    project_id: str
    project_dir: Path
    project_revision: str
    lean_version: str
    lean_interact_version: str
    repl_revision: str
    import_header: str
    options: Mapping[str, bool]

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "project_dir": str(self.project_dir),
            "project_revision": self.project_revision,
            "lean_version": self.lean_version,
            "lean_interact_version": self.lean_interact_version,
            "repl_revision": self.repl_revision,
            "import_header": self.import_header,
            "options": dict(sorted(self.options.items())),
        }


@dataclass(frozen=True, slots=True)
class EngineIdentity:
    source_sha256: str
    semantic_version: str
    import_options_fingerprint: str
    compile_context_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source_sha256": self.source_sha256,
            "semantic_version": self.semantic_version,
            "import_options_fingerprint": self.import_options_fingerprint,
            "compile_context_id": self.compile_context_id,
        }


def strip_imports(path: Path) -> str:
    return (
        "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("import ")
        ).rstrip()
        + "\n"
    )


def engine_semantic_version(repo_root: Path) -> str:
    source = (repo_root / ENGINE_RELATIVE_PATH).read_text(encoding="utf-8")
    match = _SEMANTIC_VERSION.search(source)
    if match is None:
        raise SprintEngineError("engine source does not declare engineSemanticVersion")
    return match.group(1)


def import_options_fingerprint(pins: ProjectPins) -> str:
    return hash_canonical(
        {
            "import_header": pins.import_header,
            "options": dict(sorted(pins.options.items())),
            "lean_version": pins.lean_version,
            "project_revision": pins.project_revision,
        }
    )


def build_compile_context(repo_root: Path, pins: ProjectPins) -> CompileContext:
    return CompileContext(
        project_id=pins.project_id,
        project_revision=pins.project_revision,
        lean_version=pins.lean_version,
        import_header=pins.import_header,
        command_preamble=strip_imports(repo_root / ENGINE_RELATIVE_PATH),
        options=dict(pins.options),
    )


def engine_identity(repo_root: Path, pins: ProjectPins, context: CompileContext) -> EngineIdentity:
    return EngineIdentity(
        source_sha256=hash_file(repo_root / ENGINE_RELATIVE_PATH),
        semantic_version=engine_semantic_version(repo_root),
        import_options_fingerprint=import_options_fingerprint(pins),
        compile_context_id=context.compile_context_id,
    )


def lean_string_literal(name: str) -> str:
    """Lean string literal for a root name; string literals are masked by the
    frozen render route, unlike name literals containing primes."""

    if any(ch in name for ch in ('"', "\\", "\n")):
        raise SprintEngineError(f"unsupported character in root name {name!r}")
    return json.dumps(name, ensure_ascii=False)


def operation_mask(operations: Sequence[str]) -> int:
    mask = 0
    for operation in operations:
        if operation not in OPERATION_BITS:
            raise SprintEngineError(f"unknown operation {operation!r}")
        mask |= 1 << OPERATION_BITS[operation]
    return mask


def operations_in_mask(mask: int) -> tuple[str, ...]:
    return tuple(operation for operation in OPERATIONS if mask & (1 << OPERATION_BITS[operation]))


def process_body(roots: Sequence[tuple[str, int]]) -> str:
    if not roots:
        raise SprintEngineError("process request needs at least one root")
    names = ", ".join(lean_string_literal(name) for name, _ in roots)
    masks = ", ".join(str(mask) for _, mask in roots)
    return f"run_meta do\n  LeanFaith.SFT1.Sprint.processRoots #[{names}] #[{masks}]"


def render_scope_id(engine_version: str) -> str:
    return f"{RENDER_SCOPE_PREFIX}:{engine_version}"


def render_body(pairs: Sequence[tuple[str, str]], scope: str) -> str:
    if not pairs:
        raise SprintEngineError("render request needs at least one pair")
    names = ", ".join(lean_string_literal(name) for name, _ in pairs)
    operations = ", ".join(json.dumps(operation) for _, operation in pairs)
    lines = [
        "run_meta do",
        f"  let pairs ← LeanFaith.SFT1.Sprint.rebuildPairs #[{names}] #[{operations}]",
        "  LeanFaith.SFT1.Sprint.emitRebuildReport pairs",
    ]
    for index in range(len(pairs)):
        lines.append(
            f"  LeanFaith.GoalV1.emitClosedProp {json.dumps(f'{index}.reference')} "
            f'{json.dumps(scope)} "loaded_constant_type" (pairs[{index}]!).reference'
        )
        lines.append(
            f"  LeanFaith.GoalV1.emitClosedProp {json.dumps(f'{index}.candidate')} "
            f'{json.dumps(scope)} "sft1_transformed_expr" (pairs[{index}]!).candidate'
        )
    return "\n".join(lines)


def render_inputs(
    pairs: Sequence[tuple[str, str]], statements: Mapping[str, str]
) -> tuple[ClosedExprInput, ...]:
    inputs: list[ClosedExprInput] = []
    for index, (name, operation) in enumerate(pairs):
        statement = statements.get(name) or f"theorem {name} : <statement text unavailable>"
        inputs.append(
            ClosedExprInput(
                endpoint_id=f"{index}.reference",
                endpoint_role="reference",
                expr_origin="loaded_constant_type",
                source_material=ClosedExprSourceMaterial(
                    kind="raw_statement", raw_statement=statement
                ),
            )
        )
        inputs.append(
            ClosedExprInput(
                endpoint_id=f"{index}.candidate",
                endpoint_role="candidate",
                expr_origin="sft1_transformed_expr",
                source_material=ClosedExprSourceMaterial(
                    kind="constructed_expr_no_source_text",
                    absence_reason=(
                        f"{operation} candidate constructed by the sprint engine from {name}"
                    ),
                ),
            )
        )
    return tuple(inputs)


def command_text(context: CompileContext, body: str) -> str:
    return _closed_expr_command(context, body)


def parse_evidence_lines(messages: Sequence[Mapping[str, object]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for message in messages:
        for line in str(message.get("data", "")).splitlines():
            marker = line.find(EVIDENCE_MARKER)
            if marker < 0:
                continue
            try:
                value = json.loads(line[marker + len(EVIDENCE_MARKER) :])
            except json.JSONDecodeError as exc:
                raise SprintEngineError(f"malformed engine evidence line: {exc}") from exc
            if not isinstance(value, dict):
                raise SprintEngineError("engine evidence payload is not an object")
            payloads.append(value)
    return payloads


def error_messages(result: LeanResult) -> list[str]:
    return [
        str(message.get("data", ""))[:600]
        for message in result.messages
        if str(message.get("severity", "")).lower() == "error"
    ]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    roots: dict[str, dict[str, Any]]
    request_hash: str
    elapsed_ms: int
    raw_response_path: str | None
    status: str
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenderResult:
    batch: ClosedExprBatchResult
    rebuild_hashes: dict[int, tuple[str, str]]


class SprintSession:
    """One persistent backend used for process and render requests."""

    def __init__(
        self, backend: LeanBackend, context: CompileContext, *, timeout_seconds: float
    ) -> None:
        self.backend = backend
        self.context = context
        self.timeout_seconds = timeout_seconds
        self.request_count = 0
        self.lean_elapsed_ms = 0

    def run_process(self, roots: Sequence[tuple[str, int]], *, request_id: str) -> ProcessResult:
        body = process_body(roots)
        request = LeanRequest(
            request_id=request_id,
            context_id=self.context.compile_context_id,
            code=command_text(self.context, body),
            allow_sorry=False,
            timeout_seconds=self.timeout_seconds,
            metadata={"sprint_phase": "process"},
        )
        result = self.backend.run(request)
        self.request_count += 1
        self.lean_elapsed_ms += result.elapsed_ms
        errors = tuple(error_messages(result))
        payloads = (
            parse_evidence_lines(result.messages)
            if result.status in {LeanStatus.VALID, LeanStatus.INVALID}
            else []
        )
        roots_by_name: dict[str, dict[str, Any]] = {}
        for payload in payloads:
            if payload.get("kind") != "root":
                continue
            name = payload.get("root")
            if not isinstance(name, str):
                raise SprintEngineError("engine root payload lacks a root name")
            if name in roots_by_name:
                raise SprintEngineError(f"engine emitted root {name!r} twice")
            roots_by_name[name] = payload
        return ProcessResult(
            roots=roots_by_name,
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=result.raw_response_path,
            status=result.status.value,
            errors=errors,
        )

    def run_render(
        self,
        pairs: Sequence[tuple[str, str]],
        *,
        statements: Mapping[str, str],
        scope: str,
        request_id: str,
    ) -> RenderResult:
        body = render_body(pairs, scope)
        counting = _CountingBackend(self.backend)
        batch = render_closed_expr_in_session(
            counting,
            inputs=render_inputs(pairs, statements),
            compile_context=self.context,
            render_scope_id=scope,
            session_body=body,
            request_id=request_id,
            timeout_seconds=self.timeout_seconds,
        )
        self.request_count += counting.calls
        self.lean_elapsed_ms += batch.elapsed_ms
        rebuild: dict[int, tuple[str, str]] = {}
        if counting.last_result is not None:
            for payload in parse_evidence_lines(counting.last_result.messages):
                if payload.get("kind") != "rebuild":
                    continue
                for entry in payload.get("pairs", []):
                    if not isinstance(entry, dict):
                        continue
                    rebuild[int(entry["index"])] = (
                        str(entry["reference_alpha_hash"]),
                        str(entry["candidate_alpha_hash"]),
                    )
        return RenderResult(batch=batch, rebuild_hashes=rebuild)


class _CountingBackend:
    def __init__(self, delegate: LeanBackend) -> None:
        self.delegate = delegate
        self.calls = 0
        self.last_result: LeanResult | None = None

    def run(self, request: LeanRequest) -> LeanResult:
        self.calls += 1
        self.last_result = self.delegate.run(request)
        return self.last_result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None
