"""LF-033 P16: reassociate one exact root conjunction of three atoms.

The source conclusion must elaborate to either ``(A ∧ B) ∧ C`` or
``A ∧ (B ∧ C)`` after the declaration-header binders.  The three
atoms must be pairwise distinct and none may itself be a conjunction.  P16
changes only the association, preserves atom order, re-elaborates the result
in the same context, and independently certifies the candidate expression.

P16 emits E2 provisional structural evidence only.  It never creates a
resolved semantic label, promotion, or training eligibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

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

Association = Literal["left", "right"]
_VALID_ELABORATION = frozenset(
    {ValidationStatus.ELABORATES, ValidationStatus.ELABORATES_WITH_PLACEHOLDER}
)
_REQUIRED_CAPABILITIES = (
    "distinct_conjunction_atoms",
    "exact_conjunction_reassociation_certificate",
    "exact_inverse_replay",
    "preserved_atom_order",
    "same_context_reelaboration",
    "single_root_conjunction_chain",
)
_DELIMITERS = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}


class P16ConjunctionReassociationError(ValueError):
    """A P16 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class ConjunctionReassociationSite:
    conclusion_start: int
    conclusion_end: int
    source_text: str
    candidate_text: str
    source_association: Association
    atom_texts: tuple[str, str, str]
    atom_hashes: tuple[str, str, str]
    header_binder_count: int
    source_root_hash: str
    expected_candidate_root_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "conclusion_start": self.conclusion_start,
                "conclusion_end": self.conclusion_end,
                "source_text_hash": hash_canonical({"text": self.source_text}),
                "candidate_text_hash": hash_canonical({"text": self.candidate_text}),
                "source_association": self.source_association,
                "atom_text_hashes": [hash_canonical({"text": text}) for text in self.atom_texts],
                "atom_hashes": self.atom_hashes,
                "header_binder_count": self.header_binder_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class ConjunctionReassociationCertificate:
    header_binder_count: int
    source_association: Association
    source_root_hash: str
    candidate_root_hash: str
    atom_hashes: tuple[str, str, str]

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "source_association": self.source_association,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "atom_hashes": list(self.atom_hashes),
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise P16ConjunctionReassociationError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _and_parts(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if node.get("k") != "app":
        raise P16ConjunctionReassociationError("expected_and_application")
    partial = node.get("fn")
    right = node.get("arg")
    if not isinstance(partial, dict) or not isinstance(right, dict):
        raise P16ConjunctionReassociationError("malformed_and_application")
    head = partial.get("fn")
    left = partial.get("arg")
    if (
        partial.get("k") != "app"
        or not isinstance(head, dict)
        or head.get("k") != "const"
        or head.get("n") != "And"
        or not isinstance(left, dict)
    ):
        raise P16ConjunctionReassociationError("expected_and_constant")
    return left, right


def _is_and(node: dict[str, Any]) -> bool:
    try:
        _and_parts(node)
    except P16ConjunctionReassociationError:
        return False
    return True


def _and(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    head: dict[str, Any] = {"k": "const", "n": "And", "us": "[]"}
    return {"k": "app", "fn": {"k": "app", "fn": head, "arg": left}, "arg": right}


def _target(
    node: dict[str, Any],
) -> tuple[dict[str, Any], Association, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    left, right = _and_parts(node)
    left_nested = _is_and(left)
    right_nested = _is_and(right)
    if left_nested == right_nested:
        raise P16ConjunctionReassociationError("expected_exactly_one_nested_conjunction")
    if left_nested:
        first, second = _and_parts(left)
        third = right
        association: Association = "left"
        expected = _and(first, _and(second, third))
    else:
        first = left
        second, third = _and_parts(right)
        association = "right"
        expected = _and(_and(first, second), third)
    atoms = (first, second, third)
    if any(_is_and(atom) for atom in atoms):
        raise P16ConjunctionReassociationError("nested_nonroot_conjunction_excluded")
    canonical = [alpha_canonical_bytes(atom) for atom in atoms]
    if len(set(canonical)) != 3:
        raise P16ConjunctionReassociationError("duplicate_conjuncts_excluded")
    return expected, association, atoms


def _replace_after_header(
    root: dict[str, Any], header_binder_count: int
) -> tuple[
    dict[str, Any],
    Association,
    tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
]:
    if header_binder_count < 0:
        raise P16ConjunctionReassociationError("negative_header_binder_count")
    if header_binder_count == 0:
        return _target(root)
    if root.get("k") != "forall":
        raise P16ConjunctionReassociationError("header_binder_expr_mismatch")
    body = root.get("body")
    if not isinstance(body, dict):
        raise P16ConjunctionReassociationError("malformed_header_forall")
    candidate_body, association, atoms = _replace_after_header(body, header_binder_count - 1)
    return {**root, "body": candidate_body}, association, atoms


def build_conjunction_reassociation_root(
    source_root: dict[str, Any], header_binder_count: int
) -> dict[str, Any]:
    candidate, _association, _atoms = _replace_after_header(source_root, header_binder_count)
    return candidate


def certify_conjunction_reassociation(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
) -> ConjunctionReassociationCertificate:
    expected, association, atoms = _replace_after_header(source_root, header_binder_count)
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise P16ConjunctionReassociationError("candidate_not_exact_root_conjunction_reassociation")
    return ConjunctionReassociationCertificate(
        header_binder_count=header_binder_count,
        source_association=association,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        atom_hashes=cast(tuple[str, str, str], tuple(hash_canonical(atom) for atom in atoms)),
    )


def _matching_delimiter(mask: str, start: int, end: int) -> int:
    opener = mask[start]
    if opener not in _DELIMITERS:
        raise P16ConjunctionReassociationError("expected_opening_delimiter")
    stack = [opener]
    for index in range(start + 1, end):
        character = mask[index]
        if character in _DELIMITERS:
            stack.append(character)
        elif character in _DELIMITERS.values():
            if not stack or _DELIMITERS[stack.pop()] != character:
                raise P16ConjunctionReassociationError("mismatched_conjunction_delimiter")
            if not stack:
                return index
    raise P16ConjunctionReassociationError("unclosed_conjunction_delimiter")


def _trim(mask: str, start: int, end: int) -> tuple[int, int]:
    while start < end and mask[start].isspace():
        start += 1
    while end > start and mask[end - 1].isspace():
        end -= 1
    return start, end


def _strip_outer(mask: str, start: int, end: int) -> tuple[int, int]:
    start, end = _trim(mask, start, end)
    while start < end and mask[start] == "(":
        close = _matching_delimiter(mask, start, end)
        if close != end - 1:
            break
        start, end = _trim(mask, start + 1, close)
    return start, end


def _top_level_ands(mask: str, start: int, end: int) -> tuple[int, ...]:
    stack: list[str] = []
    positions: list[int] = []
    for index in range(start, end):
        character = mask[index]
        if character in _DELIMITERS:
            stack.append(character)
        elif character in _DELIMITERS.values():
            if not stack or _DELIMITERS[stack.pop()] != character:
                raise P16ConjunctionReassociationError("mismatched_conjunction_delimiter")
        elif character == "∧" and not stack:
            positions.append(index)
    if stack:
        raise P16ConjunctionReassociationError("unclosed_conjunction_delimiter")
    return tuple(positions)


def _parse_surface(
    source: str, mask: str, start: int, end: int
) -> tuple[Association, tuple[str, str, str]]:
    start, end = _strip_outer(mask, start, end)
    outer = _top_level_ands(mask, start, end)
    if len(outer) != 1:
        raise P16ConjunctionReassociationError("expected_one_explicit_surface_root_conjunction")
    operator = outer[0]
    left_bounds = _strip_outer(mask, start, operator)
    right_bounds = _strip_outer(mask, operator + 1, end)
    left_ops = _top_level_ands(mask, *left_bounds)
    right_ops = _top_level_ands(mask, *right_bounds)
    if (len(left_ops), len(right_ops)) == (1, 0):
        nested = left_ops[0]
        spans = (
            _trim(mask, left_bounds[0], nested),
            _trim(mask, nested + 1, left_bounds[1]),
            _trim(mask, *right_bounds),
        )
        association: Association = "left"
    elif (len(left_ops), len(right_ops)) == (0, 1):
        nested = right_ops[0]
        spans = (
            _trim(mask, *left_bounds),
            _trim(mask, right_bounds[0], nested),
            _trim(mask, nested + 1, right_bounds[1]),
        )
        association = "right"
    else:
        raise P16ConjunctionReassociationError("surface_not_exact_three_atom_chain")
    atoms = cast(tuple[str, str, str], tuple(source[a:b] for a, b in spans))
    if any(not atom for atom in atoms):
        raise P16ConjunctionReassociationError("empty_conjunction_atom")
    return association, atoms


def _reject_unsafe_surface(source: str, mask: str, start: int, end: int) -> None:
    """Reject masked syntax and scope-sensitive/macro-like atom surfaces.

    P16 deliberately does not move comments, quoted tokens, guillemet names,
    term macros, or unparenthesized scope operators.  The expression-tree
    certificate is still authoritative, but these exclusions keep generation
    conservative before Lean execution.
    """

    if any(
        source[index] != mask[index] and not source[index].isspace() for index in range(start, end)
    ):
        raise P16ConjunctionReassociationError("comments_or_quoted_tokens_in_target")
    surface = mask[start:end]
    if re.search(
        r"(?<![\w'])(?:fun|let|match|if|do|by|macro|syntax|notation)(?![\w'])|"
        r"[`$;#]|[\u27e8\u27e9]",
        surface,
    ):
        raise P16ConjunctionReassociationError("unsupported_macro_or_scope_surface")


def _header_binder_count(source: str) -> int:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise P16ConjunctionReassociationError(str(exc)) from exc
    return sum(len(group.names) for group in groups)


def enumerate_p16_sites(
    source: str, operator_tree_view: dict[str, Any]
) -> tuple[ConjunctionReassociationSite, ...]:
    """Return one conservative root-reassociation site, or no site."""

    try:
        mask, raw_start, raw_end = _signature_bounds(source)
        conclusion_start, conclusion_end = _trim(mask, raw_start, raw_end)
        _reject_unsafe_surface(source, mask, conclusion_start, conclusion_end)
        surface_association, atom_texts = _parse_surface(
            source, mask, conclusion_start, conclusion_end
        )
        root = cast(dict[str, Any], operator_tree_view["root"])
        header_count = _header_binder_count(source)
        expected, expression_association, atoms = _replace_after_header(root, header_count)
        if surface_association != expression_association:
            raise P16ConjunctionReassociationError("surface_expression_association_mismatch")
        if surface_association == "left":
            candidate_text = f"({atom_texts[0]}) ∧ (({atom_texts[1]}) ∧ ({atom_texts[2]}))"
        else:
            candidate_text = f"(({atom_texts[0]}) ∧ ({atom_texts[1]})) ∧ ({atom_texts[2]})"
        site = ConjunctionReassociationSite(
            conclusion_start=conclusion_start,
            conclusion_end=conclusion_end,
            source_text=source[conclusion_start:conclusion_end],
            candidate_text=candidate_text,
            source_association=surface_association,
            atom_texts=atom_texts,
            atom_hashes=cast(tuple[str, str, str], tuple(hash_canonical(atom) for atom in atoms)),
            header_binder_count=header_count,
            source_root_hash=hash_canonical(root),
            expected_candidate_root_hash=hash_canonical(expected),
        )
        return (site,)
    except (
        BinderParseError,
        KeyError,
        P16ConjunctionReassociationError,
        TypeError,
        V2E0RuleError,
    ):
        return ()


def _trace(
    site: ConjunctionReassociationSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    expected = site.candidate_text if inverse else site.source_text
    replacement = site.source_text if inverse else site.candidate_text
    return (
        {
            "operation": "replace_exact_span",
            "p16_operation": (
                "inverse_conjunction_reassociation" if inverse else "conjunction_reassociation"
            ),
            "start": site.conclusion_start,
            "end": site.conclusion_start + len(expected),
            "expected_text": expected,
            "replacement_text": replacement,
            "source_association": site.source_association,
            "atom_hashes": list(site.atom_hashes),
            "header_binder_count": site.header_binder_count,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_p16_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "replace_exact_span":
        raise P16ConjunctionReassociationError("expected_one_replace_trace")
    step = trace[0]
    start, end = step.get("start"), step.get("end")
    expected, replacement = step.get("expected_text"), step.get("replacement_text")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(expected, str)
        or not isinstance(replacement, str)
        or not 0 <= start < end <= len(source)
    ):
        raise P16ConjunctionReassociationError("invalid_replace_trace")
    if source[start:end] != expected:
        raise P16ConjunctionReassociationError("trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(site: ConjunctionReassociationSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "root_conjunction_reassociation",
        "evidence_class": "E2",
        "source_association": site.source_association,
        "candidate_association": "right" if site.source_association == "left" else "left",
        "header_binder_count": site.header_binder_count,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
        "atom_hashes": list(site.atom_hashes),
    }


class P16ConjunctionReassociationRule:
    """One exact E2 root-conjunction reassociation; never an F1 label."""

    polarity = Polarity.POSITIVE
    rule_id = "p16_conjunction_reassociation"
    family_id = "p16_conjunction_reassociation"
    implementation_key = "p16_conjunction_reassociation"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise P16ConjunctionReassociationError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise P16ConjunctionReassociationError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "p16_conjunction_reassociation_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "requirements": _REQUIRED_CAPABILITIES,
            }
        )

    def assess(self, theorem: TheoremRecord, representation: RepresentationRecord) -> Applicability:
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
            enumerate_p16_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_root_conjunction_reassociation_site")
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=_REQUIRED_CAPABILITIES,
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=(f"root_conjunction:{sites[0].stable_key}",),
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
        (site,) = enumerate_p16_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_p16_trace(theorem.proof_stripped_declaration, forward)
        if apply_p16_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise P16ConjunctionReassociationError("internal_inverse_replay_failure")
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
                    "structural_direction": "reassociate_root_conjunction",
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

        matching: list[ConjunctionReassociationSite] = []
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_p16_sites(
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
                apply_p16_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_p16_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (P16ConjunctionReassociationError, TypeError, ValueError):
            pass
        if len(matching) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: ConjunctionReassociationCertificate | None = None
        if len(matching) == 1:
            try:
                certificate = certify_conjunction_reassociation(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    matching[0].header_binder_count,
                )
            except P16ConjunctionReassociationError as exc:
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
                matched_nodes=("p16_exact_root_conjunction_reassociation",),
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
                "root_conjunction_reassociation_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "semantic_atoms_changed": (
                    source_representation.semantic_atoms != candidate_representation.semantic_atoms
                    if atoms_present
                    else None
                ),
                "structural_direction": "reassociate_root_conjunction",
                "training_eligible": False,
            },
        )


__all__ = [
    "ConjunctionReassociationCertificate",
    "ConjunctionReassociationSite",
    "P16ConjunctionReassociationError",
    "P16ConjunctionReassociationRule",
    "apply_p16_trace",
    "build_conjunction_reassociation_root",
    "certify_conjunction_reassociation",
    "enumerate_p16_sites",
]
