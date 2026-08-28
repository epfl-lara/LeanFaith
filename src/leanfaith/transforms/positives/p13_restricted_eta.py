"""Restricted provisional P13 function-eta contraction.

P13 is an E1 study, not an E0 surface equivalence rule.  It contracts exactly
one source-visible redex of the form ``(fun (x : A) => f x)`` to ``f`` only
when the theorem header itself declares ``f`` with the simple, explicit,
nondependent type ``A -> B``.  The matcher therefore owns a local redex,
domain, dependency, and free-variable certificate without consulting broad
reduction or proof search.

The generated draft remains provisional.  This module does not register the
rule, run Lean, create labels, promote examples, or make them trainable.
Same-context source and candidate elaboration is checked by ``audit`` only
after the surrounding runtime has independently built both theorem records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import JsonValue

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas.enums import (
    IntendedRelation,
    Polarity,
    QualityTier,
    ValidationStatus,
    ViewStatus,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import Applicability, TransformationAudit, VariantDraft
from leanfaith.transforms.positives.v2_e0 import (
    PresentationSite,
    V2E0RuleError,
    _choose_site,
    _inverse_trace,
    _signature_bounds,
    _site_trace,
    apply_presentation_trace,
)
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
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_']*"
_QUALIFIED_IDENTIFIER = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*"
_EXPLICIT_SIMPLE_FUNCTION_BINDER = re.compile(
    rf"\(\s*(?P<head>{_IDENTIFIER})\s*:\s*"
    rf"(?P<domain>{_QUALIFIED_IDENTIFIER})\s*→\s*"
    rf"(?P<codomain>{_QUALIFIED_IDENTIFIER})\s*\)"
)
_EXPLICIT_ETA_REDEX = re.compile(
    rf"\(\s*fun\s+\(\s*(?P<variable>{_IDENTIFIER})\s*:\s*"
    rf"(?P<domain>{_QUALIFIED_IDENTIFIER})\s*\)\s*=>\s*"
    rf"(?P<head>{_IDENTIFIER})\s+(?P<argument>{_IDENTIFIER})\s*\)"
)
_REQUIRED_CAPABILITIES = (
    "eta_redex_certificate",
    "exact_inverse_replay",
    "free_variable_certificate",
    "nondependent_function_certificate",
    "same_context_reelaboration",
)


class P13RestrictedEtaError(ValueError):
    """A P13 source shape, trace, or local certificate failed closed."""


@dataclass(frozen=True, slots=True)
class _FunctionBinder:
    head: str
    domain: str
    codomain: str


def _function_binders(header: str) -> dict[str, tuple[_FunctionBinder, ...]]:
    depths: list[int] = []
    depth = 0
    openers = frozenset("({[⦃")
    closers = frozenset(")}]⦄")
    for character in header:
        depths.append(depth)
        if character in openers:
            depth += 1
        elif character in closers:
            depth = max(0, depth - 1)

    grouped: dict[str, list[_FunctionBinder]] = {}
    for match in _EXPLICIT_SIMPLE_FUNCTION_BINDER.finditer(header):
        # Only declaration-header binder groups begin at delimiter depth zero.
        # A type ascription nested inside another binder is not evidence that
        # the ascribed expression is a local function binder.
        if depths[match.start()] != 0:
            continue
        binder = _FunctionBinder(
            head=match.group("head"),
            domain=match.group("domain"),
            codomain=match.group("codomain"),
        )
        grouped.setdefault(binder.head, []).append(binder)
    return {head: tuple(items) for head, items in grouped.items()}


def _identifier_tokens(source: str) -> frozenset[str]:
    return frozenset(re.findall(_IDENTIFIER, source))


def enumerate_p13_sites(source: str) -> tuple[PresentationSite, ...]:
    """Return locally certified, source-visible eta-contraction sites.

    The intentionally narrow parser accepts only a parenthesized explicit
    lambda, a simple local function-binder head, and one atomic argument.  It
    cannot recognize implicit/instance binders, dependent function types,
    partially applied heads, or eta expansions.
    """

    try:
        mask, conclusion_start, conclusion_end = _signature_bounds(source)
    except V2E0RuleError as exc:
        raise P13RestrictedEtaError(str(exc)) from exc

    binders = _function_binders(mask[:conclusion_start])
    conclusion = mask[conclusion_start:conclusion_end]
    sites: list[PresentationSite] = []
    for match in _EXPLICIT_ETA_REDEX.finditer(conclusion):
        variable = match.group("variable")
        argument = match.group("argument")
        head = match.group("head")
        domain = match.group("domain")
        declarations = binders.get(head, ())

        # The body must be exactly ``f x`` and x must be absent from the
        # function position.  Although the accepted head is atomic, retaining
        # the token-level check makes the free-variable certificate explicit.
        if argument != variable or variable in _identifier_tokens(head):
            continue
        if head == variable or len(declarations) != 1:
            continue
        declaration = declarations[0]
        if declaration.domain != domain:
            continue

        start = conclusion_start + match.start()
        end = conclusion_start + match.end()
        source_text = source[start:end]
        sites.append(
            PresentationSite(
                operation="contract_explicit_nondependent_eta",
                start=start,
                end=end,
                source_text=source_text,
                replacement_text=head,
                metadata=(
                    ("binder_kind", "explicit"),
                    ("codomain", declaration.codomain),
                    ("domain", declaration.domain),
                    ("eta_argument", argument),
                    ("eta_binder", variable),
                    ("function_head", head),
                    ("free_variable_absent", "true"),
                    ("function_dependency", "nondependent_arrow"),
                ),
            )
        )
    return tuple(sorted(sites, key=lambda site: (site.start, site.end, site.stable_key)))


def apply_eta_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    """Apply one exact P13 span edit, failing closed on trace corruption."""

    try:
        return apply_presentation_trace(source, trace)
    except V2E0RuleError as exc:
        raise P13RestrictedEtaError(str(exc)) from exc


def _expected_structural_diff(site: PresentationSite) -> dict[str, JsonValue]:
    metadata = dict(site.metadata)
    return {
        "eta_step_count": 1,
        "evidence_class": "E1",
        "free_variable_absent": metadata["free_variable_absent"],
        "function_dependency": metadata["function_dependency"],
        "function_head": metadata["function_head"],
        "operation": site.operation,
        "source_span_end": site.end,
        "source_span_start": site.start,
    }


class P13RestrictedEtaRule:
    """One certified local eta contraction; outputs remain provisional."""

    polarity = Polarity.POSITIVE
    rule_id = "p13_restricted_eta"
    family_id = "p13_restricted_eta"
    implementation_key = "p13_restricted_eta"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise P13RestrictedEtaError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise P13RestrictedEtaError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "v2_e1_p13_restricted_eta_audit_v1",
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
        for view in ("signature_explicit", "operator_tree"):
            if representation.view_status[view] != ViewStatus.OK:
                reasons.append(f"source_{view}_missing")
        try:
            sites = enumerate_p13_sites(theorem.proof_stripped_declaration)
        except P13RestrictedEtaError as exc:
            reasons.append(str(exc))
            sites = ()
        if not sites:
            reasons.append("no_eligible_eta_redex")
        elif len(sites) != 1:
            reasons.append("eta_redex_not_unique")
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
            matched_nodes=(f"span:{site.start}:{site.end}:{site.operation}",),
            required_capabilities=_REQUIRED_CAPABILITIES,
            metadata={"eligible_site_count": 1},
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        applicability = self.assess(theorem, representation)
        if not applicability.applicable:
            return ()
        site = _choose_site(
            enumerate_p13_sites(theorem.proof_stripped_declaration),
            rule_id=self.rule_id,
            theorem_id=theorem.theorem_id,
            seed=seed,
        )
        forward = _site_trace(
            site,
            rule_id=self.rule_id,
            generation_config_hash=self.generation_config_hash,
        )
        inverse = _inverse_trace(
            site,
            rule_id=self.rule_id,
            generation_config_hash=self.generation_config_hash,
        )
        candidate = apply_eta_trace(theorem.proof_stripped_declaration, forward)
        if apply_eta_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise P13RestrictedEtaError("internal_inverse_replay_failure")
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
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={"generation_intention_only": True},
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
        if source_representation.raw_proof_stripped != source.proof_stripped_declaration:
            violations.append("source_representation_text_mismatch")
        if candidate.proof_stripped_declaration != draft.candidate_code:
            violations.append("candidate_code_mismatch")
        if candidate_representation.raw_proof_stripped != candidate.proof_stripped_declaration:
            violations.append("candidate_representation_text_mismatch")

        site_contract_ok = False
        forward_ok = False
        inverse_ok = False
        try:
            source_sites = enumerate_p13_sites(source.proof_stripped_declaration)
            matching_sites = tuple(
                site
                for site in source_sites
                if len(source_sites) == 1
                and _site_trace(
                    site,
                    rule_id=self.rule_id,
                    generation_config_hash=self.generation_config_hash,
                )
                == draft.transformation_trace
                and _inverse_trace(
                    site,
                    rule_id=self.rule_id,
                    generation_config_hash=self.generation_config_hash,
                )
                == draft.inverse_trace
                and _expected_structural_diff(site) == draft.expected_structural_diff
            )
            site_contract_ok = len(matching_sites) == 1
            forward_ok = (
                apply_eta_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_eta_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (P13RestrictedEtaError, V2E0RuleError):
            pass
        if not site_contract_ok:
            violations.append("eta_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        if source.elaboration_status not in _VALID_ELABORATION:
            violations.append("source_does_not_elaborate")
        if candidate.elaboration_status not in _VALID_ELABORATION:
            violations.append("candidate_does_not_elaborate")
        for side, representation in (
            ("source", source_representation),
            ("candidate", candidate_representation),
        ):
            for view in ("signature_explicit", "operator_tree"):
                if representation.view_status[view] != ViewStatus.OK:
                    violations.append(f"{side}_{view}_missing")

        structural_ok = (
            site_contract_ok
            and forward_ok
            and inverse_ok
            and draft.candidate_code != source.proof_stripped_declaration
        )
        clean = not violations
        return build_transformation_audit(
            draft=draft,
            applicability=Applicability(
                applicable=True,
                reason_codes=(),
                matched_nodes=("p13_single_certified_eta_redex",),
                required_capabilities=_REQUIRED_CAPABILITIES,
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status if clean else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(QualityTier.PROVISIONAL if clean else QualityTier.UNKNOWN),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=structural_ok,
            atom_mapping_ok=None,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "evidence_class": "E1",
                "eta_step_count": 1,
                "free_variable_certificate_ok": site_contract_ok,
                "general_reduction_invoked": False,
                "resolved_semantic_label": False,
                "same_context_elaboration_ok": (
                    source.elaboration_status in _VALID_ELABORATION
                    and candidate.elaboration_status in _VALID_ELABORATION
                    and source.context_id
                    == source_representation.context_id
                    == candidate.context_id
                    == candidate_representation.context_id
                    == draft.context_id
                ),
                "training_eligible": False,
            },
        )


__all__ = [
    "P13RestrictedEtaError",
    "P13RestrictedEtaRule",
    "apply_eta_trace",
    "enumerate_p13_sites",
]
