"""LF-034 N17: swap two allowlisted role-sensitive relation arguments.

The executable scope is intentionally narrow.  The theorem conclusion must be
one bare, binary, asymmetric relation between two distinct, explicit theorem
binders.  The surface operator and elaborated head must agree with a code-owned
allowlist, the two binder domains must be identical after outer-scope
normalization, and the elaborated relation operands must resolve to the same
surface binders.  Symmetric heads and arbitrary subterm swaps are excluded.

N17 emits provisional D0 structural evidence only.  It does not infer a
semantic negative, run proof search, promote a pair, or make it trainable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from pydantic import JsonValue

from leanfaith.config.hashing import hash_canonical
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
from leanfaith.transforms.positives.v2_e0 import V2E0RuleError, _signature_bounds
from leanfaith.transforms.protocol import (
    build_transformation_audit,
    build_variant_draft,
    verify_variant_draft_id,
)

_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_REQUIRED_CAPABILITIES = (
    "allowlisted_asymmetric_head",
    "exact_argument_swap_certificate",
    "exact_inverse_replay",
    "same_context_reelaboration",
    "same_typed_independent_binders",
)

# The tuple documents the only explicit argument positions N17 may swap.  A
# negative index is relative to the complete elaborated application spine.
_ROLE_HEAD_ALLOWLIST: dict[str, tuple[str, tuple[int, int]]] = {
    "<": ("LT.lt", (-2, -1)),
    "<=": ("LE.le", (-2, -1)),
    "≤": ("LE.le", (-2, -1)),
    ">": ("GT.gt", (-2, -1)),
    ">=": ("GE.ge", (-2, -1)),
    "≥": ("GE.ge", (-2, -1)),
    "∣": ("Dvd.dvd", (-2, -1)),  # noqa: RUF001 -- Lean's divides notation
    "⊆": ("Set.Subset", (-2, -1)),
}
_BINDER_INFO = {
    BinderKind.EXPLICIT: "default",
    BinderKind.IMPLICIT: "implicit",
    BinderKind.STRICT_IMPLICIT: "strictImplicit",
    BinderKind.INSTANCE: "instImplicit",
}


class N17RoleArgumentError(ValueError):
    """An N17 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class _ElaboratedBinder:
    outer_index: int
    binder_info: str
    domain: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _SurfaceBinder:
    name: str
    group: TypedBinder
    elaborated: _ElaboratedBinder


@dataclass(frozen=True, slots=True)
class RoleArgumentSite:
    left_start: int
    left_end: int
    right_start: int
    right_end: int
    left_name: str
    right_name: str
    surface_operator: str
    elaborated_head: str
    left_outer_index: int
    right_outer_index: int
    header_binder_count: int
    normalized_domain_hash: str
    source_root_hash: str
    expected_candidate_root_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "left_start": self.left_start,
                "left_end": self.left_end,
                "right_start": self.right_start,
                "right_end": self.right_end,
                "left_name": self.left_name,
                "right_name": self.right_name,
                "surface_operator": self.surface_operator,
                "elaborated_head": self.elaborated_head,
                "left_outer_index": self.left_outer_index,
                "right_outer_index": self.right_outer_index,
                "header_binder_count": self.header_binder_count,
                "normalized_domain_hash": self.normalized_domain_hash,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class RoleArgumentSwapCertificate:
    header_binder_count: int
    elaborated_head: str
    source_root_hash: str
    candidate_root_hash: str
    source_left_hash: str
    source_right_hash: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "elaborated_head": self.elaborated_head,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "source_left_hash": self.source_left_hash,
            "source_right_hash": self.source_right_hash,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise N17RoleArgumentError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _outer_foralls(
    root: dict[str, Any],
) -> tuple[tuple[_ElaboratedBinder, ...], dict[str, Any]]:
    binders: list[_ElaboratedBinder] = []
    current = root
    while current.get("k") == "forall":
        domain = current.get("dom")
        binder_info = current.get("bi")
        body = current.get("body")
        if (
            not isinstance(domain, dict)
            or not isinstance(binder_info, str)
            or not isinstance(body, dict)
        ):
            raise N17RoleArgumentError("malformed_outer_forall")
        binders.append(
            _ElaboratedBinder(
                outer_index=len(binders),
                binder_info=binder_info,
                domain=domain,
            )
        )
        current = body
    return tuple(binders), current


def _application_spine(
    node: dict[str, Any],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    arguments: list[dict[str, Any]] = []
    current = node
    while current.get("k") == "app":
        function = current.get("fn")
        argument = current.get("arg")
        if not isinstance(function, dict) or not isinstance(argument, dict):
            raise N17RoleArgumentError("malformed_application")
        arguments.append(argument)
        current = function
    arguments.reverse()
    return current, tuple(arguments)


def _build_application(
    head: dict[str, Any],
    arguments: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    result = head
    for argument in arguments:
        result = {"k": "app", "fn": result, "arg": argument}
    return result


def _replace_after_header(
    root: dict[str, Any],
    header_binder_count: int,
    elaborated_head: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if header_binder_count < 0:
        raise N17RoleArgumentError("negative_header_binder_count")
    if header_binder_count:
        if root.get("k") != "forall":
            raise N17RoleArgumentError("header_binder_expr_mismatch")
        body = root.get("body")
        if not isinstance(body, dict):
            raise N17RoleArgumentError("malformed_header_forall")
        candidate_body, left, right = _replace_after_header(
            body,
            header_binder_count - 1,
            elaborated_head,
        )
        return {**root, "body": candidate_body}, left, right

    head, arguments = _application_spine(root)
    if head.get("k") != "const" or head.get("n") != elaborated_head:
        raise N17RoleArgumentError("relation_head_not_allowlisted")
    if len(arguments) < 2:
        raise N17RoleArgumentError("relation_missing_explicit_operands")
    left, right = arguments[-2:]
    if left == right:
        raise N17RoleArgumentError("relation_operands_identical")
    swapped = (*arguments[:-2], right, left)
    return _build_application(head, swapped), left, right


def build_role_argument_swap_root(
    source_root: dict[str, Any],
    header_binder_count: int,
    elaborated_head: str,
) -> dict[str, Any]:
    candidate, _left, _right = _replace_after_header(
        source_root,
        header_binder_count,
        elaborated_head,
    )
    return candidate


def certify_role_argument_swap(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
    elaborated_head: str,
) -> RoleArgumentSwapCertificate:
    expected, left, right = _replace_after_header(
        source_root,
        header_binder_count,
        elaborated_head,
    )
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise N17RoleArgumentError("candidate_not_exact_role_argument_swap")
    return RoleArgumentSwapCertificate(
        header_binder_count=header_binder_count,
        elaborated_head=elaborated_head,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        source_left_hash=hash_canonical(left),
        source_right_hash=hash_canonical(right),
    )


def _normalize_domain_refs(node: object, *, outer_index: int, cutoff: int = 0) -> object:
    """Normalize de-Bruijn references in a binder domain to outer positions."""

    if isinstance(node, list):
        return [
            _normalize_domain_refs(item, outer_index=outer_index, cutoff=cutoff) for item in node
        ]
    if not isinstance(node, dict):
        return node
    kind = node.get("k")
    if kind == "bvar":
        raw_index = node.get("i")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            raise N17RoleArgumentError("malformed_domain_bvar")
        if raw_index < cutoff:
            return {"k": "local_bvar", "i": raw_index}
        referenced_outer = outer_index - 1 - (raw_index - cutoff)
        if referenced_outer < 0:
            raise N17RoleArgumentError("domain_bvar_out_of_scope")
        return {"k": "outer_binder_ref", "outer_index": referenced_outer}
    result: dict[str, object] = {}
    for key, value in node.items():
        child_cutoff = cutoff + (1 if kind in {"forall", "lam", "let"} and key == "body" else 0)
        result[key] = _normalize_domain_refs(
            value,
            outer_index=outer_index,
            cutoff=child_cutoff,
        )
    return result


def _surface_binders(
    source: str,
    elaborated: tuple[_ElaboratedBinder, ...],
) -> tuple[_SurfaceBinder, ...]:
    groups = parse_typed_binders(source)
    cursor = 0
    aligned: list[_SurfaceBinder] = []
    for group in groups:
        expected_info = _BINDER_INFO[group.kind]
        for name in group.names:
            while cursor < len(elaborated) and elaborated[cursor].binder_info != expected_info:
                # Anonymous `[Class T]` instance binders have no typed surface
                # name in parse_typed_binders, but do elaborate to instImplicit.
                if elaborated[cursor].binder_info != "instImplicit":
                    raise N17RoleArgumentError("surface_elaborated_binder_alignment_failed")
                cursor += 1
            if cursor >= len(elaborated):
                raise N17RoleArgumentError("surface_binder_missing_from_elaboration")
            aligned.append(
                _SurfaceBinder(
                    name=name,
                    group=group,
                    elaborated=elaborated[cursor],
                )
            )
            cursor += 1
    if any(item.binder_info != "instImplicit" for item in elaborated[cursor:]):
        raise N17RoleArgumentError("unmatched_elaborated_binder")
    return tuple(aligned)


def _safe_surface_name(name: str) -> bool:
    return (
        bool(name)
        and not name.startswith("«")
        and not any(character.isspace() for character in name)
    )


def enumerate_n17_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[RoleArgumentSite, ...]:
    """Return the unique allowlisted root relation-argument swap site."""

    try:
        mask, conclusion_start, conclusion_end = _signature_bounds(source)
        root = cast(dict[str, Any], operator_tree_view["root"])
        elaborated_binders, conclusion = _outer_foralls(root)
        surface = _surface_binders(source, elaborated_binders)
        eligible = tuple(
            item
            for item in surface
            if item.group.kind == BinderKind.EXPLICIT
            and not item.group.has_comment
            and _safe_surface_name(item.name)
            and item.elaborated.binder_info == "default"
        )
        by_name = {item.name: item for item in eligible}
        if len(by_name) != len(eligible) or len(by_name) < 2:
            raise N17RoleArgumentError("insufficient_unique_explicit_binders")
        alternatives = "|".join(
            sorted((re.escape(name) for name in by_name), key=len, reverse=True)
        )
        operators = "|".join(
            sorted(
                (re.escape(operator) for operator in _ROLE_HEAD_ALLOWLIST),
                key=len,
                reverse=True,
            )
        )
        segment = mask[conclusion_start:conclusion_end]
        match = re.fullmatch(
            rf"\s*(?P<left>{alternatives})\s*(?P<operator>{operators})\s*"
            rf"(?P<right>{alternatives})\s*",
            segment,
        )
        if match is None:
            raise N17RoleArgumentError("conclusion_not_bare_allowlisted_relation")
        left_name = match.group("left")
        right_name = match.group("right")
        if left_name == right_name:
            raise N17RoleArgumentError("surface_operands_identical")
        left_binder = by_name[left_name]
        right_binder = by_name[right_name]
        operator = match.group("operator")
        expected_head, positions = _ROLE_HEAD_ALLOWLIST[operator]
        if positions != (-2, -1):
            raise N17RoleArgumentError("unsupported_allowlist_positions")

        left_domain = _normalize_domain_refs(
            left_binder.elaborated.domain,
            outer_index=left_binder.elaborated.outer_index,
        )
        right_domain = _normalize_domain_refs(
            right_binder.elaborated.domain,
            outer_index=right_binder.elaborated.outer_index,
        )
        if alpha_canonical_bytes(cast(dict[str, Any], left_domain)) != alpha_canonical_bytes(
            cast(dict[str, Any], right_domain)
        ):
            raise N17RoleArgumentError("operand_binder_domains_differ")

        head, arguments = _application_spine(conclusion)
        if head.get("k") != "const" or head.get("n") != expected_head or len(arguments) < 2:
            raise N17RoleArgumentError("surface_elaborated_head_mismatch")
        expected_left_index = len(elaborated_binders) - 1 - left_binder.elaborated.outer_index
        expected_right_index = len(elaborated_binders) - 1 - right_binder.elaborated.outer_index
        if arguments[-2] != {"k": "bvar", "i": expected_left_index}:
            raise N17RoleArgumentError("left_operand_binder_mismatch")
        if arguments[-1] != {"k": "bvar", "i": expected_right_index}:
            raise N17RoleArgumentError("right_operand_binder_mismatch")

        expected_candidate = build_role_argument_swap_root(
            root,
            len(elaborated_binders),
            expected_head,
        )
        left_start = conclusion_start + match.start("left")
        right_start = conclusion_start + match.start("right")
        return (
            RoleArgumentSite(
                left_start=left_start,
                left_end=left_start + len(left_name),
                right_start=right_start,
                right_end=right_start + len(right_name),
                left_name=left_name,
                right_name=right_name,
                surface_operator=operator,
                elaborated_head=expected_head,
                left_outer_index=left_binder.elaborated.outer_index,
                right_outer_index=right_binder.elaborated.outer_index,
                header_binder_count=len(elaborated_binders),
                normalized_domain_hash=hash_canonical(left_domain),
                source_root_hash=hash_canonical(root),
                expected_candidate_root_hash=hash_canonical(expected_candidate),
            ),
        )
    except (
        BinderParseError,
        KeyError,
        N17RoleArgumentError,
        TypeError,
        V2E0RuleError,
    ):
        return ()


def _trace(
    site: RoleArgumentSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    left_text = site.left_name
    right_text = site.right_name
    right_start = site.right_start
    if inverse:
        right_start += len(site.right_name) - len(site.left_name)
        left_text, right_text = right_text, left_text
    return (
        {
            "operation": "swap_exact_spans",
            "n17_operation": "inverse_role_argument_swap" if inverse else "role_argument_swap",
            "left_start": site.left_start,
            "left_end": site.left_start + len(left_text),
            "left_text": left_text,
            "right_start": right_start,
            "right_end": right_start + len(right_text),
            "right_text": right_text,
            "surface_operator": site.surface_operator,
            "elaborated_head": site.elaborated_head,
            "left_outer_index": site.left_outer_index,
            "right_outer_index": site.right_outer_index,
            "header_binder_count": site.header_binder_count,
            "normalized_domain_hash": site.normalized_domain_hash,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_n17_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    """Apply one exact two-span swap without global search/replacement."""

    if len(trace) != 1 or trace[0].get("operation") != "swap_exact_spans":
        raise N17RoleArgumentError("expected_one_swap_trace")
    step = trace[0]
    raw_spans = tuple(
        step.get(key) for key in ("left_start", "left_end", "right_start", "right_end")
    )
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in raw_spans):
        raise N17RoleArgumentError("invalid_trace_span")
    left_start, left_end, right_start, right_end = cast(tuple[int, int, int, int], raw_spans)
    left_text = step.get("left_text")
    right_text = step.get("right_text")
    if not isinstance(left_text, str) or not isinstance(right_text, str):
        raise N17RoleArgumentError("invalid_trace_text")
    if not 0 <= left_start < left_end <= right_start < right_end <= len(source):
        raise N17RoleArgumentError("invalid_or_overlapping_trace_spans")
    if source[left_start:left_end] != left_text or source[right_start:right_end] != right_text:
        raise N17RoleArgumentError("trace_expected_text_mismatch")
    return (
        source[:left_start]
        + right_text
        + source[left_end:right_start]
        + left_text
        + source[right_end:]
    )


def _expected_structural_diff(site: RoleArgumentSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "allowlisted_role_argument_swap",
        "evidence_class": "D0",
        "surface_operator": site.surface_operator,
        "elaborated_head": site.elaborated_head,
        "left_outer_index": site.left_outer_index,
        "right_outer_index": site.right_outer_index,
        "header_binder_count": site.header_binder_count,
        "normalized_domain_hash": site.normalized_domain_hash,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
    }


class N17RoleArgumentRule:
    """One exact allowlisted D0 role swap; never a resolved negative."""

    polarity = Polarity.NEGATIVE
    rule_id = "n17_role_sensitive_arguments"
    family_id = "n17_role_sensitive_arguments"
    implementation_key = "n17_role_sensitive_arguments"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise N17RoleArgumentError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise N17RoleArgumentError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "n17_role_argument_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "allowlist": _ROLE_HEAD_ALLOWLIST,
                "requirements": _REQUIRED_CAPABILITIES,
            }
        )

    def assess(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability:
        reasons: list[str] = []
        if not theorem.is_proposition:
            reasons.append("source_not_proposition")
        if theorem.elaboration_status not in _VALID_ELABORATION:
            reasons.append("source_does_not_elaborate")
        if representation.theorem_id != theorem.theorem_id:
            reasons.append("source_representation_lineage_mismatch")
        if representation.context_id != theorem.context_id:
            reasons.append("source_representation_context_mismatch")
        if representation.raw_proof_stripped != theorem.proof_stripped_declaration:
            reasons.append("source_representation_text_mismatch")
        for view in ("operator_tree", "semantic_atoms", "signature_explicit"):
            if representation.view_status[view] != ViewStatus.OK:
                reasons.append(f"source_{view}_missing")
        sites = (
            enumerate_n17_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_role_argument_site")
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=_REQUIRED_CAPABILITIES,
            )
        site = sites[0]
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=(f"role_argument_swap:{site.stable_key}",),
            required_capabilities=_REQUIRED_CAPABILITIES,
            metadata={"eligible_site_count": 1},
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        if not self.assess(theorem, representation).applicable:
            return ()
        (site,) = enumerate_n17_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_n17_trace(theorem.proof_stripped_declaration, forward)
        if apply_n17_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N17RoleArgumentError("internal_inverse_replay_failure")
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
                intended_error_types=("E12", "E30"),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "generation_intention_only": True,
                    "near_miss": True,
                    "resolved_semantic_label": False,
                    "structural_direction": "swap_role_sensitive_relation_arguments",
                    "training_eligible": False,
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
        try:
            verify_variant_draft_id(draft)
        except ValueError:
            violations.append("draft_id_mismatch")
        if not (
            draft.rule_id == self.rule_id
            and draft.family_id == self.family_id
            and draft.rule_version == self.rule_version
            and draft.generation_config_hash == self.generation_config_hash
            and draft.source_theorem_ids == (source.theorem_id,)
            and draft.source_representation_ids == (source_representation.representation_id,)
        ):
            violations.append("draft_lineage_mismatch")
        if not (
            source.context_id
            == source_representation.context_id
            == candidate.context_id
            == candidate_representation.context_id
            == draft.context_id
        ):
            violations.append("context_mismatch")
        if not (
            source_representation.theorem_id == source.theorem_id
            and candidate_representation.theorem_id == candidate.theorem_id
        ):
            violations.append("representation_lineage_mismatch")
        if candidate.proof_stripped_declaration != draft.candidate_code:
            violations.append("candidate_code_mismatch")
        if source.elaboration_status not in _VALID_ELABORATION:
            violations.append("source_does_not_elaborate")
        if candidate.elaboration_status not in _VALID_ELABORATION:
            violations.append("candidate_does_not_elaborate")
        for side, record in (
            ("source", source_representation),
            ("candidate", candidate_representation),
        ):
            for view in ("operator_tree", "semantic_atoms", "signature_explicit"):
                if record.view_status[view] != ViewStatus.OK:
                    violations.append(f"{side}_{view}_missing")

        matching: list[RoleArgumentSite] = []
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_n17_sites(
                source.proof_stripped_declaration,
                cast(dict[str, Any], source_representation.operator_tree),
            )
            matching = [
                site
                for site in sites
                if _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
                == draft.transformation_trace
                and _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
                == draft.inverse_trace
                and _expected_structural_diff(site) == draft.expected_structural_diff
            ]
            forward_ok = (
                apply_n17_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_n17_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (N17RoleArgumentError, TypeError, ValueError):
            pass
        if len(matching) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: RoleArgumentSwapCertificate | None = None
        if len(matching) == 1:
            try:
                certificate = certify_role_argument_swap(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    matching[0].header_binder_count,
                    matching[0].elaborated_head,
                )
            except N17RoleArgumentError as exc:
                violations.append(str(exc))
        atoms_present = (
            source_representation.semantic_atoms is not None
            and candidate_representation.semantic_atoms is not None
        )
        if not atoms_present:
            violations.append("semantic_atoms_missing")
        fingerprints_differ = (
            source_representation.alpha_identity_fingerprint is not None
            and candidate_representation.alpha_identity_fingerprint is not None
            and source_representation.alpha_identity_fingerprint
            != candidate_representation.alpha_identity_fingerprint
        )
        if not fingerprints_differ:
            violations.append("alpha_fingerprint_delta_missing")

        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("n17_exact_allowlisted_role_argument_swap",),
                required_capabilities=_REQUIRED_CAPABILITIES,
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status if clean else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(QualityTier.PROVISIONAL if clean else QualityTier.UNKNOWN),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=certificate is not None,
            atom_mapping_ok=atoms_present,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "evidence_class": "D0",
                "failed_proof_search_used": False,
                "resolved_semantic_label": False,
                "role_argument_swap_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "semantic_atoms_changed": (
                    source_representation.semantic_atoms != candidate_representation.semantic_atoms
                    if atoms_present
                    else None
                ),
                "structural_direction": "swap_role_sensitive_relation_arguments",
                "training_eligible": False,
            },
        )


__all__ = [
    "N17RoleArgumentError",
    "N17RoleArgumentRule",
    "RoleArgumentSite",
    "RoleArgumentSwapCertificate",
    "apply_n17_trace",
    "build_role_argument_swap_root",
    "certify_role_argument_swap",
    "enumerate_n17_sites",
]
