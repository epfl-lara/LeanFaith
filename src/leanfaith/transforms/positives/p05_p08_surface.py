"""Capped experimental E0 surface rules P05 and P08.

This module is deliberately narrower than the design envelope in
``configs/transformations/v2.yaml`` and is not wired into a runtime profile.
It emits provisional drafts only; it cannot resolve labels, promote a family,
or make examples training-eligible.

P05 toggles only the explicit ``_root_.`` qualifier on one code-owned,
reviewed, fully qualified public declaration name.  It never rewrites to a
bare suffix.  Source shadowing, suffix ambiguity, aliases, private names, and
non-allowlisted declarations fail closed before Lean re-elaboration.

P08 inserts or removes one redundant ascription on a simple occurrence of an
explicitly typed binder.  The edited term and type must already be
source-printable.  Binder declarations themselves, coercion-owned terms,
metavariables, and multiple candidate sites fail closed.

Both families inherit the exact E0 audit: same-context Lean re-elaboration,
exact alpha-canonical theorem-type identity, semantic-atom identity, and exact
inverse replay.  The later scale profile should cap each family at 10% of
positive slots and at one variant per source theorem per family.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.positives.v2_e0 import (
    PresentationSite,
    _signature_bounds,
)
from leanfaith.transforms.positives.v2_e0_p07_p09 import _ElaboratedSurfaceRule

# These are scale-policy expectations, not runtime authorization.  P05/P08
# remain disconnected from every executable profile in this LF item.
P05_POSITIVE_SLOT_CAP = 0.10
P08_POSITIVE_SLOT_CAP = 0.10
MAX_VARIANTS_PER_SOURCE_PER_FAMILY = 1

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_']*"
_QUALIFIED_IDENTIFIER = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*"

# Adding/removing ``_root_.`` preserves the complete declaration name.  The
# list is intentionally small and declaration-specific; absence from this set
# is a hard rejection, not a prompt to guess aliases or namespace openings.
_P05_PUBLIC_GLOBALS = frozenset(
    {
        "Function.id",
        "List.append",
        "List.length",
        "List.reverse",
        "Nat.succ",
        "Prod.fst",
        "Prod.snd",
    }
)
_P05_NAME_PATTERN = "|".join(
    re.escape(name) for name in sorted(_P05_PUBLIC_GLOBALS, key=lambda item: (-len(item), item))
)
_P05_GLOBAL = re.compile(
    rf"(?<![A-Za-z0-9_'.])(?P<root>_root_\.)?"
    rf"(?P<name>{_P05_NAME_PATTERN})(?![A-Za-z0-9_'.])"
)
_BINDER_DECLARATION = re.compile(rf"[({{⦃]\s*(?P<names>{_IDENTIFIER}(?:\s+{_IDENTIFIER})*)\s*:")
_LOCAL_BINDER = re.compile(rf"(?:\bfun|[∀∃])\s+(?P<name>{_IDENTIFIER})\b")
_LOCAL_LET = re.compile(rf"\blet\s+(?P<name>{_IDENTIFIER})\b")

_P08_BINDER = re.compile(
    rf"\(\s*(?P<names>{_IDENTIFIER}(?:\s+{_IDENTIFIER})*)\s*:\s*"
    rf"(?P<type>{_QUALIFIED_IDENTIFIER})\s*\)"
)
_P08_ASCRIPTION = re.compile(
    rf"\(\s*(?P<term>{_IDENTIFIER})\s*:\s*"
    rf"(?P<type>{_QUALIFIED_IDENTIFIER})\s*\)"
)


def _tree_nodes(value: object) -> tuple[Mapping[str, object], ...]:
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


def _bound_names(mask: str) -> frozenset[str]:
    names = {
        name
        for match in _BINDER_DECLARATION.finditer(mask)
        for name in match.group("names").split()
    }
    names.update(match.group("name") for match in _LOCAL_BINDER.finditer(mask))
    names.update(match.group("name") for match in _LOCAL_LET.finditer(mask))
    return frozenset(names)


def enumerate_p05_sites(source: str) -> tuple[PresentationSite, ...]:
    """Find one full-name/root-qualified presentation toggle.

    The pure source pass checks the code-owned allowlist and syntactic
    shadowing.  The representation detector later proves that the source
    resolved to exactly one matching global and no competing same-suffix
    global.  The candidate must then independently elaborate to the exact same
    theorem type.
    """

    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    shadowed = _bound_names(mask[:conclusion_end])
    conclusion = mask[conclusion_start:conclusion_end]
    sites: list[PresentationSite] = []
    for match in _P05_GLOBAL.finditer(conclusion):
        name = match.group("name")
        namespace_root, suffix = name.split(".", 1)[0], name.rsplit(".", 1)[-1]
        if namespace_root in shadowed or suffix in shadowed:
            continue
        start = conclusion_start + match.start()
        end = conclusion_start + match.end()
        rooted = match.group("root") is not None
        sites.append(
            PresentationSite(
                operation=("remove_root_qualifier" if rooted else "insert_root_qualifier"),
                start=start,
                end=end,
                source_text=source[start:end],
                replacement_text=name if rooted else f"_root_.{name}",
                metadata=(
                    ("global_name", name),
                    ("namespace_root", namespace_root),
                    ("terminal_suffix", suffix),
                ),
            )
        )
    return tuple(sorted(sites, key=lambda site: (site.start, site.end, site.stable_key)))


def _p08_binders(header: str) -> dict[str, str]:
    binders: dict[str, str] = {}
    duplicates: set[str] = set()
    for match in _P08_BINDER.finditer(header):
        source_type = match.group("type")
        for name in match.group("names").split():
            if name in binders:
                duplicates.add(name)
            binders[name] = source_type
    for name in duplicates:
        binders.pop(name, None)
    return binders


def enumerate_p08_sites(source: str) -> tuple[PresentationSite, ...]:
    """Find source-printable insertion/removal sites outside declarations."""

    mask, conclusion_start, conclusion_end = _signature_bounds(source)
    binders = _p08_binders(mask[:conclusion_start])
    if not binders:
        return ()
    conclusion = mask[conclusion_start:conclusion_end]
    sites: list[PresentationSite] = []
    occupied: list[tuple[int, int]] = []

    for match in _P08_ASCRIPTION.finditer(conclusion):
        # Never insert inside an existing ascription, including a mismatched
        # one that is not eligible for removal.
        occupied.append((match.start(), match.end()))
        term = match.group("term")
        source_type = match.group("type")
        if binders.get(term) != source_type:
            continue
        start = conclusion_start + match.start()
        end = conclusion_start + match.end()
        sites.append(
            PresentationSite(
                operation="remove_redundant_type_ascription",
                start=start,
                end=end,
                source_text=source[start:end],
                replacement_text=term,
                metadata=(("source_type", source_type), ("term", term)),
            )
        )

    for term, source_type in sorted(binders.items()):
        occurrence = re.compile(
            rf"(?<![A-Za-z0-9_'.])(?P<term>{re.escape(term)})(?![A-Za-z0-9_'.])"
        )
        for match in occurrence.finditer(conclusion):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            start = conclusion_start + match.start("term")
            end = conclusion_start + match.end("term")
            sites.append(
                PresentationSite(
                    operation="insert_redundant_type_ascription",
                    start=start,
                    end=end,
                    source_text=source[start:end],
                    replacement_text=f"({term} : {source_type})",
                    metadata=(("source_type", source_type), ("term", term)),
                )
            )
    return tuple(sorted(sites, key=lambda site: (site.start, site.end, site.stable_key)))


class P05ResolvedGlobalNamesRule(_ElaboratedSurfaceRule):
    """Toggle one reviewed complete global name's root qualifier."""

    rule_id = "p05_resolved_names"
    family_id = "p05_resolved_names"
    implementation_key = "p05_resolved_names"
    detector_capability = "unique_resolved_public_global"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        super().__init__(
            generation_config_hash=generation_config_hash,
            candidate_pool=candidate_pool,
        )
        self.audit_config_hash = hash_canonical(
            {
                "schema": "v2_e0_p05_resolved_global_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "requirements": (
                    "single_allowlisted_complete_global_name",
                    "no_source_shadowing",
                    "unique_elaborated_global_and_suffix",
                    "same_context_reelaboration",
                    "exact_inverse_replay",
                    "alpha_canonical_identity",
                    "semantic_atom_identity",
                ),
            }
        )

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p05_sites(source)

    def _detector_reasons(
        self,
        source: TheoremRecord,
        representation: RepresentationRecord,
        sites: tuple[PresentationSite, ...],
    ) -> tuple[str, ...]:
        del source
        reasons: list[str] = []
        if len(sites) != 1:
            reasons.append("resolved_global_site_not_unique")
            return tuple(reasons)
        metadata = dict(sites[0].metadata)
        global_name = metadata["global_name"]
        suffix = metadata["terminal_suffix"]
        constants = tuple(
            str(node.get("n"))
            for node in _tree_nodes(representation.operator_tree)
            if node.get("k") == "const"
        )
        atoms = tuple(
            atom.removeprefix("const:")
            for atom in (representation.semantic_atoms or ())
            if atom.startswith("const:")
        )
        if constants.count(global_name) != 1:
            reasons.append("resolved_global_node_not_unique")
        if atoms.count(global_name) != 1:
            reasons.append("resolved_global_atom_not_unique")
        suffix_constants = tuple(name for name in constants if name.rsplit(".", 1)[-1] == suffix)
        suffix_atoms = tuple(name for name in atoms if name.rsplit(".", 1)[-1] == suffix)
        if suffix_constants != (global_name,) or suffix_atoms != (global_name,):
            reasons.append("resolved_global_suffix_ambiguous")
        return tuple(sorted(set(reasons)))


class P08TypeAscriptionsRule(_ElaboratedSurfaceRule):
    """Insert or remove one simple redundant term ascription."""

    rule_id = "p08_type_ascriptions"
    family_id = "p08_type_ascriptions"
    implementation_key = "p08_type_ascriptions"
    detector_capability = "single_source_printable_ascription"

    def __init__(self, *, generation_config_hash: str, candidate_pool: str) -> None:
        super().__init__(
            generation_config_hash=generation_config_hash,
            candidate_pool=candidate_pool,
        )
        self.audit_config_hash = hash_canonical(
            {
                "schema": "v2_e0_p08_type_ascription_audit_v1",
                "rule_id": self.rule_id,
                "rule_version": self.rule_version,
                "generation_config_hash": generation_config_hash,
                "requirements": (
                    "single_source_printable_term_ascription",
                    "no_binder_declaration_edit",
                    "no_coercion_owned_term",
                    "no_metavariables",
                    "same_context_reelaboration",
                    "exact_inverse_replay",
                    "alpha_canonical_identity",
                    "semantic_atom_identity",
                ),
            }
        )

    def _sites(self, source: str) -> tuple[PresentationSite, ...]:
        return enumerate_p08_sites(source)

    def _detector_reasons(
        self,
        source: TheoremRecord,
        representation: RepresentationRecord,
        sites: tuple[PresentationSite, ...],
    ) -> tuple[str, ...]:
        del source
        reasons: list[str] = []
        if len(sites) != 1:
            reasons.append("type_ascription_site_not_unique")
        nodes = _tree_nodes(representation.operator_tree)
        if any(node.get("k") == "mvar" for node in nodes):
            reasons.append("metavariable_type_excluded")
        constants = tuple(str(node.get("n")) for node in nodes if node.get("k") == "const")
        atoms = tuple(representation.semantic_atoms or ())
        if any(
            name.startswith(("Coe.", "CoeT.", "CoeTC.", "CoeFun.", "CoeSort."))
            for name in constants
        ):
            reasons.append("coercion_owned_case_excluded")
        if any(
            atom.startswith(
                (
                    "const:Coe.",
                    "const:CoeT.",
                    "const:CoeTC.",
                    "const:CoeFun.",
                    "const:CoeSort.",
                )
            )
            for atom in atoms
        ):
            reasons.append("coercion_owned_case_excluded")
        return tuple(sorted(set(reasons)))


__all__ = [
    "MAX_VARIANTS_PER_SOURCE_PER_FAMILY",
    "P05_POSITIVE_SLOT_CAP",
    "P08_POSITIVE_SLOT_CAP",
    "P05ResolvedGlobalNamesRule",
    "P08TypeAscriptionsRule",
    "enumerate_p05_sites",
    "enumerate_p08_sites",
]
