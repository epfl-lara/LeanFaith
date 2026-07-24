"""LF-018 N03: drop one elaboration-proven independent proposition hypothesis.

The v1 rule is intentionally narrower than the family name suggests.  It
deletes exactly one explicit, singleton header binder of the surface form
``(h : P)``, where ``P`` is itself an explicitly declared proposition
variable.  The elaborated expression tree must prove that:

* the selected binder domain is the preceding ``P : Prop`` binder;
* no later binder domain refers to ``h``; and
* the conclusion does not refer to ``h``.

The candidate is reconstructed by an exact span deletion, then audited against
the expression tree obtained by erasing that unused ``forall`` and lowering
de-Bruijn indices.  This is mutation provenance only: every mechanically clean
output remains ``provisional`` with a ``near_miss`` intention.  The rule never
performs proof search and never creates a semantic negative label.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.representations.atoms import operator_tree as build_operator_tree
from leanfaith.representations.atoms import semantic_atoms
from leanfaith.representations.pipeline import alpha_canonical_bytes
from leanfaith.schemas.enums import (
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
    ViewStatus,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import Applicability, TransformationAudit, VariantDraft
from leanfaith.transforms.positives.p02_binders import (
    BinderKind,
    BinderParseError,
    TypedBinder,
    parse_typed_binders,
)
from leanfaith.transforms.protocol import (
    TransformationIdentityError,
    build_transformation_audit,
    build_variant_draft,
    verify_variant_draft_id,
)

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
SemanticVersion = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", strict=True)]
ErrorCode = Annotated[str, Field(pattern=r"^E(0[1-9]|[12][0-9]|30)$", strict=True)]

_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_BINDER_INFO = {
    BinderKind.EXPLICIT: "default",
    BinderKind.IMPLICIT: "implicit",
    BinderKind.STRICT_IMPLICIT: "strictImplicit",
    BinderKind.INSTANCE: "instImplicit",
}
_PLACEHOLDER_RE = re.compile(r":=\s*(?:by\s+)?sorry\s*\Z", re.DOTALL)
_DECLARATION_KEYWORDS = frozenset({"lemma", "theorem"})
_EXPR_NODE_KEYS = {
    "forall": frozenset({"k", "bi", "dom", "body"}),
    "lam": frozenset({"k", "bi", "dom", "body"}),
    "app": frozenset({"k", "fn", "arg"}),
    "const": frozenset({"k", "n", "us"}),
    "bvar": frozenset({"k", "i"}),
    "sort": frozenset({"k", "u"}),
    "lit_nat": frozenset({"k", "nat"}),
    "lit_str": frozenset({"k", "str"}),
    "proj": frozenset({"k", "s", "i", "base"}),
    "let": frozenset({"k", "t", "v", "body"}),
}


class N03DropHypothesisError(ValueError):
    """An N03 config, source, trace, dependency, or audit invariant failed."""


class _UnsupportedSource(N03DropHypothesisError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class N03DropHypothesisConfig(StrictModel):
    """Strict and versioned v1 policy for independent-hypothesis deletion."""

    schema_version: Literal[1] = 1
    rule_id: Literal["n03_drop_hypothesis"] = "n03_drop_hypothesis"
    rule_version: SemanticVersion
    family_id: Literal["n03_drop_hypothesis"] = "n03_drop_hypothesis"
    implementation_key: Literal["n03_drop_hypothesis"] = "n03_drop_hypothesis"
    candidate_pool: NonEmptyStr
    supported_declaration_kinds: tuple[Literal["lemma", "theorem"], ...]
    placeholder_forms: tuple[Literal["by_sorry", "sorry"], ...]
    supported_hypothesis_binder_kind: Literal["explicit"]
    proposition_domain_policy: Literal["direct_declared_prop_variable"]
    require_no_later_dependency: Literal[True]
    require_exact_expr_erasure: Literal[True]
    intended_error_types: tuple[ErrorCode, ...]
    failed_proof_search_is_negative_evidence: Literal[False]

    @model_validator(mode="after")
    def _closed_scope(self) -> N03DropHypothesisConfig:
        if self.supported_declaration_kinds != ("lemma", "theorem"):
            raise ValueError("supported_declaration_kinds must be exactly [lemma, theorem]")
        if self.placeholder_forms != ("by_sorry", "sorry"):
            raise ValueError("placeholder_forms must be exactly [by_sorry, sorry]")
        if self.intended_error_types != ("E01",):
            raise ValueError("N03 v1 intended_error_types must be exactly [E01]")
        return self


def load_n03_drop_hypothesis_config(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedConfig[N03DropHypothesisConfig]:
    """Load the canonical N03 policy and reject repository path escapes."""

    root = find_repo_root(repo_root).resolve()
    resolved = (path or root / "configs/transformations/n03_drop_hypothesis.yaml").resolve()
    if not resolved.is_relative_to(root):
        raise N03DropHypothesisError("n03 config path escapes the repository")
    return load_config(resolved, N03DropHypothesisConfig)


@dataclass(frozen=True, slots=True)
class OuterForallBinder:
    """One outer ``forall`` binder and its elaborated dependencies."""

    index: int
    binder_info: str
    domain: dict[str, object]
    depends_on: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class OuterForallAnalysis:
    """Outer binder chain plus dependencies of the residual conclusion."""

    binders: tuple[OuterForallBinder, ...]
    conclusion: dict[str, object]
    conclusion_depends_on: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _SurfaceBinder:
    """One name from a possibly grouped declaration-header binder."""

    outer_index: int
    group: TypedBinder
    name: str


@dataclass(frozen=True, slots=True)
class HypothesisDropSite:
    """One exact, elaboration-certified N03 deletion site."""

    outer_index: int
    surface_binder_index: int
    start: int
    end: int
    hypothesis_name: str
    proposition_name: str
    source_text: str
    source_outer_binder_count: int
    source_root_hash: str
    expected_candidate_root_hash: str
    dependency_proof_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "outer_index": self.outer_index,
                "surface_binder_index": self.surface_binder_index,
                "start": self.start,
                "end": self.end,
                "hypothesis_name": self.hypothesis_name,
                "proposition_name": self.proposition_name,
                "source_text": self.source_text,
                "source_outer_binder_count": self.source_outer_binder_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
                "dependency_proof_hash": self.dependency_proof_hash,
            }
        )


def _count_declaration_keywords(source: str) -> int:
    """Count theorem/lemma tokens while ignoring comments and quoted syntax."""

    count = 0
    index = 0
    while index < len(source):
        if source.startswith("--", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline
            continue
        if source.startswith("/-", index):
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith("/-", index):
                    depth += 1
                    index += 2
                elif source.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise _UnsupportedSource("unterminated_block_comment")
            continue
        character = source[index]
        if character in {'"', "'"}:
            delimiter = character
            index += 1
            escaped = False
            while index < len(source):
                character = source[index]
                index += 1
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == delimiter:
                    break
            else:
                raise _UnsupportedSource("unterminated_quoted_token")
            continue
        if character == "«":
            close = source.find("»", index + 1)
            if close < 0:
                raise _UnsupportedSource("unterminated_guillemet_identifier")
            index = close + 1
            continue
        if character.isalpha() or character == "_":
            start = index
            index += 1
            while index < len(source) and (source[index].isalnum() or source[index] in {"_", "'"}):
                index += 1
            if source[start:index] in _DECLARATION_KEYWORDS:
                count += 1
            continue
        index += 1
    return count


def _child_dict(node: Mapping[str, object], key: str, *, reason: str) -> dict[str, object]:
    value = node.get(key)
    if not isinstance(value, dict):
        raise N03DropHypothesisError(reason)
    return value


def _validate_expr_tree(node: object, *, depth: int = 0) -> None:
    """Validate the exact proof-free Expr JSON grammar and bound-variable scope."""

    if not isinstance(node, dict):
        raise N03DropHypothesisError("expr_node_not_mapping")
    kind = node.get("k")
    if kind in {"fvar", "mvar"}:
        raise N03DropHypothesisError(f"unsupported_{kind}_in_closed_declaration_type")
    if not isinstance(kind, str):
        raise N03DropHypothesisError("expr_node_kind_missing")
    shape_key = kind
    if kind == "lit":
        if ("nat" in node) == ("str" in node):
            raise N03DropHypothesisError("malformed_literal_node")
        shape_key = "lit_nat" if "nat" in node else "lit_str"
    expected_keys = _EXPR_NODE_KEYS.get(shape_key)
    if expected_keys is None:
        raise N03DropHypothesisError(f"unsupported_expr_node_kind:{kind}")
    if frozenset(node) != expected_keys:
        raise N03DropHypothesisError(f"malformed_expr_node_shape:{kind}")

    if kind == "bvar":
        raw_index = node["i"]
        if (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or not 0 <= raw_index < depth
        ):
            raise N03DropHypothesisError("out_of_scope_bvar")
        return
    if kind == "const":
        if not isinstance(node["n"], str) or not isinstance(node["us"], str):
            raise N03DropHypothesisError("malformed_const_node")
        return
    if kind == "sort":
        if not isinstance(node["u"], str):
            raise N03DropHypothesisError("malformed_sort_node")
        return
    if kind == "lit":
        value = node["nat"] if "nat" in node else node["str"]
        if not isinstance(value, str):
            raise N03DropHypothesisError("malformed_literal_value")
        return
    if kind in {"forall", "lam"}:
        if node["bi"] not in {"default", "implicit", "strictImplicit", "instImplicit"}:
            raise N03DropHypothesisError("unknown_binder_info")
        _validate_expr_tree(node["dom"], depth=depth)
        _validate_expr_tree(node["body"], depth=depth + 1)
        return
    if kind == "app":
        _validate_expr_tree(node["fn"], depth=depth)
        _validate_expr_tree(node["arg"], depth=depth)
        return
    if kind == "proj":
        if (
            not isinstance(node["s"], str)
            or not isinstance(node["i"], int)
            or isinstance(node["i"], bool)
        ):
            raise N03DropHypothesisError("malformed_projection_node")
        _validate_expr_tree(node["base"], depth=depth)
        return
    if kind == "let":
        _validate_expr_tree(node["t"], depth=depth)
        _validate_expr_tree(node["v"], depth=depth)
        _validate_expr_tree(node["body"], depth=depth + 1)


def _outer_dependencies(
    node: object,
    *,
    preceding_binders: int,
    local_depth: int = 0,
) -> set[int]:
    """Map de-Bruijn references in ``node`` to outer-binder indices."""

    if not isinstance(node, dict):
        return set()
    kind = node.get("k")
    if kind == "bvar":
        raw_index = node.get("i")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            raise N03DropHypothesisError("malformed_bvar_index")
        if raw_index >= local_depth:
            target = preceding_binders - 1 - (raw_index - local_depth)
            if target < 0:
                raise N03DropHypothesisError("outer_bvar_target_out_of_scope")
            return {target}
        return set()

    dependencies: set[int] = set()
    if kind in {"forall", "lam"}:
        dependencies.update(
            _outer_dependencies(
                node.get("dom"),
                preceding_binders=preceding_binders,
                local_depth=local_depth,
            )
        )
        dependencies.update(
            _outer_dependencies(
                node.get("body"),
                preceding_binders=preceding_binders,
                local_depth=local_depth + 1,
            )
        )
        return dependencies
    if kind == "let":
        for key in ("t", "v"):
            dependencies.update(
                _outer_dependencies(
                    node.get(key),
                    preceding_binders=preceding_binders,
                    local_depth=local_depth,
                )
            )
        dependencies.update(
            _outer_dependencies(
                node.get("body"),
                preceding_binders=preceding_binders,
                local_depth=local_depth + 1,
            )
        )
        return dependencies

    for key in ("dom", "body", "fn", "arg", "base", "t", "v"):
        child = node.get(key)
        if isinstance(child, dict):
            dependencies.update(
                _outer_dependencies(
                    child,
                    preceding_binders=preceding_binders,
                    local_depth=local_depth,
                )
            )
    return dependencies


def analyze_outer_foralls(operator_tree_view: Mapping[str, object]) -> OuterForallAnalysis:
    """Derive the outer dependency chain from an elaborated operator tree."""

    root = operator_tree_view.get("root")
    if not isinstance(root, dict):
        raise N03DropHypothesisError("operator_tree_missing_root")
    _validate_expr_tree(root)
    binders: list[OuterForallBinder] = []
    node: dict[str, object] = root
    while node.get("k") == "forall":
        domain = _child_dict(node, "dom", reason="forall_missing_domain")
        index = len(binders)
        binders.append(
            OuterForallBinder(
                index=index,
                binder_info=str(node.get("bi", "")),
                domain=domain,
                depends_on=tuple(sorted(_outer_dependencies(domain, preceding_binders=index))),
            )
        )
        node = _child_dict(node, "body", reason="forall_missing_body")
    return OuterForallAnalysis(
        binders=tuple(binders),
        conclusion=node,
        conclusion_depends_on=tuple(
            sorted(_outer_dependencies(node, preceding_binders=len(binders)))
        ),
    )


def _surface_binders(source: str) -> tuple[_SurfaceBinder, ...]:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise _UnsupportedSource(str(exc)) from exc
    flattened: list[_SurfaceBinder] = []
    for group in groups:
        for name in group.names:
            flattened.append(
                _SurfaceBinder(
                    outer_index=len(flattened),
                    group=group,
                    name=name,
                )
            )
    return tuple(flattened)


def _prop_sort_domain(node: Mapping[str, object]) -> bool:
    return node.get("k") == "sort" and node.get("u") == "0"


def _direct_prop_variable_target(
    *,
    binder: OuterForallBinder,
    prior_binders: Sequence[OuterForallBinder],
) -> int | None:
    domain = binder.domain
    if domain.get("k") != "bvar":
        return None
    raw_index = domain.get("i")
    if not isinstance(raw_index, int) or isinstance(raw_index, bool):
        return None
    target = binder.index - 1 - raw_index
    if target < 0 or target >= len(prior_binders):
        return None
    return target if _prop_sort_domain(prior_binders[target].domain) else None


def _lower_after_erasure(node: object, *, cutoff: int = 0) -> object:
    """Lower outer references after removing one unused surrounding binder."""

    if isinstance(node, list):
        return [_lower_after_erasure(item, cutoff=cutoff) for item in node]
    if not isinstance(node, dict):
        return node
    kind = node.get("k")
    if kind == "bvar":
        raw_index = node.get("i")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            raise N03DropHypothesisError("malformed_bvar_index")
        if raw_index == cutoff:
            raise N03DropHypothesisError("selected_hypothesis_is_referenced")
        lowered = raw_index - 1 if raw_index > cutoff else raw_index
        return {**node, "i": lowered}

    result: dict[str, object] = {}
    for key, value in node.items():
        child_cutoff = cutoff
        if (kind in {"forall", "lam"} or kind == "let") and key == "body":
            child_cutoff += 1
        result[key] = _lower_after_erasure(value, cutoff=child_cutoff)
    return result


def erase_outer_forall(
    root: Mapping[str, object],
    outer_index: int,
) -> dict[str, object]:
    """Remove one unused outer ``forall`` and lower its body exactly."""

    if outer_index < 0:
        raise N03DropHypothesisError("negative_outer_binder_index")
    node = dict(root)
    if node.get("k") != "forall":
        raise N03DropHypothesisError("outer_binder_index_out_of_range")
    if outer_index == 0:
        body = _child_dict(node, "body", reason="forall_missing_body")
        lowered = _lower_after_erasure(body)
        if not isinstance(lowered, dict):
            raise N03DropHypothesisError("lowered_body_not_expression")
        return lowered
    body = _child_dict(node, "body", reason="forall_missing_body")
    return {**node, "body": erase_outer_forall(body, outer_index - 1)}


def _root_hash(root: Mapping[str, object]) -> str:
    return sha256_hex(alpha_canonical_bytes(dict(root)))


def _dependency_proof_hash(
    *,
    analysis: OuterForallAnalysis,
    selected_index: int,
    prop_index: int,
) -> str:
    return hash_canonical(
        {
            "policy": "no_later_domain_or_conclusion_dependency_v1",
            "selected_index": selected_index,
            "prop_index": prop_index,
            "selected_depends_on": analysis.binders[selected_index].depends_on,
            "later_dependencies": [
                {
                    "index": binder.index,
                    "depends_on": binder.depends_on,
                }
                for binder in analysis.binders[selected_index + 1 :]
            ],
            "conclusion_depends_on": analysis.conclusion_depends_on,
        }
    )


def enumerate_independent_prop_hypotheses(
    source: str,
    operator_tree_view: Mapping[str, object],
) -> tuple[HypothesisDropSite, ...]:
    """Enumerate exact singleton ``(h : P)`` binders proven unused downstream."""

    if _PLACEHOLDER_RE.search(source) is None:
        raise _UnsupportedSource("unsupported_proof_placeholder")
    if _count_declaration_keywords(source) != 1:
        raise _UnsupportedSource("expected_exactly_one_declaration")
    surfaces = _surface_binders(source)
    analysis = analyze_outer_foralls(operator_tree_view)
    if not surfaces:
        raise _UnsupportedSource("no_surface_typed_binders")
    if len(analysis.binders) < len(surfaces):
        raise _UnsupportedSource("surface_elaboration_binder_count_mismatch")

    # Header binders must form the exact prefix of the elaborated forall chain.
    # This catches auto-implicit binders that were absent from source syntax.
    for surface, elaborated in zip(surfaces, analysis.binders, strict=False):
        if _BINDER_INFO[surface.group.kind] != elaborated.binder_info:
            raise _UnsupportedSource("surface_elaboration_binder_alignment_mismatch")

    root = operator_tree_view.get("root")
    if not isinstance(root, dict):
        raise _UnsupportedSource("operator_tree_missing_root")
    source_root_hash = _root_hash(root)
    sites: list[HypothesisDropSite] = []
    for surface in surfaces:
        group = surface.group
        if group.kind != BinderKind.EXPLICIT or len(group.names) != 1 or group.has_comment:
            continue
        binder = analysis.binders[surface.outer_index]
        prop_index = _direct_prop_variable_target(
            binder=binder,
            prior_binders=analysis.binders[: surface.outer_index],
        )
        if prop_index is None:
            continue
        prop_surface = surfaces[prop_index] if prop_index < len(surfaces) else None
        if prop_surface is None:
            continue
        if group.type_tokens != (prop_surface.name,) or prop_surface.group.type_tokens != ("Prop",):
            continue
        if any(
            surface.outer_index in later.depends_on
            for later in analysis.binders[surface.outer_index + 1 :]
        ):
            continue
        if surface.outer_index in analysis.conclusion_depends_on:
            continue
        try:
            expected_root = erase_outer_forall(root, surface.outer_index)
        except N03DropHypothesisError:
            continue
        proof_hash = _dependency_proof_hash(
            analysis=analysis,
            selected_index=surface.outer_index,
            prop_index=prop_index,
        )
        sites.append(
            HypothesisDropSite(
                outer_index=surface.outer_index,
                surface_binder_index=group.index,
                start=group.start,
                end=group.end,
                hypothesis_name=surface.name,
                proposition_name=prop_surface.name,
                source_text=group.original_text,
                source_outer_binder_count=len(analysis.binders),
                source_root_hash=source_root_hash,
                expected_candidate_root_hash=_root_hash(expected_root),
                dependency_proof_hash=proof_hash,
            )
        )
    return tuple(sorted(sites, key=lambda site: site.stable_key))


def _choose_site(
    sites: Sequence[HypothesisDropSite],
    *,
    theorem_id: str,
    seed: int,
) -> HypothesisDropSite:
    if not sites:
        raise N03DropHypothesisError("no_independent_prop_hypothesis")

    def rank(site: HypothesisDropSite) -> bytes:
        payload = f"n03-select-v1\0{theorem_id}\0{seed}\0{site.stable_key}"
        return hashlib.sha256(payload.encode("utf-8")).digest()

    return min(sites, key=rank)


def _trace(
    source: str,
    site: HypothesisDropSite,
    *,
    rule_config_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "delete_independent_prop_hypothesis",
            "start": site.start,
            "end": site.end,
            "expected_text": site.source_text,
            "replacement_text": "",
            "input_code_hash": sha256_hex(source.encode("utf-8")),
            "rule_config_hash": rule_config_hash,
            "outer_binder_index": site.outer_index,
            "hypothesis_name": site.hypothesis_name,
            "proposition_name": site.proposition_name,
            "dependency_proof_hash": site.dependency_proof_hash,
        },
    )


def _inverse_trace(
    candidate: str,
    site: HypothesisDropSite,
    *,
    rule_config_hash: str,
) -> tuple[dict[str, JsonValue], ...]:
    return (
        {
            "operation": "insert_independent_prop_hypothesis",
            "start": site.start,
            "end": site.start,
            "expected_text": "",
            "replacement_text": site.source_text,
            "input_code_hash": sha256_hex(candidate.encode("utf-8")),
            "rule_config_hash": rule_config_hash,
            "outer_binder_index": site.outer_index,
            "hypothesis_name": site.hypothesis_name,
            "proposition_name": site.proposition_name,
            "dependency_proof_hash": site.dependency_proof_hash,
        },
    )


def apply_hypothesis_trace(
    source: str,
    trace: Sequence[Mapping[str, object]],
    *,
    expected_rule_config_hash: str | None = None,
) -> str:
    """Replay exact N03 deletion/insertion steps and reject stale traces."""

    if not trace:
        raise N03DropHypothesisError("empty_hypothesis_trace")
    result = source
    for item in trace:
        if item.get("operation") not in {
            "delete_independent_prop_hypothesis",
            "insert_independent_prop_hypothesis",
        }:
            raise N03DropHypothesisError("unexpected_trace_operation")
        if (
            expected_rule_config_hash is not None
            and item.get("rule_config_hash") != expected_rule_config_hash
        ):
            raise N03DropHypothesisError("trace_rule_config_hash_mismatch")
        if item.get("input_code_hash") != sha256_hex(result.encode("utf-8")):
            raise N03DropHypothesisError("trace_input_code_hash_mismatch")
        start = item.get("start")
        end = item.get("end")
        expected = item.get("expected_text")
        replacement_text = item.get("replacement_text")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(expected, str)
            or not isinstance(replacement_text, str)
        ):
            raise N03DropHypothesisError("malformed_hypothesis_trace")
        if not 0 <= start <= end <= len(result):
            raise N03DropHypothesisError("trace_span_out_of_bounds")
        if result[start:end] != expected:
            raise N03DropHypothesisError("trace_expected_text_mismatch")
        result = result[:start] + replacement_text + result[end:]
    return result


def _expected_structural_diff(
    site: HypothesisDropSite,
    *,
    rule_config_hash: str,
) -> dict[str, JsonValue]:
    return {
        "operation": "delete_independent_prop_hypothesis",
        "source_span_start": site.start,
        "source_span_end": site.end,
        "outer_binder_index": site.outer_index,
        "surface_binder_index": site.surface_binder_index,
        "hypothesis_name": site.hypothesis_name,
        "proposition_name": site.proposition_name,
        "source_outer_binder_count": site.source_outer_binder_count,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
        "dependency_proof_hash": site.dependency_proof_hash,
        "rule_config_hash": rule_config_hash,
    }


class N03DropHypothesisRule:
    """Delete one independently verified proposition hypothesis, provisionally."""

    rule_id = "n03_drop_hypothesis"
    family_id = "n03_drop_hypothesis"
    polarity = Polarity.NEGATIVE
    implementation_key = "n03_drop_hypothesis"

    def __init__(
        self,
        *,
        registry_hash: str,
        config: N03DropHypothesisConfig | None = None,
        rule_config_hash: str | None = None,
    ) -> None:
        if len(registry_hash) != 64:
            raise N03DropHypothesisError("registry_hash must be a SHA-256 hex digest")
        int(registry_hash, 16)
        if (config is None) != (rule_config_hash is None):
            raise N03DropHypothesisError("config and rule_config_hash must be supplied together")
        if config is None:
            loaded = load_n03_drop_hypothesis_config()
            config = loaded.config
            rule_config_hash = loaded.config_hash
        assert rule_config_hash is not None
        if len(rule_config_hash) != 64:
            raise N03DropHypothesisError("rule_config_hash must be a SHA-256 hex digest")
        int(rule_config_hash, 16)
        self.registry_hash = registry_hash
        self.config = config
        self.rule_config_hash = rule_config_hash
        self.rule_version = config.rule_version
        self.audit_config_hash = hash_canonical(
            {
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "rule_config_hash": rule_config_hash,
                "registry_hash": registry_hash,
                "policy": "exact_unused_prop_forall_erasure_provisional_v1",
            }
        )

    @classmethod
    def from_repository(
        cls,
        *,
        registry_hash: str,
        repo_root: Path | None = None,
    ) -> N03DropHypothesisRule:
        loaded = load_n03_drop_hypothesis_config(repo_root)
        return cls(
            registry_hash=registry_hash,
            config=loaded.config,
            rule_config_hash=loaded.config_hash,
        )

    def _sites(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> tuple[HypothesisDropSite, ...]:
        if not theorem.is_proposition:
            raise _UnsupportedSource("source_not_proposition")
        if theorem.declaration_kind not in self.config.supported_declaration_kinds:
            raise _UnsupportedSource("unsupported_declaration_kind")
        if theorem.elaboration_status not in _VALID_ELABORATION:
            raise _UnsupportedSource("source_does_not_elaborate")
        if representation.theorem_id != theorem.theorem_id:
            raise _UnsupportedSource("source_representation_lineage_mismatch")
        if representation.context_id != theorem.context_id:
            raise _UnsupportedSource("source_context_mismatch")
        if representation.raw_proof_stripped != theorem.proof_stripped_declaration:
            raise _UnsupportedSource("source_representation_text_mismatch")
        required_views = (
            representation.signature_explicit,
            representation.semantic_atoms,
            representation.operator_tree,
            representation.alpha_identity_fingerprint,
        )
        if any(view is None for view in required_views):
            raise _UnsupportedSource("source_required_view_missing")
        assert representation.operator_tree is not None
        source_root = representation.operator_tree.get("root")
        if (
            not isinstance(source_root, dict)
            or representation.operator_tree != build_operator_tree(source_root)
            or representation.semantic_atoms != semantic_atoms(source_root)
            or representation.alpha_identity_fingerprint
            != sha256_hex(alpha_canonical_bytes(source_root))
        ):
            raise _UnsupportedSource("source_derived_views_inconsistent")
        try:
            sites = enumerate_independent_prop_hypotheses(
                theorem.proof_stripped_declaration,
                representation.operator_tree,
            )
        except _UnsupportedSource:
            raise
        except N03DropHypothesisError as exc:
            raise _UnsupportedSource(f"malformed_operator_tree:{exc}") from exc
        if not sites:
            raise _UnsupportedSource("no_independent_prop_hypothesis")
        return sites

    def assess(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability:
        try:
            sites = self._sites(theorem, representation)
        except _UnsupportedSource as exc:
            return Applicability(applicable=False, reason_codes=(exc.reason_code,))
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=tuple(
                f"forall:{site.outer_index}:{site.hypothesis_name}:{site.start}:{site.end}"
                for site in sites
            ),
            required_capabilities=(
                "alpha_identity_fingerprint",
                "binder_dependency_audit",
                "exact_expr_erasure",
                "lean_reelaboration",
                "operator_tree",
                "semantic_atoms",
            ),
            metadata={
                "eligible_site_count": len(sites),
                "rule_config_hash": self.rule_config_hash,
            },
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> Sequence[VariantDraft]:
        sites = self._sites(theorem, representation)
        site = _choose_site(sites, theorem_id=theorem.theorem_id, seed=seed)
        trace = _trace(
            theorem.proof_stripped_declaration,
            site,
            rule_config_hash=self.rule_config_hash,
        )
        candidate = apply_hypothesis_trace(
            theorem.proof_stripped_declaration,
            trace,
            expected_rule_config_hash=self.rule_config_hash,
        )
        inverse = _inverse_trace(
            candidate,
            site,
            rule_config_hash=self.rule_config_hash,
        )
        if (
            apply_hypothesis_trace(
                candidate,
                inverse,
                expected_rule_config_hash=self.rule_config_hash,
            )
            != theorem.proof_stripped_declaration
        ):
            raise N03DropHypothesisError("n03_internal_roundtrip_failure")
        return (
            build_variant_draft(
                source_theorem_ids=(theorem.theorem_id,),
                source_representation_ids=(representation.representation_id,),
                context_id=theorem.context_id,
                rule_id=self.rule_id,
                rule_version=self.rule_version,
                family_id=self.family_id,
                seed=seed,
                candidate_code=candidate,
                intended_relation=IntendedRelation.NEAR_MISS,
                intended_error_types=self.config.intended_error_types,
                candidate_pool=self.config.candidate_pool,
                transformation_trace=trace,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(
                    site,
                    rule_config_hash=self.rule_config_hash,
                ),
                generation_config_hash=self.registry_hash,
                metadata={
                    "failed_proof_search_consulted": False,
                    "intention_is_not_label": True,
                    "rule_config_hash": self.rule_config_hash,
                    "semantic_negative_resolved": False,
                },
            ),
        )

    def audit(
        self,
        source: TheoremRecord,
        source_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        violations: list[str] = []
        lineage_ok = (
            draft.rule_id == self.rule_id
            and draft.rule_version == self.rule_version
            and draft.family_id == self.family_id
            and draft.generation_config_hash == self.registry_hash
            and draft.candidate_pool == self.config.candidate_pool
            and draft.source_theorem_ids == (source.theorem_id,)
            and draft.source_representation_ids == (source_representation.representation_id,)
        )
        if not lineage_ok:
            violations.append("draft_lineage_mismatch")
        context_ok = (
            source.context_id
            == source_representation.context_id
            == candidate.context_id
            == candidate_representation.context_id
            == draft.context_id
        )
        if not context_ok:
            violations.append("context_mismatch")
        representation_lineage_ok = (
            source_representation.theorem_id == source.theorem_id
            and candidate_representation.theorem_id == candidate.theorem_id
        )
        if not representation_lineage_ok:
            violations.append("representation_lineage_mismatch")
        ancestry_ok = (
            candidate.parent_theorem_ids == (source.theorem_id,)
            and candidate.root_ancestry_ids == source.root_ancestry_ids
        )
        if not ancestry_ok:
            violations.append("candidate_ancestry_mismatch")
        source_text_ok = (
            source_representation.raw_proof_stripped == source.proof_stripped_declaration
        )
        candidate_text_ok = (
            candidate.proof_stripped_declaration
            == candidate_representation.raw_proof_stripped
            == draft.candidate_code
        )
        if not source_text_ok:
            violations.append("source_representation_text_mismatch")
        if not candidate_text_ok:
            violations.append("candidate_code_or_representation_mismatch")
        candidate_hashes_ok = candidate.statement_content_hash == sha256_hex(
            candidate.proof_stripped_declaration.encode("utf-8")
        ) and draft.candidate_code_hash == sha256_hex(draft.candidate_code.encode("utf-8"))
        if not candidate_hashes_ok:
            violations.append("statement_content_hash_mismatch")
        try:
            verify_variant_draft_id(draft)
        except TransformationIdentityError:
            violations.append("draft_identity_mismatch")
        expected_metadata_keys = {
            "failed_proof_search_consulted",
            "intention_is_not_label",
            "rule_config_hash",
            "semantic_negative_resolved",
        }
        draft_metadata_ok = (
            set(draft.metadata) == expected_metadata_keys
            and draft.metadata.get("failed_proof_search_consulted") is False
            and draft.metadata.get("intention_is_not_label") is True
            and draft.metadata.get("rule_config_hash") == self.rule_config_hash
            and draft.metadata.get("semantic_negative_resolved") is False
        )
        if not draft_metadata_ok:
            violations.append("draft_provenance_metadata_mismatch")
        if draft.candidate_code == source.proof_stripped_declaration:
            violations.append("candidate_unchanged")
        if draft.intended_relation != IntendedRelation.NEAR_MISS:
            violations.append("intended_relation_mismatch")
        if draft.intended_error_types != self.config.intended_error_types:
            violations.append("intended_error_types_mismatch")

        matching_site: HypothesisDropSite | None = None
        if source_representation.operator_tree is not None:
            try:
                matches = tuple(
                    site
                    for site in enumerate_independent_prop_hypotheses(
                        source.proof_stripped_declaration,
                        source_representation.operator_tree,
                    )
                    if _trace(
                        source.proof_stripped_declaration,
                        site,
                        rule_config_hash=self.rule_config_hash,
                    )
                    == draft.transformation_trace
                    and _inverse_trace(
                        draft.candidate_code,
                        site,
                        rule_config_hash=self.rule_config_hash,
                    )
                    == draft.inverse_trace
                )
                if len(matches) == 1:
                    matching_site = matches[0]
            except (N03DropHypothesisError, _UnsupportedSource):
                matching_site = None
        exact_trace_ok = matching_site is not None
        if not exact_trace_ok:
            violations.append("trace_not_from_current_dependency_analysis")

        try:
            forward_ok = (
                apply_hypothesis_trace(
                    source.proof_stripped_declaration,
                    draft.transformation_trace,
                    expected_rule_config_hash=self.rule_config_hash,
                )
                == draft.candidate_code
            )
        except N03DropHypothesisError:
            forward_ok = False
        if not forward_ok:
            violations.append("forward_trace_failed")
        try:
            roundtrip_ok = (
                draft.inverse_trace is not None
                and apply_hypothesis_trace(
                    draft.candidate_code,
                    draft.inverse_trace,
                    expected_rule_config_hash=self.rule_config_hash,
                )
                == source.proof_stripped_declaration
            )
        except N03DropHypothesisError:
            roundtrip_ok = False
        if not roundtrip_ok:
            violations.append("inverse_roundtrip_failed")

        expected_diff_ok = (
            matching_site is not None
            and draft.expected_structural_diff
            == _expected_structural_diff(
                matching_site,
                rule_config_hash=self.rule_config_hash,
            )
        )
        if not expected_diff_ok:
            violations.append("expected_structural_diff_mismatch")

        source_elaborates = (
            source.is_proposition
            and source.declaration_kind in self.config.supported_declaration_kinds
            and source.elaboration_status in _VALID_ELABORATION
        )
        candidate_elaborates = (
            candidate.is_proposition
            and candidate.declaration_kind in self.config.supported_declaration_kinds
            and candidate.elaboration_status in _VALID_ELABORATION
        )
        if not source_elaborates:
            violations.append("source_not_elaborated_proposition")
        if not candidate_elaborates:
            violations.append("candidate_not_elaborated_proposition")

        audited_views = (
            "signature_explicit",
            "semantic_atoms",
            "operator_tree",
        )
        source_views_ok = (
            all(source_representation.view_status[name] == ViewStatus.OK for name in audited_views)
            and source_representation.alpha_identity_fingerprint is not None
        )
        candidate_views_ok = (
            all(
                candidate_representation.view_status[name] == ViewStatus.OK
                for name in audited_views
            )
            and candidate_representation.alpha_identity_fingerprint is not None
        )
        if not source_views_ok:
            violations.append("source_required_view_failed")
        if not candidate_views_ok:
            violations.append("candidate_required_view_failed")

        expected_root: dict[str, object] | None = None
        source_root = (
            source_representation.operator_tree.get("root")
            if source_representation.operator_tree is not None
            else None
        )
        source_tree_integrity_ok = (
            isinstance(source_root, dict)
            and source_representation.operator_tree == build_operator_tree(source_root)
            and source_representation.semantic_atoms == semantic_atoms(source_root)
            and source_representation.alpha_identity_fingerprint
            == sha256_hex(alpha_canonical_bytes(source_root))
        )
        if not source_tree_integrity_ok:
            violations.append("source_derived_views_inconsistent")
        if matching_site is not None and isinstance(source_root, dict):
            try:
                expected_root = erase_outer_forall(
                    source_root,
                    matching_site.outer_index,
                )
            except N03DropHypothesisError:
                expected_root = None
        candidate_root = (
            candidate_representation.operator_tree.get("root")
            if candidate_representation.operator_tree is not None
            else None
        )
        expr_erasure_ok = (
            expected_root is not None
            and candidate_root == expected_root
            and candidate_representation.operator_tree == build_operator_tree(expected_root)
        )
        if not expr_erasure_ok:
            violations.append("candidate_expr_not_exact_unused_binder_erasure")

        expected_atoms = semantic_atoms(expected_root) if expected_root is not None else None
        atoms_ok = (
            expected_atoms is not None and candidate_representation.semantic_atoms == expected_atoms
        )
        if not atoms_ok:
            violations.append("candidate_semantic_atoms_mismatch")

        expected_alpha = (
            sha256_hex(alpha_canonical_bytes(expected_root)) if expected_root is not None else None
        )
        alpha_ok = (
            expected_alpha is not None
            and candidate_representation.alpha_identity_fingerprint == expected_alpha
            and source_representation.alpha_identity_fingerprint != expected_alpha
        )
        if not alpha_ok:
            violations.append("candidate_alpha_identity_mismatch")

        signature_changed = (
            source_representation.signature_explicit is not None
            and candidate_representation.signature_explicit is not None
            and source_representation.signature_explicit
            != candidate_representation.signature_explicit
        )
        if not signature_changed:
            violations.append("signature_explicit_not_changed")

        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("n03_independent_explicit_prop_hypothesis",),
                required_capabilities=(
                    "alpha_identity_fingerprint",
                    "binder_dependency_audit",
                    "exact_expr_erasure",
                    "lean_reelaboration",
                    "operator_tree",
                    "semantic_atoms",
                ),
                metadata={"rule_config_hash": self.rule_config_hash},
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status
                if candidate_elaborates and clean
                else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(QualityTier.PROVISIONAL if clean else QualityTier.UNKNOWN),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=(
                exact_trace_ok
                and forward_ok
                and expected_diff_ok
                and source_tree_integrity_ok
                and expr_erasure_ok
                and alpha_ok
                and signature_changed
            ),
            atom_mapping_ok=atoms_ok,
            inverse_or_roundtrip_ok=roundtrip_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "candidate_expr_exact_erasure": expr_erasure_ok,
                "context_equal": context_ok,
                "dependency_proof_revalidated": matching_site is not None,
                "failed_proof_search_consulted": False,
                "intention_is_not_label": True,
                "semantic_negative_resolved": False,
            },
        )


__all__ = [
    "HypothesisDropSite",
    "N03DropHypothesisConfig",
    "N03DropHypothesisError",
    "N03DropHypothesisRule",
    "OuterForallAnalysis",
    "OuterForallBinder",
    "analyze_outer_foralls",
    "apply_hypothesis_trace",
    "enumerate_independent_prop_hypotheses",
    "erase_outer_forall",
    "load_n03_drop_hypothesis_config",
]
