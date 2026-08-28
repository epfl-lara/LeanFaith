"""LF-033 P15: reverse the two sides of one root ``Iff`` conclusion.

The source conclusion must contain exactly one top-level ``↔`` and the
elaborated conclusion must be exactly ``Iff A B`` after the declaration's
outer binder chain.  Generation swaps the two complete source spans.  Audit
independently requires the candidate expression tree to be the exact
``Iff B A`` tree and replays the inverse edit.

P15 is an E2 provisional study.  Although root-Iff reversal is a standard
logical equivalence, this transformation alone does not resolve F1
same-claim faithfulness, promote a family, or make examples trainable.
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
    "distinct_iff_sides",
    "exact_inverse_replay",
    "exact_root_iff_reversal_certificate",
    "same_context_reelaboration",
    "single_root_iff",
)
_OPEN_TO_CLOSE = {"(": ")", "{": "}", "[": "]", "⦃": "⦄"}
_CLOSE_TO_OPEN = {value: key for key, value in _OPEN_TO_CLOSE.items()}
_ASCII_SCOPE_OPERATOR = re.compile(r"(?<![\w'])(?:fun|let|if|match|do)(?![\w'])")


class P15IffReversalError(ValueError):
    """A P15 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class IffReversalSite:
    left_start: int
    left_end: int
    right_start: int
    right_end: int
    left_text: str
    right_text: str
    header_binder_count: int
    source_root_hash: str
    expected_candidate_root_hash: str
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
                "source_left_hash": self.source_left_hash,
                "source_right_hash": self.source_right_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class IffReversalCertificate:
    header_binder_count: int
    source_root_hash: str
    candidate_root_hash: str
    source_left_hash: str
    source_right_hash: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "source_left_hash": self.source_left_hash,
            "source_right_hash": self.source_right_hash,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise P15IffReversalError("operator_tree_missing_root")
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
            raise P15IffReversalError("malformed_application")
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
    """Count only named declaration-header binders, never conclusion quantifiers.

    Auto-implicit and anonymous binders are intentionally unsupported in this
    first P15 profile: after skipping the named surface binders, the elaborated
    root must already be ``Iff``.  This makes P15 conservative and prevents a
    conclusion-level ``forall``/``exists`` from being mistaken for header
    structure.
    """

    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise P15IffReversalError(str(exc)) from exc
    return sum(len(group.names) for group in groups)


def _replace_after_header(
    root: dict[str, Any],
    header_binder_count: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if header_binder_count < 0:
        raise P15IffReversalError("negative_header_binder_count")
    if header_binder_count:
        if root.get("k") != "forall":
            raise P15IffReversalError("header_binder_expr_mismatch")
        body = root.get("body")
        if not isinstance(body, dict):
            raise P15IffReversalError("malformed_header_forall")
        candidate_body, left, right = _replace_after_header(body, header_binder_count - 1)
        return {**root, "body": candidate_body}, left, right

    head, arguments = _application_spine(root)
    if head.get("k") != "const" or head.get("n") != "Iff" or len(arguments) != 2:
        raise P15IffReversalError("conclusion_not_exact_root_iff")
    left, right = arguments
    if alpha_canonical_bytes(left) == alpha_canonical_bytes(right):
        raise P15IffReversalError("iff_sides_identical")
    return _build_application(head, (right, left)), left, right


def build_iff_reversal_root(
    source_root: dict[str, Any],
    header_binder_count: int,
) -> dict[str, Any]:
    candidate, _left, _right = _replace_after_header(source_root, header_binder_count)
    return candidate


def certify_iff_reversal(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
) -> IffReversalCertificate:
    expected, left, right = _replace_after_header(source_root, header_binder_count)
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise P15IffReversalError("candidate_not_exact_root_iff_reversal")
    return IffReversalCertificate(
        header_binder_count=header_binder_count,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        source_left_hash=hash_canonical(left),
        source_right_hash=hash_canonical(right),
    )


def _top_level_iff_positions(mask: str, start: int, end: int) -> tuple[int, ...]:
    stack: list[str] = []
    positions: list[int] = []
    for position in range(start, end):
        character = mask[position]
        if character in _OPEN_TO_CLOSE:
            stack.append(character)
        elif character in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[character]:
                raise P15IffReversalError("mismatched_conclusion_delimiter")
            stack.pop()
        elif character == "↔" and not stack:
            positions.append(position)
    if stack:
        raise P15IffReversalError("unterminated_conclusion_delimiter")
    return tuple(positions)


def _has_top_level_scope_operator(mask: str, start: int, end: int) -> bool:
    """Reject surface forms whose scope would change when moved across ``↔``."""

    stack: list[str] = []
    position = start
    while position < end:
        character = mask[position]
        if character in _OPEN_TO_CLOSE:
            stack.append(character)
        elif character in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[character]:
                raise P15IffReversalError("mismatched_iff_side_delimiter")
            stack.pop()
        elif not stack:
            if character in {"∀", "∃", "λ"}:
                return True
            match = _ASCII_SCOPE_OPERATOR.match(mask, position, end)
            if match is not None:
                return True
        position += 1
    if stack:
        raise P15IffReversalError("unterminated_iff_side_delimiter")
    return False


def _trim_span(source: str, start: int, end: int) -> tuple[int, int, str]:
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    text = source[start:end]
    if not text:
        raise P15IffReversalError("empty_iff_side")
    return start, end, text


def enumerate_p15_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[IffReversalSite, ...]:
    """Return one exact root-Iff site, or no site on any ambiguity."""

    try:
        mask, conclusion_start, conclusion_end = _signature_bounds(source)
        positions = _top_level_iff_positions(mask, conclusion_start, conclusion_end)
        if len(positions) != 1:
            raise P15IffReversalError("root_iff_not_unique")
        iff_position = positions[0]
        if _has_top_level_scope_operator(mask, conclusion_start, iff_position):
            raise P15IffReversalError("left_iff_side_has_unparenthesized_scope_operator")
        if _has_top_level_scope_operator(mask, iff_position + 1, conclusion_end):
            raise P15IffReversalError("right_iff_side_has_unparenthesized_scope_operator")
        left_start, left_end, left_text = _trim_span(
            source,
            conclusion_start,
            iff_position,
        )
        right_start, right_end, right_text = _trim_span(
            source,
            iff_position + 1,
            conclusion_end,
        )
        root = cast(dict[str, Any], operator_tree_view["root"])
        header_binder_count = _header_binder_count(source)
        expected, left, right = _replace_after_header(root, header_binder_count)
        return (
            IffReversalSite(
                left_start=left_start,
                left_end=left_end,
                right_start=right_start,
                right_end=right_end,
                left_text=left_text,
                right_text=right_text,
                header_binder_count=header_binder_count,
                source_root_hash=hash_canonical(root),
                expected_candidate_root_hash=hash_canonical(expected),
                source_left_hash=hash_canonical(left),
                source_right_hash=hash_canonical(right),
            ),
        )
    except (
        KeyError,
        BinderParseError,
        P15IffReversalError,
        TypeError,
        V2E0RuleError,
    ):
        return ()


def _trace(
    site: IffReversalSite,
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
            "p15_operation": "inverse_root_iff_reversal" if inverse else "root_iff_reversal",
            "left_start": site.left_start,
            "left_end": site.left_start + len(left_text),
            "left_text": left_text,
            "right_start": right_start,
            "right_end": right_start + len(right_text),
            "right_text": right_text,
            "header_binder_count": site.header_binder_count,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "source_left_hash": site.source_left_hash,
            "source_right_hash": site.source_right_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_p15_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "swap_exact_spans":
        raise P15IffReversalError("expected_one_swap_trace")
    step = trace[0]
    raw_spans = tuple(
        step.get(key) for key in ("left_start", "left_end", "right_start", "right_end")
    )
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in raw_spans):
        raise P15IffReversalError("invalid_trace_span")
    left_start, left_end, right_start, right_end = cast(tuple[int, int, int, int], raw_spans)
    left_text = step.get("left_text")
    right_text = step.get("right_text")
    if not isinstance(left_text, str) or not isinstance(right_text, str):
        raise P15IffReversalError("invalid_trace_text")
    if not 0 <= left_start < left_end <= right_start < right_end <= len(source):
        raise P15IffReversalError("invalid_or_overlapping_trace_spans")
    if source[left_start:left_end] != left_text or source[right_start:right_end] != right_text:
        raise P15IffReversalError("trace_expected_text_mismatch")
    return (
        source[:left_start]
        + right_text
        + source[left_end:right_start]
        + left_text
        + source[right_end:]
    )


def _expected_structural_diff(site: IffReversalSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "root_iff_reversal",
        "evidence_class": "E2",
        "header_binder_count": site.header_binder_count,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
        "source_left_hash": site.source_left_hash,
        "source_right_hash": site.source_right_hash,
    }


class P15IffReversalRule:
    """One exact E2 root-Iff reversal; never an automatic F1 label."""

    polarity = Polarity.POSITIVE
    rule_id = "p15_root_iff_reversal"
    family_id = "p15_root_iff_reversal"
    implementation_key = "p15_root_iff_reversal"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise P15IffReversalError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise P15IffReversalError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "p15_iff_reversal_audit_v1",
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
            enumerate_p15_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_root_iff_site")
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
            matched_nodes=(f"root_iff:{site.stable_key}",),
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
        (site,) = enumerate_p15_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_p15_trace(theorem.proof_stripped_declaration, forward)
        if apply_p15_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise P15IffReversalError("internal_inverse_replay_failure")
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
                    "structural_direction": "reverse_root_iff_sides",
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

        matching: list[IffReversalSite] = []
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_p15_sites(
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
                apply_p15_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_p15_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (P15IffReversalError, TypeError, ValueError):
            pass
        if len(matching) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: IffReversalCertificate | None = None
        if len(matching) == 1:
            try:
                certificate = certify_iff_reversal(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    matching[0].header_binder_count,
                )
            except P15IffReversalError as exc:
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
                matched_nodes=("p15_exact_root_iff_reversal",),
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
                "evidence_class": "E2",
                "failed_proof_search_used": False,
                "resolved_semantic_label": False,
                "root_iff_reversal_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "semantic_atoms_changed": (
                    source_representation.semantic_atoms != candidate_representation.semantic_atoms
                    if atoms_present
                    else None
                ),
                "structural_direction": "reverse_root_iff_sides",
                "training_eligible": False,
            },
        )


__all__ = [
    "IffReversalCertificate",
    "IffReversalSite",
    "P15IffReversalError",
    "P15IffReversalRule",
    "apply_p15_trace",
    "build_iff_reversal_root",
    "certify_iff_reversal",
    "enumerate_p15_sites",
]
