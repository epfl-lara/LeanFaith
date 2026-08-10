"""LF-034 N12: swap one root proposition hypothesis with its conclusion.

The initial executable scope is deliberately narrower than arbitrary
implication converse. It accepts declarations whose final explicit header
binder is ``(h : P)`` and whose conclusion is ``Q``, where ``P`` and ``Q`` are
distinct, earlier, explicitly declared proposition variables. The elaborated
type must be exactly the corresponding outer-forall chain and neither side may
depend on ``h``.

Generation changes ``(h : P) : Q`` to ``(h : Q) : P``. The audit independently
reconstructs the only permitted expression-tree delta, including the required
de-Bruijn shifts when moving a subtree across the proof binder. This is D0
structural evidence only: no proof search, semantic label, promotion, or
training eligibility is produced.
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
from leanfaith.transforms.negatives.n03_drop_hypothesis import (
    N03DropHypothesisError,
    analyze_outer_foralls,
)
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

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_']*")
_VALID_ELABORATION = frozenset(
    {
        ValidationStatus.ELABORATES,
        ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    }
)
_REQUIRED_CAPABILITIES = (
    "exact_inverse_replay",
    "exact_root_converse_certificate",
    "same_context_reelaboration",
    "surface_expr_alignment",
)


class N12ImplicationConverseError(ValueError):
    """An N12 source, trace, or structural certificate failed closed."""


@dataclass(frozen=True, slots=True)
class _SurfaceBinder:
    outer_index: int
    group: TypedBinder
    name: str


@dataclass(frozen=True, slots=True)
class ImplicationConverseSite:
    """The exact surface and elaborated binding for one N12 mutation."""

    hypothesis_type_start: int
    hypothesis_type_end: int
    conclusion_start: int
    conclusion_end: int
    hypothesis_name: str
    premise_name: str
    conclusion_name: str
    hypothesis_outer_index: int
    premise_outer_index: int
    conclusion_outer_index: int
    source_root_hash: str
    expected_candidate_root_hash: str

    @property
    def stable_key(self) -> str:
        return hash_canonical(
            {
                "hypothesis_type_start": self.hypothesis_type_start,
                "hypothesis_type_end": self.hypothesis_type_end,
                "conclusion_start": self.conclusion_start,
                "conclusion_end": self.conclusion_end,
                "hypothesis_name": self.hypothesis_name,
                "premise_name": self.premise_name,
                "conclusion_name": self.conclusion_name,
                "hypothesis_outer_index": self.hypothesis_outer_index,
                "premise_outer_index": self.premise_outer_index,
                "conclusion_outer_index": self.conclusion_outer_index,
                "source_root_hash": self.source_root_hash,
                "expected_candidate_root_hash": self.expected_candidate_root_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class ImplicationConverseCertificate:
    hypothesis_outer_index: int
    source_root_hash: str
    candidate_root_hash: str
    premise_subtree_hash: str
    conclusion_subtree_hash: str

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "hypothesis_outer_index": self.hypothesis_outer_index,
            "source_root_hash": self.source_root_hash,
            "candidate_root_hash": self.candidate_root_hash,
            "premise_subtree_hash": self.premise_subtree_hash,
            "conclusion_subtree_hash": self.conclusion_subtree_hash,
        }


def _operator_root(record: RepresentationRecord) -> dict[str, Any]:
    tree = record.operator_tree
    if not isinstance(tree, dict) or not isinstance(tree.get("root"), dict):
        raise N12ImplicationConverseError("operator_tree_missing_root")
    return cast(dict[str, Any], tree["root"])


def _surface_binders(source: str) -> tuple[_SurfaceBinder, ...]:
    try:
        groups = parse_typed_binders(source)
    except BinderParseError as exc:
        raise N12ImplicationConverseError(str(exc)) from exc
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


def _shift_outer_bvars(node: object, delta: int, *, cutoff: int = 0) -> object:
    """Shift references crossing a removed or inserted surrounding binder."""

    if isinstance(node, list):
        return [_shift_outer_bvars(item, delta, cutoff=cutoff) for item in node]
    if not isinstance(node, dict):
        return node
    kind = node.get("k")
    if kind == "bvar":
        raw_index = node.get("i")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            raise N12ImplicationConverseError("malformed_bvar_index")
        if raw_index < cutoff:
            return dict(node)
        shifted = raw_index + delta
        if shifted < cutoff:
            raise N12ImplicationConverseError("conclusion_depends_on_hypothesis")
        return {**node, "i": shifted}
    result: dict[str, object] = {}
    for key, value in node.items():
        child_cutoff = cutoff
        if kind in {"forall", "lam", "let"} and key == "body":
            child_cutoff += 1
        result[key] = _shift_outer_bvars(value, delta, cutoff=child_cutoff)
    return result


def _replace_last_forall(
    root: dict[str, Any],
    hypothesis_outer_index: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return candidate root, source premise, and source conclusion."""

    def visit(
        node: dict[str, Any],
        index: int,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if node.get("k") != "forall":
            raise N12ImplicationConverseError("hypothesis_outer_index_out_of_range")
        domain = node.get("dom")
        body = node.get("body")
        if not isinstance(domain, dict) or not isinstance(body, dict):
            raise N12ImplicationConverseError("malformed_forall")
        if index == hypothesis_outer_index:
            if body.get("k") == "forall":
                raise N12ImplicationConverseError("nested_implication_or_quantifier")
            candidate_domain = _shift_outer_bvars(body, -1)
            candidate_body = _shift_outer_bvars(domain, 1)
            if not isinstance(candidate_domain, dict) or not isinstance(candidate_body, dict):
                raise N12ImplicationConverseError("malformed_converse_subtree")
            return (
                {**node, "dom": candidate_domain, "body": candidate_body},
                domain,
                body,
            )
        candidate_body, premise, conclusion = visit(body, index + 1)
        return {**node, "body": candidate_body}, premise, conclusion

    return visit(root, 0)


def build_implication_converse_root(
    source_root: dict[str, Any],
    hypothesis_outer_index: int,
) -> dict[str, Any]:
    """Build the exact expression tree permitted by N12."""

    candidate, premise, conclusion = _replace_last_forall(
        source_root,
        hypothesis_outer_index,
    )
    lowered_conclusion = _shift_outer_bvars(conclusion, -1)
    if not isinstance(lowered_conclusion, dict):
        raise N12ImplicationConverseError("malformed_conclusion_subtree")
    if alpha_canonical_bytes(premise) == alpha_canonical_bytes(lowered_conclusion):
        raise N12ImplicationConverseError("proposition_identical_sides")
    return candidate


def certify_implication_converse(
    source_root: dict[str, Any],
    candidate_root: dict[str, Any],
    hypothesis_outer_index: int,
) -> ImplicationConverseCertificate:
    """Certify that the candidate is exactly the permitted root converse."""

    expected, premise, conclusion = _replace_last_forall(
        source_root,
        hypothesis_outer_index,
    )
    if alpha_canonical_bytes(expected) != alpha_canonical_bytes(candidate_root):
        raise N12ImplicationConverseError("candidate_not_exact_root_converse")
    return ImplicationConverseCertificate(
        hypothesis_outer_index=hypothesis_outer_index,
        source_root_hash=hash_canonical(source_root),
        candidate_root_hash=hash_canonical(candidate_root),
        premise_subtree_hash=hash_canonical(premise),
        conclusion_subtree_hash=hash_canonical(conclusion),
    )


def _direct_prop_names(surface: tuple[_SurfaceBinder, ...]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in surface:
        if (
            item.group.kind == BinderKind.EXPLICIT
            and not item.group.has_comment
            and item.group.type_tokens == ("Prop",)
        ):
            result[item.name] = item.outer_index
    return result


def _is_iff(node: dict[str, Any]) -> bool:
    current: object = node
    while isinstance(current, dict) and current.get("k") == "app":
        current = current.get("fn")
    return isinstance(current, dict) and current.get("k") == "const" and current.get("n") == "Iff"


def enumerate_n12_sites(
    source: str,
    operator_tree_view: dict[str, Any],
) -> tuple[ImplicationConverseSite, ...]:
    """Return the unique narrow N12 site, or no site when an invariant fails."""

    try:
        mask, conclusion_start_raw, conclusion_end_raw = _signature_bounds(source)
        groups = parse_typed_binders(source)
        surface = _surface_binders(source)
        analysis = analyze_outer_foralls(operator_tree_view)
    except (
        V2E0RuleError,
        BinderParseError,
        N03DropHypothesisError,
        N12ImplicationConverseError,
    ):
        return ()
    if not surface or len(surface) != len(analysis.binders):
        return ()
    hypothesis = surface[-1]
    if (
        hypothesis.group.kind != BinderKind.EXPLICIT
        or hypothesis.group.has_comment
        or len(hypothesis.group.names) != 1
        or not groups
        or hypothesis.group != groups[-1]
        or analysis.binders[-1].binder_info != "default"
    ):
        return ()
    prop_names = _direct_prop_names(surface[:-1])
    if len(hypothesis.group.type_tokens) != 1:
        return ()
    premise_name = hypothesis.group.type_tokens[0]
    conclusion_text = source[conclusion_start_raw:conclusion_end_raw]
    leading = len(conclusion_text) - len(conclusion_text.lstrip())
    trailing = len(conclusion_text) - len(conclusion_text.rstrip())
    conclusion_start = conclusion_start_raw + leading
    conclusion_end = conclusion_end_raw - trailing
    conclusion_name = source[conclusion_start:conclusion_end]
    if (
        _IDENTIFIER.fullmatch(premise_name) is None
        or _IDENTIFIER.fullmatch(conclusion_name) is None
        or premise_name == conclusion_name
        or premise_name not in prop_names
        or conclusion_name not in prop_names
        or _is_iff(cast(dict[str, Any], analysis.conclusion))
    ):
        return ()

    hypothesis_outer_index = hypothesis.outer_index
    premise_outer_index = prop_names[premise_name]
    conclusion_outer_index = prop_names[conclusion_name]
    domain = analysis.binders[-1].domain
    conclusion = analysis.conclusion
    expected_domain_index = hypothesis_outer_index - 1 - premise_outer_index
    expected_conclusion_index = hypothesis_outer_index - conclusion_outer_index
    if domain != {"k": "bvar", "i": expected_domain_index}:
        return ()
    if conclusion != {"k": "bvar", "i": expected_conclusion_index}:
        return ()

    type_offset = hypothesis.group.original_text.find(hypothesis.group.type_text)
    if type_offset < 0 or hypothesis.group.original_text.count(hypothesis.group.type_text) != 1:
        return ()
    hypothesis_type_start = hypothesis.group.start + type_offset
    hypothesis_type_end = hypothesis_type_start + len(hypothesis.group.type_text)
    if mask[hypothesis_type_start:hypothesis_type_end] != hypothesis.group.type_text:
        return ()
    try:
        root = cast(dict[str, Any], operator_tree_view["root"])
        expected = build_implication_converse_root(root, hypothesis_outer_index)
    except (KeyError, TypeError, N12ImplicationConverseError):
        return ()
    return (
        ImplicationConverseSite(
            hypothesis_type_start=hypothesis_type_start,
            hypothesis_type_end=hypothesis_type_end,
            conclusion_start=conclusion_start,
            conclusion_end=conclusion_end,
            hypothesis_name=hypothesis.name,
            premise_name=premise_name,
            conclusion_name=conclusion_name,
            hypothesis_outer_index=hypothesis_outer_index,
            premise_outer_index=premise_outer_index,
            conclusion_outer_index=conclusion_outer_index,
            source_root_hash=hash_canonical(root),
            expected_candidate_root_hash=hash_canonical(expected),
        ),
    )


def _trace(
    site: ImplicationConverseSite,
    *,
    generation_config_hash: str,
    inverse: bool,
) -> tuple[dict[str, JsonValue], ...]:
    left_text = site.premise_name
    right_text = site.conclusion_name
    left_start = site.hypothesis_type_start
    right_start = site.conclusion_start
    if inverse:
        right_start = site.conclusion_start + len(right_text) - len(left_text)
        left_text, right_text = right_text, left_text
    return (
        {
            "operation": "swap_exact_spans",
            "n12_operation": (
                "inverse_implication_converse" if inverse else "implication_converse"
            ),
            "left_start": left_start,
            "left_end": left_start + len(left_text),
            "left_text": left_text,
            "right_start": right_start,
            "right_end": right_start + len(right_text),
            "right_text": right_text,
            "hypothesis_outer_index": site.hypothesis_outer_index,
            "source_root_hash": site.source_root_hash,
            "expected_candidate_root_hash": site.expected_candidate_root_hash,
            "generation_config_hash": generation_config_hash,
        },
    )


def apply_n12_trace(source: str, trace: tuple[dict[str, JsonValue], ...]) -> str:
    """Apply one exact two-span swap without search or global replacement."""

    if len(trace) != 1:
        raise N12ImplicationConverseError("expected_one_swap_trace")
    step = trace[0]
    if step.get("operation") != "swap_exact_spans":
        raise N12ImplicationConverseError("unsupported_trace_operation")
    values = tuple(step.get(key) for key in ("left_start", "left_end", "right_start", "right_end"))
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        raise N12ImplicationConverseError("invalid_trace_span")
    left_start, left_end, right_start, right_end = cast(tuple[int, int, int, int], values)
    left_text = step.get("left_text")
    right_text = step.get("right_text")
    if not isinstance(left_text, str) or not isinstance(right_text, str):
        raise N12ImplicationConverseError("invalid_trace_text")
    if not 0 <= left_start < left_end <= right_start < right_end <= len(source):
        raise N12ImplicationConverseError("invalid_or_overlapping_trace_spans")
    if source[left_start:left_end] != left_text or source[right_start:right_end] != right_text:
        raise N12ImplicationConverseError("trace_expected_text_mismatch")
    return (
        source[:left_start]
        + right_text
        + source[left_end:right_start]
        + left_text
        + source[right_end:]
    )


def _expected_structural_diff(site: ImplicationConverseSite) -> dict[str, JsonValue]:
    return {
        "delta_kind": "root_implication_converse",
        "evidence_class": "D0",
        "hypothesis_outer_index": site.hypothesis_outer_index,
        "premise_outer_index": site.premise_outer_index,
        "conclusion_outer_index": site.conclusion_outer_index,
        "source_root_hash": site.source_root_hash,
        "expected_candidate_root_hash": site.expected_candidate_root_hash,
    }


class N12ImplicationConverseRule:
    """One exact D0 root-converse mutation; never a resolved negative."""

    polarity = Polarity.NEGATIVE
    rule_id = "n12_implication_converse"
    family_id = "n12_implication_converse"
    implementation_key = "n12_implication_converse"
    rule_version = "1.0.0"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", generation_config_hash) is None:
            raise N12ImplicationConverseError("generation_config_hash must be SHA-256 hex")
        if not candidate_pool.strip():
            raise N12ImplicationConverseError("candidate_pool must be nonempty")
        self.generation_config_hash = generation_config_hash
        self.candidate_pool = candidate_pool
        self.audit_config_hash = hash_canonical(
            {
                "schema": "n12_implication_converse_audit_v1",
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
            enumerate_n12_sites(
                theorem.proof_stripped_declaration,
                cast(dict[str, Any], representation.operator_tree),
            )
            if representation.operator_tree is not None
            else ()
        )
        if len(sites) != 1:
            reasons.append("no_unique_root_implication_converse_site")
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
            matched_nodes=(
                f"root_converse:{site.hypothesis_outer_index}:"
                f"{site.premise_name}:{site.conclusion_name}",
            ),
            required_capabilities=_REQUIRED_CAPABILITIES,
        )

    def generate(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
        seed: int,
    ) -> tuple[VariantDraft, ...]:
        if not self.assess(theorem, representation).applicable:
            return ()
        (site,) = enumerate_n12_sites(
            theorem.proof_stripped_declaration,
            cast(dict[str, Any], representation.operator_tree),
        )
        forward = _trace(
            site,
            generation_config_hash=self.generation_config_hash,
            inverse=False,
        )
        inverse = _trace(
            site,
            generation_config_hash=self.generation_config_hash,
            inverse=True,
        )
        candidate = apply_n12_trace(theorem.proof_stripped_declaration, forward)
        if apply_n12_trace(candidate, inverse) != theorem.proof_stripped_declaration:
            raise N12ImplicationConverseError("internal_inverse_replay_failure")
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
                intended_error_types=("E26", "E30"),
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

        site: ImplicationConverseSite | None = None
        forward_ok = False
        inverse_ok = False
        try:
            (site,) = enumerate_n12_sites(
                source.proof_stripped_declaration,
                cast(dict[str, Any], source_representation.operator_tree),
            )
            forward_ok = (
                _trace(
                    site,
                    generation_config_hash=self.generation_config_hash,
                    inverse=False,
                )
                == draft.transformation_trace
                and apply_n12_trace(
                    source.proof_stripped_declaration,
                    draft.transformation_trace,
                )
                == draft.candidate_code
            )
            inverse_ok = (
                draft.inverse_trace
                == _trace(
                    site,
                    generation_config_hash=self.generation_config_hash,
                    inverse=True,
                )
                and draft.inverse_trace is not None
                and apply_n12_trace(draft.candidate_code, draft.inverse_trace)
                == source.proof_stripped_declaration
            )
            if _expected_structural_diff(site) != draft.expected_structural_diff:
                violations.append("expected_structural_diff_mismatch")
        except (ValueError, TypeError, N12ImplicationConverseError):
            violations.append("site_certificate_mismatch")
        if not forward_ok:
            violations.append("forward_trace_failed")
        if not inverse_ok:
            violations.append("inverse_replay_failed")

        certificate: ImplicationConverseCertificate | None = None
        if site is not None:
            try:
                certificate = certify_implication_converse(
                    _operator_root(source_representation),
                    _operator_root(candidate_representation),
                    site.hypothesis_outer_index,
                )
            except N12ImplicationConverseError as exc:
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
                matched_nodes=("n12_exact_root_implication_converse",),
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
                "converse_certificate": (
                    hash_canonical(certificate.as_json()) if certificate is not None else None
                ),
                "evidence_class": "D0",
                "failed_proof_search_used": False,
                "resolved_semantic_label": False,
                "semantic_atoms_changed": (
                    source_representation.semantic_atoms != candidate_representation.semantic_atoms
                    if atoms_present
                    else None
                ),
                "training_eligible": False,
            },
        )


__all__ = [
    "ImplicationConverseCertificate",
    "ImplicationConverseSite",
    "N12ImplicationConverseError",
    "N12ImplicationConverseRule",
    "apply_n12_trace",
    "build_implication_converse_root",
    "certify_implication_converse",
    "enumerate_n12_sites",
]
