"""Lean-backed representation builder (PLAN.md §13, LF-014).

Supersedes the minimal ``repr_v0_extract`` record from extraction with a
``repr_v1`` multi-view record: the three required v0 views plus the
elaborated ``signature_explicit``, obtained by batched ``#check @name`` under
pinned pp options. Batching loads the environment once per option set.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.atoms import (
    operator_tree,
    parse_lfjson_line,
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
    calls = "\n".join(f'lfDump "{name}"' for name in full_names)
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
    result = backend.run(
        LeanRequest(
            request_id=request_id,
            context_id=context_id,
            code=_dump_command(imports, _expr_json_helper(), names),
            timeout_seconds=600.0,
        )
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
    result = backend.run(
        LeanRequest(
            request_id=request_id,
            context_id=context_id,
            code=check_command(imports, options_inline, names),
            timeout_seconds=300.0,
        )
    )
    if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
        return {}
    return _map_check_types(_info_messages(result), set(names))


def build_representations(
    backend: LeanInteractBackend,
    theorems: list[TheoremForRepresentation],
    *,
    imports: str,
    created_at: datetime.datetime,
    batch_label: str = "repr",
) -> list[RepresentationRecord]:
    """Build ``repr_v1`` records for a batch of pre-existing declarations."""
    names = [t.full_name for t in theorems]
    signature_pp = _run_check_batch(
        backend,
        theorems[0].context_id,
        imports,
        PP_SIGNATURE_INLINE,
        names,
        f"{batch_label}-sig",
    )
    signature_explicit = _run_check_batch(
        backend,
        theorems[0].context_id,
        imports,
        PP_EXPLICIT_INLINE,
        names,
        f"{batch_label}-explicit",
    )
    trees = _run_expr_dump_batch(
        backend, theorems[0].context_id, imports, names, f"{batch_label}-expr"
    )
    records = []
    for theorem in theorems:
        records.append(
            _build_record(
                theorem,
                signature_pp.get(theorem.full_name),
                signature_explicit.get(theorem.full_name),
                trees.get(theorem.full_name),
                created_at,
            )
        )
    return records


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
        view_status=view_status,
        option_profile={
            "signature_pins": PP_SIGNATURE_INLINE,
            "explicit_pins": PP_EXPLICIT_INLINE,
        },
        content_hash=representation_content_hash(views_full),
        created_at=created_at,
    )
