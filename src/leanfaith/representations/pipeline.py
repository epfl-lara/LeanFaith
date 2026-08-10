"""Lean-backed representation builder (PLAN.md §13, LF-014).

Supersedes the minimal ``repr_v0_extract`` record from extraction with a
``repr_v3`` multi-view record: the three required v0 views plus the
elaborated ``signature_explicit``. Both signature views are pretty-printed
directly from the declaration's ``ConstantInfo.type`` under ``Options.empty``;
this isolates them from ambient core and extension ``pp.*`` settings, including
private environment-only names that Lean syntax cannot address.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import (
    RetryPolicy,
    run_batch_with_retries,
    run_with_retries,
)
from leanfaith.lean.source_scan import scan_lean_line
from leanfaith.representations.atoms import (
    operator_tree,
    parse_lfjson_line,
    parse_lfjson_payload,
    parse_lfsignature_payload,
    parse_lftree_payload,
    semantic_atoms,
)
from leanfaith.representations.views import (
    NORMALIZATION_VERSION,
    PP_EXPLICIT_INLINE,
    PP_SIGNATURE_INLINE,
    check_command,
    normalize_headless,
    parse_check_type,
    representation_content_hash,
)
from leanfaith.schemas.enums import ViewStatus
from leanfaith.schemas.ids import REPRESENTATION_PREFIX, make_id
from leanfaith.schemas.theorem import CANONICAL_VIEW_NAMES, RepresentationRecord

_CHECK_NAME = re.compile(r"^@?(?P<name>[^\s.]+(?:\.[^\s.{:]+)*)")
_UNIVERSE_TOKEN = re.compile(r"(?:[^\W\d]|_)[\w\x27.]*", re.UNICODE)
_UNIVERSE_KEYWORDS = frozenset({"max", "imax", "succ", "zero"})
_REPRESENTATION_RETRY_POLICY = RetryPolicy(
    max_attempts=2,
    retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT}),
)


def _run_representation_request(
    backend: LeanInteractBackend,
    request: LeanRequest,
) -> LeanResult:
    """Run one correctness request with bounded infrastructure-only retries."""

    return run_with_retries(
        backend.run,
        request,
        _REPRESENTATION_RETRY_POLICY,
    ).result


def _run_representation_requests(
    backend: LeanInteractBackend,
    requests: list[LeanRequest],
) -> list[LeanResult]:
    """Run independent theorem requests through the configured server pool.

    Stable/auto backends remain sequential through ``LeanBackend.run_batch``;
    pool mode distributes the requests over persistent LeanInteract workers.
    Only infrastructure failures are retried, and input ordering is retained.
    """

    backend_batch = getattr(backend, "run_batch", None)
    batch_runner: Callable[[Sequence[LeanRequest]], Sequence[LeanResult]]
    if not callable(backend_batch):
        # Small unit-test and downstream protocol doubles written before the
        # canonical batch method remain valid. Production backends always use
        # ``run_batch`` and therefore reach LeanServerPool in pool mode.
        def sequential_batch(items: Sequence[LeanRequest]) -> Sequence[LeanResult]:
            return [backend.run(item) for item in items]

        batch_runner = sequential_batch
    else:
        batch_runner = backend_batch
    return list(
        run_batch_with_retries(
            batch_runner,
            requests,
            _REPRESENTATION_RETRY_POLICY,
        ).results
    )


@lru_cache(maxsize=1)
def _expr_json_helper() -> str:
    """The ``LeanFaith/Meta/ExprJson.lean`` helper body with its ``import``
    lines stripped (the batch Command supplies ``import Mathlib``)."""
    path = find_repo_root(Path(__file__).parent) / "LeanFaith" / "Meta" / "ExprJson.lean"
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("import "))


def _dump_command(imports: str, helper: str, full_names: list[str]) -> str:
    # All imports first (Lean's meta machinery + the domain env), then the
    # import-stripped helper body, then the calls. `import Lean` is explicit
    # because the domain import may not transitively provide Elab/Meta (the
    # fixture library does not; Mathlib does — a duplicate import is harmless).
    # The name is JSON-escaped (ensure_ascii=False keeps guillemets literal but
    # escapes `"`/`\`), so a name containing a quote or backslash — both legal
    # in `«...»` identifiers — cannot turn one lfDump line into a syntax error
    # that would fail the whole batch and drop atoms for every theorem in it.
    calls = "\n".join(f"lfDump {json.dumps(name, ensure_ascii=False)}" for name in full_names)
    import_lines = ["import Lean"]
    for line in imports.splitlines():
        if line.strip() and line.strip() != "import Lean":
            import_lines.append(line.strip())
    return "\n".join(import_lines) + f"\n{helper}\n{calls}"


def _run_expr_dump_batch(
    backend: LeanInteractBackend,
    context_id: str,
    imports: str,
    names: list[str],
    request_id: str,
) -> dict[str, dict[str, Any]]:
    result = _run_representation_request(
        backend,
        LeanRequest(
            request_id=request_id,
            context_id=context_id,
            code=_dump_command(imports, _expr_json_helper(), names),
            timeout_seconds=600.0,
        ),
    )
    if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
        return {}
    trees: dict[str, dict[str, Any]] = {}
    for message in result.messages:
        data = str(message.get("data", ""))
        # One IO.println per dump, but tolerate several landing in one message.
        for candidate in data.splitlines():
            if "LFJSON " not in candidate:
                continue
            name, tree = parse_lfjson_line(candidate)
            if tree is not None:
                trees[name] = tree
    return trees


@dataclass(frozen=True, slots=True)
class TheoremForRepresentation:
    theorem_id: str
    full_name: str
    proof_stripped: str
    context_id: str
    #: The Lean-parsed source signature (the declaration response's
    #: ``signature.pp``, e.g. ``(x y : Nat) : x + y = y + x``) — name/proof/
    #: comment/attribute-free by construction, so it is the robust headless
    #: view. When absent (e.g. a raw benchmark reference), the pipeline falls
    #: back to the string-based ``normalize_headless``.
    source_signature: str | None = None
    #: Inline dataset statements are declared and inspected in the same
    #: LeanInteract request. Repository declarations already exist in imports.
    inline_declaration: bool = False
    #: Full non-model command context for inline elaboration. The raw/headless
    #: views continue to use ``proof_stripped`` exclusively.
    inline_source: str | None = None
    #: Environment-facing declaration name. LeanInteract renders private
    #: declarations as ``_private.0.<suffix>`` in FileCommand metadata, while
    #: the imported environment stores the source-qualified name
    #: ``_private.<module>.0.<suffix>``. Model-visible identity continues to
    #: use ``full_name``; only meta-level environment lookup uses this field.
    environment_lookup_name: str | None = None


@dataclass(frozen=True, slots=True)
class RepresentationBatch:
    """One homogeneous context/import unit for representation orchestration."""

    context_id: str
    import_header: str
    ordered_theorem_inputs: tuple[TheoremForRepresentation, ...]


@dataclass(frozen=True, slots=True)
class RepresentationFailure:
    theorem_id: str
    view: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class RepresentationBatchResult:
    ordered_representation_records: tuple[RepresentationRecord, ...]
    per_theorem_failures: tuple[RepresentationFailure, ...]


def declaration_environment_lookup_name(full_name: str, source_file: str | None) -> str:
    """Recover the environment name for a FileCommand-rendered private name.

    LeanInteract intentionally pretty-prints private declarations without the
    source module (for example ``_private.0.ZMod.foo``), but ``Environment``
    keys retain it (``_private.Mathlib.Algebra.Field.ZMod.0.ZMod.foo``).
    Exact source qualification avoids an ambiguous suffix scan. Public names,
    or private names without a trustworthy ``.lean`` source path, are left
    unchanged so lookup fails explicitly rather than guessing.
    """

    if not full_name.startswith("_private.") or source_file is None:
        return full_name
    normalized_source = source_file.replace("\\", "/").lstrip("/")
    if not normalized_source.endswith(".lean"):
        return full_name
    module_name = normalized_source.removesuffix(".lean").replace("/", ".")
    if not module_name:
        return full_name
    return f"_private.{module_name}.{full_name.removeprefix('_private.')}"


def _expr_lookup_name(theorem: TheoremForRepresentation) -> str:
    return theorem.environment_lookup_name or theorem.full_name


def _requires_environment_only_lookup(theorem: TheoremForRepresentation) -> bool:
    """Whether Lean's parser cannot address the environment declaration name."""

    return (
        theorem.environment_lookup_name is not None
        and theorem.environment_lookup_name != theorem.full_name
    )


def alpha_canonical_bytes(expr_tree: dict[str, Any]) -> bytes:
    """Canonical binder-normalized bytes for a declaration *type* tree.

    The Lean helper serializes bound variables by de Bruijn index and omits
    declaration values/proofs. Binder domains, binder metadata, constants,
    literals, projections, lets, and application structure remain. Universe
    identifiers are represented only by normalized shape/count metadata.
    """

    names: dict[str, str] = {}

    def normalize_universe(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            token = match.group(0)
            if token in _UNIVERSE_KEYWORDS:
                return token
            if token not in names:
                names[token] = f"u{len(names)}"
            return names[token]

        return _UNIVERSE_TOKEN.sub(replace, text)

    def visit(value: object, key: str | None = None) -> object:
        if isinstance(value, dict):
            return {name: visit(item, name) for name, item in sorted(value.items())}
        if isinstance(value, list):
            return [visit(item, key) for item in value]
        if isinstance(value, str) and key in {"u", "us"}:
            return normalize_universe(value)
        return value

    normalized = visit(expr_tree)
    return canonical_json_bytes(normalized)


def alpha_identity_fingerprint(expr_tree: dict[str, Any]) -> str:
    return sha256_hex(alpha_canonical_bytes(expr_tree))


def _info_messages(result: LeanResult) -> list[str]:
    return [
        str(message.get("data", ""))
        for message in result.messages
        if message.get("severity") == "info"
    ]


def _map_check_types(messages: list[str], expected: set[str]) -> dict[str, str]:
    """Map each ``#check`` info message to its declaration name → type text.

    Names are matched by the ``@name`` token (universe annotations stripped),
    so a failed check (which emits an error, not an info) simply leaves that
    name unmapped rather than shifting the alignment."""
    out: dict[str, str] = {}
    for message in messages:
        match = _CHECK_NAME.match(message.strip())
        if match is None:
            continue
        name = match.group("name")
        if name in expected:
            parsed = parse_check_type(message, name)
            if parsed is not None:
                out[name] = parsed
    return out


def _run_check_batch(
    backend: LeanInteractBackend,
    context_id: str,
    imports: str,
    options_inline: str,
    names: list[str],
    request_id: str,
) -> dict[str, str]:
    result = _run_representation_request(
        backend,
        LeanRequest(
            request_id=request_id,
            context_id=context_id,
            code=check_command(_imports_with_lean(imports), options_inline, names),
            timeout_seconds=300.0,
        ),
    )
    if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
        return {}
    return _map_check_types(_info_messages(result), set(names))


def _combined_command(
    imports: str,
    theorem: TheoremForRepresentation,
) -> str:
    inline_imports, inline_body = _hoist_inline_imports(
        (theorem.inline_source or theorem.proof_stripped) if theorem.inline_declaration else ""
    )
    lines = [_imports_with_lean("\n".join((imports, inline_imports))), _expr_json_helper()]
    if theorem.inline_declaration:
        lines.append(inline_body)
    lookup_literal = json.dumps(_expr_lookup_name(theorem), ensure_ascii=False)
    lines.extend(
        (
            f"lfDumpSignaturePP {lookup_literal}",
            f"lfDumpSignatureExplicit {lookup_literal}",
        )
    )
    lines.append(f"lfDumpTree {json.dumps(_expr_lookup_name(theorem), ensure_ascii=False)}")
    return "\n".join(line for line in lines if line)


def _parse_combined_result(
    result: LeanResult,
    full_name: str,
    *,
    dump_name: str | None = None,
    allow_environment_signatures: bool = False,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    parsed_checks = [
        parsed
        for message in _info_messages(result)
        if (parsed := parse_check_type(message, full_name)) is not None
    ]
    signature_pp = parsed_checks[0] if parsed_checks else None
    signature_explicit = parsed_checks[1] if len(parsed_checks) > 1 else None
    tree: dict[str, Any] | None = None
    environment_signature_pp: str | None = None
    environment_signature_explicit: str | None = None
    for message in result.messages:
        for candidate in str(message.get("data", "")).splitlines():
            expected_name = dump_name or full_name
            if "LFSIGPPJSON " in candidate:
                parsed_name, parsed_signature = parse_lfsignature_payload(
                    candidate,
                    prefix="LFSIGPPJSON ",
                    field="signature_pp",
                )
                if parsed_name == expected_name:
                    environment_signature_pp = parsed_signature
                continue
            if "LFSIGEXPLICITJSON " in candidate:
                parsed_name, parsed_signature = parse_lfsignature_payload(
                    candidate,
                    prefix="LFSIGEXPLICITJSON ",
                    field="signature_explicit",
                )
                if parsed_name == expected_name:
                    environment_signature_explicit = parsed_signature
                continue
            if "LFTREEJSON " in candidate:
                parsed_name, parsed_tree = parse_lftree_payload(candidate)
                if parsed_name == expected_name:
                    tree = parsed_tree
                continue
            if "LFJSON " in candidate:
                (
                    parsed_name,
                    parsed_tree,
                    parsed_environment_pp,
                    parsed_environment_explicit,
                ) = parse_lfjson_payload(candidate)
                if parsed_name == expected_name:
                    tree = tree or parsed_tree
                    environment_signature_pp = environment_signature_pp or parsed_environment_pp
                    environment_signature_explicit = (
                        environment_signature_explicit or parsed_environment_explicit
                    )
    if allow_environment_signatures:
        # repr_v3's option-isolated ConstantInfo helper is authoritative.
        # Inline source may itself contain arbitrary ``#check`` commands whose
        # info messages inherit hostile ambient pp options. Those diagnostics
        # must never override (or stand in for) a missing canonical helper
        # payload. A missing helper view remains missing and is retried through
        # its independent command below.
        signature_pp = environment_signature_pp
        signature_explicit = environment_signature_explicit
    return signature_pp, signature_explicit, tree


def _single_check_command(
    imports: str,
    theorem: TheoremForRepresentation,
    options_inline: str,
) -> str:
    inline_imports, inline_body = _hoist_inline_imports(
        (theorem.inline_source or theorem.proof_stripped) if theorem.inline_declaration else ""
    )
    lines = [_imports_with_lean("\n".join((imports, inline_imports)))]
    if theorem.inline_declaration:
        lines.append(inline_body)
    lines.append(f"{options_inline} #check @{theorem.full_name}")
    return "\n".join(lines)


def _single_tree_command(imports: str, theorem: TheoremForRepresentation) -> str:
    inline_imports, inline_body = _hoist_inline_imports(
        (theorem.inline_source or theorem.proof_stripped) if theorem.inline_declaration else ""
    )
    lines = [_imports_with_lean("\n".join((imports, inline_imports))), _expr_json_helper()]
    if theorem.inline_declaration:
        lines.append(inline_body)
    lines.append(f"lfDumpTree {json.dumps(_expr_lookup_name(theorem), ensure_ascii=False)}")
    return "\n".join(lines)


def _single_environment_signature_command(
    imports: str,
    theorem: TheoremForRepresentation,
    *,
    explicit: bool,
) -> str:
    inline_imports, inline_body = _hoist_inline_imports(
        (theorem.inline_source or theorem.proof_stripped) if theorem.inline_declaration else ""
    )
    lines = [_imports_with_lean("\n".join((imports, inline_imports))), _expr_json_helper()]
    if theorem.inline_declaration:
        lines.append(inline_body)
    command = "lfDumpSignatureExplicit" if explicit else "lfDumpSignaturePP"
    lines.append(f"{command} {json.dumps(_expr_lookup_name(theorem), ensure_ascii=False)}")
    return "\n".join(lines)


def _imports_with_lean(imports: str) -> str:
    """Ensure meta-command requests import Lean's elaborator API explicitly."""

    lines = [line.strip() for line in imports.splitlines() if line.strip()]
    return "\n".join(["import Lean", *(line for line in lines if line != "import Lean")])


def _hoist_inline_imports(source: str) -> tuple[str, str]:
    """Move inline module/import preamble ahead of the injected meta helper.

    Dataset declarations frequently preserve their original import header.
    Appending that source after helper declarations would place imports after
    declarations, which Lean rejects. Mathlib source prefixes additionally use
    the Lean module system's leading ``module`` command and ``public import``
    declarations. The one-shot inspection command deliberately drops
    ``module`` and rewrites ``public import``/``public`` modifiers to their
    ordinary equivalents. Module export visibility is irrelevant inside one
    request, while leaving module mode enabled changes the meta-status rules
    for the injected helper definitions.

    Hoisting preserves the non-preamble command order and does not mistake
    import-like prose inside comments or strings for commands.
    """

    imports: list[str] = []
    body: list[str] = []
    block_depth = 0
    for line in source.splitlines():
        initial_depth = block_depth
        first_code, block_depth = scan_lean_line(line, block_depth)
        # Only move a real top-level preamble command. In particular, prose
        # such as `import geometry;` inside a nested Lean block comment must
        # remain a comment. Requiring balanced comments on the line avoids
        # moving one half of a multi-line comment.
        command = line[first_code:] if first_code is not None else ""
        is_top_level_code = initial_depth == 0 and block_depth == 0 and first_code is not None
        if is_top_level_code and re.match(r"module(?:\s|$)", command):
            continue
        if is_top_level_code and re.match(r"public\s+import(?:\s|$)", command):
            imports.append(re.sub(r"^public\s+", "", command, count=1).strip())
            continue
        if is_top_level_code and re.match(r"import(?:\s|$)", command):
            imports.append(command.strip())
            continue
        if is_top_level_code:
            ordinary_command = re.sub(
                r"^((?:@\[[^\n]*\]\s*)*)public\s+",
                r"\1",
                command,
                count=1,
            )
            if ordinary_command != command:
                body.append(line[:first_code] + ordinary_command)
                continue
        body.append(line)
    return "\n".join(dict.fromkeys(imports)), "\n".join(body)


def _retry_check_views(
    backend: LeanInteractBackend,
    theorems: list[TheoremForRepresentation],
    imports: str,
    options_inline: str,
    view: str,
) -> dict[str, str]:
    requests = [
        LeanRequest(
            request_id=(
                f"repr-{theorem.theorem_id.removeprefix('thm:')[:16]}-"
                f"{NORMALIZATION_VERSION}-{view}"
            ),
            context_id=theorem.context_id,
            code=_single_check_command(imports, theorem, options_inline),
            allow_sorry=theorem.inline_declaration,
            timeout_seconds=300.0,
        )
        for theorem in theorems
    ]
    recovered: dict[str, str] = {}
    for theorem, result in zip(
        theorems, _run_representation_requests(backend, requests), strict=True
    ):
        if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
            continue
        mapped = _map_check_types(_info_messages(result), {theorem.full_name})
        value = mapped.get(theorem.full_name)
        if value is not None:
            recovered[theorem.theorem_id] = value
    return recovered


def _retry_environment_signature_views(
    backend: LeanInteractBackend,
    theorems: list[TheoremForRepresentation],
    imports: str,
    *,
    explicit: bool,
) -> dict[str, str]:
    view = "signature_explicit" if explicit else "signature_pp"
    prefix = "LFSIGEXPLICITJSON " if explicit else "LFSIGPPJSON "
    field = "signature_explicit" if explicit else "signature_pp"
    requests = [
        LeanRequest(
            request_id=(
                f"repr-{theorem.theorem_id.removeprefix('thm:')[:16]}-"
                f"{NORMALIZATION_VERSION}-{view}"
            ),
            context_id=theorem.context_id,
            code=_single_environment_signature_command(
                imports,
                theorem,
                explicit=explicit,
            ),
            allow_sorry=theorem.inline_declaration,
            timeout_seconds=300.0,
        )
        for theorem in theorems
    ]
    recovered: dict[str, str] = {}
    for theorem, result in zip(
        theorems, _run_representation_requests(backend, requests), strict=True
    ):
        if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
            continue
        expected_name = _expr_lookup_name(theorem)
        selected_signature: str | None = None
        saw_expected_or_malformed_payload = False
        for message in result.messages:
            for candidate in str(message.get("data", "")).splitlines():
                if prefix not in candidate:
                    continue
                parsed_name, signature = parse_lfsignature_payload(
                    candidate,
                    prefix=prefix,
                    field=field,
                )
                if parsed_name == expected_name:
                    # Commands execute after inline source, so the last
                    # matching payload is the authoritative helper outcome.
                    # A later notfound/missing-field payload intentionally
                    # clears an earlier source-authored spoof.
                    selected_signature = signature
                    saw_expected_or_malformed_payload = True
                elif not parsed_name:
                    # A truncated/malformed later helper payload cannot name
                    # the theorem. Fail closed rather than retaining a prior
                    # source-authored marker.
                    selected_signature = None
                    saw_expected_or_malformed_payload = True
        if saw_expected_or_malformed_payload and selected_signature is not None:
            recovered[theorem.theorem_id] = selected_signature
    return recovered


def _retry_tree_views(
    backend: LeanInteractBackend,
    theorems: list[TheoremForRepresentation],
    imports: str,
) -> dict[str, dict[str, Any]]:
    requests = [
        LeanRequest(
            request_id=(
                f"repr-{theorem.theorem_id.removeprefix('thm:')[:16]}-{NORMALIZATION_VERSION}-expr"
            ),
            context_id=theorem.context_id,
            code=_single_tree_command(imports, theorem),
            allow_sorry=theorem.inline_declaration,
            timeout_seconds=600.0,
        )
        for theorem in theorems
    ]
    recovered: dict[str, dict[str, Any]] = {}
    for theorem, result in zip(
        theorems, _run_representation_requests(backend, requests), strict=True
    ):
        if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
            continue
        expected_name = _expr_lookup_name(theorem)
        selected_tree: dict[str, Any] | None = None
        saw_expected_or_malformed_payload = False
        for message in result.messages:
            for candidate in str(message.get("data", "")).splitlines():
                if "LFTREEJSON " not in candidate:
                    continue
                parsed_name, tree = parse_lftree_payload(candidate)
                if parsed_name == expected_name:
                    selected_tree = tree
                    saw_expected_or_malformed_payload = True
                elif not parsed_name:
                    selected_tree = None
                    saw_expected_or_malformed_payload = True
        if saw_expected_or_malformed_payload and selected_tree is not None:
            recovered[theorem.theorem_id] = selected_tree
    return recovered


def build_representation_batch(
    backend: LeanInteractBackend,
    batch: RepresentationBatch,
    *,
    created_at: datetime.datetime,
) -> RepresentationBatchResult:
    """Build one homogeneous batch without cross-theorem Lean requests."""

    if not batch.ordered_theorem_inputs:
        return RepresentationBatchResult((), ())
    mismatched = [
        theorem.theorem_id
        for theorem in batch.ordered_theorem_inputs
        if theorem.context_id != batch.context_id
    ]
    if mismatched:
        raise ValueError(
            "RepresentationBatch contains mixed contexts; rejected before Lean execution: "
            + ", ".join(mismatched)
        )

    theorems = list(batch.ordered_theorem_inputs)
    requests = [
        LeanRequest(
            request_id=(
                f"repr-{theorem.theorem_id.removeprefix('thm:')[:16]}-"
                f"{NORMALIZATION_VERSION}-combined"
            ),
            context_id=theorem.context_id,
            code=_combined_command(batch.import_header, theorem),
            allow_sorry=theorem.inline_declaration,
            timeout_seconds=600.0,
        )
        for theorem in theorems
    ]
    parsed: dict[
        str,
        tuple[str | None, str | None, dict[str, Any] | None],
    ] = {}
    for theorem, result in zip(
        theorems, _run_representation_requests(backend, requests), strict=True
    ):
        values: tuple[str | None, str | None, dict[str, Any] | None] = (None, None, None)
        if result.status in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
            values = _parse_combined_result(
                result,
                theorem.full_name,
                dump_name=_expr_lookup_name(theorem),
                allow_environment_signatures=True,
            )
        parsed[theorem.theorem_id] = values

    missing_signature_pp = [
        theorem for theorem in theorems if parsed[theorem.theorem_id][0] is None
    ]
    missing_signature_explicit = [
        theorem for theorem in theorems if parsed[theorem.theorem_id][1] is None
    ]
    missing_tree_views = [theorem for theorem in theorems if parsed[theorem.theorem_id][2] is None]
    recovered_pp = _retry_environment_signature_views(
        backend,
        missing_signature_pp,
        batch.import_header,
        explicit=False,
    )
    recovered_explicit = _retry_environment_signature_views(
        backend,
        missing_signature_explicit,
        batch.import_header,
        explicit=True,
    )
    recovered_tree = _retry_tree_views(
        backend,
        missing_tree_views,
        batch.import_header,
    )

    records: list[RepresentationRecord] = []
    failures: list[RepresentationFailure] = []
    for theorem in theorems:
        signature_pp, signature_explicit, tree = parsed[theorem.theorem_id]
        signature_pp = signature_pp or recovered_pp.get(theorem.theorem_id)
        signature_explicit = signature_explicit or recovered_explicit.get(theorem.theorem_id)
        tree = tree or recovered_tree.get(theorem.theorem_id)
        record = _build_record(
            theorem,
            signature_pp,
            signature_explicit,
            tree,
            created_at,
        )
        records.append(record)
        for view, status in record.view_status.items():
            if status == ViewStatus.FAILED:
                detail = "combined request and independent view retry did not recover view"
                failures.append(
                    RepresentationFailure(
                        theorem_id=theorem.theorem_id,
                        view=view,
                        status="failed",
                        detail=detail,
                    )
                )
    return RepresentationBatchResult(tuple(records), tuple(failures))


def build_representations(
    backend: LeanInteractBackend,
    theorems: list[TheoremForRepresentation],
    *,
    imports: str,
    created_at: datetime.datetime,
    batch_label: str = "repr",
) -> list[RepresentationRecord]:
    """Compatibility wrapper around the canonical RepresentationBatch API."""

    del batch_label
    if not theorems:
        return []
    result = build_representation_batch(
        backend,
        RepresentationBatch(
            context_id=theorems[0].context_id,
            import_header=imports,
            ordered_theorem_inputs=tuple(theorems),
        ),
        created_at=created_at,
    )
    return list(result.ordered_representation_records)


def _build_record(
    theorem: TheoremForRepresentation,
    signature_pp: str | None,
    signature_explicit: str | None,
    expr_tree: dict[str, Any] | None,
    created_at: datetime.datetime,
) -> RepresentationRecord:
    # Prefer the Lean-parsed signature (robust to comments/attributes/guillemet
    # names); fall back to string normalization only when it is unavailable.
    headless = theorem.source_signature or normalize_headless(theorem.proof_stripped)
    views: dict[str, str | None] = {
        "raw_proof_stripped": theorem.proof_stripped,
        "headless": headless,
        "signature_pp": signature_pp,
        "signature_explicit": signature_explicit,
    }
    atoms = semantic_atoms(expr_tree) if expr_tree is not None else None
    op_tree = operator_tree(expr_tree) if expr_tree is not None else None
    views_full: dict[str, object] = dict(views)
    views_full["semantic_atoms"] = list(atoms) if atoms is not None else None
    views_full["operator_tree"] = op_tree
    identity_fingerprint = alpha_identity_fingerprint(expr_tree) if expr_tree is not None else None
    views_full["alpha_identity_fingerprint"] = identity_fingerprint

    view_status = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    view_status["raw_proof_stripped"] = ViewStatus.OK
    view_status["headless"] = ViewStatus.OK if headless else ViewStatus.FAILED
    view_status["signature_pp"] = ViewStatus.OK if signature_pp else ViewStatus.FAILED
    view_status["signature_explicit"] = ViewStatus.OK if signature_explicit else ViewStatus.FAILED
    view_status["semantic_atoms"] = ViewStatus.OK if atoms is not None else ViewStatus.FAILED
    view_status["operator_tree"] = ViewStatus.OK if op_tree is not None else ViewStatus.FAILED
    representation_id = make_id(
        REPRESENTATION_PREFIX,
        {"theorem_id": theorem.theorem_id, "normalization_version": NORMALIZATION_VERSION},
    )
    return RepresentationRecord(
        representation_id=representation_id,
        theorem_id=theorem.theorem_id,
        normalization_version=NORMALIZATION_VERSION,
        context_id=theorem.context_id,
        raw_proof_stripped=theorem.proof_stripped,
        headless=headless,
        signature_pp=signature_pp,
        signature_explicit=signature_explicit,
        semantic_atoms=atoms,
        operator_tree=op_tree,
        alpha_identity_fingerprint=identity_fingerprint,
        view_status=view_status,
        option_profile={
            "signature_source": "ConstantInfo.type",
            "signature_option_base": "Options.empty",
            "signature_profile": "lfPpType(explicit=false,universes=false)",
            "explicit_profile": "lfPpType(explicit=true,universes=true)",
            "legacy_check_signature_pins": PP_SIGNATURE_INLINE,
            "legacy_check_explicit_pins": PP_EXPLICIT_INLINE,
        },
        content_hash=representation_content_hash(views_full),
        created_at=created_at,
    )
