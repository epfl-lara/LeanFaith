"""Structural candidate-set manifests for LF-024.

This module closes only the *serialized set* supplied to the resolver.  It
binds a target, its theorem/context records, the active policy, all four
registered authority-source slots, and the canonical candidate JSONL bytes.

It deliberately does not load or replay the referenced authority inventories
or closure receipts.  Consequently :func:`verify_candidate_set_structure`
returns a diagnostic structural result whose production-closure fields are
literal ``False``.  A future source-specific authority layer must replay every
bound inventory before it may construct a production ``VerifiedCandidateSet``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.labeling.quality import (
    ActiveLabelResolutionPolicy,
    ResolutionCandidate,
    ResolutionSource,
    make_resolution_candidate_id,
)
from leanfaith.schemas.enums import SemanticLabelTargetKind
from leanfaith.schemas.ids import (
    CONTEXT_PREFIX,
    HEX64_PATTERN,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.nl_lean import NLPLeanRecord
from leanfaith.schemas.pair import PairRecord
from leanfaith.schemas.theorem import ContextRecord, TheoremRecord

CANDIDATE_ENUMERATION_SCOPE_PREFIX = "candidate_enumeration_scope"
CANDIDATE_SOURCE_CLOSURE_PREFIX = "candidate_source_closure"
CANDIDATE_SET_MANIFEST_PREFIX = "candidate_set_manifest"

CandidateSetTarget = PairRecord | NLPLeanRecord

CANONICAL_RESOLUTION_SOURCES: tuple[ResolutionSource, ...] = tuple(
    sorted(ResolutionSource, key=lambda source: source.value)
)

_TARGET_PATTERNS = {
    SemanticLabelTargetKind.LEAN_PAIR: id_pattern(PAIR_PREFIX),
    SemanticLabelTargetKind.NL_LEAN: id_pattern(NL_LEAN_PREFIX),
}


class CandidateSetStructureError(ValueError):
    """A candidate-set structure is incomplete, stale, or cross-target."""


class TargetTheoremContextBinding(StrictModel):
    """Exact theorem/context records needed to interpret one target theorem."""

    schema_version: Literal[1] = 1
    theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    theorem_record_sha256: str = Field(pattern=HEX64_PATTERN)
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    context_record_sha256: str = Field(pattern=HEX64_PATTERN)


def _context_binding_sha256(
    bindings: Sequence[TargetTheoremContextBinding],
) -> str:
    return hash_canonical([item.model_dump(mode="json") for item in bindings])


class CandidateEnumerationScope(StrictModel):
    """Content-addressed target/policy/context scope shared by all sources."""

    schema_version: Literal[1] = 1
    scope_id: str = Field(pattern=id_pattern(CANDIDATE_ENUMERATION_SCOPE_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    target_input_sha256: str = Field(pattern=HEX64_PATTERN)
    target_context_bindings: tuple[TargetTheoremContextBinding, ...] = Field(min_length=1)
    target_context_binding_sha256: str = Field(pattern=HEX64_PATTERN)
    policy_version: str = Field(min_length=1)
    policy_file_sha256: str = Field(pattern=HEX64_PATTERN)
    gate_file_sha256: str = Field(pattern=HEX64_PATTERN)
    registered_sources: tuple[ResolutionSource, ...]

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        if re.fullmatch(_TARGET_PATTERNS[self.target_kind], self.target_id) is None:
            raise ValueError("target_id does not match target_kind")
        binding_keys = tuple(
            (item.theorem_id, item.context_id) for item in self.target_context_bindings
        )
        if binding_keys != tuple(sorted(binding_keys)) or len(set(binding_keys)) != len(
            binding_keys
        ):
            raise ValueError("target_context_bindings must be sorted and unique")
        if self.target_context_binding_sha256 != _context_binding_sha256(
            self.target_context_bindings
        ):
            raise ValueError("target_context_binding_sha256 differs from bindings")
        if self.registered_sources != CANONICAL_RESOLUTION_SOURCES:
            raise ValueError("registered_sources must contain the exact four canonical sources")
        expected = make_id(
            CANDIDATE_ENUMERATION_SCOPE_PREFIX,
            self.model_dump(mode="json", exclude={"scope_id"}),
        )
        if self.scope_id != expected:
            raise ValueError("scope_id does not match scope content")
        return self


class CandidateSourceClosureBinding(StrictModel):
    """Structural reference to one source-specific closure receipt.

    The referenced inventory and receipt are intentionally not trusted here.
    A later typed adapter must load and replay them before production use.
    """

    schema_version: Literal[1] = 1
    source_closure_binding_id: str = Field(pattern=id_pattern(CANDIDATE_SOURCE_CLOSURE_PREFIX))
    scope_id: str = Field(pattern=id_pattern(CANDIDATE_ENUMERATION_SCOPE_PREFIX))
    source: ResolutionSource
    adapter_method_version: str = Field(min_length=1)
    adapter_config_sha256: str = Field(pattern=HEX64_PATTERN)
    authority_inventory_manifest_id: str = Field(min_length=1)
    authority_inventory_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    closure_receipt_id: str = Field(min_length=1)
    closure_receipt_sha256: str = Field(pattern=HEX64_PATTERN)
    candidate_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        if self.candidate_ids != tuple(sorted(set(self.candidate_ids))):
            raise ValueError("candidate_ids must be sorted and unique")
        candidate_pattern = id_pattern("resolution_candidate")
        if any(re.fullmatch(candidate_pattern, item) is None for item in self.candidate_ids):
            raise ValueError("candidate_ids contain a non-canonical resolution-candidate ID")
        expected = make_id(
            CANDIDATE_SOURCE_CLOSURE_PREFIX,
            self.model_dump(mode="json", exclude={"source_closure_binding_id"}),
        )
        if self.source_closure_binding_id != expected:
            raise ValueError("source_closure_binding_id does not match binding content")
        return self


class CandidateSetManifest(StrictModel):
    """Content-addressed structural candidate-set manifest for one target."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["lf024_candidate_set_manifest"] = "lf024_candidate_set_manifest"
    candidate_set_manifest_id: str = Field(pattern=id_pattern(CANDIDATE_SET_MANIFEST_PREFIX))
    scope: CandidateEnumerationScope
    source_closures: tuple[CandidateSourceClosureBinding, ...] = Field(min_length=4)
    candidate_schema_version: Literal[1] = 1
    candidate_count: int = Field(ge=0, strict=True)
    candidate_ids: tuple[str, ...] = ()
    candidate_records_sha256: str = Field(pattern=HEX64_PATTERN)
    structural_reference_only: Literal[True] = True
    authority_replays_verified: Literal[False] = False
    production_candidate_set_closed: Literal[False] = False

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        sources = tuple(item.source for item in self.source_closures)
        if sources != CANONICAL_RESOLUTION_SOURCES:
            raise ValueError("source_closures must contain exactly one canonical source each")
        binding_ids = tuple(item.source_closure_binding_id for item in self.source_closures)
        if len(set(binding_ids)) != len(binding_ids):
            raise ValueError("source_closures contain duplicate binding IDs")
        if any(item.scope_id != self.scope.scope_id for item in self.source_closures):
            raise ValueError("source closure binds a different enumeration scope")
        if self.candidate_ids != tuple(sorted(set(self.candidate_ids))):
            raise ValueError("candidate_ids must be sorted and unique")
        if self.candidate_count != len(self.candidate_ids):
            raise ValueError("candidate_count differs from candidate_ids")
        source_candidate_ids = tuple(
            candidate_id
            for closure in self.source_closures
            for candidate_id in closure.candidate_ids
        )
        if len(source_candidate_ids) != len(set(source_candidate_ids)):
            raise ValueError("a candidate is assigned to multiple source closures")
        if tuple(sorted(source_candidate_ids)) != self.candidate_ids:
            raise ValueError("candidate_ids differ from the exact source-closure union")
        expected = make_id(
            CANDIDATE_SET_MANIFEST_PREFIX,
            self.model_dump(mode="json", exclude={"candidate_set_manifest_id"}),
        )
        if self.candidate_set_manifest_id != expected:
            raise ValueError("candidate_set_manifest_id does not match manifest content")
        return self


class StructuralCandidateSetVerification(StrictModel):
    """Diagnostic proof of serialization/set equality, never authority closure."""

    schema_version: Literal[1] = 1
    verification_level: Literal["structural_diagnostic_only"] = "structural_diagnostic_only"
    candidate_set_manifest_id: str = Field(pattern=id_pattern(CANDIDATE_SET_MANIFEST_PREFIX))
    scope_id: str = Field(pattern=id_pattern(CANDIDATE_ENUMERATION_SCOPE_PREFIX))
    target_kind: SemanticLabelTargetKind
    target_id: str
    candidate_count: int = Field(ge=0, strict=True)
    candidate_ids: tuple[str, ...]
    candidate_records_sha256: str = Field(pattern=HEX64_PATTERN)
    source_closure_binding_ids: tuple[str, ...]
    authority_inventory_replays_verified: Literal[False] = False
    production_candidate_set_closed: Literal[False] = False


def _target_identity(
    target: CandidateSetTarget,
) -> tuple[SemanticLabelTargetKind, str, tuple[str, ...]]:
    if isinstance(target, PairRecord):
        return (
            SemanticLabelTargetKind.LEAN_PAIR,
            target.pair_id,
            tuple(sorted({target.theorem_a_id, target.theorem_b_id})),
        )
    return (
        SemanticLabelTargetKind.NL_LEAN,
        target.nl_lean_id,
        tuple(sorted({target.candidate_theorem_id, *target.reference_theorem_ids})),
    )


def _canonical_target_context_bindings(
    *,
    expected_theorem_ids: tuple[str, ...],
    theorem_records: Sequence[TheoremRecord],
    context_records: Sequence[ContextRecord],
) -> tuple[TargetTheoremContextBinding, ...]:
    theorem_by_id: dict[str, TheoremRecord] = {}
    for theorem in theorem_records:
        if theorem.theorem_id in theorem_by_id:
            raise CandidateSetStructureError(f"duplicate theorem record {theorem.theorem_id}")
        theorem_by_id[theorem.theorem_id] = theorem
    if set(theorem_by_id) != set(expected_theorem_ids):
        raise CandidateSetStructureError(
            "target theorem partition is not exact; "
            f"expected={list(expected_theorem_ids)}, supplied={sorted(theorem_by_id)}"
        )

    context_by_id: dict[str, ContextRecord] = {}
    for context in context_records:
        if context.context_id in context_by_id:
            raise CandidateSetStructureError(f"duplicate context record {context.context_id}")
        context_by_id[context.context_id] = context
    expected_context_ids = {theorem.context_id for theorem in theorem_by_id.values()}
    if set(context_by_id) != expected_context_ids:
        raise CandidateSetStructureError(
            "target context partition is not exact; "
            f"expected={sorted(expected_context_ids)}, supplied={sorted(context_by_id)}"
        )

    return tuple(
        TargetTheoremContextBinding(
            theorem_id=theorem.theorem_id,
            theorem_record_sha256=hash_canonical(theorem.model_dump(mode="json")),
            context_id=theorem.context_id,
            context_record_sha256=hash_canonical(
                context_by_id[theorem.context_id].model_dump(mode="json")
            ),
        )
        for theorem in sorted(theorem_by_id.values(), key=lambda item: item.theorem_id)
    )


def build_candidate_enumeration_scope(
    *,
    target: CandidateSetTarget,
    theorem_records: Sequence[TheoremRecord],
    context_records: Sequence[ContextRecord],
    policy: ActiveLabelResolutionPolicy,
) -> CandidateEnumerationScope:
    """Build the exact target/policy/context scope independent of input order."""

    target_kind, target_id, theorem_ids = _target_identity(target)
    bindings = _canonical_target_context_bindings(
        expected_theorem_ids=theorem_ids,
        theorem_records=theorem_records,
        context_records=context_records,
    )
    identity_payload = {
        "schema_version": 1,
        "target_kind": target_kind.value,
        "target_id": target_id,
        "target_input_sha256": hash_canonical(target.model_dump(mode="json")),
        "target_context_bindings": [item.model_dump(mode="json") for item in bindings],
        "target_context_binding_sha256": _context_binding_sha256(bindings),
        "policy_version": policy.policy_version,
        "policy_file_sha256": policy.policy_file_sha256,
        "gate_file_sha256": policy.gate_file_sha256,
        "registered_sources": [source.value for source in CANONICAL_RESOLUTION_SOURCES],
    }
    return CandidateEnumerationScope(
        scope_id=make_id(CANDIDATE_ENUMERATION_SCOPE_PREFIX, identity_payload),
        target_kind=target_kind,
        target_id=target_id,
        target_input_sha256=hash_canonical(target.model_dump(mode="json")),
        target_context_bindings=bindings,
        target_context_binding_sha256=_context_binding_sha256(bindings),
        policy_version=policy.policy_version,
        policy_file_sha256=policy.policy_file_sha256,
        gate_file_sha256=policy.gate_file_sha256,
        registered_sources=CANONICAL_RESOLUTION_SOURCES,
    )


def build_candidate_source_closure_binding(
    *,
    scope: CandidateEnumerationScope,
    source: ResolutionSource,
    adapter_method_version: str,
    adapter_config_sha256: str,
    authority_inventory_manifest_id: str,
    authority_inventory_manifest_sha256: str,
    closure_receipt_id: str,
    closure_receipt_sha256: str,
    candidate_ids: Sequence[str],
) -> CandidateSourceClosureBinding:
    """Bind, but do not trust, one source-specific inventory/receipt pair."""

    ordered_ids = tuple(sorted(candidate_ids))
    if len(ordered_ids) != len(set(ordered_ids)):
        raise CandidateSetStructureError("duplicate candidate IDs in source closure")
    identity_payload = {
        "schema_version": 1,
        "scope_id": scope.scope_id,
        "source": source.value,
        "adapter_method_version": adapter_method_version,
        "adapter_config_sha256": adapter_config_sha256,
        "authority_inventory_manifest_id": authority_inventory_manifest_id,
        "authority_inventory_manifest_sha256": authority_inventory_manifest_sha256,
        "closure_receipt_id": closure_receipt_id,
        "closure_receipt_sha256": closure_receipt_sha256,
        "candidate_ids": list(ordered_ids),
    }
    return CandidateSourceClosureBinding(
        source_closure_binding_id=make_id(
            CANDIDATE_SOURCE_CLOSURE_PREFIX,
            identity_payload,
        ),
        scope_id=scope.scope_id,
        source=source,
        adapter_method_version=adapter_method_version,
        adapter_config_sha256=adapter_config_sha256,
        authority_inventory_manifest_id=authority_inventory_manifest_id,
        authority_inventory_manifest_sha256=authority_inventory_manifest_sha256,
        closure_receipt_id=closure_receipt_id,
        closure_receipt_sha256=closure_receipt_sha256,
        candidate_ids=ordered_ids,
    )


def canonical_candidate_jsonl_bytes(candidates: Sequence[ResolutionCandidate]) -> bytes:
    """Canonical target-local candidate JSONL, sorted by semantic candidate ID."""

    ordered = sorted(candidates, key=lambda item: item.candidate_id)
    ids = tuple(item.candidate_id for item in ordered)
    if len(ids) != len(set(ids)):
        raise CandidateSetStructureError("duplicate ResolutionCandidate IDs")
    return b"".join(
        canonical_json_bytes(candidate.model_dump(mode="json")) + b"\n" for candidate in ordered
    )


def _check_candidate_scope_and_sources(
    *,
    scope: CandidateEnumerationScope,
    source_closures: Sequence[CandidateSourceClosureBinding],
    candidates: Sequence[ResolutionCandidate],
) -> None:
    closure_by_source = {item.source: item for item in source_closures}
    if len(closure_by_source) != len(source_closures):
        raise CandidateSetStructureError("duplicate candidate source closure")
    if set(closure_by_source) != set(CANONICAL_RESOLUTION_SOURCES):
        raise CandidateSetStructureError("all four candidate source closures are required")
    for closure in source_closures:
        if closure.scope_id != scope.scope_id:
            raise CandidateSetStructureError("source closure binds another scope")

    seen_ids: set[str] = set()
    candidate_by_id: dict[str, ResolutionCandidate] = {}
    for candidate in candidates:
        if candidate.candidate_id in seen_ids:
            raise CandidateSetStructureError("duplicate ResolutionCandidate IDs")
        seen_ids.add(candidate.candidate_id)
        candidate_by_id[candidate.candidate_id] = candidate
        if candidate.candidate_id != make_resolution_candidate_id(candidate):
            raise CandidateSetStructureError(
                f"candidate {candidate.candidate_id} differs from its content-addressed ID"
            )
        if candidate.target_kind is not scope.target_kind or candidate.target_id != scope.target_id:
            raise CandidateSetStructureError(
                f"candidate {candidate.candidate_id} targets another item"
            )
        if (
            candidate.policy_version != scope.policy_version
            or candidate.policy_file_sha256 != scope.policy_file_sha256
        ):
            raise CandidateSetStructureError(
                f"candidate {candidate.candidate_id} has a stale policy binding"
            )

    union: list[str] = []
    for source in CANONICAL_RESOLUTION_SOURCES:
        closure = closure_by_source[source]
        for candidate_id in closure.candidate_ids:
            referenced_candidate = candidate_by_id.get(candidate_id)
            if referenced_candidate is None:
                raise CandidateSetStructureError(
                    f"source closure references absent candidate {candidate_id}"
                )
            if referenced_candidate.source is not source:
                raise CandidateSetStructureError(
                    f"candidate {candidate_id} is assigned to the wrong source closure"
                )
            union.append(candidate_id)
    if len(union) != len(set(union)):
        raise CandidateSetStructureError("candidate appears in more than one source closure")
    if set(union) != set(candidate_by_id):
        raise CandidateSetStructureError("candidate records differ from source-closure union")


def build_candidate_set_manifest(
    *,
    scope: CandidateEnumerationScope,
    source_closures: Sequence[CandidateSourceClosureBinding],
    candidates: Sequence[ResolutionCandidate],
) -> CandidateSetManifest:
    """Build a deterministic structural manifest; no authority replay occurs."""

    _check_candidate_scope_and_sources(
        scope=scope,
        source_closures=source_closures,
        candidates=candidates,
    )
    ordered_closures = tuple(sorted(source_closures, key=lambda item: item.source.value))
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    candidate_ids = tuple(item.candidate_id for item in ordered_candidates)
    candidate_records_sha256 = sha256_hex(canonical_candidate_jsonl_bytes(ordered_candidates))
    identity_payload = {
        "schema_version": 1,
        "artifact_kind": "lf024_candidate_set_manifest",
        "scope": scope.model_dump(mode="json"),
        "source_closures": [item.model_dump(mode="json") for item in ordered_closures],
        "candidate_schema_version": 1,
        "candidate_count": len(candidate_ids),
        "candidate_ids": list(candidate_ids),
        "candidate_records_sha256": candidate_records_sha256,
        "structural_reference_only": True,
        "authority_replays_verified": False,
        "production_candidate_set_closed": False,
    }
    return CandidateSetManifest(
        candidate_set_manifest_id=make_id(
            CANDIDATE_SET_MANIFEST_PREFIX,
            identity_payload,
        ),
        scope=scope,
        source_closures=ordered_closures,
        candidate_count=len(candidate_ids),
        candidate_ids=candidate_ids,
        candidate_records_sha256=candidate_records_sha256,
    )


def verify_candidate_set_structure(
    *,
    manifest: CandidateSetManifest,
    target: CandidateSetTarget,
    theorem_records: Sequence[TheoremRecord],
    context_records: Sequence[ContextRecord],
    candidates: Sequence[ResolutionCandidate],
    policy: ActiveLabelResolutionPolicy,
) -> StructuralCandidateSetVerification:
    """Verify exact structural equality without asserting authority completeness."""

    try:
        validated_manifest = CandidateSetManifest.model_validate(manifest.model_dump(mode="python"))
    except ValueError as exc:
        raise CandidateSetStructureError(f"candidate-set manifest is invalid: {exc}") from exc
    expected_scope = build_candidate_enumeration_scope(
        target=target,
        theorem_records=theorem_records,
        context_records=context_records,
        policy=policy,
    )
    if validated_manifest.scope != expected_scope:
        raise CandidateSetStructureError("manifest target/policy/context scope is stale")
    _check_candidate_scope_and_sources(
        scope=expected_scope,
        source_closures=validated_manifest.source_closures,
        candidates=candidates,
    )
    ordered_candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    candidate_ids = tuple(item.candidate_id for item in ordered_candidates)
    if candidate_ids != validated_manifest.candidate_ids:
        raise CandidateSetStructureError("supplied candidates differ from manifest candidate IDs")
    candidate_records_sha256 = sha256_hex(canonical_candidate_jsonl_bytes(ordered_candidates))
    if candidate_records_sha256 != validated_manifest.candidate_records_sha256:
        raise CandidateSetStructureError("canonical candidate JSONL digest differs from manifest")
    return StructuralCandidateSetVerification(
        candidate_set_manifest_id=validated_manifest.candidate_set_manifest_id,
        scope_id=validated_manifest.scope.scope_id,
        target_kind=validated_manifest.scope.target_kind,
        target_id=validated_manifest.scope.target_id,
        candidate_count=validated_manifest.candidate_count,
        candidate_ids=validated_manifest.candidate_ids,
        candidate_records_sha256=validated_manifest.candidate_records_sha256,
        source_closure_binding_ids=tuple(
            item.source_closure_binding_id for item in validated_manifest.source_closures
        ),
    )


__all__ = [
    "CANDIDATE_ENUMERATION_SCOPE_PREFIX",
    "CANDIDATE_SET_MANIFEST_PREFIX",
    "CANDIDATE_SOURCE_CLOSURE_PREFIX",
    "CANONICAL_RESOLUTION_SOURCES",
    "CandidateEnumerationScope",
    "CandidateSetManifest",
    "CandidateSetStructureError",
    "CandidateSourceClosureBinding",
    "StructuralCandidateSetVerification",
    "TargetTheoremContextBinding",
    "build_candidate_enumeration_scope",
    "build_candidate_set_manifest",
    "build_candidate_source_closure_binding",
    "canonical_candidate_jsonl_bytes",
    "verify_candidate_set_structure",
]
