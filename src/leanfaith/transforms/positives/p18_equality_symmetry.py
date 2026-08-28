"""P18 v1.0: swap two distinct sides of one exact root Lean equality.

P18 is intentionally narrower than a generic equality rewriter.  The source
must expose exactly one root ``=`` after its declaration-header binders, and
the elaborated conclusion at that same depth must be exactly
``Eq alpha lhs rhs``.  The edit swaps the two complete surface spans; audit
then requires the candidate tree to be exactly ``Eq alpha rhs lhs`` and
recomputes the expected alpha fingerprint and semantic atoms.

This is experimental E2 evidence.  Even a clean candidate remains
``provisional`` and receives no resolved label, promotion, or training credit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

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
    "distinct_equality_sides",
    "exact_inverse_replay",
    "exact_root_equality_symmetry_certificate",
    "same_context_reelaboration",
    "single_surface_root_equality",
)
_OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
_CLOSE_TO_OPEN = {value: key for key, value in _OPEN_TO_CLOSE.items()}
_SCOPED_TERM = re.compile(
    r"(?<![\w'])(?:by|calc|do|fun|if|let|match|show|syntax|macro|scoped)(?![\w'])"
)
_MACRO_MARKERS = ("`(", "`[", "`{", "$(", "term%", "tactic%", "open scoped")
_EXPECTED_DRAFT_METADATA = {
    "positive_intention_only": True,
    "resolved_semantic_label": False,
    "structural_direction": "swap_root_equality_sides",
    "training_eligible": False,
}


class P18EqualitySymmetryError(ValueError):
    """A P18 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class EqualitySymmetrySite:
    left_start: int
    left_end: int
    right_start: int
    right_end: int
    left_text: str
    right_text: str
    header_binder_count: int
    source_root_hash: str
    expected_candidate_root_hash: str
    source_type_hash: str
    source_left_hash: str
    source_right_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "left_start": self.left_start,
                "left_end": self.left_end,
                "right_start": self.right_start,
                "right_end": self.right_end,
                "left_text_hash": hash_canonical({"text": self.left_text}),
                "right_text_hash": hash_canonical({"text": self.right_text}),
                "header_binder_count": self.header_binder_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
                "source_type_hash": self.source_type_hash,
                "source_left_hash": self.source_left_hash,
                "source_right_hash": self.source_right_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class EqualitySymmetryCertificate:
    header_binder_count: int
    source_root_hash: str
    candidate_root_hash: str
    source_type_hash: str
    source_left_hash: str
    source_right_hash: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "source_type_hash": self.source_type_hash,
            "source_left_hash": self.source_left_hash,
            "source_right_hash": self.source_right_hash,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise P18EqualitySymmetryError("operator_tree_missing_root")
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
            raise P18EqualitySymmetryError("malformed_application")
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


def _header_binder_count(source: str) -> int:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise P18EqualitySymmetryError(str(exc)) from exc
    return sum(len(group.names) for group in groups)


def _replace_after_header(
    root: dict[str, Any],
    header_binder_count: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if header_binder_count < 0:
        raise P18EqualitySymmetryError("negative_header_binder_count")
    if header_binder_count:
        if root.get("k") != "forall":
            raise P18EqualitySymmetryError("header_binder_expr_mismatch")
        body = root.get("body")
        if not isinstance(body, dict):
            raise P18EqualitySymmetryError("malformed_header_forall")
        candidate_body, equality_type, left, right = _replace_after_header(
            body, header_binder_count - 1
        )
        return {**root, "body": candidate_body}, equality_type, left, right

    head, arguments = _application_spine(root)
    if head.get("k") != "const" or head.get("n") != "Eq" or len(arguments) != 3:
        raise P18EqualitySymmetryError("conclusion_not_exact_root_eq")
    equality_type, left, right = arguments
    if alpha_canonical_bytes(left) == alpha_canonical_bytes(right):
        raise P18EqualitySymmetryError("equality_sides_identical")
    return _build_application(head, (equality_type, right, left)), equality_type, left, right


def build_equality_symmetry_root(
    source_root: dict[str, Any], header_binder_count: int
) -> dict[str, Any]:
    candidate, _equality_type, _left, _right = _replace_after_header(
        source_root, header_binder_count
    )
    return candidate


def certify_equality_symmetry(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
) -> EqualitySymmetryCertificate:
    expected, equality_type, left, right = _replace_after_header(source_root, header_binder_count)
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise P18EqualitySymmetryError("candidate_not_exact_root_equality_symmetry")
    return EqualitySymmetryCertificate(
        header_binder_count=header_binder_count,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        source_type_hash=hash_canonical(equality_type),
        source_left_hash=hash_canonical(left),
        source_right_hash=hash_canonical(right),
    )


def _matching_delimiter(mask: str, start: int, end: int) -> int:
    opener = mask[start]
    if opener not in _OPEN_TO_CLOSE:
        raise P18EqualitySymmetryError("expected_opening_delimiter")
    stack = [opener]
    for index in range(start + 1, end):
        character = mask[index]
        if character in _OPEN_TO_CLOSE:
            stack.append(character)
        elif character in _CLOSE_TO_OPEN:
            if not stack or _OPEN_TO_CLOSE[stack.pop()] != character:
                raise P18EqualitySymmetryError("mismatched_equality_delimiter")
            if not stack:
                return index
    raise P18EqualitySymmetryError("unterminated_equality_delimiter")


def _trim(source: str, start: int, end: int) -> tuple[int, int, str]:
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    text = source[start:end]
    if not text:
        raise P18EqualitySymmetryError("empty_equality_side")
    return start, end, text


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


def _reject_unsafe_surface(source: str, conclusion_start: int, conclusion_end: int) -> None:
    signature = source[:conclusion_end]
    conclusion = source[conclusion_start:conclusion_end]
    if any(marker in signature for marker in ("--", "/-", "-/", '"')):
        raise P18EqualitySymmetryError("comment_or_quoted_surface_excluded")
    if _SCOPED_TERM.search(conclusion) is not None:
        raise P18EqualitySymmetryError("scoped_term_surface_excluded")
    if any(operator in conclusion for operator in ("→", "↔", "∀", "∃", "λ")):
        raise P18EqualitySymmetryError("scoped_operator_surface_excluded")
    if any(marker in conclusion for marker in _MACRO_MARKERS):
        raise P18EqualitySymmetryError("macro_surface_excluded")


def _top_level_equals(mask: str, start: int, end: int) -> tuple[int, ...]:
    stack: list[str] = []
    positions: list[int] = []
    for index in range(start, end):
        character = mask[index]
        if character in _OPEN_TO_CLOSE:
            stack.append(character)
        elif character in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[character]:
                raise P18EqualitySymmetryError("mismatched_equality_delimiter")
            stack.pop()
        elif character == "=" and not stack:
            previous = mask[index - 1] if index > start else ""
            following = mask[index + 1] if index + 1 < end else ""
            if previous not in {"!", ":", "<", ">", "="} and following not in {"=", ">"}:
                positions.append(index)
    if stack:
        raise P18EqualitySymmetryError("unterminated_equality_delimiter")
    return tuple(positions)


def enumerate_p18_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[EqualitySymmetrySite, ...]:
    """Return one exact root equality site, or no site on any ambiguity."""

    try:
        mask, conclusion_start, conclusion_end = _signature_bounds(source)
        _reject_unsafe_surface(source, conclusion_start, conclusion_end)
        root_start, root_end = _strip_outer(mask, conclusion_start, conclusion_end)
        positions = _top_level_equals(mask, root_start, root_end)
        if len(positions) != 1:
            raise P18EqualitySymmetryError("root_equality_not_unique")
        equality_position = positions[0]
        left_start, left_end, left_text = _trim(source, root_start, equality_position)
        right_start, right_end, right_text = _trim(source, equality_position + 1, root_end)
        if left_text == right_text:
            raise P18EqualitySymmetryError("surface_equality_sides_identical")
        root = operator_tree_view.get("root")
        if not isinstance(root, dict):
            raise P18EqualitySymmetryError("operator_tree_missing_root")
        header_binder_count = _header_binder_count(source)
        expected, equality_type, left, right = _replace_after_header(root, header_binder_count)
        return (
            EqualitySymmetrySite(
                left_start=left_start,
                left_end=left_end,
                right_start=right_start,
                right_end=right_end,
                left_text=left_text,
                right_text=right_text,
                header_binder_count=header_binder_count,
                source_root_hash=hash_canonical(root),
                expected_candidate_root_hash=hash_canonical(expected),
                source_type_hash=hash_canonical(equality_type),
                source_left_hash=hash_canonical(left),
                source_right_hash=hash_canonical(right),
            ),
        )
    except (
        BinderParseError,
        P18EqualitySymmetryError,
        TypeError,
        V2E0RuleError,
    ):
        return ()


def _trace(
    site: EqualitySymmetrySite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    left_text = site.left_text
    right_text = site.right_text
    right_start = site.right_start
    if inverse:
        right_start += len(site.right_text) - len(site.left_text)
        left_text, right_text = right_text, left_text
    return (
        {
            "operation": "swap_exact_spans",
            "p18_operation": (
                "inverse_root_equality_symmetry" if inverse else "root_equality_symmetry"
            ),
            "left_start": site.left_start,
            "left_end": site.left_start + len(left_text),
            "left_text": left_text,
            "right_start": right_start,
            "right_end": right_start + len(right_text),
            "right_text": right_text,
            "header_binder_count": site.header_binder_count,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "source_type_hash": site.source_type_hash,
            "source_left_hash": site.source_left_hash,
            "source_right_hash": site.source_right_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_p18_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "swap_exact_spans":
        raise P18EqualitySymmetryError("expected_one_swap_trace")
    step = trace[0]
    raw_spans = tuple(
        step.get(key) for key in ("left_start", "left_end", "right_start", "right_end")
    )
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in raw_spans):
        raise P18EqualitySymmetryError("invalid_trace_span")
    left_start, left_end, right_start, right_end = cast(tuple[int, int, int, int], raw_spans)
    left_text = step.get("left_text")
    right_text = step.get("right_text")
    if not isinstance(left_text, str) or not isinstance(right_text, str):
        raise P18EqualitySymmetryError("invalid_trace_text")
    if not 0 <= left_start < left_end <= right_start < right_end <= len(source):
        raise P18EqualitySymmetryError("invalid_or_overlapping_trace_spans")
    if source[left_start:left_end] != left_text or source[right_start:right_end] != right_text:
        raise P18EqualitySymmetryError("trace_expected_text_mismatch")
    return (
        source[:left_start]
        + right_text
        + source[left_end:right_start]
        + left_text
        + source[right_end:]
    )


def _expected_structural_diff(site: EqualitySymmetrySite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "root_equality_symmetry",
        "evidence_class": "E2",
        "header_binder_count": site.header_binder_count,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
        "source_type_hash": site.source_type_hash,
        "source_left_hash": site.source_left_hash,
        "source_right_hash": site.source_right_hash,
    }


class P18EqualitySymmetryRule:
    """One exact E2 root-equality symmetry edit; never an F1 label."""

    polarity = Polarity.POSITIVE
    rule_id = "p18_root_equality_symmetry"
    family_id = "p18_root_equality_symmetry"
    implementation_key = "p18_root_equality_symmetry"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise P18EqualitySymmetryError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise P18EqualitySymmetryError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "p18_equality_symmetry_audit_v1",
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
            enumerate_p18_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_root_equality_site")
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
            matched_nodes=(f"root_equality:{site.stable_key}",),
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
        (site,) = enumerate_p18_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_p18_trace(theorem.proof_stripped_declaration, forward)
        if apply_p18_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise P18EqualitySymmetryError("internal_inverse_replay_failure")
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
                intended_relation=IntendedRelation.EQUIVALENT,
                intended_error_types=(),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "positive_intention_only": True,
                    "resolved_semantic_label": False,
                    "structural_direction": "swap_root_equality_sides",
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
            draft.intended_relation == IntendedRelation.EQUIVALENT
            and draft.intended_error_types == ()
        ):
            violations.append("draft_semantic_intention_mismatch")
        if draft.candidate_pool != self.candidate_pool:
            violations.append("draft_candidate_pool_mismatch")
        if draft.metadata != _EXPECTED_DRAFT_METADATA:
            violations.append("draft_fixed_metadata_mismatch")
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

        matching: list[EqualitySymmetrySite] = []
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_p18_sites(
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
                apply_p18_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_p18_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (P18EqualitySymmetryError, TypeError, ValueError):
            pass
        if len(matching) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: EqualitySymmetryCertificate | None = None
        expected_candidate_root: dict[str, Any] | None = None
        source_root: dict[str, Any] | None = None
        if len(matching) == 1:
            try:
                source_root = _operator_root(source_representation)
                expected_candidate_root = build_equality_symmetry_root(
                    source_root, matching[0].header_binder_count
                )
                certificate = certify_equality_symmetry(
                    source_root,
                    _operator_root(candidate_representation),
                    matching[0].header_binder_count,
                )
            except P18EqualitySymmetryError as exc:
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
                matched_nodes=("p18_exact_root_equality_symmetry",),
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
            atom_mapping_ok=source_repr_ok and candidate_repr_ok,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "evidence_class": "E2",
                "failed_proof_search_used": False,
                "resolved_semantic_label": False,
                "root_equality_symmetry_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "source_representation_recomputed": source_repr_ok,
                "candidate_representation_recomputed": candidate_repr_ok,
                "structural_direction": "swap_root_equality_sides",
                "training_eligible": False,
            },
        )


__all__ = [
    "EqualitySymmetryCertificate",
    "EqualitySymmetrySite",
    "P18EqualitySymmetryError",
    "P18EqualitySymmetryRule",
    "apply_p18_trace",
    "build_equality_symmetry_root",
    "certify_equality_symmetry",
    "enumerate_p18_sites",
]
