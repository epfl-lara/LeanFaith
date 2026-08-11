"""N18 v1.0: flip one exact root equality polarity (``=`` / ``≠`` glyph).

The executable scope is intentionally narrow.  After the declaration's
surface binders, the conclusion must expose exactly one root ``=`` or Unicode
``≠`` glyph and the elaborated conclusion at that same depth must be exactly
``Eq alpha lhs rhs`` or ``Ne alpha lhs rhs``.  The operands must be
structurally distinct.  Generation changes only the operator glyph.

The same-context Lean audit reconstructs the exact expected Expr tree and
semantic-atom sequence.  A clean result is still only provisional D0 evidence:
it receives no resolved semantic label, promotion, or training eligibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import JsonValue

from leanfaith.config.hashing import hash_canonical
from leanfaith.representations import alpha_identity_fingerprint
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
from leanfaith.transforms.positives.p02_binders import BinderParseError, parse_typed_binders
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
    "distinct_equality_operands",
    "exact_equality_polarity_certificate",
    "exact_inverse_replay",
    "exact_semantic_atom_delta",
    "same_context_reelaboration",
    "single_surface_root_equality_glyph",
)
_OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
_CLOSE_TO_OPEN = {value: key for key, value in _OPEN_TO_CLOSE.items()}
_SCOPED_TERM = re.compile(
    r"(?<![\w'])(?:by|calc|do|fun|if|let|match|show|syntax|macro|scoped)(?![\w'])"
)
_MACRO_MARKERS = ("`(", "`[", "`{", "$(", "term%", "tactic%", "open scoped")
EqualityPolarityDirection = Literal["eq_to_ne", "ne_to_eq"]


class N18EqualityPolarityError(ValueError):
    """An N18 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class EqualityPolaritySite:
    operator_start: int
    operator_end: int
    source_operator: Literal["=", "≠"]
    candidate_operator: Literal["=", "≠"]
    left_text: str
    right_text: str
    header_binder_count: int
    source_root_hash: str
    expected_candidate_root_hash: str
    equality_type_hash: str
    left_operand_hash: str
    right_operand_hash: str
    source_atoms_hash: str
    expected_candidate_atoms_hash: str
    direction: EqualityPolarityDirection

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "operator_start": self.operator_start,
                "operator_end": self.operator_end,
                "source_operator": self.source_operator,
                "candidate_operator": self.candidate_operator,
                "left_text_hash": hash_canonical({"text": self.left_text}),
                "right_text_hash": hash_canonical({"text": self.right_text}),
                "header_binder_count": self.header_binder_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
                "equality_type_hash": self.equality_type_hash,
                "left_operand_hash": self.left_operand_hash,
                "right_operand_hash": self.right_operand_hash,
                "source_atoms_hash": self.source_atoms_hash,
                "expected_candidate_atoms_hash": self.expected_candidate_atoms_hash,
                "direction": self.direction,
            }
        )


@dataclass(frozen=True, slots=True)
class EqualityPolarityCertificate:
    header_binder_count: int
    source_root_hash: str
    candidate_root_hash: str
    equality_type_hash: str
    left_operand_hash: str
    right_operand_hash: str
    source_atoms_hash: str
    candidate_atoms_hash: str
    direction: EqualityPolarityDirection

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "equality_type_hash": self.equality_type_hash,
            "left_operand_hash": self.left_operand_hash,
            "right_operand_hash": self.right_operand_hash,
            "source_atoms_hash": self.source_atoms_hash,
            "candidate_atoms_hash": self.candidate_atoms_hash,
            "direction": self.direction,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise N18EqualityPolarityError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _application_spine(
    node: dict[str, Any],
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    arguments: list[dict[str, Any]] = []
    current = node
    while current.get("k") == "app":
        function = current.get("fn")
        argument = current.get("arg")
        if not isinstance(function, dict) or not isinstance(argument, dict):
            raise N18EqualityPolarityError("malformed_application")
        arguments.append(argument)
        current = function
    arguments.reverse()
    return current, tuple(arguments)


def _relation_parts(
    node: dict[str, Any],
) -> tuple[
    Literal["Eq", "Ne"],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    head, arguments = _application_spine(node)
    head_name = head.get("n")
    if head.get("k") != "const" or head_name not in {"Eq", "Ne"} or len(arguments) != 3:
        raise N18EqualityPolarityError("expected_exact_eq_or_ne")
    equality_type, left, right = arguments
    if alpha_canonical_bytes(left) == alpha_canonical_bytes(right):
        raise N18EqualityPolarityError("equality_operands_identical")
    return cast(Literal["Eq", "Ne"], head_name), head, equality_type, left, right


def _build_application(
    head: dict[str, Any], arguments: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    result = head
    for argument in arguments:
        result = {"k": "app", "fn": result, "arg": argument}
    return result


def _atom_delta_is_exact(
    source_atoms: tuple[str, ...],
    candidate_atoms: tuple[str, ...],
    direction: EqualityPolarityDirection,
) -> bool:
    if len(source_atoms) != len(candidate_atoms):
        return False
    expected_source = "const:Eq" if direction == "eq_to_ne" else "const:Ne"
    expected_candidate = "const:Ne" if direction == "eq_to_ne" else "const:Eq"
    differences = [
        index
        for index, (source, candidate) in enumerate(zip(source_atoms, candidate_atoms, strict=True))
        if source != candidate
    ]
    return (
        len(differences) == 1
        and source_atoms[differences[0]] == expected_source
        and candidate_atoms[differences[0]] == expected_candidate
    )


def _toggle_target(
    target: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    EqualityPolarityDirection,
]:
    head_name, head, equality_type, left, right = _relation_parts(target)
    direction: EqualityPolarityDirection = "eq_to_ne" if head_name == "Eq" else "ne_to_eq"
    candidate_head = {**head, "n": "Ne" if head_name == "Eq" else "Eq"}
    candidate = _build_application(candidate_head, (equality_type, left, right))
    if not _atom_delta_is_exact(semantic_atoms(target), semantic_atoms(candidate), direction):
        raise N18EqualityPolarityError("unexpected_semantic_atom_delta")
    return candidate, equality_type, left, right, direction


def _replace_after_header(
    root: dict[str, Any],
    header_binder_count: int,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    EqualityPolarityDirection,
]:
    if header_binder_count < 0:
        raise N18EqualityPolarityError("negative_header_binder_count")
    if header_binder_count:
        if root.get("k") != "forall":
            raise N18EqualityPolarityError("header_binder_expr_mismatch")
        body = root.get("body")
        if not isinstance(body, dict):
            raise N18EqualityPolarityError("malformed_header_forall")
        candidate_body, equality_type, left, right, direction = _replace_after_header(
            body, header_binder_count - 1
        )
        return {**root, "body": candidate_body}, equality_type, left, right, direction
    return _toggle_target(root)


def build_equality_polarity_root(
    source_root: dict[str, Any], header_binder_count: int
) -> dict[str, Any]:
    candidate, _equality_type, _left, _right, _direction = _replace_after_header(
        source_root, header_binder_count
    )
    return candidate


def certify_equality_polarity(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
) -> EqualityPolarityCertificate:
    expected, equality_type, left, right, direction = _replace_after_header(
        source_root, header_binder_count
    )
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise N18EqualityPolarityError("candidate_not_exact_equality_polarity_flip")
    source_atoms = semantic_atoms(source_root)
    candidate_atoms = semantic_atoms(candidate_root)
    if not _atom_delta_is_exact(source_atoms, candidate_atoms, direction):
        raise N18EqualityPolarityError("unexpected_semantic_atom_delta")
    return EqualityPolarityCertificate(
        header_binder_count=header_binder_count,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        equality_type_hash=hash_canonical(equality_type),
        left_operand_hash=hash_canonical(left),
        right_operand_hash=hash_canonical(right),
        source_atoms_hash=hash_canonical(source_atoms),
        candidate_atoms_hash=hash_canonical(candidate_atoms),
        direction=direction,
    )


def _header_binder_count(source: str) -> int:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise N18EqualityPolarityError(str(exc)) from exc
    return sum(len(group.names) for group in groups)


def _matching_delimiter(mask: str, start: int, end: int) -> int:
    opener = mask[start]
    if opener not in _OPEN_TO_CLOSE:
        raise N18EqualityPolarityError("expected_opening_delimiter")
    stack = [opener]
    for index in range(start + 1, end):
        character = mask[index]
        if character in _OPEN_TO_CLOSE:
            stack.append(character)
        elif character in _CLOSE_TO_OPEN:
            if not stack or _OPEN_TO_CLOSE[stack.pop()] != character:
                raise N18EqualityPolarityError("mismatched_equality_delimiter")
            if not stack:
                return index
    raise N18EqualityPolarityError("unterminated_equality_delimiter")


def _strip_outer(mask: str, start: int, end: int) -> tuple[int, int]:
    while start < end and mask[start].isspace():
        start += 1
    while end > start and mask[end - 1].isspace():
        end -= 1
    while start < end and mask[start] == "(":
        close = _matching_delimiter(mask, start, end)
        if close != end - 1:
            break
        start += 1
        end = close
        while start < end and mask[start].isspace():
            start += 1
        while end > start and mask[end - 1].isspace():
            end -= 1
    return start, end


def _trim(source: str, start: int, end: int) -> tuple[int, int, str]:
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    text = source[start:end]
    if not text:
        raise N18EqualityPolarityError("empty_equality_operand")
    return start, end, text


def _reject_unsafe_surface(source: str, conclusion_start: int, conclusion_end: int) -> None:
    signature = source[:conclusion_end]
    conclusion = source[conclusion_start:conclusion_end]
    if any(marker in signature for marker in ("--", "/-", "-/", '"')):
        raise N18EqualityPolarityError("comment_or_quoted_surface_excluded")
    if _SCOPED_TERM.search(conclusion) is not None:
        raise N18EqualityPolarityError("scoped_term_surface_excluded")
    if any(operator in conclusion for operator in ("→", "↔", "∀", "∃", "λ")):
        raise N18EqualityPolarityError("scoped_operator_surface_excluded")
    if any(marker in conclusion for marker in _MACRO_MARKERS):
        raise N18EqualityPolarityError("macro_surface_excluded")


def _top_level_polarity_operators(
    mask: str, start: int, end: int
) -> tuple[tuple[int, Literal["=", "≠"]], ...]:
    stack: list[str] = []
    positions: list[tuple[int, Literal["=", "≠"]]] = []
    for index in range(start, end):
        character = mask[index]
        if character in _OPEN_TO_CLOSE:
            stack.append(character)
        elif character in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[character]:
                raise N18EqualityPolarityError("mismatched_equality_delimiter")
            stack.pop()
        elif not stack and character == "≠":
            positions.append((index, "≠"))
        elif not stack and character == "=":
            previous = mask[index - 1] if index > start else ""
            following = mask[index + 1] if index + 1 < end else ""
            if previous not in {"!", ":", "<", ">", "="} and following not in {"=", ">"}:
                positions.append((index, "="))
    if stack:
        raise N18EqualityPolarityError("unterminated_equality_delimiter")
    return tuple(positions)


def _all_polarity_operators(
    mask: str, start: int, end: int
) -> tuple[tuple[int, Literal["=", "≠"]], ...]:
    positions: list[tuple[int, Literal["=", "≠"]]] = []
    for index in range(start, end):
        character = mask[index]
        if character == "≠":
            positions.append((index, "≠"))
        elif character == "=":
            previous = mask[index - 1] if index > start else ""
            following = mask[index + 1] if index + 1 < end else ""
            if previous not in {"!", ":", "<", ">", "="} and following not in {"=", ">"}:
                positions.append((index, "="))
    return tuple(positions)


def enumerate_n18_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[EqualityPolaritySite, ...]:
    """Return one exact root equality-polarity site, or no site on ambiguity."""

    try:
        mask, conclusion_start, conclusion_end = _signature_bounds(source)
        _reject_unsafe_surface(source, conclusion_start, conclusion_end)
        root_start, root_end = _strip_outer(mask, conclusion_start, conclusion_end)
        positions = _top_level_polarity_operators(mask, root_start, root_end)
        all_positions = _all_polarity_operators(mask, root_start, root_end)
        if len(positions) != 1 or all_positions != positions:
            raise N18EqualityPolarityError("root_equality_polarity_not_unique")
        operator_start, source_operator = positions[0]
        _left_start, _left_end, left_text = _trim(source, root_start, operator_start)
        _right_start, _right_end, right_text = _trim(source, operator_start + 1, root_end)
        if left_text == right_text:
            raise N18EqualityPolarityError("surface_equality_operands_identical")
        root = operator_tree_view.get("root")
        if not isinstance(root, dict):
            raise N18EqualityPolarityError("operator_tree_missing_root")
        header_count = _header_binder_count(source)
        expected, equality_type, left, right, direction = _replace_after_header(root, header_count)
        expected_source_operator: Literal["=", "≠"] = "=" if direction == "eq_to_ne" else "≠"
        if source_operator != expected_source_operator:
            raise N18EqualityPolarityError("surface_expr_polarity_mismatch")
        source_atoms = semantic_atoms(root)
        candidate_atoms = semantic_atoms(expected)
        return (
            EqualityPolaritySite(
                operator_start=operator_start,
                operator_end=operator_start + 1,
                source_operator=source_operator,
                candidate_operator="≠" if source_operator == "=" else "=",
                left_text=left_text,
                right_text=right_text,
                header_binder_count=header_count,
                source_root_hash=hash_canonical(root),
                expected_candidate_root_hash=hash_canonical(expected),
                equality_type_hash=hash_canonical(equality_type),
                left_operand_hash=hash_canonical(left),
                right_operand_hash=hash_canonical(right),
                source_atoms_hash=hash_canonical(source_atoms),
                expected_candidate_atoms_hash=hash_canonical(candidate_atoms),
                direction=direction,
            ),
        )
    except (
        BinderParseError,
        N18EqualityPolarityError,
        TypeError,
        V2E0RuleError,
    ):
        return ()


def _trace(
    site: EqualityPolaritySite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    expected = site.candidate_operator if inverse else site.source_operator
    replacement = site.source_operator if inverse else site.candidate_operator
    return (
        {
            "operation": "replace_exact_span",
            "n18_operation": (
                "inverse_root_equality_polarity" if inverse else "root_equality_polarity"
            ),
            "start": site.operator_start,
            "end": site.operator_start + 1,
            "expected_text": expected,
            "replacement_text": replacement,
            "header_binder_count": site.header_binder_count,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "equality_type_hash": site.equality_type_hash,
            "left_operand_hash": site.left_operand_hash,
            "right_operand_hash": site.right_operand_hash,
            "source_atoms_hash": site.source_atoms_hash,
            "expected_candidate_atoms_hash": site.expected_candidate_atoms_hash,
            "direction": site.direction,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_n18_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "replace_exact_span":
        raise N18EqualityPolarityError("expected_one_replace_trace")
    step = trace[0]
    start = step.get("start")
    end = step.get("end")
    expected = step.get("expected_text")
    replacement = step.get("replacement_text")
    if not (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and isinstance(expected, str)
        and expected in {"=", "≠"}
        and isinstance(replacement, str)
        and replacement in {"=", "≠"}
        and expected != replacement
        and end == start + 1
        and 0 <= start < end <= len(source)
    ):
        raise N18EqualityPolarityError("malformed_trace")
    if source[start:end] != expected:
        raise N18EqualityPolarityError("trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(site: EqualityPolaritySite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "root_equality_polarity",
        "evidence_class": "D0",
        "direction": site.direction,
        "header_binder_count": site.header_binder_count,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
        "equality_type_hash": site.equality_type_hash,
        "left_operand_hash": site.left_operand_hash,
        "right_operand_hash": site.right_operand_hash,
        "source_atoms_hash": site.source_atoms_hash,
        "expected_candidate_atoms_hash": site.expected_candidate_atoms_hash,
    }


def _expected_draft_metadata(site: EqualityPolaritySite) -> dict[str, str | bool]:
    return {
        "generation_intention_only": True,
        "near_miss": True,
        "resolved_semantic_label": False,
        "structural_direction": site.direction,
        "training_eligible": False,
    }


class N18EqualityPolarityRule:
    """One exact D0 root equality-polarity edit; never a semantic label."""

    polarity = Polarity.NEGATIVE
    rule_id = "n18_root_equality_polarity"
    family_id = "n18_root_equality_polarity"
    implementation_key = "n18_root_equality_polarity"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise N18EqualityPolarityError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise N18EqualityPolarityError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "n18_equality_polarity_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
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
            enumerate_n18_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_root_equality_polarity_site")
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
            matched_nodes=(f"root_equality_polarity:{site.stable_key}",),
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
        (site,) = enumerate_n18_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_n18_trace(theorem.proof_stripped_declaration, forward)
        if apply_n18_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N18EqualityPolarityError("internal_inverse_replay_failure")
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
                intended_error_types=("E10", "E26"),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata=_expected_draft_metadata(site),
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
            draft.intended_relation == IntendedRelation.NEAR_MISS
            and draft.intended_error_types == ("E10", "E26")
        ):
            violations.append("draft_semantic_intention_mismatch")
        if draft.candidate_pool != self.candidate_pool:
            violations.append("draft_candidate_pool_mismatch")
        if draft.expected_atom_mapping != {}:
            violations.append("draft_expected_atom_mapping_mismatch")
        if not (
            draft.formalrx_sci_requested is None
            and draft.formalrx_sci_validated is None
            and draft.formalrx_sci_validation_status == "not_requested"
            and draft.formalrx_sci_proposer_family is None
            and draft.formalrx_sci_validator_family is None
        ):
            violations.append("draft_sci_provenance_mismatch")
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

        matching: list[EqualityPolaritySite] = []
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_n18_sites(
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
            if len(matching) == 1 and draft.metadata != _expected_draft_metadata(matching[0]):
                violations.append("draft_fixed_metadata_mismatch")
            forward_ok = (
                apply_n18_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_n18_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (N18EqualityPolarityError, TypeError, ValueError):
            pass
        if len(matching) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: EqualityPolarityCertificate | None = None
        expected_candidate_root: dict[str, Any] | None = None
        source_root: dict[str, Any] | None = None
        if len(matching) == 1:
            try:
                source_root = _operator_root(source_representation)
                expected_candidate_root = build_equality_polarity_root(
                    source_root, matching[0].header_binder_count
                )
                certificate = certify_equality_polarity(
                    source_root,
                    _operator_root(candidate_representation),
                    matching[0].header_binder_count,
                )
            except N18EqualityPolarityError as exc:
                violations.append(str(exc))

        source_repr_ok = source_root is not None and (
            source_representation.alpha_identity_fingerprint
            == alpha_identity_fingerprint(source_root)
            and source_representation.semantic_atoms == semantic_atoms(source_root)
        )
        if not source_repr_ok:
            violations.append("source_parser_tree_representation_mismatch")
        candidate_repr_ok = expected_candidate_root is not None and (
            candidate_representation.alpha_identity_fingerprint
            == alpha_identity_fingerprint(expected_candidate_root)
            and candidate_representation.semantic_atoms == semantic_atoms(expected_candidate_root)
        )
        if not candidate_repr_ok:
            violations.append("candidate_alpha_or_semantic_audit_mismatch")

        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("n18_exact_root_equality_polarity",),
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
            atom_mapping_ok=source_repr_ok and candidate_repr_ok and certificate is not None,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "equality_polarity_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "evidence_class": "D0",
                "failed_proof_search_used": False,
                "resolved_semantic_label": False,
                "source_representation_recomputed": source_repr_ok,
                "candidate_representation_recomputed": candidate_repr_ok,
                "structural_direction": (
                    certificate.direction if certificate is not None else None
                ),
                "training_eligible": False,
            },
        )


__all__ = [
    "EqualityPolarityCertificate",
    "EqualityPolaritySite",
    "N18EqualityPolarityError",
    "N18EqualityPolarityRule",
    "apply_n18_trace",
    "build_equality_polarity_root",
    "certify_equality_polarity",
    "enumerate_n18_sites",
]
