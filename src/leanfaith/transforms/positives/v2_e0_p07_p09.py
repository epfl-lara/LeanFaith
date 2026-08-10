"""Conservative experimental E0 presentation rules P07 and P09.

This module is intentionally separate from the first P11/P12 executable slice.
It provides code-owned rules for later runtime integration, but does not alter
the accepted registry, emit labels, promote a family, or make data trainable.

P07 handles only one explicit ``↑term`` inside a source-printable type
ascription when the source expression tree contains exactly one ordinary
``Coe``/``CoeT``-style constant.  The forward edit hides that already explicit
surface marker; its exact inverse restores it.  Implicit-to-explicit discovery
is deliberately not attempted because the current representation does not map
source spans to elaborated nodes.

P09 handles only a single, direct ``Prod`` projection on an explicitly typed
simple binder: ``.1``/``.fst`` or ``.2``/``.snd``.  The elaborated tree and
semantic atoms must contain exactly that one direct projection.  General
structure fields, receiver chains, coercion fields, and record updates fail
closed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas.enums import QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import Applicability, TransformationAudit, VariantDraft
from leanfaith.transforms.positives.v2_e0 import (
    PresentationSite,
    V2E0RuleError,
    _E0PresentationRule,
    _signature_bounds,
)
from leanfaith.transforms.protocol import build_transformation_audit

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_']*"
_QUALIFIED_IDENTIFIER = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*"

_EXPLICIT_COERCION_ASCRIPTION = re.compile(
    rf"\(\s*↑\s*(?P<term>{_IDENTIFIER})\s*:\s*"
    rf"(?P<target>{_QUALIFIED_IDENTIFIER})\s*\)"
)
_PROJECTION = re.compile(
    rf"(?<![A-Za-z0-9_'.])(?P<receiver>{_IDENTIFIER})\."
    rf"(?P<field>fst|snd|1|2)(?![A-Za-z0-9_'.])"
)
_PRODUCT_BINDER = re.compile(
    rf"\(\s*(?P<receiver>{_IDENTIFIER})\s*:\s*"
    rf"(?:(?P<left>{_QUALIFIED_IDENTIFIER})\s*\N{{MULTIPLICATION SIGN}}\s*"
    rf"(?P<right>{_QUALIFIED_IDENTIFIER})|Prod\s+"
    rf"(?P<prod_left>{_QUALIFIED_IDENTIFIER})\s+"
    rf"(?P<prod_right>{_QUALIFIED_IDENTIFIER}))\s*\)"
)

_ALLOWED_COERCION_CONSTANTS = frozenset({"Coe.coe", "CoeT.coe", "CoeTC.coe"})
_DISALLOWED_COERCION_PREFIXES = ("CoeFun.", "CoeSort.")
_PROJECTION_FIELD = {
    "1": (0, "fst"),
    "fst": (0, "1"),
    "2": (1, "snd"),
    "snd": (1, "2"),
}


def _tree_nodes(value: object) -> tuple[Mapping[str, object], ...]:
    """Return every mapping node without assuming a particular tree wrapper."""

    nodes: list[Mapping[str, object]] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            nodes.append(item)
            stack.extend(item.values())
        elif isinstance(item, (tuple, list)):
            stack.extend(item)
    return tuple(nodes)


def _coercion_constants(representation: RepresentationRecord) -> tuple[str, ...]:
    return tuple(
        str(node.get("n"))
        for node in _tree_nodes(representation.operator_tree)
        if node.get("k") == "const"
        and (
            str(node.get("n")) in _ALLOWED_COERCION_CONSTANTS
            or str(node.get("n")).startswith(_DISALLOWED_COERCION_PREFIXES)
        )
    )


def _coercion_atoms(representation: RepresentationRecord) -> tuple[str, ...]:
    return tuple(
        atom
        for atom in (representation.semantic_atoms or ())
        if atom.startswith(
            (
                "const:Coe.coe",
                "const:CoeT.coe",
                "const:CoeTC.coe",
                "const:CoeFun.",
                "const:CoeSort.",
            )
        )
    )


def _projection_nodes(representation: RepresentationRecord) -> tuple[Mapping[str, object], ...]:
    return tuple(
        node for node in _tree_nodes(representation.operator_tree) if node.get("k") == "proj"
    )


def _projection_atoms(representation: RepresentationRecord) -> tuple[str, ...]:
    return tuple(atom for atom in (representation.semantic_atoms or ()) if atom.startswith("proj:"))


def _projection_accessor_constants(representation: RepresentationRecord) -> tuple[str, ...]:
    return tuple(
        str(node.get("n"))
        for node in _tree_nodes(representation.operator_tree)
        if node.get("k") == "const" and node.get("n") in {"Prod.fst", "Prod.snd"}
    )


def _projection_accessor_atoms(representation: RepresentationRecord) -> tuple[str, ...]:
    return tuple(
        atom
        for atom in (representation.semantic_atoms or ())
        if atom in {"const:Prod.fst", "const:Prod.snd"}
    )


def enumerate_p07_sites(source: str) -> tuple[PresentationSite, ...]:
    """Find explicit, single-term coercion-ascription surface sites.

    Elaborated coercion ownership is checked separately against the source
    ``RepresentationRecord``.  This pure source pass never guesses a hidden
    coercion and never rewrites comments, quoted tokens, binders, or proofs.
    """

    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    conclusion = mask[conclusion_start:conclusion_end]
    sites: list[PresentationSite] = []
    for match in _EXPLICIT_COERCION_ASCRIPTION.finditer(conclusion):
        start = conclusion_start + match.start()
        end = conclusion_start + match.end()
        term = match.group("term")
        target = match.group("target")
        sites.append(
            PresentationSite(
                operation="hide_explicit_coercion",
                start=start,
                end=end,
                source_text=source[start:end],
                replacement_text=f"({term} : {target})",
                metadata=(("target_type", target), ("term", term)),
            )
        )
    return tuple(sorted(sites, key=lambda site: (site.start, site.end, site.stable_key)))


def _product_binders(header: str) -> frozenset[str]:
    return frozenset(match.group("receiver") for match in _PRODUCT_BINDER.finditer(header))


def enumerate_p09_sites(source: str) -> tuple[PresentationSite, ...]:
    """Find direct simple-binder ``Prod`` projection presentation sites."""

    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    binders = _product_binders(mask[:conclusion_start])
    conclusion = mask[conclusion_start:conclusion_end]
    if re.search(r"\{[^{}\n]*\bwith\b", conclusion):
        return ()
    sites: list[PresentationSite] = []
    for match in _PROJECTION.finditer(conclusion):
        receiver = match.group("receiver")
        if receiver not in binders:
            continue
        field = match.group("field")
        field_index, replacement_field = _PROJECTION_FIELD[field]
        start = conclusion_start + match.start()
        end = conclusion_start + match.end()
        sites.append(
            PresentationSite(
                operation=(
                    "numeric_to_named_projection"
                    if field in {"1", "2"}
                    else "named_to_numeric_projection"
                ),
                start=start,
                end=end,
                source_text=source[start:end],
                replacement_text=f"{receiver}.{replacement_field}",
                metadata=(
                    ("field_index", str(field_index)),
                    ("receiver", receiver),
                    ("structure", "Prod"),
                ),
            )
        )
    return tuple(sorted(sites, key=lambda site: (site.start, site.end, site.stable_key)))


class _ElaboratedSurfaceRule(_E0PresentationRule):
    """E0 presentation rule with a source-representation detector contract."""

    detector_capability: str

    def _detector_reasons(
        self,
        source: TheoremRecord,
        representation: RepresentationRecord,
        sites: tuple[PresentationSite, ...],
    ) -> tuple[str, ...]:
        raise NotImplementedError

    def assess(
        self,
        theorem: TheoremRecord,
        representation: RepresentationRecord,
    ) -> Applicability:
        base = super().assess(theorem, representation)
        if not base.applicable:
            return base
        sites = self._sites(theorem.proof_stripped_declaration)
        reasons = self._detector_reasons(theorem, representation, sites)
        capabilities = tuple(sorted((*base.required_capabilities, self.detector_capability)))
        metadata = {**base.metadata, "detector_contract_checked": True}
        if reasons:
            return Applicability(
                applicable=False,
                reason_codes=tuple(sorted(set(reasons))),
                required_capabilities=capabilities,
                metadata=metadata,
            )
        return Applicability(
            applicable=True,
            reason_codes=(),
            matched_nodes=base.matched_nodes,
            required_capabilities=capabilities,
            metadata=metadata,
        )

    def audit(
        self,
        source: TheoremRecord,
        source_representation: RepresentationRecord,
        candidate: TheoremRecord,
        candidate_representation: RepresentationRecord,
        draft: VariantDraft,
    ) -> TransformationAudit:
        base = super().audit(
            source,
            source_representation,
            candidate,
            candidate_representation,
            draft,
        )
        try:
            source_sites = self._sites(source.proof_stripped_declaration)
            detector_reasons = self._detector_reasons(
                source,
                source_representation,
                source_sites,
            )
        except V2E0RuleError as exc:
            detector_reasons = (str(exc),)
        if not detector_reasons:
            return base

        violations = tuple(
            sorted(
                {
                    *base.violation_codes,
                    "source_detector_contract_mismatch",
                    *(f"source_{reason}" for reason in detector_reasons),
                }
            )
        )
        return build_transformation_audit(
            draft=draft,
            applicability=base.applicability,
            audit_config_hash=self.audit_config_hash,
            recommended_validation_status=ValidationStatus.QUARANTINED,
            recommended_quality_tier=QualityTier.UNKNOWN,
            candidate_theorem_id=base.candidate_theorem_id,
            candidate_representation_id=base.candidate_representation_id,
            elaboration_evidence_id=base.elaboration_evidence_id,
            structural_diff_ok=False,
            atom_mapping_ok=base.atom_mapping_ok,
            inverse_or_roundtrip_ok=base.inverse_or_roundtrip_ok,
            certificate_evidence_ids=base.certificate_evidence_ids,
            violation_codes=violations,
            metadata={
                **base.metadata,
                "source_detector_contract_ok": False,
                "source_detector_reason_count": len(detector_reasons),
            },
        )


class P07CoercionSurfaceRule(_ElaboratedSurfaceRule):
    """Hide one explicit, uniquely elaborated ordinary coercion marker."""

    rule_id = "p07_coercion_surface"
    family_id = "p07_coercion_surface"
    implementation_key = "p07_coercion_surface"
    detector_capability = "elaborated_coercion_hop"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        super().__init__(
            generation_config_hash=generation_config_hash,
            candidate_pool=candidate_pool,
        )
        self.audit_config_hash = hash_canonical(
            {
                "schema": "v2_e0_p07_coercion_surface_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "requirements": (
                    "single_explicit_source_coercion",
                    "single_elaborated_ordinary_coercion",
                    "same_context_reelaboration",
                    "exact_inverse_replay",
                    "alpha_canonical_identity",
                    "semantic_atom_identity",
                ),
            }
        )

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p07_sites(source)

    def _detector_reasons(
        self,
        source: TheoremRecord,
        representation: RepresentationRecord,
        sites: tuple[PresentationSite, ...],
    ) -> tuple[str, ...]:
        del source
        reasons: list[str] = []
        if len(sites) != 1:
            reasons.append("coercion_surface_site_not_unique")
        constants = _coercion_constants(representation)
        atoms = _coercion_atoms(representation)
        if any(name.startswith(_DISALLOWED_COERCION_PREFIXES) for name in constants):
            reasons.append("coercion_kind_excluded")
        ordinary_constants = tuple(
            name for name in constants if name in _ALLOWED_COERCION_CONSTANTS
        )
        ordinary_atoms = tuple(
            atom for atom in atoms if atom.removeprefix("const:") in _ALLOWED_COERCION_CONSTANTS
        )
        if len(ordinary_constants) != 1:
            reasons.append("elaborated_coercion_hop_not_unique")
        if len(ordinary_atoms) != 1:
            reasons.append("coercion_atom_not_unique")
        if len(atoms) != len(ordinary_atoms):
            reasons.append("coercion_kind_excluded")
        return tuple(sorted(set(reasons)))


class P09ProjectionSurfaceRule(_ElaboratedSurfaceRule):
    """Switch one direct ``Prod`` projection between numeric and named syntax."""

    rule_id = "p09_projections"
    family_id = "p09_projections"
    implementation_key = "p09_projections"
    detector_capability = "direct_projection_node"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        super().__init__(
            generation_config_hash=generation_config_hash,
            candidate_pool=candidate_pool,
        )
        self.audit_config_hash = hash_canonical(
            {
                "schema": "v2_e0_p09_projection_surface_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "requirements": (
                    "single_direct_prod_projection",
                    "explicit_unambiguous_product_receiver",
                    "no_coercion_field",
                    "same_context_reelaboration",
                    "exact_inverse_replay",
                    "alpha_canonical_identity",
                    "semantic_atom_identity",
                ),
            }
        )

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p09_sites(source)

    def _detector_reasons(
        self,
        source: TheoremRecord,
        representation: RepresentationRecord,
        sites: tuple[PresentationSite, ...],
    ) -> tuple[str, ...]:
        del source
        reasons: list[str] = []
        if len(sites) != 1:
            reasons.append("projection_surface_site_not_unique")
            expected_index = None
        else:
            expected_index = int(dict(sites[0].metadata)["field_index"])
        if _coercion_atoms(representation) or _coercion_constants(representation):
            reasons.append("coercion_field_excluded")
        nodes = _projection_nodes(representation)
        node_atoms = _projection_atoms(representation)
        accessor_constants = _projection_accessor_constants(representation)
        accessor_atoms = _projection_accessor_atoms(representation)
        expected_accessor = (
            ("Prod.fst" if expected_index == 0 else "Prod.snd")
            if expected_index is not None
            else None
        )
        direct_node_form = len(nodes) == 1 and not accessor_constants and not accessor_atoms
        accessor_form = not nodes and len(accessor_constants) == 1 and len(accessor_atoms) == 1
        if direct_node_form:
            projection = nodes[0]
            base = projection.get("base")
            if not (
                projection.get("s") == "Prod"
                and projection.get("i") == expected_index
                and isinstance(base, Mapping)
                and base.get("k") in {"bvar", "fvar"}
            ):
                reasons.append("direct_projection_node_mismatch")
            expected_atom = f"proj:Prod:{expected_index}"
            if len(node_atoms) != 1 or node_atoms[0] != expected_atom:
                reasons.append("projection_atom_mismatch")
        elif accessor_form:
            if (
                accessor_constants[0] != expected_accessor
                or accessor_atoms[0] != f"const:{expected_accessor}"
                or node_atoms
            ):
                reasons.append("direct_projection_accessor_mismatch")
        else:
            reasons.append("direct_projection_evidence_not_unique")
        return tuple(sorted(set(reasons)))


__all__ = [
    "P07CoercionSurfaceRule",
    "P09ProjectionSurfaceRule",
    "enumerate_p07_sites",
    "enumerate_p09_sites",
]
