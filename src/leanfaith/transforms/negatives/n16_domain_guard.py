"""LF-034 N16: remove one exact bounded-universal membership guard.

The executable scope is deliberately narrow.  The source conclusion must have
one of the surface forms ``forall x : A, x in S -> P`` or ``forall x in S, P``
(using Lean's Unicode tokens), with one explicit, singly named binder.  The
elaborated target must be exactly an explicit universal binder followed by an
anonymous proof binder whose domain is ``Membership.mem ... x``.  The
candidate removes only that proof binder and lowers the preserved predicate's
de Bruijn indices.

N16 emits provisional D0 structural evidence only.  Removing a guard normally
strengthens a claim, but redundant guards and ambient facts prevent a
mechanical same-claim label.  No semantic label, promotion, or training credit
is created here.
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
    "exact_inverse_replay",
    "exact_membership_guard_removal_certificate",
    "same_context_reelaboration",
    "single_bounded_universal",
)
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_']*"
_FORALL_HEAD = re.compile(rf"∀\s+(?P<var>{_IDENTIFIER})")


class N16DomainGuardError(ValueError):
    """An N16 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class DomainGuardSite:
    edit_start: int
    edit_end: int
    source_text: str
    replacement_text: str
    surface_form: str
    variable_name: str
    binder_type_text: str
    guard_text: str
    body_text_hash: str
    header_binder_count: int
    source_root_hash: str
    expected_candidate_root_hash: str
    removed_guard_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "edit_start": self.edit_start,
                "edit_end": self.edit_end,
                "source_text": self.source_text,
                "replacement_text": self.replacement_text,
                "surface_form": self.surface_form,
                "variable_name": self.variable_name,
                "binder_type_text": self.binder_type_text,
                "guard_text": self.guard_text,
                "body_text_hash": self.body_text_hash,
                "header_binder_count": self.header_binder_count,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
                "removed_guard_hash": self.removed_guard_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class DomainGuardCertificate:
    header_binder_count: int
    source_root_hash: str
    candidate_root_hash: str
    removed_guard_hash: str
    preserved_body_hash: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "header_binder_count": self.header_binder_count,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "removed_guard_hash": self.removed_guard_hash,
            "preserved_body_hash": self.preserved_body_hash,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise N16DomainGuardError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _application_spine(node: dict[str, Any]) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    arguments: list[dict[str, Any]] = []
    current = node
    while current.get("k") == "app":
        function = current.get("fn")
        argument = current.get("arg")
        if not isinstance(function, dict) or not isinstance(argument, dict):
            raise N16DomainGuardError("malformed_application")
        arguments.append(argument)
        current = function
    arguments.reverse()
    return current, tuple(arguments)


def _membership_member(node: dict[str, Any]) -> dict[str, Any]:
    head, arguments = _application_spine(node)
    if head.get("k") != "const" or head.get("n") != "Membership.mem":
        raise N16DomainGuardError("expected_membership_guard")
    if len(arguments) < 2:
        raise N16DomainGuardError("malformed_membership_guard")
    return arguments[-1]


def _references_outer_binder(node: object, *, outer_index: int, cutoff: int = 0) -> bool:
    if isinstance(node, list):
        return any(
            _references_outer_binder(item, outer_index=outer_index, cutoff=cutoff) for item in node
        )
    if not isinstance(node, dict):
        return False
    kind = node.get("k")
    if kind == "bvar":
        index = node.get("i")
        if not isinstance(index, int) or isinstance(index, bool):
            raise N16DomainGuardError("malformed_bvar_index")
        return index == cutoff + outer_index
    for key, value in node.items():
        child_cutoff = cutoff + (1 if kind in {"forall", "lam", "let"} and key == "body" else 0)
        if _references_outer_binder(value, outer_index=outer_index, cutoff=child_cutoff):
            return True
    return False


def _remove_surrounding_binder(node: object, *, cutoff: int = 0) -> object:
    """Lower references crossing the anonymous guard-proof binder."""

    if isinstance(node, list):
        return [_remove_surrounding_binder(item, cutoff=cutoff) for item in node]
    if not isinstance(node, dict):
        return node
    kind = node.get("k")
    if kind == "bvar":
        raw_index = node.get("i")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            raise N16DomainGuardError("malformed_bvar_index")
        if raw_index < cutoff:
            return dict(node)
        if raw_index == cutoff:
            raise N16DomainGuardError("body_depends_on_guard_proof")
        return {**node, "i": raw_index - 1}
    result: dict[str, object] = {}
    for key, value in node.items():
        child_cutoff = cutoff + (1 if kind in {"forall", "lam", "let"} and key == "body" else 0)
        result[key] = _remove_surrounding_binder(value, cutoff=child_cutoff)
    return result


def _transform_target(
    target: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if target.get("k") != "forall" or target.get("bi") != "default":
        raise N16DomainGuardError("expected_explicit_universal_target")
    guard_binder = target.get("body")
    if not isinstance(guard_binder, dict) or guard_binder.get("k") != "forall":
        raise N16DomainGuardError("expected_guard_implication")
    if guard_binder.get("bi") != "default":
        raise N16DomainGuardError("guard_proof_binder_not_explicit")
    guard = guard_binder.get("dom")
    predicate = guard_binder.get("body")
    if not isinstance(guard, dict) or not isinstance(predicate, dict):
        raise N16DomainGuardError("malformed_guard_implication")
    member = _membership_member(guard)
    if member != {"k": "bvar", "i": 0}:
        raise N16DomainGuardError("membership_does_not_guard_target_binder")
    if predicate.get("k") == "forall":
        nested_domain = predicate.get("dom")
        if isinstance(nested_domain, dict):
            try:
                _membership_member(nested_domain)
            except N16DomainGuardError:
                pass
            else:
                raise N16DomainGuardError("multiple_membership_guards_excluded")
    lowered = _remove_surrounding_binder(predicate)
    if not isinstance(lowered, dict):
        raise N16DomainGuardError("malformed_preserved_predicate")
    if not _references_outer_binder(lowered, outer_index=0):
        raise N16DomainGuardError("predicate_does_not_use_guarded_variable")
    return {**target, "body": lowered}, guard, lowered


def _replace_after_header(
    root: dict[str, Any],
    header_binder_count: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if header_binder_count == 0:
        return _transform_target(root)
    if root.get("k") != "forall":
        raise N16DomainGuardError("header_binder_expr_mismatch")
    body = root.get("body")
    if not isinstance(body, dict):
        raise N16DomainGuardError("malformed_header_forall")
    candidate_body, guard, preserved = _replace_after_header(body, header_binder_count - 1)
    return {**root, "body": candidate_body}, guard, preserved


def build_domain_guard_removal_root(
    source_root: dict[str, Any],
    header_binder_count: int,
) -> dict[str, Any]:
    candidate, _guard, _preserved = _replace_after_header(source_root, header_binder_count)
    return candidate


def certify_domain_guard_removal(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    header_binder_count: int,
) -> DomainGuardCertificate:
    expected, guard, preserved = _replace_after_header(source_root, header_binder_count)
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise N16DomainGuardError("candidate_not_exact_domain_guard_removal")
    return DomainGuardCertificate(
        header_binder_count=header_binder_count,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        removed_guard_hash=hash_canonical(guard),
        preserved_body_hash=hash_canonical(preserved),
    )


def _find_top_level_token(mask: str, start: int, end: int, token: str) -> int:
    pairs = {"(": ")", "[": "]", "{": "}", "⦃": "⦄"}
    closes = {close: open_ for open_, close in pairs.items()}
    stack: list[str] = []
    index = start
    while index < end:
        character = mask[index]
        if character in pairs:
            stack.append(character)
        elif character in closes:
            if not stack or stack.pop() != closes[character]:
                raise N16DomainGuardError("mismatched_guard_delimiter")
        elif not stack and mask.startswith(token, index):
            return index
        index += 1
    if stack:
        raise N16DomainGuardError("unclosed_guard_delimiter")
    raise N16DomainGuardError(f"missing_top_level_{'comma' if token == ',' else 'arrow'}")


def _header_binder_count(source: str) -> int:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise N16DomainGuardError(str(exc)) from exc
    return sum(len(group.names) for group in groups)


def enumerate_n16_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[DomainGuardSite, ...]:
    try:
        mask, raw_start, raw_end = _signature_bounds(source)
        raw_conclusion = source[raw_start:raw_end]
        leading = len(raw_conclusion) - len(raw_conclusion.lstrip())
        trailing = len(raw_conclusion) - len(raw_conclusion.rstrip())
        conclusion_start = raw_start + leading
        conclusion_end = raw_end - trailing
        if source[conclusion_start:conclusion_end] != mask[conclusion_start:conclusion_end]:
            raise N16DomainGuardError("comments_or_quoted_tokens_in_target")
        head = _FORALL_HEAD.match(mask, conclusion_start, conclusion_end)
        if head is None or head.start() != conclusion_start:
            raise N16DomainGuardError("expected_surface_universal")
        variable = head.group("var")
        cursor = head.end()
        while cursor < conclusion_end and mask[cursor].isspace():
            cursor += 1
        if cursor < conclusion_end and mask[cursor] == ":":
            surface_form = "explicit_membership_implication"
            type_start = cursor + 1
            comma = _find_top_level_token(mask, type_start, conclusion_end, ",")
            binder_type = source[type_start:comma].strip()
            if not binder_type:
                raise N16DomainGuardError("empty_binder_type")
            membership_prefix = re.compile(rf"\s*{re.escape(variable)}\s*∈\s*").match(
                mask,
                comma + 1,
                conclusion_end,
            )
            if membership_prefix is None:
                raise N16DomainGuardError("expected_surface_membership_guard")
            arrow = _find_top_level_token(mask, membership_prefix.end(), conclusion_end, "→")
            guard_text = source[membership_prefix.end() : arrow].strip()
            body_start = arrow + 1
            edit_start = comma + 1
            replacement_text = " "
        elif mask.startswith("∈", cursor):
            surface_form = "bounded_notation"
            guard_start = cursor + 1
            while guard_start < conclusion_end and mask[guard_start].isspace():
                guard_start += 1
            comma = _find_top_level_token(mask, guard_start, conclusion_end, ",")
            guard_text = source[guard_start:comma].strip()
            binder_type = "<inferred>"
            body_start = comma + 1
            edit_start = head.end()
            replacement_text = ", "
        else:
            raise N16DomainGuardError("expected_surface_membership_guard")
        while body_start < conclusion_end and mask[body_start].isspace():
            body_start += 1
        body_text = source[body_start:conclusion_end]
        if not guard_text or not body_text.strip():
            raise N16DomainGuardError("empty_guard_or_body")
        header_count = _header_binder_count(source)
        root = cast(dict[str, Any], operator_tree_view["root"])
        expected, guard, _preserved = _replace_after_header(root, header_count)
        edit_end = body_start
        return (
            DomainGuardSite(
                edit_start=edit_start,
                edit_end=edit_end,
                source_text=source[edit_start:edit_end],
                replacement_text=replacement_text,
                surface_form=surface_form,
                variable_name=variable,
                binder_type_text=binder_type,
                guard_text=guard_text,
                body_text_hash=hash_canonical({"text": body_text}),
                header_binder_count=header_count,
                source_root_hash=hash_canonical(root),
                expected_candidate_root_hash=hash_canonical(expected),
                removed_guard_hash=hash_canonical(guard),
            ),
        )
    except (
        KeyError,
        TypeError,
        V2E0RuleError,
        N16DomainGuardError,
    ):
        return ()


def _trace(
    site: DomainGuardSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    expected = site.replacement_text if inverse else site.source_text
    replacement = site.source_text if inverse else site.replacement_text
    return (
        {
            "operation": "replace_exact_span",
            "n16_operation": "restore_membership_guard" if inverse else "remove_membership_guard",
            "start": site.edit_start,
            "end": site.edit_start + len(expected),
            "expected_text": expected,
            "replacement_text": replacement,
            "surface_form": site.surface_form,
            "variable_name": site.variable_name,
            "binder_type_text": site.binder_type_text,
            "guard_text": site.guard_text,
            "body_text_hash": site.body_text_hash,
            "header_binder_count": site.header_binder_count,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "removed_guard_hash": site.removed_guard_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_n16_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    if len(trace) != 1 or trace[0].get("operation") != "replace_exact_span":
        raise N16DomainGuardError("expected_one_replace_trace")
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
        raise N16DomainGuardError("malformed_trace")
    if source[start:end] != expected:
        raise N16DomainGuardError("trace_expected_text_mismatch")
    return source[:start] + replacement + source[end:]


def _expected_structural_diff(site: DomainGuardSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "bounded_universal_domain_guard_removal",
        "evidence_class": "D0",
        "variable_name": site.variable_name,
        "binder_type_text": site.binder_type_text,
        "guard_text": site.guard_text,
        "surface_form": site.surface_form,
        "body_text_hash": site.body_text_hash,
        "header_binder_count": site.header_binder_count,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
        "removed_guard_hash": site.removed_guard_hash,
    }


class N16DomainGuardRule:
    polarity = Polarity.NEGATIVE
    rule_id = "n16_domain_guard_removal"
    family_id = "n16_domain_guard_removal"
    implementation_key = "n16_domain_guard_removal"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise N16DomainGuardError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise N16DomainGuardError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "n16_domain_guard_audit_v1",
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
            enumerate_n16_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if not sites:
            reasons.append("no_eligible_domain_guard_site")
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=_REQUIRED_CAPABILITIES,
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=(f"domain_guard:{sites[0].stable_key}",),
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
        (site,) = enumerate_n16_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_n16_trace(theorem.proof_stripped_declaration, forward)
        if apply_n16_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N16DomainGuardError("internal_inverse_replay_failure")
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
                intended_error_types=("E01", "E20", "E26"),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "generation_intention_only": True,
                    "near_miss": True,
                    "resolved_semantic_label": False,
                    "structural_direction": "bounded_domain_to_unrestricted_domain",
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

        matching: list[DomainGuardSite] = []
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_n16_sites(
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
                apply_n16_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_n16_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (ValueError, TypeError, N16DomainGuardError):
            pass
        if len(matching) != 1:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: DomainGuardCertificate | None = None
        if len(matching) == 1:
            try:
                certificate = certify_domain_guard_removal(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    matching[0].header_binder_count,
                )
            except N16DomainGuardError as exc:
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
                matched_nodes=("n16_exact_membership_guard_removal",),
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
                "domain_guard_removal_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "evidence_class": "D0",
                "failed_proof_search_used": False,
                "resolved_semantic_label": False,
                "structural_direction": "bounded_domain_to_unrestricted_domain",
                "training_eligible": False,
            },
        )


__all__ = [
    "DomainGuardCertificate",
    "DomainGuardSite",
    "N16DomainGuardError",
    "N16DomainGuardRule",
    "apply_n16_trace",
    "build_domain_guard_removal_root",
    "certify_domain_guard_removal",
    "enumerate_n16_sites",
]
