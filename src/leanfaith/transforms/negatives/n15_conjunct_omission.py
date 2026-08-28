"""LF-034 N15: omit one distinct root-conclusion conjunct.

The executable scope is intentionally narrow.  The theorem conclusion must be
one binary root ``And`` whose children are distinct and are not themselves
root conjunctions.  One seed-selected side is retained and the other is
omitted.  The source and candidate are re-elaborated in one frozen context and
the audit reconstructs the exact elaborated-expression projection.

N15 emits only provisional D0 structural evidence.  It creates no semantic
label, promotion, or training eligibility: an omitted conjunct can be
redundant in a particular mathematical context even though the syntax changed.
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

RetainedSide = Literal["left", "right"]
_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_REQUIRED_CAPABILITIES = (
    "distinct_top_level_conjuncts",
    "exact_conjunct_projection_certificate",
    "exact_inverse_replay",
    "same_context_reelaboration",
)


class N15ConjunctOmissionError(ValueError):
    """An N15 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class ConjunctOmissionSite:
    conclusion_start: int
    conclusion_end: int
    source_text: str
    candidate_text: str
    retained_side: RetainedSide
    retained_text_hash: str
    omitted_text_hash: str
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
                "retained_side": self.retained_side,
                "retained_text_hash": self.retained_text_hash,
                "omitted_text_hash": self.omitted_text_hash,
                "header_binder_count": self.header_binder_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class ConjunctOmissionCertificate:
    header_binder_count: int
    retained_side: RetainedSide
    source_root_hash: str
    candidate_root_hash: str
    retained_conjunct_hash: str
    omitted_conjunct_hash: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "retained_side": self.retained_side,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "retained_conjunct_hash": self.retained_conjunct_hash,
            "omitted_conjunct_hash": self.omitted_conjunct_hash,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise N15ConjunctOmissionError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _and_parts(node: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if node.get("k") != "app":
        raise N15ConjunctOmissionError("expected_and_application")
    and_left = node.get("fn")
    right = node.get("arg")
    if not isinstance(and_left, dict) or not isinstance(right, dict):
        raise N15ConjunctOmissionError("malformed_and_application")
    if and_left.get("k") != "app":
        raise N15ConjunctOmissionError("malformed_and_left_application")
    and_const = and_left.get("fn")
    left = and_left.get("arg")
    if (
        not isinstance(and_const, dict)
        or and_const.get("k") != "const"
        or and_const.get("n") != "And"
        or not isinstance(left, dict)
    ):
        raise N15ConjunctOmissionError("expected_and_constant")
    return left, right


def _is_and_application(node: dict[str, Any]) -> bool:
    fn = node.get("fn")
    if node.get("k") != "app" or not isinstance(fn, dict) or fn.get("k") != "app":
        return False
    const = fn.get("fn")
    return isinstance(const, dict) and const.get("k") == "const" and const.get("n") == "And"


def _transform_target(
    node: dict[str, Any],
    retained_side: RetainedSide,
) -> tuple[dict[str, Any], dict[str, Any]]:
    left, right = _and_parts(node)
    if _is_and_application(left) or _is_and_application(right):
        raise N15ConjunctOmissionError("nested_conjunction_excluded")
    if alpha_canonical_bytes(left) == alpha_canonical_bytes(right):
        raise N15ConjunctOmissionError("duplicate_conjuncts_excluded")
    return (left, right) if retained_side == "left" else (right, left)


def _replace_after_header(
    root: dict[str, Any],
    header_binder_count: int,
    retained_side: RetainedSide,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if header_binder_count == 0:
        retained, omitted = _transform_target(root, retained_side)
        return retained, retained, omitted
    if root.get("k") != "forall":
        raise N15ConjunctOmissionError("header_binder_expr_mismatch")
    body = root.get("body")
    if not isinstance(body, dict):
        raise N15ConjunctOmissionError("malformed_header_forall")
    candidate_body, retained, omitted = _replace_after_header(
        body,
        header_binder_count - 1,
        retained_side,
    )
    return {**root, "body": candidate_body}, retained, omitted


def build_conjunct_omission_root(
    source_root: dict[str, Any],
    header_binder_count: int,
    retained_side: RetainedSide,
) -> dict[str, Any]:
    candidate, _retained, _omitted = _replace_after_header(
        source_root,
        header_binder_count,
        retained_side,
    )
    return candidate


def certify_conjunct_omission(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
    retained_side: RetainedSide,
) -> ConjunctOmissionCertificate:
    expected, retained, omitted = _replace_after_header(
        source_root,
        header_binder_count,
        retained_side,
    )
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise N15ConjunctOmissionError("candidate_not_exact_conjunct_projection")
    return ConjunctOmissionCertificate(
        header_binder_count=header_binder_count,
        retained_side=retained_side,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        retained_conjunct_hash=hash_canonical(retained),
        omitted_conjunct_hash=hash_canonical(omitted),
    )


def _matching_delimiter(mask: str, start: int, end: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
    opener = mask[start]
    if opener not in pairs:
        raise N15ConjunctOmissionError("expected_opening_delimiter")
    stack = [opener]
    for index in range(start + 1, end):
        character = mask[index]
        if character in pairs:
            stack.append(character)
        elif character in pairs.values():
            if not stack or pairs[stack.pop()] != character:
                raise N15ConjunctOmissionError("mismatched_conjunct_delimiter")
            if not stack:
                return index
    raise N15ConjunctOmissionError("unclosed_conjunct_delimiter")


def _strip_outer_parentheses(mask: str, start: int, end: int) -> tuple[int, int]:
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


def _top_level_and(mask: str, start: int, end: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
    stack: list[str] = []
    matches: list[int] = []
    for index in range(start, end):
        character = mask[index]
        if character in pairs:
            stack.append(character)
        elif character in pairs.values():
            if not stack or pairs[stack.pop()] != character:
                raise N15ConjunctOmissionError("mismatched_conjunct_delimiter")
        elif character == "∧" and not stack:
            matches.append(index)
    if stack:
        raise N15ConjunctOmissionError("unclosed_conjunct_delimiter")
    if len(matches) != 1:
        raise N15ConjunctOmissionError("expected_one_surface_root_conjunction")
    return matches[0]


def _header_binder_count(source: str) -> int:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise N15ConjunctOmissionError(str(exc)) from exc
    return sum(len(group.names) for group in groups)


def enumerate_n15_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[ConjunctOmissionSite, ...]:
    try:
        mask, raw_start, raw_end = _signature_bounds(source)
        raw_conclusion = source[raw_start:raw_end]
        leading = len(raw_conclusion) - len(raw_conclusion.lstrip())
        trailing = len(raw_conclusion) - len(raw_conclusion.rstrip())
        conclusion_start = raw_start + leading
        conclusion_end = raw_end - trailing
        expression_start, expression_end = _strip_outer_parentheses(
            mask,
            conclusion_start,
            conclusion_end,
        )
        operator = _top_level_and(mask, expression_start, expression_end)
        left_text = source[expression_start:operator].strip()
        right_text = source[operator + 1 : expression_end].strip()
        if not left_text or not right_text or left_text == right_text:
            return ()
        source_text = source[conclusion_start:conclusion_end]
        header_count = _header_binder_count(source)
        root = cast(dict[str, Any], operator_tree_view["root"])
        sites: list[ConjunctOmissionSite] = []
        cases: tuple[tuple[RetainedSide, str, str], ...] = (
            ("left", left_text, right_text),
            ("right", right_text, left_text),
        )
        for retained_side, retained_text, omitted_text in cases:
            expected = build_conjunct_omission_root(root, header_count, retained_side)
            sites.append(
                ConjunctOmissionSite(
                    conclusion_start=conclusion_start,
                    conclusion_end=conclusion_end,
                    source_text=source_text,
                    candidate_text=retained_text,
                    retained_side=retained_side,
                    retained_text_hash=hash_canonical({"text": retained_text}),
                    omitted_text_hash=hash_canonical({"text": omitted_text}),
                    header_binder_count=header_count,
                    source_root_hash=hash_canonical(root),
                    expected_candidate_root_hash=hash_canonical(expected),
                )
            )
    except (
        KeyError,
        IndexError,
        TypeError,
        V2E0RuleError,
        N15ConjunctOmissionError,
    ):
        return ()
    return tuple(sorted(sites, key=lambda site: site.stable_key))


def _site_for_seed(
    sites: tuple[ConjunctOmissionSite, ...],
    *,
    theorem_id: str,
    seed: int,
) -> ConjunctOmissionSite:
    if not sites:
        raise N15ConjunctOmissionError("no_conjunct_omission_sites")
    digest = hash_canonical(
        {
            "family_id": "n15_conjunct_omission",
            "theorem_id": theorem_id,
            "seed": seed,
            "site_keys": [site.stable_key for site in sites],
        }
    )
    return sites[int(digest[:16], 16) % len(sites)]


def _trace(
    site: ConjunctOmissionSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    expected = site.candidate_text if inverse else site.source_text
    replacement = site.source_text if inverse else site.candidate_text
    return (
        {
            "operation": "replace_exact_span",
            "n15_operation": "restore_conjunction" if inverse else "omit_conjunct",
            "start": site.conclusion_start,
            "end": site.conclusion_start + len(expected),
            "expected_text": expected,
            "replacement_text": replacement,
            "retained_side": site.retained_side,
            "retained_text_hash": site.retained_text_hash,
            "omitted_text_hash": site.omitted_text_hash,
            "header_binder_count": site.header_binder_count,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_n15_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "replace_exact_span":
        raise N15ConjunctOmissionError("expected_one_replace_trace")
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
        and isinstance(replacement, str)
        and 0 <= start <= end <= len(source)
    ):
        raise N15ConjunctOmissionError("malformed_trace")
    if source[start:end] != expected:
        raise N15ConjunctOmissionError("trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(site: ConjunctOmissionSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "top_level_conjunct_omission",
        "evidence_class": "D0",
        "retained_side": site.retained_side,
        "retained_text_hash": site.retained_text_hash,
        "omitted_text_hash": site.omitted_text_hash,
        "header_binder_count": site.header_binder_count,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
    }


class N15ConjunctOmissionRule:
    polarity = Polarity.NEGATIVE
    rule_id = "n15_conjunct_omission"
    family_id = "n15_conjunct_omission"
    implementation_key = "n15_conjunct_omission"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise N15ConjunctOmissionError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise N15ConjunctOmissionError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "n15_conjunct_omission_audit_v1",
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
            enumerate_n15_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if not sites:
            reasons.append("no_eligible_conjunct_omission_site")
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=_REQUIRED_CAPABILITIES,
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=tuple(
                sorted(f"and:{site.retained_side}:{site.stable_key}" for site in sites)
            ),
            required_capabilities=_REQUIRED_CAPABILITIES,
            metadata={"eligible_site_count": len(sites)},
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        if not self.assess(theorem, representation).applicable:
            return ()
        site = _site_for_seed(
            enumerate_n15_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            ),
            theorem_id=theorem.theorem_id,
            seed=seed,
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_n15_trace(theorem.proof_stripped_declaration, forward)
        if apply_n15_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N15ConjunctOmissionError("internal_inverse_replay_failure")
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
                intended_error_types=("E20", "E26"),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "generation_intention_only": True,
                    "near_miss": True,
                    "resolved_semantic_label": False,
                    "retained_side": site.retained_side,
                    "structural_direction": "source_conjunction_to_retained_conjunct",
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

        matching: list[ConjunctOmissionSite] = []
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_n15_sites(
                source.proof_stripped_declaration,
                cast(dict[str, Any], source_representation.operator_tree),
            )
            matching = [
                site
                for site in sites
                if _trace(
                    site,
                    generation_config_hash=self.generation_config_hash,
                    inverse=False,
                )
                == draft.transformation_trace
                and _trace(
                    site,
                    generation_config_hash=self.generation_config_hash,
                    inverse=True,
                )
                == draft.inverse_trace
                and _expected_structural_diff(site) == draft.expected_structural_diff
            ]
            forward_ok = (
                apply_n15_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_n15_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (ValueError, TypeError, N15ConjunctOmissionError):
            pass
        if len(matching) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: ConjunctOmissionCertificate | None = None
        if len(matching) == 1:
            try:
                certificate = certify_conjunct_omission(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    matching[0].header_binder_count,
                    matching[0].retained_side,
                )
            except N15ConjunctOmissionError as exc:
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
                matched_nodes=("n15_exact_top_level_conjunct_omission",),
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
                "conjunct_omission_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "evidence_class": "D0",
                "failed_proof_search_used": False,
                "resolved_semantic_label": False,
                "retained_side": certificate.retained_side if certificate is not None else None,
                "structural_direction": "source_conjunction_to_retained_conjunct",
                "training_eligible": False,
            },
        )


__all__ = [
    "ConjunctOmissionCertificate",
    "ConjunctOmissionSite",
    "N15ConjunctOmissionError",
    "N15ConjunctOmissionRule",
    "RetainedSide",
    "apply_n15_trace",
    "build_conjunct_omission_root",
    "certify_conjunct_omission",
    "enumerate_n15_sites",
]
