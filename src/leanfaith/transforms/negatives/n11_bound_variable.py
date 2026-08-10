"""LF-034 N11: one same-typed bound-variable substitution candidate.

N11 changes exactly one source-visible occurrence of an explicit theorem
binder to a distinct explicit binder with the same surface type.  The edit is
only a *negative candidate*: the audit must independently observe one and only
one elaborated ``bvar`` index delta, prove that both referenced binders have
the same alpha-normalized domain, and preserve every semantic atom.

The family deliberately performs no proof search and creates no semantic
label.  A clean result remains provisional, unpromoted, and ineligible for
training until one of PLAN.md section 15.7's existing promotion routes applies.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from pydantic import JsonValue

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
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
from leanfaith.transforms.positives.v2_e0 import (
    V2E0RuleError,
    _signature_bounds,
    apply_presentation_trace,
)
from leanfaith.transforms.protocol import (
    build_transformation_audit,
    build_variant_draft,
    verify_variant_draft_id,
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_REQUIRED_CAPABILITIES = (
    "exact_inverse_replay",
    "same_context_reelaboration",
    "same_typed_binder_certificate",
    "single_bvar_delta_certificate",
)


class N11BoundVariableError(ValueError):
    """An N11 source, trace, or structural-delta invariant failed closed."""


@dataclass(frozen=True, slots=True)
class BoundVariableSite:
    """One exact conclusion occurrence and its same-typed replacement."""

    start: int
    end: int
    source_name: str
    target_name: str
    type_token_hash: str
    source_binder_index: int
    target_binder_index: int

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "start": self.start,
                "end": self.end,
                "source_name": self.source_name,
                "target_name": self.target_name,
                "type_token_hash": self.type_token_hash,
                "source_binder_index": self.source_binder_index,
                "target_binder_index": self.target_binder_index,
            }
        )


@dataclass(frozen=True, slots=True)
class BVarDeltaCertificate:
    """The unique elaborated bvar replacement observed by an N11 audit."""

    path: str
    source_index: int
    target_index: int
    domain_hash: str
    source_binder_info: str
    target_binder_info: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "source_index": self.source_index,
            "target_index": self.target_index,
            "domain_hash": self.domain_hash,
            "source_binder_info": self.source_binder_info,
            "target_binder_info": self.target_binder_info,
        }


@dataclass(frozen=True, slots=True)
class _FlatBinder:
    index: int
    name: str
    group: TypedBinder


@dataclass(frozen=True, slots=True)
class _ScopeEntry:
    domain: dict[str, Any]
    binder_info: str
    origin: str


def _flat_explicit_binders(source: str) -> tuple[_FlatBinder, ...]:
    binders = parse_typed_binders(source)
    all_names = {name for binder in binders for name in binder.names}
    flat: list[_FlatBinder] = []
    for binder in binders:
        if (
            binder.kind != BinderKind.EXPLICIT
            or binder.has_comment
            or binder.type_mentions_group_name
            or all_names.intersection(binder.type_tokens)
        ):
            continue
        for name in binder.names:
            if _IDENTIFIER.fullmatch(name) is None:
                continue
            flat.append(_FlatBinder(index=len(flat), name=name, group=binder))
    return tuple(flat)


def _identifier_occurrences(
    mask: str,
    start: int,
    end: int,
    name: str,
) -> tuple[tuple[int, int], ...]:
    pattern = re.compile(rf"(?<![A-Za-z0-9_'.]){re.escape(name)}(?![A-Za-z0-9_'])")
    return tuple(
        (start + match.start(), start + match.end()) for match in pattern.finditer(mask[start:end])
    )


def enumerate_n11_sites(source: str) -> tuple[BoundVariableSite, ...]:
    """Enumerate exact one-token substitutions in the theorem conclusion.

    This is intentionally a conservative surface proposal.  It excludes
    implicit/instance/dependent binders and comments/quoted tokens.  The later
    audit, not this scanner, is authoritative about the elaborated delta.
    """

    try:
        mask, conclusion_start, conclusion_end = _signature_bounds(source)
        binders = _flat_explicit_binders(source)
    except (BinderParseError, V2E0RuleError) as exc:
        raise N11BoundVariableError(str(exc)) from exc

    by_type: dict[tuple[str, ...], list[_FlatBinder]] = {}
    for binder in binders:
        by_type.setdefault(binder.group.type_tokens, []).append(binder)

    sites: list[BoundVariableSite] = []
    for same_typed in by_type.values():
        if len(same_typed) < 2:
            continue
        for source_binder in same_typed:
            for occurrence_start, occurrence_end in _identifier_occurrences(
                mask,
                conclusion_start,
                conclusion_end,
                source_binder.name,
            ):
                for target_binder in same_typed:
                    if target_binder.name == source_binder.name:
                        continue
                    sites.append(
                        BoundVariableSite(
                            start=occurrence_start,
                            end=occurrence_end,
                            source_name=source_binder.name,
                            target_name=target_binder.name,
                            type_token_hash=hash_canonical(
                                {"lean_type_tokens": source_binder.group.type_tokens}
                            ),
                            source_binder_index=source_binder.index,
                            target_binder_index=target_binder.index,
                        )
                    )
    return tuple(
        sorted(
            sites,
            key=lambda site: (
                site.start,
                site.end,
                site.source_name,
                site.target_name,
                site.stable_key,
            ),
        )
    )


def _site_for_seed(
    sites: tuple[BoundVariableSite, ...],
    *,
    theorem_id: str,
    seed: int,
) -> BoundVariableSite:
    if not sites:
        raise N11BoundVariableError("no_eligible_bound_variable_site")
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "n11_site_selection_v1",
                "rule_id": "n11_bound_variable_substitution",
                "theorem_id": theorem_id,
                "seed": seed,
            }
        )
    ).digest()
    return sites[int.from_bytes(digest[:8], "big") % len(sites)]


def _trace(
    site: BoundVariableSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    source_name = site.target_name if inverse else site.source_name
    target_name = site.source_name if inverse else site.target_name
    return (
        {
            "operation": "replace_exact_span",
            "n11_operation": (
                "inverse_bound_variable_substitution" if inverse else "bound_variable_substitution"
            ),
            "start": site.start,
            "end": site.start + len(source_name),
            "expected_text": source_name,
            "replacement_text": target_name,
            "source_binder_index": (
                site.target_binder_index if inverse else site.source_binder_index
            ),
            "target_binder_index": (
                site.source_binder_index if inverse else site.target_binder_index
            ),
            "type_token_hash": site.type_token_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_n11_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    """Replay one exact N11 token substitution."""

    try:
        return apply_presentation_trace(source, trace)
    except V2E0RuleError as exc:
        raise N11BoundVariableError(str(exc)) from exc


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise N11BoundVariableError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def certify_single_bvar_delta(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
) -> BVarDeltaCertificate:
    """Require exact tree identity except for one same-typed explicit bvar.

    Binder scope is tracked while descending both trees synchronously.  A
    bvar index resolves against the innermost scope entry; both selected scope
    entries must be explicit ``forall`` binders with identical canonical
    domains.  No unfolding, simplification, or proof search is performed.
    """

    deltas: list[BVarDeltaCertificate] = []

    def visit(
        left: object,
        right: object,
        scope_left: tuple[_ScopeEntry, ...],
        scope_right: tuple[_ScopeEntry, ...],
        path: str,
    ) -> None:
        if not isinstance(left, dict) or not isinstance(right, dict):
            if left != right:
                raise N11BoundVariableError("non_bvar_structural_delta")
            return
        if left.get("k") != right.get("k"):
            raise N11BoundVariableError("node_kind_delta")
        kind = left.get("k")
        if kind == "bvar":
            left_index = left.get("i")
            right_index = right.get("i")
            if not isinstance(left_index, int) or not isinstance(right_index, int):
                raise N11BoundVariableError("malformed_bvar")
            if set(left) != set(right) or any(left[key] != right[key] for key in set(left) - {"i"}):
                raise N11BoundVariableError("bvar_metadata_delta")
            if left_index == right_index:
                return
            if left_index >= len(scope_left) or right_index >= len(scope_right):
                raise N11BoundVariableError("bvar_out_of_scope")
            left_entry = scope_left[-1 - left_index]
            right_entry = scope_right[-1 - right_index]
            if left_entry.origin != "forall" or right_entry.origin != "forall":
                raise N11BoundVariableError("non_forall_binder_substitution")
            if left_entry.binder_info != "default" or right_entry.binder_info != "default":
                raise N11BoundVariableError("non_explicit_binder_substitution")
            left_domain_bytes = alpha_canonical_bytes(left_entry.domain)
            right_domain_bytes = alpha_canonical_bytes(right_entry.domain)
            if left_domain_bytes != right_domain_bytes:
                raise N11BoundVariableError("binder_domain_delta")
            deltas.append(
                BVarDeltaCertificate(
                    path=path,
                    source_index=left_index,
                    target_index=right_index,
                    domain_hash=hashlib.sha256(left_domain_bytes).hexdigest(),
                    source_binder_info=left_entry.binder_info,
                    target_binder_info=right_entry.binder_info,
                )
            )
            return

        if set(left) != set(right):
            raise N11BoundVariableError("node_key_delta")
        if kind in {"forall", "lam"}:
            for key in set(left) - {"dom", "body"}:
                if left[key] != right[key]:
                    raise N11BoundVariableError("binder_metadata_delta")
            left_domain_node = left.get("dom")
            right_domain_node = right.get("dom")
            if not isinstance(left_domain_node, dict) or not isinstance(right_domain_node, dict):
                raise N11BoundVariableError("binder_domain_missing")
            visit(left_domain_node, right_domain_node, scope_left, scope_right, f"{path}/dom")
            binder_info = str(left.get("bi", ""))
            right_binder_info = str(right.get("bi", ""))
            visit(
                left.get("body"),
                right.get("body"),
                (*scope_left, _ScopeEntry(left_domain_node, binder_info, str(kind))),
                (*scope_right, _ScopeEntry(right_domain_node, right_binder_info, str(kind))),
                f"{path}/body",
            )
            return
        if kind == "let":
            for key in set(left) - {"t", "v", "body"}:
                if left[key] != right[key]:
                    raise N11BoundVariableError("let_metadata_delta")
            left_type = left.get("t")
            right_type = right.get("t")
            if not isinstance(left_type, dict) or not isinstance(right_type, dict):
                raise N11BoundVariableError("let_type_missing")
            visit(left_type, right_type, scope_left, scope_right, f"{path}/t")
            visit(left.get("v"), right.get("v"), scope_left, scope_right, f"{path}/v")
            visit(
                left.get("body"),
                right.get("body"),
                (*scope_left, _ScopeEntry(left_type, "let", "let")),
                (*scope_right, _ScopeEntry(right_type, "let", "let")),
                f"{path}/body",
            )
            return

        child_keys = {"fn", "arg", "base"}
        for key in set(left):
            if key in child_keys and isinstance(left[key], dict) and isinstance(right[key], dict):
                visit(left[key], right[key], scope_left, scope_right, f"{path}/{key}")
            elif left[key] != right[key]:
                raise N11BoundVariableError("non_bvar_structural_delta")

    visit(source_root, candidate_root, (), (), "root")
    if len(deltas) != 1:
        raise N11BoundVariableError("expected_exactly_one_bvar_delta")
    return deltas[0]


def _expected_structural_diff(site: BoundVariableSite) -> dict[str, JsonValue]:
    return {
        "delta_count": 1,
        "delta_kind": "same_typed_bound_variable_substitution",
        "evidence_class": "D0",
        "source_binder_index": site.source_binder_index,
        "source_name": site.source_name,
        "target_binder_index": site.target_binder_index,
        "target_name": site.target_name,
        "type_token_hash": site.type_token_hash,
    }


class N11BoundVariableRule:
    """One elaboration-audited D0 mutation; never a resolved negative."""

    polarity = Polarity.NEGATIVE
    rule_id = "n11_bound_variable_substitution"
    family_id = "n11_bound_variable_substitution"
    implementation_key = "n11_bound_variable_substitution"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise N11BoundVariableError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise N11BoundVariableError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "n11_bound_variable_audit_v1",
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
        try:
            sites = enumerate_n11_sites(theorem.proof_stripped_declaration)
        except N11BoundVariableError as exc:
            reasons.append(str(exc))
            sites = ()
        if not sites:
            reasons.append("no_eligible_bound_variable_site")
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
                sorted(
                    f"span:{site.start}:{site.end}:{site.source_name}:{site.target_name}"
                    for site in sites
                )
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
            enumerate_n11_sites(theorem.proof_stripped_declaration),
            theorem_id=theorem.theorem_id,
            seed=seed,
        )
        forward = _trace(site, generation_config_hash=self.generation_config_hash, inverse=False)
        inverse = _trace(site, generation_config_hash=self.generation_config_hash, inverse=True)
        candidate = apply_n11_trace(theorem.proof_stripped_declaration, forward)
        if apply_n11_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N11BoundVariableError("internal_inverse_replay_failure")
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
                intended_error_types=("E16", "E26"),
                candidate_pool=self.candidate_pool,
                transformation_trace=forward,
                inverse_trace=inverse,
                expected_structural_diff=_expected_structural_diff(site),
                generation_config_hash=self.generation_config_hash,
                metadata={
                    "generation_intention_only": True,
                    "near_miss": True,
                    "resolved_semantic_label": False,
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
        if source_representation.raw_proof_stripped != source.proof_stripped_declaration:
            violations.append("source_representation_text_mismatch")
        if candidate.proof_stripped_declaration != draft.candidate_code:
            violations.append("candidate_code_mismatch")
        if candidate_representation.raw_proof_stripped != candidate.proof_stripped_declaration:
            violations.append("candidate_representation_text_mismatch")
        if source.elaboration_status not in _VALID_ELABORATION:
            violations.append("source_does_not_elaborate")
        if candidate.elaboration_status not in _VALID_ELABORATION:
            violations.append("candidate_does_not_elaborate")
        for side, representation in (
            ("source", source_representation),
            ("candidate", candidate_representation),
        ):
            for view in ("operator_tree", "semantic_atoms", "signature_explicit"):
                if representation.view_status[view] != ViewStatus.OK:
                    violations.append(f"{side}_{view}_missing")

        site_ok = False
        forward_ok = False
        inverse_ok = False
        try:
            sites = enumerate_n11_sites(source.proof_stripped_declaration)
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
            site_ok = len(matching) == 1
            forward_ok = (
                apply_n11_trace(source.proof_stripped_declaration, draft.transformation_trace)
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace is not None
                and apply_n11_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
        except (N11BoundVariableError, V2E0RuleError):
            pass
        if not site_ok:
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        delta: BVarDeltaCertificate | None = None
        try:
            delta = certify_single_bvar_delta(
                _operator_root(source_representation),
                _operator_root(candidate_representation),
            )
        except N11BoundVariableError as exc:
            violations.append(str(exc))
        atoms_ok = (
            source_representation.semantic_atoms is not None
            and source_representation.semantic_atoms == candidate_representation.semantic_atoms
        )
        if not atoms_ok:
            violations.append("semantic_atoms_changed")
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
                matched_nodes=("n11_single_same_typed_bvar_delta",),
                required_capabilities=_REQUIRED_CAPABILITIES,
            ),
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=(
                candidate.elaboration_status if clean else ValidationStatus.QUARANTINED
            ),
            recommended_quality_tier=(QualityTier.PROVISIONAL if clean else QualityTier.UNKNOWN),
            candidate_theorem_id=candidate.theorem_id,
            candidate_representation_id=candidate_representation.representation_id,
            structural_diff_ok=delta is not None,
            atom_mapping_ok=atoms_ok,
            inverse_or_roundtrip_ok=inverse_ok,
            violation_codes=tuple(sorted(set(violations))),
            metadata={
                "bvar_delta_certificate": (
                    hash_canonical(delta.as_json()) if delta is not None else None
                ),
                "evidence_class": "D0",
                "failed_proof_search_used": False,
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
    "BVarDeltaCertificate",
    "BoundVariableSite",
    "N11BoundVariableError",
    "N11BoundVariableRule",
    "apply_n11_trace",
    "certify_single_bvar_delta",
    "enumerate_n11_sites",
]
