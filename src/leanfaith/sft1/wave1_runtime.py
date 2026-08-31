"""Fail-closed Wave 1 composition, P01 identity, cap, and dedup runtime.

This module performs deterministic bookkeeping over complete endpoint and
typed-certificate receipts.  It does not invoke Lean, render expressions,
create model-facing rows, or admit an operation.  A live adapter must replay a
certificate in persistent Meta before it can construct the literal-true
receipt accepted here.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.sft1.p01_identity_policy import (
    EXPECTED_APPROVED_V0_3_5_SEMANTIC_HASH,
    EXPECTED_OVERLAY_SEMANTIC_HASH,
    load_p01_identity_policy,
)
from leanfaith.sft1.wave1_readiness import load_wave1_implementation_readiness

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
OperationId = Annotated[str, Field(pattern=r"^[PN][0-9]{2}_[A-Z0-9_]+_V[0-9]+$", strict=True)]
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]

P01_OPERATION_ID = "P01_ALPHA_RENAME_SINGLE_V1"
P01_POLICY_SEMANTIC_HASH: Final[
    Literal["a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd"]
] = "a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd"
P01_CORRECTED_ENVELOPE_HASH: Final[
    Literal["dcdd6c07a83aa84faf81b448e2732121027b5a93fc89512caa38035b9c4cdbe4"]
] = "dcdd6c07a83aa84faf81b448e2732121027b5a93fc89512caa38035b9c4cdbe4"

_REPR_SPEC_HASH = "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"
_RENDERER_API_HASH = "c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"
_UNIVERSE_PROFILE_ID = "goal_v1_first_occurrence_u_i_v1"
_UNIVERSE_PROFILE_HASH = "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
_RENDER_CONTEXT_ID = "goal_v1_render_context_v1"
_RENDER_CONTEXT_HASH = "5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"
_SUBEXPR_PATH = re.compile(r"^/(?:[0-3](?:/[0-3])*)?$")

_EXPECTED_OPERATION_TOKENS = {
    "P01_ALPHA_RENAME_SINGLE_V1": ("presentation_alpha", "P01_ALPHA_RENAME"),
    "P15_SWAP_IFF_SIDES_V1": ("logical_symmetry", "P15_IFF_SWAP"),
    "P18_SYMMETRIZE_EQUALITY_V1": ("relation_symmetry", "P18_EQUALITY_SYMMETRY"),
    "P21_BETA_REDUCE_V1": ("definitional_beta", "P21_BETA_INTRO_REDUCE"),
    "N31_DROP_REQUIRED_GUARD_RUBRIC_V1": (
        "required_guard_mutation",
        "N31_DROP_REQUIRED_GUARD",
    ),
}

_P01_BINDER_FINGERPRINT_BASIS = "sft1_p01_binder_aware_endpoint_fingerprint_v0_3_6"


def p01_outer_binder_site_path(ordinal: int) -> str:
    """Return the frozen ``outerBinderSite`` path for one binder ordinal."""

    if isinstance(ordinal, bool) or ordinal < 0:
        raise Wave1RuntimeError("P01 selected-site ordinal must be a natural number")
    return "/" if ordinal == 0 else "/" + "/".join("1" for _ in range(ordinal))


def compute_p01_binder_aware_fingerprint(
    *,
    endpoint_role: Literal["source", "candidate"],
    closed_expr_hash: str,
    sidecar_sha256: str,
    selected_site_path: str,
    selected_site_ordinal: int,
    binder_name: str,
    binder_info: Literal["default"],
) -> str:
    """Bind the P01 name field to its endpoint, site, ordinal, and BinderInfo."""

    return hash_canonical(
        {
            "basis_id": _P01_BINDER_FINGERPRINT_BASIS,
            "operation_id": P01_OPERATION_ID,
            "endpoint_role": endpoint_role,
            "closed_expr_hash": closed_expr_hash,
            "complete_sidecar_sha256": sidecar_sha256,
            "selected_site_path": selected_site_path,
            "selected_site_ordinal": selected_site_ordinal,
            "binder_name": binder_name,
            "binder_info": binder_info,
        }
    )


def compute_p01_selected_site_lineage_hash(
    *, selected_site_path: str, selected_site_ordinal: int
) -> str:
    """Bind P01 lineage to the exact frozen outer-binder selector and site."""

    return hash_canonical(
        {
            "basis_id": "sft1_p01_selected_site_lineage_v0_3_6",
            "operation_id": P01_OPERATION_ID,
            "selector": {
                "kind": "outerBinder",
                "ordinal": selected_site_ordinal,
            },
            "selected_site_path": selected_site_path,
        }
    )


class Wave1RuntimeError(ValueError):
    """Raised when a composition, cap, or duplicate contract fails closed."""


class RuntimeEndpoint(StrictModel):
    """Complete hash identity of one canonical closed-Expr sidecar."""

    closed_expr_hash: Sha256
    render_hash: Sha256
    core_text_sha256: Sha256
    complete_sidecar_sha256: Sha256
    render_request_hash: Sha256
    render_scope_id: NonEmptyStr
    repr_spec_hash: Literal["68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"]
    renderer_api_hash: Literal["c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d"]
    universe_profile_id: Literal["goal_v1_first_occurrence_u_i_v1"]
    universe_profile_hash: Literal[
        "d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"
    ]
    render_context_id: Literal["goal_v1_render_context_v1"]
    render_context_hash: Literal["5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"]


class P01NameOnlyDelta(StrictModel):
    """P01-specific payload embedded in a replayed typed certificate."""

    old_name: NonEmptyStr
    new_name: NonEmptyStr
    binder_info: Literal["default"]
    selected_site_ordinal: int = Field(ge=0, strict=True)
    selected_site_rediscovery_count: Literal[1]
    domains_unchanged: Literal[True]
    bodies_unchanged_except_selected_name: Literal[True]
    bound_variable_indices_unchanged: Literal[True]
    universes_unchanged: Literal[True]
    metadata_unchanged: Literal[True]
    other_binders_unchanged: Literal[True]
    binder_info_unchanged: Literal[True]

    @model_validator(mode="after")
    def _actual_name_delta(self) -> P01NameOnlyDelta:
        if self.old_name == self.new_name:
            raise ValueError("P01 certificate must bind a nontrivial name delta")
        return self


class TypedCertificateReceipt(StrictModel):
    """Hash-bound result of a real typed certificate replay."""

    operation_id: OperationId
    source_closed_expr_hash: Sha256
    candidate_closed_expr_hash: Sha256
    source_sidecar_sha256: Sha256
    candidate_sidecar_sha256: Sha256
    render_request_hash: Sha256
    replay_request_hash: Sha256
    selected_site_path: NonEmptyStr
    selected_site_path_fingerprint: Sha256
    selected_site_lineage_hash: Sha256
    binder_aware_source_fingerprint: Sha256
    binder_aware_candidate_fingerprint: Sha256
    selected_site_uniquely_rediscovered: Literal[True]
    replayed_in_persistent_meta: Literal[True]
    certificate_replay_passed: Literal[True]
    candidate_is_exact_deterministic_replay_result: Literal[True]
    p01_name_only_delta: P01NameOnlyDelta | None = None

    @model_validator(mode="after")
    def _operation_specific_shape(self) -> TypedCertificateReceipt:
        if _SUBEXPR_PATH.fullmatch(self.selected_site_path) is None:
            raise ValueError("selected site path is not a canonical Lean SubExpr.Pos path")
        if self.selected_site_path_fingerprint != hash_canonical(self.selected_site_path):
            raise ValueError("selected site path fingerprint mismatch")
        if self.replay_request_hash != self.render_request_hash:
            raise ValueError(
                "certificate replay and endpoint rendering must share one Meta request"
            )
        if (self.operation_id == P01_OPERATION_ID) != (self.p01_name_only_delta is not None):
            raise ValueError("P01 name-only payload presence must match the P01 operation")
        if self.p01_name_only_delta is not None:
            delta = self.p01_name_only_delta
            expected_path = p01_outer_binder_site_path(delta.selected_site_ordinal)
            if self.selected_site_path != expected_path:
                raise ValueError("P01 binder ordinal and selected-site path disagree")
            if self.selected_site_lineage_hash != compute_p01_selected_site_lineage_hash(
                selected_site_path=self.selected_site_path,
                selected_site_ordinal=delta.selected_site_ordinal,
            ):
                raise ValueError("P01 selected-site lineage mismatch")
            expected_source_fingerprint = compute_p01_binder_aware_fingerprint(
                endpoint_role="source",
                closed_expr_hash=self.source_closed_expr_hash,
                sidecar_sha256=self.source_sidecar_sha256,
                selected_site_path=self.selected_site_path,
                selected_site_ordinal=delta.selected_site_ordinal,
                binder_name=delta.old_name,
                binder_info=delta.binder_info,
            )
            expected_candidate_fingerprint = compute_p01_binder_aware_fingerprint(
                endpoint_role="candidate",
                closed_expr_hash=self.candidate_closed_expr_hash,
                sidecar_sha256=self.candidate_sidecar_sha256,
                selected_site_path=self.selected_site_path,
                selected_site_ordinal=delta.selected_site_ordinal,
                binder_name=delta.new_name,
                binder_info=delta.binder_info,
            )
            if (
                self.binder_aware_source_fingerprint != expected_source_fingerprint
                or self.binder_aware_candidate_fingerprint != expected_candidate_fingerprint
            ):
                raise ValueError("P01 binder-aware endpoint fingerprint mismatch")
        if self.binder_aware_source_fingerprint == self.binder_aware_candidate_fingerprint:
            raise ValueError("typed certificate must bind distinct endpoint fingerprints")
        return self


# Compatibility name retained for the focused public runtime surface.
P01NameOnlyCertificateReceipt = TypedCertificateReceipt


class RuntimeEdge(StrictModel):
    operation_id: OperationId
    mechanism_superclass: NonEmptyStr
    inverse_token: NonEmptyStr
    registry_entry_hash: Sha256
    anchor_hash: Sha256
    operation_bank_entry_hash: Sha256
    certificate_payload_hash: Sha256
    certificate: TypedCertificateReceipt

    @model_validator(mode="after")
    def _certificate_is_content_bound(self) -> RuntimeEdge:
        if self.certificate.operation_id != self.operation_id:
            raise ValueError("typed certificate operation does not match its runtime edge")
        expected_hash = hash_canonical(self.certificate.model_dump(mode="json"))
        if self.certificate_payload_hash != expected_hash:
            raise ValueError("typed certificate payload hash mismatch")
        return self


class RuntimeChain(StrictModel):
    root_ancestry_id: NonEmptyStr
    source_identity_hash: Sha256
    polarity: Literal["positive", "negative"]
    label: Literal[0, 1]
    endpoints: tuple[RuntimeEndpoint, ...] = Field(min_length=2, max_length=4)
    edges: tuple[RuntimeEdge, ...] = Field(min_length=1, max_length=3)
    operation_chain_hash: Sha256
    selected_site_lineage_hash: Sha256
    evidence_certificate_payload_hash: Sha256
    stable_row_hash: Sha256

    @model_validator(mode="after")
    def _shape_and_polarity(self) -> RuntimeChain:
        if len(self.endpoints) != len(self.edges) + 1:
            raise ValueError("runtime chain must have one more endpoint than edge")
        negative_count = sum(edge.operation_id.startswith("N") for edge in self.edges)
        if self.polarity == "positive":
            if self.label != 1 or negative_count != 0:
                raise ValueError("positive chain must be label 1 with zero negative edges")
        elif (
            self.label != 0
            or negative_count != 1
            or not self.edges[-1].operation_id.startswith("N")
        ):
            raise ValueError("negative chain must be label 0 with one final negative edge")
        expected_operation_chain_hash = hash_canonical(
            [edge.model_dump(mode="json") for edge in self.edges]
        )
        if self.operation_chain_hash != expected_operation_chain_hash:
            raise ValueError("operation chain hash mismatch")
        expected_lineage_hash = hash_canonical(
            [edge.certificate.selected_site_lineage_hash for edge in self.edges]
        )
        if self.selected_site_lineage_hash != expected_lineage_hash:
            raise ValueError("selected-site lineage aggregate hash mismatch")
        expected_evidence_hash = hash_canonical(
            [edge.certificate_payload_hash for edge in self.edges]
        )
        if self.evidence_certificate_payload_hash != expected_evidence_hash:
            raise ValueError("certificate aggregate hash mismatch")
        expected_stable_hash = compute_stable_row_hash(
            root_ancestry_id=self.root_ancestry_id,
            source_identity_hash=self.source_identity_hash,
            reference_closed_expr_hash=self.endpoints[0].closed_expr_hash,
            candidate_closed_expr_hash=self.endpoints[-1].closed_expr_hash,
            operation_chain_hash=self.operation_chain_hash,
            selected_site_lineage_hash=self.selected_site_lineage_hash,
            label=self.label,
            evidence_certificate_payload_hash=self.evidence_certificate_payload_hash,
        )
        if self.stable_row_hash != expected_stable_hash:
            raise ValueError("stable row hash mismatch")
        return self


def compute_stable_row_hash(
    *,
    root_ancestry_id: str,
    source_identity_hash: str,
    reference_closed_expr_hash: str,
    candidate_closed_expr_hash: str,
    operation_chain_hash: str,
    selected_site_lineage_hash: str,
    label: int,
    evidence_certificate_payload_hash: str,
) -> str:
    """Replay the frozen 12-field stable-row selection identity."""

    return hash_canonical(
        {
            "root_ancestry_id": root_ancestry_id,
            "source_identity_hash": source_identity_hash,
            "reference_closed_expr_hash": reference_closed_expr_hash,
            "candidate_closed_expr_hash": candidate_closed_expr_hash,
            "operation_chain_hash": operation_chain_hash,
            "selected_site_lineage_hash": selected_site_lineage_hash,
            "label": label,
            "evidence_certificate_payload_hash": evidence_certificate_payload_hash,
            "renderer_api_hash": _RENDERER_API_HASH,
            "repr_spec_hash": _REPR_SPEC_HASH,
            "canonical_universe_profile_hash": _UNIVERSE_PROFILE_HASH,
            "render_context_hash": _RENDER_CONTEXT_HASH,
        }
    )


def make_runtime_chain(
    *,
    root_ancestry_id: str,
    source_identity_hash: str,
    polarity: Literal["positive", "negative"],
    label: Literal[0, 1],
    endpoints: tuple[RuntimeEndpoint, ...],
    edges: tuple[RuntimeEdge, ...],
) -> RuntimeChain:
    """Construct a chain while deriving every aggregate and stable-row hash."""

    operation_chain_hash = hash_canonical([edge.model_dump(mode="json") for edge in edges])
    selected_site_lineage_hash = hash_canonical(
        [edge.certificate.selected_site_lineage_hash for edge in edges]
    )
    evidence_certificate_payload_hash = hash_canonical(
        [edge.certificate_payload_hash for edge in edges]
    )
    stable_row_hash = compute_stable_row_hash(
        root_ancestry_id=root_ancestry_id,
        source_identity_hash=source_identity_hash,
        reference_closed_expr_hash=endpoints[0].closed_expr_hash,
        candidate_closed_expr_hash=endpoints[-1].closed_expr_hash,
        operation_chain_hash=operation_chain_hash,
        selected_site_lineage_hash=selected_site_lineage_hash,
        label=label,
        evidence_certificate_payload_hash=evidence_certificate_payload_hash,
    )
    return RuntimeChain(
        root_ancestry_id=root_ancestry_id,
        source_identity_hash=source_identity_hash,
        polarity=polarity,
        label=label,
        endpoints=endpoints,
        edges=edges,
        operation_chain_hash=operation_chain_hash,
        selected_site_lineage_hash=selected_site_lineage_hash,
        evidence_certificate_payload_hash=evidence_certificate_payload_hash,
        stable_row_hash=stable_row_hash,
    )


class OperationRuntimeBinding(StrictModel):
    operation_id: OperationId
    mechanism_superclass: NonEmptyStr
    inverse_token: NonEmptyStr
    registry_entry_hash: Sha256
    anchor_hash: Sha256
    operation_bank_entry_hash: Sha256
    runtime_activated: bool = Field(strict=True)


class P01RuntimeBinding(StrictModel):
    required_policy_semantic_hash: Literal[
        "a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd"
    ]
    corrected_envelope_semantic_hash: Literal[
        "dcdd6c07a83aa84faf81b448e2732121027b5a93fc89512caa38035b9c4cdbe4"
    ]
    runtime_implementation_version: Literal["sft1_wave1_composition_runtime_v0_3_6"]
    operations: tuple[OperationRuntimeBinding, ...] = Field(min_length=5, max_length=5)


@dataclass(frozen=True, slots=True)
class P01ValidationResult:
    p01_present: bool
    exception_used: bool
    repeated_closed_expr_hash: str | None


@lru_cache(maxsize=1)
def load_and_validate_p01_runtime_binding() -> P01RuntimeBinding:
    """Load the exact P01 policy and five frozen operation bundles."""

    if (
        EXPECTED_APPROVED_V0_3_5_SEMANTIC_HASH != P01_POLICY_SEMANTIC_HASH
        or EXPECTED_OVERLAY_SEMANTIC_HASH != P01_CORRECTED_ENVELOPE_HASH
    ):
        raise Wave1RuntimeError("P01 loader/runtime policy constant drift")
    loaded_policy = load_p01_identity_policy()
    if loaded_policy.approved_runtime_policy_semantic_hash != P01_POLICY_SEMANTIC_HASH:
        raise Wave1RuntimeError("P01 approved runtime policy semantic hash mismatch")
    if loaded_policy.config_hash != P01_CORRECTED_ENVELOPE_HASH:
        raise Wave1RuntimeError("P01 corrected envelope semantic hash mismatch")

    readiness = load_wave1_implementation_readiness()
    policy = readiness.parent.loaded_admission.loaded_base_policy.config
    registry = {
        operation.operation_id: operation
        for operation in (*policy.operations, *policy.synthetic_track.operations)
    }
    bindings: list[OperationRuntimeBinding] = []
    for bundle in readiness.config.primary_bundles:
        operation = registry[bundle.operation_id]
        expected_tokens = _EXPECTED_OPERATION_TOKENS.get(bundle.operation_id)
        observed_tokens = (operation.mechanism_superclass, operation.inverse_token)
        if expected_tokens != observed_tokens:
            raise Wave1RuntimeError(f"frozen operation token drift: {bundle.operation_id}")
        bindings.append(
            OperationRuntimeBinding(
                operation_id=bundle.operation_id,
                mechanism_superclass=operation.mechanism_superclass,
                inverse_token=operation.inverse_token,
                registry_entry_hash=bundle.registry_entry_hash,
                anchor_hash=bundle.anchor_hash,
                operation_bank_entry_hash=bundle.operation_bank_entry_hash,
                runtime_activated=bundle.operation_id != "N31_DROP_REQUIRED_GUARD_RUBRIC_V1",
            )
        )
    return P01RuntimeBinding(
        required_policy_semantic_hash=P01_POLICY_SEMANTIC_HASH,
        corrected_envelope_semantic_hash=P01_CORRECTED_ENVELOPE_HASH,
        runtime_implementation_version="sft1_wave1_composition_runtime_v0_3_6",
        operations=tuple(bindings),
    )


def _duplicate_positions(values: tuple[str, ...]) -> dict[str, tuple[int, ...]]:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, value in enumerate(values):
        positions[value].append(index)
    return {value: tuple(indices) for value, indices in positions.items() if len(indices) > 1}


def _paths_overlap(left: str, right: str) -> bool:
    """Treat slash-delimited structural paths as ancestor/descendant paths."""

    left_parts = tuple(part for part in left.split("/") if part)
    right_parts = tuple(part for part in right.split("/") if part)
    shortest = min(len(left_parts), len(right_parts))
    return left_parts[:shortest] == right_parts[:shortest]


def _path_is_ancestor_or_equal(ancestor: str, descendant: str) -> bool:
    ancestor_parts = tuple(part for part in ancestor.split("/") if part)
    descendant_parts = tuple(part for part in descendant.split("/") if part)
    return len(ancestor_parts) <= len(descendant_parts) and (
        descendant_parts[: len(ancestor_parts)] == ancestor_parts
    )


def _selected_sites_overlap(left: RuntimeEdge, right: RuntimeEdge) -> bool:
    """Compare field-sensitive mutation sites, not only their enclosing Expr nodes.

    P01 mutates the binder-name slot of its selected ``forallE`` node.  An
    ordinary Expr mutation at that node or an ancestor owns the whole selected
    node and therefore overlaps.  A strict descendant (the binder domain/body)
    is a separate field and may compose.  Two ordinary Expr mutations retain
    the frozen ancestor/descendant rejection.  Multiple P01 edges are rejected
    earlier.
    """

    left_is_binder_name = left.operation_id == P01_OPERATION_ID
    right_is_binder_name = right.operation_id == P01_OPERATION_ID
    if left_is_binder_name != right_is_binder_name:
        binder_edge = left if left_is_binder_name else right
        expr_edge = right if left_is_binder_name else left
        return _path_is_ancestor_or_equal(
            expr_edge.certificate.selected_site_path,
            binder_edge.certificate.selected_site_path,
        )
    return _paths_overlap(
        left.certificate.selected_site_path,
        right.certificate.selected_site_path,
    )


def _revalidate_chain(chain: RuntimeChain) -> RuntimeChain:
    """Defeat unchecked ``model_copy`` mutations at this trust boundary."""

    try:
        return RuntimeChain.model_validate(chain.model_dump(mode="json"))
    except ValueError as exc:
        raise Wave1RuntimeError(f"invalid_runtime_chain_receipt: {exc}") from exc


def validate_runtime_chain(
    chain: RuntimeChain,
) -> P01ValidationResult:
    """Apply the exact P01 exception while preserving every other cycle rule."""

    chain = _revalidate_chain(chain)
    active_binding = load_and_validate_p01_runtime_binding()
    if (
        active_binding.required_policy_semantic_hash != P01_POLICY_SEMANTIC_HASH
        or active_binding.corrected_envelope_semantic_hash != P01_CORRECTED_ENVELOPE_HASH
    ):
        raise Wave1RuntimeError("P01 runtime evaluated under an unbound policy")
    expected_by_operation = {item.operation_id: item for item in active_binding.operations}

    operation_ids = tuple(edge.operation_id for edge in chain.edges)
    p01_indices = tuple(
        index for index, value in enumerate(operation_ids) if value == P01_OPERATION_ID
    )
    if len(p01_indices) > 1:
        raise Wave1RuntimeError("multiple_p01_hops")
    if len(operation_ids) != len(set(operation_ids)):
        raise Wave1RuntimeError("operation_cycle")
    for edge in chain.edges:
        expected = expected_by_operation.get(edge.operation_id)
        if expected is None:
            raise Wave1RuntimeError("operation_not_in_wave1_runtime_binding")
        if not expected.runtime_activated:
            raise Wave1RuntimeError(f"operation_not_runtime_admitted:{edge.operation_id}")
        if any(
            (
                edge.mechanism_superclass != expected.mechanism_superclass,
                edge.inverse_token != expected.inverse_token,
                edge.registry_entry_hash != expected.registry_entry_hash,
                edge.anchor_hash != expected.anchor_hash,
                edge.operation_bank_entry_hash != expected.operation_bank_entry_hash,
            )
        ):
            raise Wave1RuntimeError(f"operation_binding_mismatch:{edge.operation_id}")

    mechanisms = tuple(edge.mechanism_superclass for edge in chain.edges)
    inverse_tokens = tuple(edge.inverse_token for edge in chain.edges)
    if len(mechanisms) != len(set(mechanisms)):
        raise Wave1RuntimeError("mechanism_superclass_cycle")
    if len(inverse_tokens) != len(set(inverse_tokens)):
        raise Wave1RuntimeError("inverse_token_cycle")
    lineages = tuple(edge.certificate.selected_site_lineage_hash for edge in chain.edges)
    if len(lineages) != len(set(lineages)):
        raise Wave1RuntimeError("selected_site_lineage_cycle")
    if any(
        _selected_sites_overlap(chain.edges[left], chain.edges[right])
        for left in range(len(chain.edges))
        for right in range(left + 1, len(chain.edges))
    ):
        raise Wave1RuntimeError("selected_site_path_overlap")

    request_hashes = {endpoint.render_request_hash for endpoint in chain.endpoints}
    render_scopes = {endpoint.render_scope_id for endpoint in chain.endpoints}
    if len(request_hashes) != 1 or len(render_scopes) != 1:
        raise Wave1RuntimeError("endpoints_not_rendered_in_same_request_and_scope")
    expected_request_hash = next(iter(request_hashes))
    for index, edge in enumerate(chain.edges):
        source = chain.endpoints[index]
        candidate = chain.endpoints[index + 1]
        certificate = edge.certificate
        if (
            certificate.source_closed_expr_hash != source.closed_expr_hash
            or certificate.candidate_closed_expr_hash != candidate.closed_expr_hash
            or certificate.source_sidecar_sha256 != source.complete_sidecar_sha256
            or certificate.candidate_sidecar_sha256 != candidate.complete_sidecar_sha256
            or certificate.render_request_hash != expected_request_hash
        ):
            raise Wave1RuntimeError("certificate_endpoint_or_same_request_binding_mismatch")

    if len({endpoint.render_hash for endpoint in chain.endpoints}) != len(chain.endpoints):
        raise Wave1RuntimeError("render_hash_cycle")
    if len({endpoint.core_text_sha256 for endpoint in chain.endpoints}) != len(chain.endpoints):
        raise Wave1RuntimeError("model_facing_text_cycle")

    closed_expr_hashes = tuple(endpoint.closed_expr_hash for endpoint in chain.endpoints)
    duplicates = _duplicate_positions(closed_expr_hashes)
    if not p01_indices:
        if duplicates:
            raise Wave1RuntimeError("closed_expr_hash_cycle_without_p01")
        return P01ValidationResult(False, False, None)

    edge_index = p01_indices[0]
    if not duplicates:
        raise Wave1RuntimeError("p01_alpha_invariant_closed_expr_hash_mismatch")
    if len(duplicates) != 1:
        raise Wave1RuntimeError("multiple_repeated_closed_expr_hash_classes")
    repeated_hash, positions = next(iter(duplicates.items()))
    if positions != (edge_index, edge_index + 1):
        if len(positions) > 2:
            raise Wave1RuntimeError("third_closed_expr_hash_occurrence")
        raise Wave1RuntimeError("nonadjacent_or_wrong_operation_edge_repeat")
    source = chain.endpoints[edge_index]
    candidate = chain.endpoints[edge_index + 1]
    if source.render_hash == candidate.render_hash:
        raise Wave1RuntimeError("equal_p01_render_hash")
    if source.core_text_sha256 == candidate.core_text_sha256:
        raise Wave1RuntimeError("equal_p01_model_facing_text")
    if chain.edges[edge_index].certificate.p01_name_only_delta is None:
        raise Wave1RuntimeError("missing_p01_certificate")
    return P01ValidationResult(True, True, repeated_hash)


class P01CapObservation(StrictModel):
    retained_semantic_pair_count: int = Field(gt=0, strict=True)
    p01_pair_count: int = Field(ge=0, strict=True)
    p01_procedure_pair_count: int = Field(ge=0, strict=True)
    p01_pairs_by_root: dict[NonEmptyStr, NonNegativeInt]
    positive_p01_pair_count: int = Field(ge=0, strict=True)
    negative_p01_pair_count: int = Field(ge=0, strict=True)
    direct_p01_pair_count: int = Field(ge=0, strict=True)
    composed_p01_pair_count: int = Field(ge=0, strict=True)


def validate_p01_caps(observation: P01CapObservation) -> None:
    """Enforce exact integer P01 maxima across all polarities/compositions."""

    observation = P01CapObservation.model_validate(observation.model_dump(mode="json"))
    if any(count > 1 for count in observation.p01_pairs_by_root.values()):
        raise Wave1RuntimeError("p01_per_root_cap_exceeded")
    if sum(observation.p01_pairs_by_root.values()) != observation.p01_pair_count:
        raise Wave1RuntimeError("p01_root_accounting_mismatch")
    if observation.p01_procedure_pair_count != observation.p01_pair_count:
        raise Wave1RuntimeError("p01_procedure_accounting_mismatch")
    if (
        observation.positive_p01_pair_count + observation.negative_p01_pair_count
        != observation.p01_pair_count
    ):
        raise Wave1RuntimeError("p01_polarity_accounting_mismatch")
    if (
        observation.direct_p01_pair_count + observation.composed_p01_pair_count
        != observation.p01_pair_count
    ):
        raise Wave1RuntimeError("p01_composition_accounting_mismatch")
    # 0.5% == 1/200, and every P01 pair inherits the 0.25% procedure cap == 1/400.
    if observation.p01_pair_count * 200 > observation.retained_semantic_pair_count:
        raise Wave1RuntimeError("p01_retained_share_cap_exceeded")
    if observation.p01_procedure_pair_count * 400 > observation.retained_semantic_pair_count:
        raise Wave1RuntimeError("p01_procedure_share_cap_exceeded")


class DedupCandidate(StrictModel):
    stable_row_hash: Sha256
    reference_render_hash: Sha256
    candidate_render_hash: Sha256
    label: Literal[0, 1]


class DedupResult(StrictModel):
    retained_stable_row_hashes: tuple[Sha256, ...]
    rejected_conflict_class_keys: tuple[Sha256, ...]
    suppressed_duplicate_stable_row_hashes: tuple[Sha256, ...]


class RuntimeRetentionJournalRecord(StrictModel):
    schema_version: Literal[1]
    sequence: int = Field(ge=0, strict=True)
    previous_chain_hash: Sha256
    retention_scope_id: NonEmptyStr
    event: Literal["prospective_chain_bound"]
    stable_row_hash: Sha256
    chain_hash: Sha256

    @model_validator(mode="after")
    def _record_hash_replays(self) -> RuntimeRetentionJournalRecord:
        core = self.model_dump(mode="json")
        observed = core.pop("chain_hash")
        if observed != hash_canonical(core):
            raise ValueError("retention journal record hash mismatch")
        return self


class RuntimeRetentionScopeManifest(StrictModel):
    schema_version: Literal[1]
    manifest_kind: Literal["durable_complete_prospective_retention_manifest_v1"]
    scope_purpose: Literal["bounded_readiness_contract_fixture_scope_v1"]
    retention_scope_id: NonEmptyStr
    evidence_root_path: NonEmptyStr
    scope_manifest_relative_path: NonEmptyStr
    scope_journal_relative_path: NonEmptyStr
    scope_journal_file_sha256: Sha256
    complete_scope: Literal[True]
    scope_record_count: int = Field(gt=0, strict=True)
    scope_journal_final_chain_hash: Sha256
    chains: tuple[RuntimeChain, ...] = Field(min_length=1)
    wave1_gate_executed: Literal[False]
    model_facing_rows_emitted: Literal[False]
    production_admission_changed: Literal[False]
    manifest_hash: Sha256

    @model_validator(mode="after")
    def _manifest_hash_replays(self) -> RuntimeRetentionScopeManifest:
        if self.scope_record_count != len(self.chains):
            raise ValueError("retention scope record count mismatch")
        if len({chain.stable_row_hash for chain in self.chains}) != len(self.chains):
            raise ValueError("retention scope repeats a stable row hash")
        core = self.model_dump(mode="json")
        observed = core.pop("manifest_hash")
        if observed != hash_canonical(core):
            raise ValueError("retention scope manifest hash mismatch")
        return self


class RuntimeRetentionBatch(StrictModel):
    """Replay handle for one durable, reviewable readiness-only scope."""

    retention_scope_id: NonEmptyStr
    evidence_root_path: NonEmptyStr
    scope_manifest_relative_path: NonEmptyStr
    scope_manifest_path: NonEmptyStr
    scope_manifest_file_sha256: Sha256
    scope_manifest_hash: Sha256
    scope_journal_relative_path: NonEmptyStr
    scope_journal_path: NonEmptyStr
    scope_journal_file_sha256: Sha256
    scope_journal_final_chain_hash: Sha256


class RuntimeRetentionResult(StrictModel):
    retention_scope_id: NonEmptyStr
    retained_stable_row_hashes: tuple[Sha256, ...]
    suppressed_duplicate_stable_row_hashes: tuple[Sha256, ...]
    p01_cap_observation: P01CapObservation


def canonical_unordered_pair_key(reference_render_hash: str, candidate_render_hash: str) -> str:
    return hash_canonical(sorted((reference_render_hash, candidate_render_hash)))


def deduplicate_unordered_pairs(candidates: tuple[DedupCandidate, ...]) -> DedupResult:
    """Keep the minimum stable hash per same-label class; reject conflicts."""

    grouped: dict[str, list[DedupCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[
            canonical_unordered_pair_key(
                candidate.reference_render_hash, candidate.candidate_render_hash
            )
        ].append(candidate)
    retained: list[str] = []
    conflicts: list[str] = []
    suppressed: list[str] = []
    for key in sorted(grouped):
        members = sorted(grouped[key], key=lambda item: item.stable_row_hash)
        if len({member.label for member in members}) != 1:
            conflicts.append(key)
            suppressed.extend(member.stable_row_hash for member in members)
            continue
        retained.append(members[0].stable_row_hash)
        suppressed.extend(member.stable_row_hash for member in members[1:])
    return DedupResult(
        retained_stable_row_hashes=tuple(retained),
        rejected_conflict_class_keys=tuple(conflicts),
        suppressed_duplicate_stable_row_hashes=tuple(sorted(suppressed)),
    )


def assert_post_orientation_unique(candidates: tuple[DedupCandidate, ...]) -> None:
    """Fail a prospective shard if either orientation duplicates or conflicts."""

    keys: dict[str, int] = {}
    for candidate in candidates:
        key = canonical_unordered_pair_key(
            candidate.reference_render_hash, candidate.candidate_render_hash
        )
        previous = keys.get(key)
        if previous is not None:
            reason = "conflicting_label" if previous != candidate.label else "duplicate"
            raise Wave1RuntimeError(f"post_orientation_{reason}_class")
        keys[key] = candidate.label


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> object:
    raise ValueError(f"nonfinite JSON value: {value}")


def _canonical_relative_evidence_path(raw_path: str, *, label: str) -> Path:
    """Validate one canonical POSIX-relative path from a durable receipt."""

    path = Path(raw_path)
    if (
        path.is_absolute()
        or raw_path != path.as_posix()
        or "\\" in raw_path
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise Wave1RuntimeError(f"retention scope {label} relative path is unsafe")
    return path


def _require_safe_existing_path(raw_path: str, path: Path, *, label: str, directory: bool) -> None:
    """Reject noncanonical paths and every symlink in their existing ancestry."""

    if (
        not path.is_absolute()
        or raw_path != path.as_posix()
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise Wave1RuntimeError(f"retention scope {label} path is not canonical absolute")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise Wave1RuntimeError(f"retention scope {label} path contains a symlink")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Wave1RuntimeError(f"retention scope {label} is unavailable or unsafe") from exc
    expected_kind = path.is_dir() if directory else path.is_file()
    if resolved != path or not expected_kind:
        raise Wave1RuntimeError(f"retention scope {label} is unavailable or unsafe")


def _load_retention_scope(batch: RuntimeRetentionBatch) -> RuntimeRetentionScopeManifest:
    evidence_root = Path(batch.evidence_root_path)
    manifest_path = Path(batch.scope_manifest_path)
    journal_path = Path(batch.scope_journal_path)
    manifest_relative = _canonical_relative_evidence_path(
        batch.scope_manifest_relative_path, label="manifest"
    )
    journal_relative = _canonical_relative_evidence_path(
        batch.scope_journal_relative_path, label="journal"
    )
    _require_safe_existing_path(
        batch.evidence_root_path, evidence_root, label="evidence root", directory=True
    )
    if manifest_relative == journal_relative or manifest_path == journal_path:
        raise Wave1RuntimeError("retention scope manifest and journal must be distinct files")
    if (
        manifest_path != evidence_root / manifest_relative
        or journal_path != evidence_root / journal_relative
    ):
        raise Wave1RuntimeError("retention scope artifact path/root identity mismatch")
    for path, expected_sha, label in (
        (manifest_path, batch.scope_manifest_file_sha256, "manifest"),
        (journal_path, batch.scope_journal_file_sha256, "journal"),
    ):
        raw_path = batch.scope_manifest_path if label == "manifest" else batch.scope_journal_path
        _require_safe_existing_path(raw_path, path, label=label, directory=False)
        if hash_file(path) != expected_sha:
            raise Wave1RuntimeError(f"retention scope {label} file hash mismatch")
    try:
        if manifest_path.samefile(journal_path):
            raise Wave1RuntimeError("retention scope manifest and journal share a file identity")
    except OSError as exc:
        raise Wave1RuntimeError("retention scope artifact identity is unavailable") from exc
    try:
        raw_manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_nonfinite_json,
        )
        manifest = RuntimeRetentionScopeManifest.model_validate(raw_manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise Wave1RuntimeError(f"invalid durable retention scope manifest: {exc}") from exc
    if (
        manifest.retention_scope_id != batch.retention_scope_id
        or manifest.evidence_root_path != batch.evidence_root_path
        or manifest.scope_manifest_relative_path != batch.scope_manifest_relative_path
        or manifest.scope_journal_relative_path != batch.scope_journal_relative_path
        or manifest.scope_journal_file_sha256 != batch.scope_journal_file_sha256
        or manifest.manifest_hash != batch.scope_manifest_hash
        or manifest.scope_journal_final_chain_hash != batch.scope_journal_final_chain_hash
    ):
        raise Wave1RuntimeError("retention scope handle/manifest identity mismatch")
    previous = "0" * 64
    stable_hashes: list[str] = []
    try:
        lines = journal_path.read_text(encoding="utf-8").splitlines()
        records = tuple(
            RuntimeRetentionJournalRecord.model_validate(
                json.loads(
                    line,
                    object_pairs_hook=_json_object_without_duplicates,
                    parse_constant=_reject_nonfinite_json,
                )
            )
            for line in lines
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise Wave1RuntimeError(f"invalid durable retention scope journal: {exc}") from exc
    if len(records) != manifest.scope_record_count:
        raise Wave1RuntimeError("retention scope journal record count mismatch")
    for sequence, record in enumerate(records):
        if (
            record.sequence != sequence
            or record.previous_chain_hash != previous
            or record.retention_scope_id != manifest.retention_scope_id
        ):
            raise Wave1RuntimeError("retention scope journal chain/identity mismatch")
        previous = record.chain_hash
        stable_hashes.append(record.stable_row_hash)
    if previous != manifest.scope_journal_final_chain_hash or tuple(stable_hashes) != tuple(
        chain.stable_row_hash for chain in manifest.chains
    ):
        raise Wave1RuntimeError("retention scope journal/manifest inventory mismatch")
    return manifest


def validate_retention_batch(batch: RuntimeRetentionBatch) -> RuntimeRetentionResult:
    """Run the mandatory chain -> dedup -> cap -> final-assertion path.

    This is the only retention-level API in this module.  Counts are derived
    from the validated chains after canonical unordered-pair deduplication;
    callers cannot supply P01 counts independently or enable an inactive
    operation by injecting a runtime binding.
    """

    try:
        batch = RuntimeRetentionBatch.model_validate(batch.model_dump(mode="json"))
    except ValueError as exc:
        raise Wave1RuntimeError(f"invalid_runtime_retention_batch: {exc}") from exc
    manifest = _load_retention_scope(batch)
    chains = manifest.chains
    stable_hashes = tuple(chain.stable_row_hash for chain in chains)
    if len(stable_hashes) != len(set(stable_hashes)):
        raise Wave1RuntimeError("stable_row_hash_collision_or_duplicate")
    for chain in chains:
        validate_runtime_chain(chain)

    prospective = tuple(
        DedupCandidate(
            stable_row_hash=chain.stable_row_hash,
            reference_render_hash=chain.endpoints[0].render_hash,
            candidate_render_hash=chain.endpoints[-1].render_hash,
            label=chain.label,
        )
        for chain in chains
    )
    dedup = deduplicate_unordered_pairs(prospective)
    if dedup.rejected_conflict_class_keys:
        raise Wave1RuntimeError("global_conflicting_label_pair_class")
    retained_hashes = set(dedup.retained_stable_row_hashes)
    retained_chains = tuple(chain for chain in chains if chain.stable_row_hash in retained_hashes)
    retained_candidates = tuple(
        candidate for candidate in prospective if candidate.stable_row_hash in retained_hashes
    )
    assert_post_orientation_unique(retained_candidates)

    p01_chains = tuple(
        chain
        for chain in retained_chains
        if any(edge.operation_id == P01_OPERATION_ID for edge in chain.edges)
    )
    by_root: dict[str, int] = defaultdict(int)
    for chain in p01_chains:
        by_root[chain.root_ancestry_id] += 1
    observation = P01CapObservation(
        retained_semantic_pair_count=len(retained_chains),
        p01_pair_count=len(p01_chains),
        p01_procedure_pair_count=len(p01_chains),
        p01_pairs_by_root=dict(sorted(by_root.items())),
        positive_p01_pair_count=sum(chain.polarity == "positive" for chain in p01_chains),
        negative_p01_pair_count=sum(chain.polarity == "negative" for chain in p01_chains),
        direct_p01_pair_count=sum(len(chain.edges) == 1 for chain in p01_chains),
        composed_p01_pair_count=sum(len(chain.edges) > 1 for chain in p01_chains),
    )
    validate_p01_caps(observation)
    return RuntimeRetentionResult(
        retention_scope_id=batch.retention_scope_id,
        retained_stable_row_hashes=dedup.retained_stable_row_hashes,
        suppressed_duplicate_stable_row_hashes=(dedup.suppressed_duplicate_stable_row_hashes),
        p01_cap_observation=observation,
    )


__all__ = [
    "DedupCandidate",
    "DedupResult",
    "OperationRuntimeBinding",
    "P01CapObservation",
    "P01NameOnlyCertificateReceipt",
    "P01NameOnlyDelta",
    "P01RuntimeBinding",
    "P01ValidationResult",
    "RuntimeChain",
    "RuntimeEdge",
    "RuntimeEndpoint",
    "RuntimeRetentionBatch",
    "RuntimeRetentionJournalRecord",
    "RuntimeRetentionResult",
    "RuntimeRetentionScopeManifest",
    "TypedCertificateReceipt",
    "Wave1RuntimeError",
    "assert_post_orientation_unique",
    "canonical_unordered_pair_key",
    "compute_p01_binder_aware_fingerprint",
    "compute_p01_selected_site_lineage_hash",
    "compute_stable_row_hash",
    "deduplicate_unordered_pairs",
    "load_and_validate_p01_runtime_binding",
    "make_runtime_chain",
    "p01_outer_binder_site_path",
    "validate_p01_caps",
    "validate_retention_batch",
    "validate_runtime_chain",
]
