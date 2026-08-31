"""Applicability-aware SFT2A v5 mechanism planning and cheap shortcut screens."""

# ruff: noqa: RUF001

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from leanfaith.config.hashing import hash_canonical

MechanismPolarity = Literal["preserving", "breaking"]

_WS = re.compile(r"\s+")
_BINDER = re.compile(r"∀|forall\b|[({[][^\n,:]+\s*:")
_ARROW = re.compile(r"→|->")
_REFLEXIVE_ATOM = re.compile(r"(?<![\w.'])\b([A-Za-z_][A-Za-z0-9_'.]*)\s*=\s*\1\b(?![\w.'])")


@dataclass(frozen=True, slots=True)
class SignatureShape:
    """Cheap, zero-Lean features used only for routing and stratification."""

    binder_count: int
    premise_count: int
    conjunction_count: int
    disjunction_count: int
    has_equality: bool
    has_order: bool
    has_set: bool
    has_exists: bool
    has_negation: bool
    has_function: bool
    has_boundary_domain: bool

    @property
    def shape_id(self) -> str:
        flags = [
            "eq" if self.has_equality else "noeq",
            "ord" if self.has_order else "noord",
            "set" if self.has_set else "noset",
            "ex" if self.has_exists else "noex",
            "neg" if self.has_negation else "noneg",
            "fun" if self.has_function else "nofun",
            "bdry" if self.has_boundary_domain else "nobdry",
        ]
        return f"b{min(self.binder_count, 4)}-p{min(self.premise_count, 4)}-" + "-".join(flags)


@dataclass(frozen=True, slots=True)
class MechanismSpec:
    family: str
    polarity: MechanismPolarity
    instruction: str
    applicability: str
    shortcut_risk: bool = False


@dataclass(frozen=True, slots=True)
class MechanismAssignment:
    family: str
    polarity: MechanismPolarity
    instruction: str
    applicability: str
    shape_id: str

    def prompt_text(self) -> str:
        return (
            f"mechanism_family={self.family}; applicability={self.applicability}; "
            f"instruction={self.instruction}"
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


PRESERVING_MECHANISMS: tuple[MechanismSpec, ...] = (
    MechanismSpec(
        "definitional_unfold_refold",
        "preserving",
        "Use a substantive definitional unfolding, refolding, or equivalent named predicate; "
        "do not return the original text or add logical padding.",
        "general",
    ),
    MechanismSpec(
        "argument_permutation_with_recovery",
        "preserving",
        "Permute universally bound arguments only when the entire closed proposition recovers "
        "the original by inverse instantiation.",
        "two_binders",
    ),
    MechanismSpec(
        "equality_symmetry",
        "preserving",
        "Reverse an equality while retaining the same binders and hypotheses.",
        "equality",
    ),
    MechanismSpec(
        "premise_permutation",
        "preserving",
        "Reorder independent premises without adding, dropping, or weakening any premise.",
        "two_premises",
    ),
    MechanismSpec(
        "curry_uncurry",
        "preserving",
        "Convert between multiple independent premises and a conjunction in a logically "
        "reversible way.",
        "premises_or_conjunction",
    ),
    MechanismSpec(
        "quantifier_reordering_independent",
        "preserving",
        "Reorder only demonstrably independent universal binders and preserve dependent order.",
        "two_binders",
    ),
    MechanismSpec(
        "conjunction_reassociation",
        "preserving",
        "Reassociate or commute a substantive conjunction without inserting True or reflexive "
        "padding.",
        "two_conjunctions",
    ),
    MechanismSpec(
        "disjunction_reassociation",
        "preserving",
        "Reassociate or reorder a substantive disjunction while preserving every alternative.",
        "two_disjunctions",
    ),
    MechanismSpec(
        "antisymmetry_expansion",
        "preserving",
        "Replace an equality by both meaningful order directions only when antisymmetry "
        "recovers it.",
        "equality_and_order",
    ),
    MechanismSpec(
        "set_extensionality_expansion",
        "preserving",
        "Replace set equality with substantive membership or mutual-inclusion conditions that "
        "recover it by extensionality.",
        "set_equality",
    ),
    MechanismSpec(
        "recoverable_boundary_partition",
        "preserving",
        "Partition a boundary case and its complement only when the branches jointly cover the "
        "original closed proposition.",
        "boundary_domain",
    ),
    MechanismSpec(
        "existential_witness_repackaging",
        "preserving",
        "Repackage an existential witness without changing its dependencies or uniqueness content.",
        "exists",
    ),
    MechanismSpec(
        "negation_implication_duality",
        "preserving",
        "Use a reversible negation/implication reformulation without changing constructive "
        "commitments.",
        "negation_or_premise",
    ),
    MechanismSpec(
        "function_extensionality_expansion",
        "preserving",
        "Replace function equality with pointwise equality, retaining the full domain and all "
        "binders.",
        "function_equality",
    ),
)

BREAKING_MECHANISMS: tuple[MechanismSpec, ...] = (
    MechanismSpec(
        "premise_strengthening",
        "breaking",
        "Add a substantive non-recoverable premise that changes the closed claim rather than a "
        "vacuous True guard.",
        "general",
    ),
    MechanismSpec(
        "premise_weakening_or_drop",
        "breaking",
        "Remove or weaken one substantive premise while leaving the candidate meaningful and "
        "valid.",
        "premise",
    ),
    MechanismSpec(
        "conclusion_strengthening",
        "breaking",
        "Strengthen the conclusion in a way not recoverable from symmetry, antisymmetry, or "
        "argument swapping.",
        "general",
    ),
    MechanismSpec(
        "conclusion_weakening",
        "breaking",
        "Weaken the conclusion only when universal argument swapping or antisymmetry cannot "
        "recover the reference.",
        "equality_or_order",
    ),
    MechanismSpec(
        "quantifier_scope_change",
        "breaking",
        "Change quantifier scope or order where dependency makes the resulting claim genuinely "
        "different.",
        "two_binders",
    ),
    MechanismSpec(
        "type_or_domain_shift",
        "breaking",
        "Change a binder domain or type while retaining a well-formed, nontrivial proposition.",
        "general",
    ),
    MechanismSpec(
        "guard_or_boundary_change",
        "breaking",
        "Alter a boundary guard only when omitted or added cases are not recoverable from the "
        "remaining universally closed claim.",
        "boundary_or_premise",
    ),
    MechanismSpec(
        "converse_direction",
        "breaking",
        "Take a genuine converse rather than merely swapping universally quantified argument "
        "names.",
        "premise",
    ),
    MechanismSpec(
        "negation_scope_change",
        "breaking",
        "Move or introduce negation so its scope changes the mathematical assertion.",
        "negation_or_premise",
    ),
    MechanismSpec(
        "existence_uniqueness_change",
        "breaking",
        "Change existence versus uniqueness or witness availability without making the "
        "proposition inconsistent by construction.",
        "exists",
    ),
    MechanismSpec(
        "witness_dependency_change",
        "breaking",
        "Move an existential across a universal binder so the witness dependency genuinely "
        "changes.",
        "exists_and_binder",
    ),
    MechanismSpec(
        "connective_change",
        "breaking",
        "Replace a substantive conjunction/disjunction/implication connective without vacuous "
        "constants.",
        "connective",
    ),
    MechanismSpec(
        "operator_or_index_shift",
        "breaking",
        "Change one mathematically meaningful operator, index, exponent, or constant while "
        "preserving type correctness.",
        "general",
    ),
    MechanismSpec(
        "relation_change_nonrecoverable",
        "breaking",
        "Change equality/order/membership only after checking the full closure cannot recover the "
        "original by symmetry, antisymmetry, or extensionality.",
        "relation",
    ),
)

ALL_MECHANISM_FAMILIES = tuple(
    spec.family for spec in (*PRESERVING_MECHANISMS, *BREAKING_MECHANISMS)
)


def signature_shape(signature: str) -> SignatureShape:
    collapsed = _WS.sub(" ", signature).strip()
    return SignatureShape(
        binder_count=len(_BINDER.findall(collapsed)),
        premise_count=len(_ARROW.findall(collapsed)),
        conjunction_count=collapsed.count("∧"),
        disjunction_count=collapsed.count("∨"),
        has_equality="=" in collapsed,
        has_order=any(token in collapsed for token in ("≤", "<", "≥", ">", "⊆", "⊂")),
        has_set=any(token in collapsed for token in ("Set ", "Set.", "∈", "∉", "⊆", "∪", "∩")),
        has_exists="∃" in collapsed or "Exists" in collapsed,
        has_negation="¬" in collapsed or "≠" in collapsed,
        has_function="→" in collapsed or "fun " in collapsed,
        has_boundary_domain=any(
            token in collapsed for token in ("Nat", "ℕ", "Int", "ℤ", "Fin ", "Icc", "Ioc")
        ),
    )


def planning_signature_from_goal_v1(goal: str) -> str:
    """Convert a certified rendered goal into binder-aware planning text."""

    lines = [line.strip() for line in goal.splitlines() if line.strip()]
    turnstiles = [index for index, line in enumerate(lines) if line.startswith("⊢ ")]
    if len(turnstiles) != 1 or turnstiles[0] != len(lines) - 1:
        raise ValueError("certified goal_v1 has a malformed turnstile layout")
    target = lines[-1][2:].strip()
    binders = " ".join(f"({line})" for line in lines[:-1])
    return f"∀ {binders}, {target}" if binders else target


def _walk_expr_tree(value: object) -> list[Mapping[str, object]]:
    nodes: list[Mapping[str, object]] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            nodes.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return nodes


def _local_name_count(line: str) -> int:
    """Count names in one rendered local declaration, not colons in its type."""

    depth = 0
    for index, character in enumerate(line):
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth = max(0, depth - 1)
        elif character == ":" and depth == 0:
            names = line[:index].strip().split()
            if not names or any(name in {"⊢", ":="} for name in names):
                raise ValueError("certified goal local declaration is malformed")
            return len(names)
    raise ValueError("certified goal local declaration lacks a top-level colon")


def structured_signature_shape(goal: str, expr_tree: Mapping[str, object]) -> SignatureShape:
    """Derive applicability from a certified goal and its closed Expr tree.

    The rendered context determines the number of displayed binders exactly once.  The
    structural Expr determines logical constants.  Only top-level arrows in the rendered target
    are counted as premises; arrows nested in function domains are intentionally ignored.
    """

    if any(marker in goal for marker in ("[anonymous]", "⋯", "...")):
        raise ValueError("certified model-facing goal contains a forbidden placeholder")
    lines = [line.strip() for line in goal.splitlines() if line.strip()]
    turnstiles = [index for index, line in enumerate(lines) if line.startswith("⊢ ")]
    if len(turnstiles) != 1 or turnstiles[0] != len(lines) - 1:
        raise ValueError("certified goal_v1 has a malformed turnstile layout")
    binder_count = sum(_local_name_count(line) for line in lines[:-1])
    target = lines[-1][2:].strip()

    # Count only arrows at the target's outermost delimiter depth.  In particular, `=>` is a
    # lambda token rather than equality and arrows in `(f : A → B)` are not premises.
    premise_count = 0
    depth = 0
    index = 0
    while index < len(target):
        character = target[index]
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and character == "→":
            premise_count += 1
        elif depth == 0 and target[index : index + 2] == "->":
            premise_count += 1
            index += 1
        index += 1

    nodes = _walk_expr_tree(expr_tree)
    constant_nodes = [
        str(node["name"])
        for node in nodes
        if node.get("k") == "const" and isinstance(node.get("name"), str)
    ]
    constants = set(constant_nodes)
    conjunction_count = sum(name == "And" for name in constant_nodes)
    disjunction_count = sum(name == "Or" for name in constant_nodes)
    has_equality = "Eq" in constants
    has_order = any(
        name in {"LE.le", "LT.lt", "Set.Subset"} or name.endswith((".le", ".lt", ".ge", ".gt"))
        for name in constants
    )
    has_set = any(
        name.startswith("Set.")
        or name in {"Set", "Membership.mem", "HasSubset.Subset", "Set.Subset"}
        for name in constants
    )
    has_exists = "Exists" in constants
    has_negation = any(name in {"Not", "Ne"} for name in constants)

    def function_node(node: Mapping[str, object]) -> bool:
        domain = node.get("domain")
        return node.get("k") == "lam" or (
            node.get("k") == "forall"
            and isinstance(domain, Mapping)
            and domain.get("k") == "forall"
        )

    has_function = any(function_node(node) for node in nodes)
    has_boundary_domain = any(
        name == "Nat"
        or name == "Int"
        or name == "Fin"
        or name.startswith(("Fin.", "Set.Icc", "Set.Ioc"))
        for name in constants
    )
    return SignatureShape(
        binder_count=binder_count,
        premise_count=premise_count,
        conjunction_count=conjunction_count,
        disjunction_count=disjunction_count,
        has_equality=has_equality,
        has_order=has_order,
        has_set=has_set,
        has_exists=has_exists,
        has_negation=has_negation,
        has_function=has_function,
        has_boundary_domain=has_boundary_domain,
    )


def _applicable(spec: MechanismSpec, shape: SignatureShape) -> bool:
    rule = spec.applicability
    return {
        "general": True,
        "two_binders": shape.binder_count >= 2,
        "equality": shape.has_equality,
        "two_premises": shape.premise_count >= 2,
        "premises_or_conjunction": shape.premise_count >= 1 or shape.conjunction_count >= 1,
        "two_conjunctions": shape.conjunction_count >= 2,
        "two_disjunctions": shape.disjunction_count >= 2,
        "equality_and_order": shape.has_equality and shape.has_order,
        "set_equality": shape.has_set and shape.has_equality,
        "boundary_domain": shape.has_boundary_domain,
        "exists": shape.has_exists,
        "negation_or_premise": shape.has_negation or shape.premise_count >= 1,
        "function_equality": shape.has_function and shape.has_equality,
        "premise": shape.premise_count >= 1,
        "equality_or_order": shape.has_equality or shape.has_order,
        "boundary_or_premise": shape.has_boundary_domain or shape.premise_count >= 1,
        "exists_and_binder": shape.has_exists and shape.binder_count >= 1,
        "connective": (
            shape.premise_count + shape.conjunction_count + shape.disjunction_count >= 1
        ),
        "relation": shape.has_equality or shape.has_order or shape.has_set,
    }.get(rule, False)


def applicable_mechanisms(signature: str, polarity: MechanismPolarity) -> tuple[MechanismSpec, ...]:
    """Return the substantive families whose frozen rule applies to a signature."""

    shape = signature_shape(signature)
    specs = PRESERVING_MECHANISMS if polarity == "preserving" else BREAKING_MECHANISMS
    return tuple(spec for spec in specs if _applicable(spec, shape))


def _root_fields(row: Mapping[str, object]) -> tuple[str, str]:
    root = row.get("root", row)
    if not isinstance(root, Mapping):
        raise ValueError("mechanism planning root is not a mapping")
    root_id = root.get("root_id")
    signature = root.get("reference_signature")
    if not isinstance(root_id, str) or not isinstance(signature, str):
        raise ValueError("mechanism planning root lacks root_id/reference_signature")
    return root_id, signature


def plan_mechanism_rotation(
    roots: Sequence[Mapping[str, object]],
    *,
    salt: str,
    maximum_family_fraction_per_polarity: float,
) -> dict[str, dict[str, MechanismAssignment]]:
    """Plan a deterministic balanced rotation before any provider or Lean call."""

    if not roots or not 0.0 < maximum_family_fraction_per_polarity <= 1.0:
        raise ValueError("invalid mechanism rotation population or diversity cap")
    slots = (
        ("preserve_0", "preserving"),
        ("preserve_1", "preserving"),
        ("break_0", "breaking"),
        ("break_1", "breaking"),
    )
    per_polarity = len(roots) * 2
    family_cap = max(1, math.ceil(per_polarity * maximum_family_fraction_per_polarity))
    counts: Counter[tuple[str, str]] = Counter()
    planned: dict[str, dict[str, MechanismAssignment]] = {}

    def constraint_rank(item: Mapping[str, object]) -> tuple[int, str]:
        root_id, signature = _root_fields(item)
        preserving = len(applicable_mechanisms(signature, "preserving"))
        breaking = len(applicable_mechanisms(signature, "breaking"))
        return preserving + breaking, root_id

    for row in sorted(roots, key=constraint_rank):
        root_id, signature = _root_fields(row)
        shape = signature_shape(signature)
        root_plan: dict[str, MechanismAssignment] = {}
        for slot_id, polarity_text in slots:
            specs = PRESERVING_MECHANISMS if polarity_text == "preserving" else BREAKING_MECHANISMS
            eligible = [
                spec
                for spec in specs
                if _applicable(spec, shape) and counts[(polarity_text, spec.family)] < family_cap
            ]
            if not eligible:
                raise ValueError(
                    f"mechanism diversity cap leaves no applicable family for {root_id}:{slot_id}"
                )
            selected = min(
                eligible,
                key=lambda spec: (
                    counts[(polarity_text, spec.family)],
                    hash_canonical(
                        {
                            "salt": salt,
                            "root_id": root_id,
                            "slot_id": slot_id,
                            "family": spec.family,
                            "shape_id": shape.shape_id,
                        }
                    ),
                ),
            )
            counts[(polarity_text, selected.family)] += 1
            root_plan[slot_id] = MechanismAssignment(
                family=selected.family,
                polarity=selected.polarity,
                instruction=selected.instruction,
                applicability=selected.applicability,
                shape_id=shape.shape_id,
            )
        planned[root_id] = root_plan
    return planned


def plan_structured_mechanism_rotation(
    roots: Sequence[tuple[str, SignatureShape]],
    *,
    salt: str,
    maximum_family_fraction_per_polarity: float,
) -> dict[str, dict[str, MechanismAssignment]]:
    """Plan the rotation from prevalidated structured certified-goal shapes."""

    if not roots or not 0.0 < maximum_family_fraction_per_polarity <= 1.0:
        raise ValueError("invalid structured mechanism rotation population or diversity cap")
    if len({root_id for root_id, _shape in roots}) != len(roots):
        raise ValueError("structured mechanism rotation contains duplicate root IDs")
    slots = (
        ("preserve_0", "preserving"),
        ("preserve_1", "preserving"),
        ("break_0", "breaking"),
        ("break_1", "breaking"),
    )
    family_cap = max(1, math.ceil(len(roots) * 2 * maximum_family_fraction_per_polarity))
    counts: Counter[tuple[str, str]] = Counter()
    planned: dict[str, dict[str, MechanismAssignment]] = {}

    def rank(item: tuple[str, SignatureShape]) -> tuple[int, str]:
        root_id, shape = item
        eligible = sum(
            _applicable(spec, shape) for spec in (*PRESERVING_MECHANISMS, *BREAKING_MECHANISMS)
        )
        return eligible, root_id

    for root_id, shape in sorted(roots, key=rank):
        root_plan: dict[str, MechanismAssignment] = {}
        for slot_id, polarity in slots:
            specs = PRESERVING_MECHANISMS if polarity == "preserving" else BREAKING_MECHANISMS
            eligible = [
                spec
                for spec in specs
                if _applicable(spec, shape) and counts[(polarity, spec.family)] < family_cap
            ]
            if not eligible:
                raise ValueError(
                    f"mechanism diversity cap leaves no structured family for {root_id}:{slot_id}"
                )
            selected = min(
                eligible,
                key=lambda spec: (
                    counts[(polarity, spec.family)],
                    hash_canonical(
                        {
                            "salt": salt,
                            "root_id": root_id,
                            "slot_id": slot_id,
                            "family": spec.family,
                            "shape_id": shape.shape_id,
                        }
                    ),
                ),
            )
            counts[(polarity, selected.family)] += 1
            root_plan[slot_id] = MechanismAssignment(
                family=selected.family,
                polarity=selected.polarity,
                instruction=selected.instruction,
                applicability=selected.applicability,
                shape_id=shape.shape_id,
            )
        planned[root_id] = root_plan
    return planned


def mechanism_histogram(
    plan: Mapping[str, Mapping[str, MechanismAssignment]],
) -> dict[str, dict[str, int]]:
    counts: Counter[tuple[str, str]] = Counter()
    for root_plan in plan.values():
        for assignment in root_plan.values():
            counts[(assignment.polarity, assignment.family)] += 1
    return {
        polarity: {
            family: counts[(polarity, family)]
            for family in sorted({key[1] for key in counts if key[0] == polarity})
        }
        for polarity in ("preserving", "breaking")
    }


def shortcut_violation(reference_signature: str, candidate_signature: str) -> str | None:
    """Reject exact, vacuous, and reflexive-padding candidates before Lean."""

    reference = _WS.sub(" ", reference_signature).strip()
    candidate = _WS.sub(" ", candidate_signature).strip()
    if candidate == reference:
        return "exact_reference_copy"
    shortcut_tokens = {
        "True →": "vacuous_true_implication",
        "True ->": "vacuous_true_implication",
        "∧ True": "vacuous_true_conjunction",
        "True ∧": "vacuous_true_conjunction",
        "∨ False": "vacuous_false_disjunction",
        "False ∨": "vacuous_false_disjunction",
    }
    for token, reason in shortcut_tokens.items():
        if token in candidate:
            return reason
    if _REFLEXIVE_ATOM.search(candidate) is not None and _REFLEXIVE_ATOM.search(reference) is None:
        return "reflexive_equality_padding"
    return None


__all__ = [
    "ALL_MECHANISM_FAMILIES",
    "BREAKING_MECHANISMS",
    "PRESERVING_MECHANISMS",
    "MechanismAssignment",
    "SignatureShape",
    "applicable_mechanisms",
    "mechanism_histogram",
    "plan_mechanism_rotation",
    "plan_structured_mechanism_rotation",
    "shortcut_violation",
    "signature_shape",
    "structured_signature_shape",
]
