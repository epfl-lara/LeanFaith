"""Lean-free identities and closure selection for Wave 4 SFT1 orbits.

This module deliberately does not run Lean and is not a second runner.  It is the
deterministic data-model layer that ``square.py`` can call after the persistent
Lean engine has returned typed sites, checked preserving hops, and certified
negative edges.  Historical single-square records therefore keep their existing
schema while Wave 4 can use content-addressed chains and shared-edge closure
groups.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, Protocol, cast

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas.ids import PAIR_PREFIX, VARIANT_PREFIX, make_id
from leanfaith.sft1.sprint.screens import render_hash, unordered_pair_key

EdgeRole = Literal[
    "preserving_reference",
    "preserving_candidate",
    "negative_base",
    "negative_last",
]

EDGE_ROLES: tuple[EdgeRole, ...] = (
    "preserving_reference",
    "preserving_candidate",
    "negative_base",
    "negative_last",
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class OrbitError(ValueError):
    """Raised when an orbit identity or certificate-closure contract is invalid."""


class NegativeOperationGroup(Protocol):
    """Minimal release-group view consumed by operation-share capping.

    ``ClosureGroup`` implements this protocol directly.  A compactor may also
    pass an equivalent immutable record, provided its row IDs identify the
    complete physical rows that selecting the group would retain.
    """

    @property
    def group_id(self) -> str: ...

    @property
    def operation_id(self) -> str: ...

    @property
    def mechanism(self) -> str: ...

    @property
    def row_ids(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class NegativeOperationShareCapReport:
    """Deterministic accounting for one group-preserving operation cap.

    Row counts are unique physical-row counts.  In particular, a base edge
    shared by multiple closure groups is counted once.
    """

    operation_id: str
    mechanism: str | None
    maximum_share: float
    selection_salt: str
    input_group_count: int
    selected_group_count: int
    dropped_group_count: int
    operation_input_group_count: int
    operation_selected_group_count: int
    operation_dropped_group_count: int
    input_row_count: int
    selected_row_count: int
    dropped_row_count: int
    operation_input_row_count: int
    operation_selected_row_count: int
    operation_dropped_row_count: int
    maximum_operation_row_count: int
    dropped_group_ids: tuple[str, ...]

    def record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "operation_id": self.operation_id,
            "mechanism": self.mechanism,
            "maximum_share": self.maximum_share,
            "selection_salt": self.selection_salt,
            "input_group_count": self.input_group_count,
            "selected_group_count": self.selected_group_count,
            "dropped_group_count": self.dropped_group_count,
            "operation_input_group_count": self.operation_input_group_count,
            "operation_selected_group_count": self.operation_selected_group_count,
            "operation_dropped_group_count": self.operation_dropped_group_count,
            "input_row_count": self.input_row_count,
            "selected_row_count": self.selected_row_count,
            "dropped_row_count": self.dropped_row_count,
            "operation_input_row_count": self.operation_input_row_count,
            "operation_selected_row_count": self.operation_selected_row_count,
            "operation_dropped_row_count": self.operation_dropped_row_count,
            "maximum_operation_row_count": self.maximum_operation_row_count,
            "dropped_group_ids": list(self.dropped_group_ids),
        }


@dataclass(frozen=True, slots=True)
class NegativeOperationShareCapResult[GroupT: NegativeOperationGroup]:
    """Whole groups selected under an operation-level released-row cap."""

    selected_groups: tuple[GroupT, ...]
    report: NegativeOperationShareCapReport


def _require_text(field: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise OrbitError(f"{field} must be nonempty")


def _require_hash(field: str, value: object) -> None:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise OrbitError(f"{field} must be a lowercase SHA-256 hex digest")


def _paths_overlap(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    shared = min(len(left), len(right))
    return left[:shared] == right[:shared]


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """Composition metadata whose values must agree with the typed Lean result."""

    operation_id: str
    mechanism: str
    superclass: str
    inverse_token: str

    def __post_init__(self) -> None:
        for field in ("operation_id", "mechanism", "superclass", "inverse_token"):
            _require_text(field, getattr(self, field))

    def payload(self) -> dict[str, str]:
        return {
            "operation_id": self.operation_id,
            "mechanism": self.mechanism,
            "superclass": self.superclass,
            "inverse_token": self.inverse_token,
        }


@dataclass(frozen=True, slots=True)
class SiteLineage:
    """A typed site in the current expression plus its root-coordinate lineage.

    ``path`` addresses the site in the current hop input. ``origin_path`` is its
    structural coordinate in the chain root.  If an earlier overlapping rewrite
    changed how the latter is reached, ``transported_from`` names every relevant
    earlier site lineage and ``transport_certificate_hash`` binds the checked
    transport evidence returned by Lean.
    """

    kind: str
    path: tuple[int, ...]
    origin_path: tuple[int, ...]
    occurrence: int
    input_expr_hash: str
    focus_expr_hash: str
    footprint_hash: str
    binder_context_hash: str
    transported_from: tuple[str, ...] = ()
    transport_certificate_hash: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        _require_text("site.kind", self.kind)
        if type(self.occurrence) is not int or self.occurrence < 0:
            raise OrbitError("site.occurrence must be nonnegative")
        if any(type(step) is not int or step < 0 for step in (*self.path, *self.origin_path)):
            raise OrbitError("site paths must contain only nonnegative child indices")
        for field in (
            "input_expr_hash",
            "focus_expr_hash",
            "footprint_hash",
            "binder_context_hash",
        ):
            _require_hash(f"site.{field}", getattr(self, field))
        transports = tuple(sorted(self.transported_from))
        if len(transports) != len(set(transports)):
            raise OrbitError("site.transported_from must not contain duplicates")
        for value in transports:
            _require_hash("site.transported_from", value)
        object.__setattr__(self, "transported_from", transports)
        if transports and self.transport_certificate_hash is None:
            raise OrbitError("a transported site requires a transport certificate")
        if not transports and self.transport_certificate_hash is not None:
            raise OrbitError("a transport certificate requires at least one predecessor site")
        if self.transport_certificate_hash is not None:
            _require_hash("site.transport_certificate_hash", self.transport_certificate_hash)

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": list(self.path),
            "origin_path": list(self.origin_path),
            "occurrence": self.occurrence,
            "input_expr_hash": self.input_expr_hash,
            "focus_expr_hash": self.focus_expr_hash,
            "footprint_hash": self.footprint_hash,
            "binder_context_hash": self.binder_context_hash,
            "transported_from": list(self.transported_from),
            "transport_certificate_hash": self.transport_certificate_hash,
            "detail": self.detail,
        }

    @property
    def lineage_hash(self) -> str:
        return hash_canonical({"kind": "sft1_wave4_site_lineage_v1", **self.payload()})


@dataclass(frozen=True, slots=True)
class PreservingHop:
    """One visibly distinct, directly checked preserving rewrite."""

    operation_id: str
    mechanism: str
    superclass: str
    inverse_token: str
    site: SiteLineage
    input_expr_hash: str
    output_expr_hash: str
    input_render_hash: str
    output_render_hash: str
    certificate_hash: str

    def __post_init__(self) -> None:
        for field in ("operation_id", "mechanism", "superclass", "inverse_token"):
            _require_text(f"hop.{field}", getattr(self, field))
        for field in (
            "input_expr_hash",
            "output_expr_hash",
            "input_render_hash",
            "output_render_hash",
            "certificate_hash",
        ):
            _require_hash(f"hop.{field}", getattr(self, field))
        if self.site.input_expr_hash != self.input_expr_hash:
            raise OrbitError("site input expression does not match its preserving hop")
        if self.input_expr_hash == self.output_expr_hash:
            raise OrbitError("preserving hop must change the checked expression")
        if self.input_render_hash == self.output_render_hash:
            raise OrbitError("preserving hop must have visibly distinct goal_v1 output")

    def payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "mechanism": self.mechanism,
            "superclass": self.superclass,
            "inverse_token": self.inverse_token,
            "site": self.site.payload(),
            "site_lineage_hash": self.site.lineage_hash,
            "input_expr_hash": self.input_expr_hash,
            "output_expr_hash": self.output_expr_hash,
            "input_render_hash": self.input_render_hash,
            "output_render_hash": self.output_render_hash,
            "certificate_hash": self.certificate_hash,
        }

    @property
    def hop_hash(self) -> str:
        return hash_canonical({"kind": "sft1_wave4_preserving_hop_v1", **self.payload()})


@dataclass(frozen=True, slots=True)
class PreservingChain:
    """A one-, two-, or three-hop typed preserving chain for one ancestry root."""

    root_id: str
    hops: tuple[PreservingHop, ...]

    def __post_init__(self) -> None:
        _require_text("chain.root_id", self.root_id)
        if not self.hops:
            raise OrbitError("a preserving chain must contain at least one hop")

    @property
    def start_expr_hash(self) -> str:
        return self.hops[0].input_expr_hash

    @property
    def end_expr_hash(self) -> str:
        return self.hops[-1].output_expr_hash

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(hop.operation_id for hop in self.hops)

    @property
    def site_lineage_hashes(self) -> tuple[str, ...]:
        return tuple(hop.site.lineage_hash for hop in self.hops)

    def selection_payload(self) -> dict[str, object]:
        return {
            "root_id": self.root_id,
            "operation_ids": list(self.operation_ids),
            "site_lineage_hashes": list(self.site_lineage_hashes),
        }

    @property
    def chain_hash(self) -> str:
        return hash_canonical(
            {
                "kind": "sft1_wave4_preserving_chain_v1",
                "root_id": self.root_id,
                "hops": [hop.payload() for hop in self.hops],
            }
        )


@dataclass(frozen=True, slots=True)
class OrbitPolicy:
    """Lean-free composition and deterministic selection policy."""

    policy_id: str
    selection_salt: str
    operations: tuple[OperationSpec, ...]
    maximum_depth: int = 3
    maximum_variants_per_root: int = 5
    require_disjoint_or_transported_sites: bool = True
    reject_repeated_expr_hashes: bool = True
    reject_repeated_render_hashes: bool = True

    def __post_init__(self) -> None:
        _require_text("policy_id", self.policy_id)
        _require_text("selection_salt", self.selection_salt)
        if not 1 <= self.maximum_depth <= 3:
            raise OrbitError("maximum_depth must be between one and three")
        if not 1 <= self.maximum_variants_per_root <= 5:
            raise OrbitError("maximum_variants_per_root must be between one and five")
        if not self.operations:
            raise OrbitError("the preserving operation registry must be nonempty")
        operation_ids = [spec.operation_id for spec in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise OrbitError("the preserving operation registry has duplicate operation IDs")

    def operation_map(self) -> dict[str, OperationSpec]:
        return {spec.operation_id: spec for spec in self.operations}

    def payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "selection_salt": self.selection_salt,
            "operations": [spec.payload() for spec in self.operations],
            "maximum_depth": self.maximum_depth,
            "maximum_variants_per_root": self.maximum_variants_per_root,
            "require_disjoint_or_transported_sites": self.require_disjoint_or_transported_sites,
            "reject_repeated_expr_hashes": self.reject_repeated_expr_hashes,
            "reject_repeated_render_hashes": self.reject_repeated_render_hashes,
        }

    @property
    def policy_hash(self) -> str:
        return hash_canonical({"kind": "sft1_wave4_orbit_policy_v1", **self.payload()})


def _config_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise OrbitError(f"wave4 config field {field} must be a string-keyed mapping")
    return cast(Mapping[str, object], value)


def _config_sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise OrbitError(f"wave4 config field {field} must be a sequence")
    return cast(Sequence[object], value)


def _config_text(value: object, field: str) -> str:
    _require_text(f"wave4 config field {field}", value)
    return cast(str, value)


def _config_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise OrbitError(f"wave4 config field {field} must be an integer")
    return value


def policy_from_config(config: Mapping[str, object]) -> OrbitPolicy:
    """Build the executable orbit policy from the additive Wave 4 YAML mapping.

    The shared-edge closure emits one preserving and one negative-last variant
    per selected chain, so the two configured per-root limits must agree.
    Fields governing required composition safety may only be set to ``true``.
    """

    preserving = _config_mapping(config.get("preserving_operations"), "preserving_operations")
    enabled = _config_sequence(preserving.get("enabled"), "preserving_operations.enabled")
    specs: list[OperationSpec] = []
    for index, value in enumerate(enabled):
        entry = _config_mapping(value, f"preserving_operations.enabled[{index}]")
        specs.append(
            OperationSpec(
                operation_id=_config_text(entry.get("operation_id"), "operation_id"),
                mechanism=_config_text(entry.get("mechanism"), "mechanism"),
                superclass=_config_text(entry.get("superclass"), "superclass"),
                inverse_token=_config_text(entry.get("inverse_token"), "inverse_token"),
            )
        )

    composition = _config_mapping(config.get("composition"), "composition")
    if composition.get("negative_operation_position") != "last":
        raise OrbitError("wave4 config must require the negative operation last")
    required_true = (
        "distinct_mechanism_superclasses",
        "repeated_mechanisms_forbidden",
        "repeated_inverse_tokens_forbidden",
        "repeated_expression_hashes_forbidden",
        "repeated_render_hashes_forbidden",
        "repeated_site_lineages_forbidden",
        "sites_must_be_disjoint_or_checked_transported",
        "per_hop_direct_iff_certificate_required",
        "composite_iff_certificate_required",
        "exact_negative_last_refutation_required",
        "exact_typed_closure_required",
    )
    disabled = [field for field in required_true if composition.get(field) is not True]
    if disabled:
        raise OrbitError(f"wave4 config disables required safety fields: {disabled}")

    selection = _config_mapping(config.get("selection"), "selection")
    preserving_max = _config_int(
        selection.get("maximum_preserving_variants_per_root"),
        "selection.maximum_preserving_variants_per_root",
    )
    negative_max = _config_int(
        selection.get("maximum_negative_last_variants_per_root"),
        "selection.maximum_negative_last_variants_per_root",
    )
    if preserving_max != negative_max:
        raise OrbitError("shared-edge closure requires equal preserving and negative-last limits")
    return OrbitPolicy(
        policy_id=_config_text(config.get("wave_id"), "wave_id"),
        selection_salt=_config_text(selection.get("salt"), "selection.salt"),
        operations=tuple(specs),
        maximum_depth=_config_int(composition.get("maximum_depth"), "composition.maximum_depth"),
        maximum_variants_per_root=preserving_max,
        require_disjoint_or_transported_sites=True,
        reject_repeated_expr_hashes=True,
        reject_repeated_render_hashes=True,
    )


def validate_chain(chain: PreservingChain, policy: OrbitPolicy) -> None:
    """Fail closed unless a preserving chain satisfies the Wave 4 grammar."""

    if len(chain.hops) > policy.maximum_depth:
        raise OrbitError("preserving chain exceeds the configured depth")
    registry = policy.operation_map()
    mechanisms: set[str] = set()
    superclasses: set[str] = set()
    inverse_tokens: set[str] = set()
    lineage_hashes: set[str] = set()
    expression_hashes = {chain.start_expr_hash}
    render_hashes = {chain.hops[0].input_render_hash}
    previous_sites: list[SiteLineage] = []

    for index, hop in enumerate(chain.hops):
        spec = registry.get(hop.operation_id)
        if spec is None:
            raise OrbitError(f"operation {hop.operation_id!r} is absent from the policy")
        if hop.mechanism != spec.mechanism:
            raise OrbitError(f"mechanism metadata mismatch for {hop.operation_id}")
        if hop.superclass != spec.superclass:
            raise OrbitError(f"superclass metadata mismatch for {hop.operation_id}")
        if hop.inverse_token != spec.inverse_token:
            raise OrbitError(f"inverse-token metadata mismatch for {hop.operation_id}")
        if hop.mechanism in mechanisms:
            raise OrbitError("a preserving chain repeats a mechanism")
        if hop.superclass in superclasses:
            raise OrbitError("a preserving chain repeats a mechanism superclass")
        if hop.inverse_token in inverse_tokens:
            raise OrbitError("a preserving chain repeats an inverse token")
        mechanisms.add(hop.mechanism)
        superclasses.add(hop.superclass)
        inverse_tokens.add(hop.inverse_token)

        if index and chain.hops[index - 1].output_expr_hash != hop.input_expr_hash:
            raise OrbitError("preserving chain expression hashes do not link")
        if policy.reject_repeated_expr_hashes and hop.output_expr_hash in expression_hashes:
            raise OrbitError("preserving chain repeats a checked expression hash")
        if policy.reject_repeated_render_hashes and hop.output_render_hash in render_hashes:
            raise OrbitError("preserving chain repeats a goal_v1 render hash")
        expression_hashes.add(hop.output_expr_hash)
        render_hashes.add(hop.output_render_hash)

        lineage_hash = hop.site.lineage_hash
        if lineage_hash in lineage_hashes:
            raise OrbitError("preserving chain repeats an exact selected-site lineage")
        lineage_hashes.add(lineage_hash)
        if policy.require_disjoint_or_transported_sites:
            previous_hashes = {site.lineage_hash for site in previous_sites}
            transports = set(hop.site.transported_from)
            unknown = transports.difference(previous_hashes)
            if unknown:
                raise OrbitError("site transport references a site outside the preceding chain")
            overlapping = {
                site.lineage_hash
                for site in previous_sites
                if _paths_overlap(site.origin_path, hop.site.origin_path)
            }
            if not overlapping.issubset(transports):
                raise OrbitError("overlapping preserving sites lack checked transport evidence")
        previous_sites.append(hop.site)


@dataclass(frozen=True, slots=True)
class CertifiedPair:
    """A unique physical training row plus the hashes kept in its keyed sidecar."""

    root_id: str
    reference: str
    candidate: str
    label: bool
    reference_expr_hash: str
    candidate_expr_hash: str
    operation_chain_hash: str
    selected_site_lineage_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        for field in ("root_id", "reference", "candidate"):
            _require_text(f"pair.{field}", getattr(self, field))
        for field in (
            "reference_expr_hash",
            "candidate_expr_hash",
            "operation_chain_hash",
            "selected_site_lineage_hash",
            "evidence_hash",
        ):
            _require_hash(f"pair.{field}", getattr(self, field))
        if type(self.label) is not bool:
            raise OrbitError("pair.label must be a boolean")
        if self.reference_expr_hash == self.candidate_expr_hash:
            raise OrbitError("a certified pair cannot be an expression self-pair")
        if self.reference == self.candidate:
            raise OrbitError("a certified pair cannot be a rendered self-pair")

    def identity_payload(self) -> dict[str, object]:
        return {
            "root_id": self.root_id,
            "reference_expr_hash": self.reference_expr_hash,
            "candidate_expr_hash": self.candidate_expr_hash,
            "reference_render_hash": render_hash(self.reference),
            "candidate_render_hash": render_hash(self.candidate),
            "operation_chain_hash": self.operation_chain_hash,
            "selected_site_lineage_hash": self.selected_site_lineage_hash,
            "label": self.label,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def pair_id(self) -> str:
        return make_id(PAIR_PREFIX, self.identity_payload())

    @property
    def unordered_pair_key(self) -> str:
        return unordered_pair_key(render_hash(self.reference), render_hash(self.candidate))

    def model_row(self) -> dict[str, object]:
        return {"reference": self.reference, "candidate": self.candidate, "label": self.label}


@dataclass(frozen=True, slots=True)
class CertifiedNegativeEdge:
    """One source-proved/reference-refuted negative edge."""

    operation_id: str
    mechanism: str
    site_lineage_hash: str
    certificate_hash: str
    proved_expr_hash: str
    refuted_expr_hash: str
    pair: CertifiedPair

    def __post_init__(self) -> None:
        _require_text("negative.operation_id", self.operation_id)
        _require_text("negative.mechanism", self.mechanism)
        for field in (
            "site_lineage_hash",
            "certificate_hash",
            "proved_expr_hash",
            "refuted_expr_hash",
        ):
            _require_hash(f"negative.{field}", getattr(self, field))
        if self.pair.label:
            raise OrbitError("a certified negative edge must have label false")
        if {self.pair.reference_expr_hash, self.pair.candidate_expr_hash} != {
            self.proved_expr_hash,
            self.refuted_expr_hash,
        }:
            raise OrbitError("negative pair endpoints do not match its proved/refuted expressions")

    @property
    def edge_id(self) -> str:
        return make_id(
            VARIANT_PREFIX,
            {
                "kind": "sft1_wave4_negative_edge_v1",
                "operation_id": self.operation_id,
                "mechanism": self.mechanism,
                "site_lineage_hash": self.site_lineage_hash,
                "certificate_hash": self.certificate_hash,
                "proved_expr_hash": self.proved_expr_hash,
                "refuted_expr_hash": self.refuted_expr_hash,
                "pair_id": self.pair.pair_id,
            },
        )


@dataclass(frozen=True, slots=True)
class ClosureGroup:
    """One logical four-edge closure; physical rows may share its base edge."""

    root_id: str
    base_negative: CertifiedNegativeEdge
    terminal_negative: CertifiedNegativeEdge
    reference_chain: PreservingChain
    candidate_chain: PreservingChain
    preserving_reference: CertifiedPair
    preserving_candidate: CertifiedPair
    closure_certificate_hash: str

    def __post_init__(self) -> None:
        _require_text("closure.root_id", self.root_id)
        _require_hash("closure.certificate_hash", self.closure_certificate_hash)
        roots = {
            self.root_id,
            self.base_negative.pair.root_id,
            self.terminal_negative.pair.root_id,
            self.reference_chain.root_id,
            self.candidate_chain.root_id,
            self.preserving_reference.root_id,
            self.preserving_candidate.root_id,
        }
        if len(roots) != 1:
            raise OrbitError("all closure members must share one ancestry root")
        if self.base_negative.operation_id != self.terminal_negative.operation_id:
            raise OrbitError("terminal negative must replay the base negative operation")
        if self.base_negative.mechanism != self.terminal_negative.mechanism:
            raise OrbitError("terminal negative must replay the base negative mechanism")
        if self.reference_chain.start_expr_hash != self.base_negative.proved_expr_hash:
            raise OrbitError("reference preserving chain does not start at the proved root")
        if self.candidate_chain.start_expr_hash != self.base_negative.refuted_expr_hash:
            raise OrbitError("candidate preserving chain does not start at the refuted root")
        if self.reference_chain.end_expr_hash != self.terminal_negative.proved_expr_hash:
            raise OrbitError("negative-last proved endpoint is not the preserving-chain result")
        if self.candidate_chain.end_expr_hash != self.terminal_negative.refuted_expr_hash:
            raise OrbitError("negative-last refuted endpoint is not the transported result")
        self._require_positive_edge(
            "preserving_reference",
            self.preserving_reference,
            self.reference_chain.start_expr_hash,
            self.reference_chain.end_expr_hash,
        )
        self._require_positive_edge(
            "preserving_candidate",
            self.preserving_candidate,
            self.candidate_chain.start_expr_hash,
            self.candidate_chain.end_expr_hash,
        )

    @staticmethod
    def _require_positive_edge(
        role: str, pair: CertifiedPair, start_hash: str, end_hash: str
    ) -> None:
        if not pair.label:
            raise OrbitError(f"{role} must have label true")
        if {pair.reference_expr_hash, pair.candidate_expr_hash} != {start_hash, end_hash}:
            raise OrbitError(f"{role} endpoints do not close its preserving chain")

    @property
    def logical_pairs(self) -> tuple[tuple[EdgeRole, CertifiedPair], ...]:
        return (
            ("preserving_reference", self.preserving_reference),
            ("preserving_candidate", self.preserving_candidate),
            ("negative_base", self.base_negative.pair),
            ("negative_last", self.terminal_negative.pair),
        )

    @property
    def operation_id(self) -> str:
        return self.base_negative.operation_id

    @property
    def mechanism(self) -> str:
        return self.base_negative.mechanism

    @property
    def row_ids(self) -> tuple[str, ...]:
        return tuple(pair.pair_id for _, pair in self.logical_pairs)

    def selection_payload(self) -> dict[str, object]:
        return {
            "root_id": self.root_id,
            "negative_operation": self.base_negative.operation_id,
            "base_negative_site_lineage_hash": self.base_negative.site_lineage_hash,
            "terminal_negative_site_lineage_hash": self.terminal_negative.site_lineage_hash,
            "reference_chain": self.reference_chain.selection_payload(),
            "candidate_chain": self.candidate_chain.selection_payload(),
        }

    @property
    def selection_hash(self) -> str:
        return hash_canonical(
            {"kind": "sft1_wave4_closure_selection_identity_v1", **self.selection_payload()}
        )

    @property
    def group_id(self) -> str:
        return make_id(
            VARIANT_PREFIX,
            {
                "kind": "sft1_wave4_certificate_closure_v1",
                **self.selection_payload(),
                "base_negative_edge_id": self.base_negative.edge_id,
                "reference_chain_hash": self.reference_chain.chain_hash,
                "candidate_chain_hash": self.candidate_chain.chain_hash,
                "terminal_negative_edge_id": self.terminal_negative.edge_id,
                "logical_pairs": {role: pair.pair_id for role, pair in self.logical_pairs},
                "closure_certificate_hash": self.closure_certificate_hash,
            },
        )


def validate_closure_group(group: ClosureGroup, policy: OrbitPolicy) -> None:
    """Validate both preserving paths and their exact negative-last alignment."""

    validate_chain(group.reference_chain, policy)
    validate_chain(group.candidate_chain, policy)
    if len(group.reference_chain.hops) != len(group.candidate_chain.hops):
        raise OrbitError("the two sides of a closure must have equal preserving depth")
    reference_classes = tuple(hop.superclass for hop in group.reference_chain.hops)
    candidate_classes = tuple(hop.superclass for hop in group.candidate_chain.hops)
    if reference_classes != candidate_classes:
        raise OrbitError("the transported chain changes preserving mechanism superclasses")


def select_closure_groups(
    groups: tuple[ClosureGroup, ...], policy: OrbitPolicy
) -> tuple[ClosureGroup, ...]:
    """Select at most five stable op/site variants for each ancestry root.

    Exact duplicates are collapsed.  Two results with the same root, negative
    edge, operation chain, and selected-site lineages but different certified
    content are a deterministic-identity conflict and fail closed.
    """

    by_selection: dict[str, ClosureGroup] = {}
    for group in groups:
        validate_closure_group(group, policy)
        previous = by_selection.get(group.selection_hash)
        if previous is None:
            by_selection[group.selection_hash] = group
        elif previous != group:
            raise OrbitError("one exact operation/site selection produced conflicting groups")

    per_root: dict[str, list[ClosureGroup]] = {}
    for group in by_selection.values():
        per_root.setdefault(group.root_id, []).append(group)

    selected: list[ClosureGroup] = []
    for root_id in sorted(per_root):
        ranked = sorted(
            per_root[root_id],
            key=lambda group: (
                hash_canonical(
                    {
                        "salt": policy.selection_salt,
                        "root_id": root_id,
                        "negative_operation": group.base_negative.operation_id,
                        "base_negative_site_hash": group.base_negative.site_lineage_hash,
                        "terminal_negative_site_hash": group.terminal_negative.site_lineage_hash,
                        "reference_operation_chain": list(group.reference_chain.operation_ids),
                        "reference_site_hashes": list(group.reference_chain.site_lineage_hashes),
                        "candidate_operation_chain": list(group.candidate_chain.operation_ids),
                        "candidate_site_hashes": list(group.candidate_chain.site_lineage_hashes),
                    }
                ),
                group.group_id,
            ),
        )
        selected.extend(ranked[: policy.maximum_variants_per_root])
    return tuple(selected)


def cap_negative_operation_share[GroupT: NegativeOperationGroup](
    groups: Sequence[GroupT],
    operation_id: str,
    maximum_share: float,
    *,
    selection_salt: str = "sft1_negative_operation_share_cap_v1",
) -> NegativeOperationShareCapResult[GroupT]:
    """Retain whole groups while bounding one operation's physical-row share.

    Every non-target group is retained.  Target groups are ranked by a hash of
    their complete semantic identity and admitted only when all of their rows
    fit.  A physical row is charged to ``operation_id`` whenever it belongs to
    a selected target group; shared row IDs are counted once.
    """

    _require_text("operation_id", operation_id)
    _require_text("selection_salt", selection_salt)
    if isinstance(maximum_share, bool) or not isinstance(maximum_share, int | float):
        raise OrbitError("maximum_share must be a number")
    try:
        share = Fraction(str(maximum_share))
    except (ValueError, ZeroDivisionError) as ex:
        raise OrbitError("maximum_share must be finite") from ex
    if not 0 <= share <= 1:
        raise OrbitError("maximum_share must be between zero and one")

    by_group_id: dict[str, GroupT] = {}
    group_rows: dict[str, frozenset[str]] = {}
    for group in groups:
        _require_text("group.group_id", group.group_id)
        _require_text("group.operation_id", group.operation_id)
        _require_text("group.mechanism", group.mechanism)
        rows = tuple(group.row_ids)
        if not rows:
            raise OrbitError("a release group must contain at least one row")
        for row_id in rows:
            _require_text("group.row_ids", row_id)
        if len(rows) != len(set(rows)):
            raise OrbitError("a release group must not repeat a physical row ID")
        if group.group_id in by_group_id:
            raise OrbitError("duplicate release group ID")
        by_group_id[group.group_id] = group
        group_rows[group.group_id] = frozenset(rows)

    target_groups = [group for group in by_group_id.values() if group.operation_id == operation_id]
    target_mechanisms = sorted({group.mechanism for group in target_groups})
    if len(target_mechanisms) > 1:
        raise OrbitError("one negative operation maps to multiple mechanisms")

    non_target_groups = [
        group for group in by_group_id.values() if group.operation_id != operation_id
    ]
    selected_ids = {group.group_id for group in non_target_groups}
    selected_rows: set[str] = set()
    for group in non_target_groups:
        selected_rows.update(group_rows[group.group_id])
    selected_operation_rows: set[str] = set()

    ranked_targets = sorted(
        target_groups,
        key=lambda group: (
            hash_canonical(
                {
                    "kind": "sft1_negative_operation_share_cap_rank_v1",
                    "selection_salt": selection_salt,
                    "operation_id": group.operation_id,
                    "mechanism": group.mechanism,
                    "group_id": group.group_id,
                    "row_ids": sorted(group_rows[group.group_id]),
                }
            ),
            group.group_id,
        ),
    )
    dropped_ids: list[str] = []
    for group in ranked_targets:
        target_row_ids = group_rows[group.group_id]
        candidate_rows = selected_rows.union(target_row_ids)
        candidate_operation_rows = selected_operation_rows.union(target_row_ids)
        maximum_operation_rows = len(candidate_rows) * share.numerator // share.denominator
        if len(candidate_operation_rows) <= maximum_operation_rows:
            selected_ids.add(group.group_id)
            selected_rows = candidate_rows
            selected_operation_rows = candidate_operation_rows
        else:
            dropped_ids.append(group.group_id)

    input_rows = set().union(*group_rows.values()) if group_rows else set()
    input_operation_rows: set[str] = set()
    for group in target_groups:
        input_operation_rows.update(group_rows[group.group_id])
    selected_groups = tuple(by_group_id[group_id] for group_id in sorted(selected_ids))
    selected_operation_group_count = sum(
        group.operation_id == operation_id for group in selected_groups
    )
    maximum_operation_row_count = len(selected_rows) * share.numerator // share.denominator
    report = NegativeOperationShareCapReport(
        operation_id=operation_id,
        mechanism=target_mechanisms[0] if target_mechanisms else None,
        maximum_share=float(share),
        selection_salt=selection_salt,
        input_group_count=len(by_group_id),
        selected_group_count=len(selected_groups),
        dropped_group_count=len(dropped_ids),
        operation_input_group_count=len(target_groups),
        operation_selected_group_count=selected_operation_group_count,
        operation_dropped_group_count=len(target_groups) - selected_operation_group_count,
        input_row_count=len(input_rows),
        selected_row_count=len(selected_rows),
        dropped_row_count=len(input_rows.difference(selected_rows)),
        operation_input_row_count=len(input_operation_rows),
        operation_selected_row_count=len(selected_operation_rows),
        operation_dropped_row_count=len(input_operation_rows.difference(selected_operation_rows)),
        maximum_operation_row_count=maximum_operation_row_count,
        dropped_group_ids=tuple(sorted(dropped_ids)),
    )
    return NegativeOperationShareCapResult(selected_groups=selected_groups, report=report)


@dataclass(frozen=True, slots=True)
class ClosureGroupIndex:
    """A logical closure whose pair IDs point into unique physical rows."""

    group_id: str
    root_id: str
    negative_edge_id: str
    logical_pair_ids: tuple[tuple[EdgeRole, str], ...]
    closure_certificate_hash: str

    def record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "group_id": self.group_id,
            "root_id": self.root_id,
            "negative_edge_id": self.negative_edge_id,
            "logical_pair_ids": dict(self.logical_pair_ids),
            "closure_certificate_hash": self.closure_certificate_hash,
        }


@dataclass(frozen=True, slots=True)
class MaterializedClosure:
    """Unique physical rows together with complete logical closure indices."""

    pairs: tuple[CertifiedPair, ...]
    groups: tuple[ClosureGroupIndex, ...]
    pair_group_ids: tuple[tuple[str, tuple[str, ...]], ...]

    def model_rows(self) -> tuple[dict[str, object], ...]:
        return tuple(pair.model_row() for pair in self.pairs)

    def sidecars(self) -> tuple[dict[str, object], ...]:
        memberships = dict(self.pair_group_ids)
        return tuple(
            {
                "pair_id": pair.pair_id,
                "root_id": pair.root_id,
                "closure_group_ids": list(memberships[pair.pair_id]),
                "reference_expr_hash": pair.reference_expr_hash,
                "candidate_expr_hash": pair.candidate_expr_hash,
                "reference_render_hash": render_hash(pair.reference),
                "candidate_render_hash": render_hash(pair.candidate),
                "operation_chain_hash": pair.operation_chain_hash,
                "selected_site_lineage_hash": pair.selected_site_lineage_hash,
                "evidence_hash": pair.evidence_hash,
                "unordered_pair_key": pair.unordered_pair_key,
            }
            for pair in self.pairs
        )

    def group_records(self) -> tuple[dict[str, object], ...]:
        return tuple(group.record() for group in self.groups)


def materialize_closure_groups(
    groups: tuple[ClosureGroup, ...], policy: OrbitPolicy
) -> MaterializedClosure:
    """Materialize closure groups without duplicating their shared base rows.

    A certified base negative may be referenced by several logical groups.  All
    other pair sharing is rejected: it indicates a cycle, duplicate variant, or
    an identity bug.  Distinct pair IDs with the same unordered model-facing
    text are also rejected, including conflicting labels.
    """

    by_group_id: dict[str, ClosureGroup] = {}
    by_selection: dict[str, str] = {}
    roots: dict[str, int] = {}
    for group in groups:
        validate_closure_group(group, policy)
        previous = by_group_id.get(group.group_id)
        if previous is not None:
            raise OrbitError("duplicate closure group ID")
        conflicting_group_id = by_selection.get(group.selection_hash)
        if conflicting_group_id is not None:
            raise OrbitError(
                "materialization received more than one exact operation/site selection"
            )
        by_group_id[group.group_id] = group
        by_selection[group.selection_hash] = group.group_id
        roots[group.root_id] = roots.get(group.root_id, 0) + 1
        if roots[group.root_id] > policy.maximum_variants_per_root:
            raise OrbitError("materialization exceeds the per-root selected-variant bound")

    pairs: dict[str, CertifiedPair] = {}
    memberships: dict[str, list[str]] = {}
    roles: dict[str, set[EdgeRole]] = {}
    base_edges: dict[str, set[str]] = {}
    unordered: dict[str, tuple[str, bool]] = {}
    indices: list[ClosureGroupIndex] = []

    for group_id in sorted(by_group_id):
        group = by_group_id[group_id]
        logical_ids: list[tuple[EdgeRole, str]] = []
        for role, pair in group.logical_pairs:
            pair_id = pair.pair_id
            logical_ids.append((role, pair_id))
            previous_pair = pairs.get(pair_id)
            if previous_pair is not None and previous_pair != pair:
                raise OrbitError("pair ID collision across closure groups")
            pair_roles = roles.setdefault(pair_id, set())
            if previous_pair is not None and (role != "negative_base" or pair_roles != {role}):
                raise OrbitError("only one identical base-negative row may be shared")
            pairs.setdefault(pair_id, pair)
            pair_roles.add(role)
            memberships.setdefault(pair_id, []).append(group_id)
            if role == "negative_base":
                base_edges.setdefault(pair_id, set()).add(group.base_negative.edge_id)

            owner = unordered.get(pair.unordered_pair_key)
            if owner is None:
                unordered[pair.unordered_pair_key] = (pair_id, pair.label)
            elif owner[0] != pair_id:
                kind = "conflicting labels" if owner[1] != pair.label else "duplicate pair"
                raise OrbitError(f"{kind} across physical closure rows")

        indices.append(
            ClosureGroupIndex(
                group_id=group_id,
                root_id=group.root_id,
                negative_edge_id=group.base_negative.edge_id,
                logical_pair_ids=tuple(logical_ids),
                closure_certificate_hash=group.closure_certificate_hash,
            )
        )

    for pair_id, edge_ids in base_edges.items():
        if len(edge_ids) != 1:
            raise OrbitError(f"shared base pair {pair_id} names multiple negative edges")

    ordered_pairs = tuple(pairs[pair_id] for pair_id in sorted(pairs))
    pair_group_ids = tuple(
        (pair_id, tuple(sorted(group_ids))) for pair_id, group_ids in sorted(memberships.items())
    )
    physical_pair_ids = {pair.pair_id for pair in ordered_pairs}
    for index in indices:
        if {pair_id for _, pair_id in index.logical_pair_ids}.difference(physical_pair_ids):
            raise OrbitError("closure group references a missing physical row")
        if tuple(role for role, _ in index.logical_pair_ids) != EDGE_ROLES:
            raise OrbitError("closure group is partial or has a noncanonical edge order")
    return MaterializedClosure(ordered_pairs, tuple(indices), pair_group_ids)
