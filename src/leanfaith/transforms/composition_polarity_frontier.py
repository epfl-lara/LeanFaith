"""Prepare an immutable polarity-safe frontier for a third deterministic hop.

Depth-two composition deliberately admits only ``P -> P`` and ``P -> N``.
This module extracts the alpha-novel, intention-consistent final statements
from that receipt and makes them directly consumable by the existing E2 scale
runner.  A frontier statement may receive *only* another certificate-backed
positive hop:

``P -> P -> P`` preserves the equivalent-candidate intention, while
``P -> N -> P`` preserves the near-miss-candidate intention.  A second
negative hop is never authorized.  The artifact remains provisional and
creates no semantic label, promotion, or training/evaluation eligibility.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.representations import NORMALIZATION_VERSION
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import (
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
)
from leanfaith.transforms.composition_unique_pairs import (
    CompositionUniquePairError,
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
    _absolute_path,
    _child_directory_metadata,
    _cleanup_private_directory,
    _HeldDirectory,
    _HeldDirectorySnapshot,
    _open_child_directory,
    _open_held_directory,
    _rename_noreplace_at,
    _snapshot_exact_directory,
    _verify_child_identity,
    _verify_directory_path_identity,
    _verify_held_snapshot,
    _write_new_file_at,
)
from leanfaith.transforms.provisional_pair_combine import (
    ProvisionalPairCombineError,
    _iter_jsonl_objects,
    _load_root,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_d0_materializer import V2D0MaterializationResult
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult

_HEX64 = r"^[0-9a-f]{64}$"
_FRONTIER_ID = r"^detcomp_frontier:[0-9a-f]{64}$"
_FRONTIER_SET_ID = r"^detcomp_frontier_set:[0-9a-f]{64}$"
_CHAIN_FILES = frozenset({"chains.jsonl", "manifest.json"})
_UNIQUE_FILES = frozenset({"unique_pairs.jsonl", "manifest.json"})
_OUTPUT_FILES = frozenset(
    {"frontier.jsonl", "theorems.jsonl", "representations.jsonl", "manifest.json"}
)

type PreservedIntention = Literal["equivalent_candidate", "near_miss_candidate"]


class CompositionPolarityFrontierError(ValueError):
    """The depth-two inputs or immutable frontier replay failed closed."""


def _without_id(payload: Mapping[str, object], field: str) -> dict[str, object]:
    output = dict(payload)
    output.pop(field, None)
    return output


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(item) for item in records)


class DeterministicCompositionPolarityFrontierRecord(StrictModel):
    """One depth-two final statement admitted only to a positive third hop."""

    schema_version: Literal[1] = 1
    frontier_id: str = Field(pattern=_FRONTIER_ID)
    input_unique_pair_id: str = Field(pattern=r"^detcomp_unique_pair:[0-9a-f]{64}$")
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    context_id: str = Field(min_length=1)
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    original_source_theorem_id: str = Field(min_length=1)
    original_source_representation_id: str = Field(min_length=1)
    depth_two_theorem_ids: tuple[str, ...] = Field(min_length=1)
    depth_two_representation_ids: tuple[str, ...] = Field(min_length=1)
    selected_frontier_theorem_id: str = Field(min_length=1)
    selected_frontier_representation_id: str = Field(min_length=1)
    final_candidate_code_hash: str = Field(pattern=_HEX64)
    final_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    parent_chain_ids: tuple[str, ...] = Field(min_length=1)
    parent_chain_sequences: tuple[str, ...] = Field(min_length=1)
    parent_chain_kind: Literal["P_to_P", "P_to_N"]
    preserved_intention: PreservedIntention
    semantic_negative_hop_count: Literal[0, 1]
    chain_depth: Literal[2] = 2
    permitted_next_hop: Literal["E2_positive_only"] = "E2_positive_only"
    intended_third_hop_polarity: Literal["positive"] = "positive"
    intended_third_hop_relation: Literal["equivalent"] = "equivalent"
    second_negative_hop_authorized: Literal[False] = False
    quality_tier: Literal["provisional"] = "provisional"
    intention_only: Literal[True] = True
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> DeterministicCompositionPolarityFrontierRecord:
        expected = "detcomp_frontier:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "frontier_id")
        )
        if self.frontier_id != expected:
            raise ValueError("frontier_id does not match immutable payload")
        for field_name in (
            "root_ancestry_ids",
            "depth_two_theorem_ids",
            "depth_two_representation_ids",
            "parent_chain_ids",
            "parent_chain_sequences",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if self.selected_frontier_theorem_id not in self.depth_two_theorem_ids:
            raise ValueError("selected frontier theorem is outside the exact pair lineage")
        if self.selected_frontier_representation_id not in self.depth_two_representation_ids:
            raise ValueError("selected frontier representation is outside the exact pair lineage")
        if self.parent_chain_kind == "P_to_P":
            if self.preserved_intention != "equivalent_candidate":
                raise ValueError("P-to-P frontier lost its equivalent intention")
            if self.semantic_negative_hop_count != 0:
                raise ValueError("P-to-P frontier claims a negative hop")
        else:
            if self.preserved_intention != "near_miss_candidate":
                raise ValueError("P-to-N frontier lost its near-miss intention")
            if self.semantic_negative_hop_count != 1:
                raise ValueError("P-to-N frontier must carry exactly one negative hop")
        return self


class DeterministicCompositionPolarityFrontierManifest(StrictModel):
    """Self-authenticating manifest for a polarity-safe third-hop frontier."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_composition_polarity_frontier"] = (
        "deterministic_v2_composition_polarity_frontier"
    )
    frontier_set_id: str = Field(pattern=_FRONTIER_SET_ID)
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    input_chain_manifest_sha256: str = Field(pattern=_HEX64)
    input_chain_records_sha256: str = Field(pattern=_HEX64)
    input_unique_pair_set_id: str = Field(pattern=r"^detcomp_unique_pair_set:[0-9a-f]{64}$")
    input_unique_manifest_sha256: str = Field(pattern=_HEX64)
    input_unique_records_sha256: str = Field(pattern=_HEX64)
    input_root_binding_ids: tuple[str, ...] = Field(min_length=1)
    input_unique_pair_count: int = Field(ge=0)
    excluded_counts: dict[str, int]
    frontier_count: int = Field(ge=1)
    intention_counts: dict[str, int]
    frontier_output: Literal["frontier.jsonl"] = "frontier.jsonl"
    theorem_output: Literal["theorems.jsonl"] = "theorems.jsonl"
    representation_output: Literal["representations.jsonl"] = "representations.jsonl"
    frontier_output_sha256: str = Field(pattern=_HEX64)
    theorem_output_sha256: str = Field(pattern=_HEX64)
    representation_output_sha256: str = Field(pattern=_HEX64)
    chain_depth_limit: Literal[3] = 3
    source_depth: Literal[2] = 2
    next_hop_policy: Literal["certificate_backed_e2_positive_only_v1"] = (
        "certificate_backed_e2_positive_only_v1"
    )
    maximum_semantic_negative_hops: Literal[1] = 1
    negative_after_negative_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _reconciles(self) -> DeterministicCompositionPolarityFrontierManifest:
        expected = "detcomp_frontier_set:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "frontier_set_id")
        )
        if self.frontier_set_id != expected:
            raise ValueError("frontier_set_id does not match immutable payload")
        if self.input_root_binding_ids != tuple(sorted(set(self.input_root_binding_ids))):
            raise ValueError("input root bindings must be sorted and unique")
        if any(value < 0 for value in self.excluded_counts.values()):
            raise ValueError("excluded counts cannot be negative")
        if any(value < 0 for value in self.intention_counts.values()):
            raise ValueError("intention counts cannot be negative")
        if self.frontier_count + sum(self.excluded_counts.values()) != self.input_unique_pair_count:
            raise ValueError("frontier admission and exclusion counts do not reconcile")
        if self.frontier_count != sum(self.intention_counts.values()):
            raise ValueError("frontier intention counts do not reconcile")
        return self


@dataclass(frozen=True, slots=True)
class CompositionPolarityFrontierArtifacts:
    output_dir: Path
    manifest_path: Path
    frontier_path: Path
    theorem_path: Path
    representation_path: Path
    frontier_set_id: str
    frontier_count: int
    replayed: bool


def _load_manifest_bytes[ModelT: StrictModel](
    payload: bytes,
    *,
    label: str,
    model: type[ModelT],
) -> ModelT:
    try:
        parsed = model.model_validate_json(payload)
    except ValueError as exc:
        raise CompositionPolarityFrontierError(f"invalid {model.__name__}: {label}: {exc}") from exc
    if payload != _canonical_line(parsed):
        raise CompositionPolarityFrontierError(f"non-canonical {model.__name__}: {label}")
    return parsed


def _load_records_bytes[ModelT: StrictModel](
    payload: bytes,
    *,
    label: str,
    model: type[ModelT],
) -> tuple[ModelT, ...]:
    output: list[ModelT] = []
    for line_number, raw_line in enumerate(payload.splitlines(keepends=True), start=1):
        if not raw_line.endswith(b"\n") or not raw_line.strip():
            raise CompositionPolarityFrontierError(
                f"invalid JSONL framing at {label}:{line_number}"
            )
        try:
            item = model.model_validate_json(raw_line)
        except ValueError as exc:
            raise CompositionPolarityFrontierError(
                f"invalid {model.__name__} at {label}:{line_number}: {exc}"
            ) from exc
        if raw_line != _canonical_line(item):
            raise CompositionPolarityFrontierError(
                f"non-canonical {model.__name__} at {label}:{line_number}"
            )
        output.append(item)
    return tuple(output)


def _load_inputs(
    *, chain_snapshot: _HeldDirectorySnapshot, unique_snapshot: _HeldDirectorySnapshot
) -> tuple[
    DeterministicCompositionChainManifest,
    tuple[DeterministicCompositionChainRecord, ...],
    DeterministicCompositionUniquePairManifest,
    tuple[DeterministicCompositionUniquePairRecord, ...],
]:
    # These snapshots are held no-follow directory/file descriptors supplied by
    # ``prepare_deterministic_v2_polarity_frontier``.  Avoid reopening their
    # paths and accidentally following a replacement or symlink.
    chain_files = chain_snapshot.files
    unique_files = unique_snapshot.files
    chain_manifest = _load_manifest_bytes(
        chain_files["manifest.json"].payload,
        label="chain/manifest.json",
        model=DeterministicCompositionChainManifest,
    )
    chains = _load_records_bytes(
        chain_files[chain_manifest.chain_output].payload,
        label=f"chain/{chain_manifest.chain_output}",
        model=DeterministicCompositionChainRecord,
    )
    unique_manifest = _load_manifest_bytes(
        unique_files["manifest.json"].payload,
        label="unique-pair/manifest.json",
        model=DeterministicCompositionUniquePairManifest,
    )
    unique_pairs = _load_records_bytes(
        unique_files[unique_manifest.unique_output].payload,
        label=f"unique-pair/{unique_manifest.unique_output}",
        model=DeterministicCompositionUniquePairRecord,
    )
    if (
        chain_files[chain_manifest.chain_output].sha256 != chain_manifest.chain_output_sha256
        or len(chains) != chain_manifest.chain_count
    ):
        raise CompositionPolarityFrontierError("chain records differ from manifest")
    if (
        unique_manifest.input_chain_set_id != chain_manifest.chain_set_id
        or unique_manifest.input_chain_manifest_sha256 != chain_files["manifest.json"].sha256
        or unique_manifest.input_chain_records_sha256 != chain_manifest.chain_output_sha256
        or unique_files[unique_manifest.unique_output].sha256
        != unique_manifest.unique_output_sha256
        or len(unique_pairs) != unique_manifest.unique_pair_count
    ):
        raise CompositionPolarityFrontierError("unique-pair input does not bind exact chains")
    return chain_manifest, chains, unique_manifest, unique_pairs


def _verify_unique_pairs_against_chains(
    *,
    chain_manifest: DeterministicCompositionChainManifest,
    chains: Sequence[DeterministicCompositionChainRecord],
    unique_manifest: DeterministicCompositionUniquePairManifest,
    unique_pairs: Sequence[DeterministicCompositionUniquePairRecord],
) -> None:
    """Re-derive every polarity-bearing unique-pair field from exact chains."""

    if unique_manifest.input_chain_set_id != chain_manifest.chain_set_id:
        raise CompositionPolarityFrontierError("unique pairs bind a different chain set")
    by_id = {item.chain_id: item for item in chains}
    if len(by_id) != len(chains):
        raise CompositionPolarityFrontierError("chain IDs are duplicated")
    memberships: Counter[str] = Counter()
    for pair in unique_pairs:
        if pair.input_chain_set_id != chain_manifest.chain_set_id:
            raise CompositionPolarityFrontierError("unique pair binds a different chain set")
        try:
            members = tuple(by_id[item] for item in pair.chain_ids)
        except KeyError as exc:
            raise CompositionPolarityFrontierError(
                "unique pair references a chain outside the exact receipt"
            ) from exc
        memberships.update(pair.chain_ids)
        if len(members) != pair.gross_chain_count:
            raise CompositionPolarityFrontierError("unique-pair chain count differs")
        first = members[0]
        scalar_checks = (
            pair.input_seed_set_id == first.seed_set_id,
            pair.context_id == first.context_id,
            pair.root_ancestry_ids == first.root_ancestry_ids,
            pair.original_source_theorem_id == first.original_source_theorem_id,
            pair.original_source_representation_id == first.original_source_representation_id,
            pair.final_candidate_code_hash == first.final_candidate_code_hash,
            pair.final_alpha_identity_fingerprint == first.final_alpha_identity_fingerprint,
        )
        if not all(scalar_checks) or any(
            (
                item.seed_set_id != first.seed_set_id
                or item.context_id != first.context_id
                or item.root_ancestry_ids != first.root_ancestry_ids
                or item.original_source_theorem_id != first.original_source_theorem_id
                or item.original_source_representation_id != first.original_source_representation_id
                or item.final_candidate_code_hash != first.final_candidate_code_hash
                or item.final_alpha_identity_fingerprint != first.final_alpha_identity_fingerprint
            )
            for item in members
        ):
            raise CompositionPolarityFrontierError("unique-pair scalar lineage differs from chains")
        derived = {
            "intermediate_theorem_ids": tuple(
                sorted({item.intermediate_theorem_id for item in members})
            ),
            "intermediate_representation_ids": tuple(
                sorted({item.intermediate_representation_id for item in members})
            ),
            "final_theorem_ids": tuple(sorted({item.final_theorem_id for item in members})),
            "final_representation_ids": tuple(
                sorted({item.final_representation_id for item in members})
            ),
            "chain_kinds": tuple(sorted({item.chain_kind for item in members})),
            "chain_sequences": tuple(
                sorted({f"{item.first_hop_rule_id}->{item.second_hop_rule_id}" for item in members})
            ),
        }
        if any(getattr(pair, field) != value for field, value in derived.items()):
            raise CompositionPolarityFrontierError(
                "unique-pair polarity or theorem lineage differs from exact chains"
            )
    if set(memberships) != set(by_id) or any(value != 1 for value in memberships.values()):
        raise CompositionPolarityFrontierError(
            "unique pairs do not partition the exact chain receipt once"
        )


def _candidate_inventory(
    *,
    root_paths: Sequence[Path],
    chain_manifest: DeterministicCompositionChainManifest,
    wanted_theorem_ids: set[str],
) -> tuple[
    dict[str, tuple[TheoremRecord, RepresentationRecord]],
    tuple[str, ...],
]:
    expected_bindings = {item.root_binding_id for item in chain_manifest.second_hop_roots}
    if len(root_paths) != len(expected_bindings):
        raise CompositionPolarityFrontierError("second-hop root count differs from chain manifest")
    found_bindings: set[str] = set()
    candidates: dict[str, tuple[TheoremRecord, RepresentationRecord]] = {}
    for supplied in root_paths:
        try:
            root = supplied.resolve(strict=True)
            loaded = _load_root(root)
        except (OSError, ProvisionalPairCombineError) as exc:
            raise CompositionPolarityFrontierError(f"second-hop root failed audit: {exc}") from exc
        binding = loaded.binding
        if binding.root_binding_id not in expected_bindings:
            raise CompositionPolarityFrontierError("foreign second-hop root binding")
        if binding.root_binding_id in found_bindings:
            raise CompositionPolarityFrontierError("duplicate second-hop root binding")
        expected = next(
            item
            for item in chain_manifest.second_hop_roots
            if item.root_binding_id == binding.root_binding_id
        )
        if (
            binding.root_tree_hash != expected.root_tree_hash
            or binding.results.sha256 != expected.results.sha256
            or binding.run_kind != expected.run_kind
            or binding.rule_ids != expected.rule_ids
        ):
            raise CompositionPolarityFrontierError("second-hop root differs from chain binding")
        found_bindings.add(binding.root_binding_id)
        model: type[V2E2MaterializationResult] | type[V2D0MaterializationResult]
        model = V2E2MaterializationResult if binding.run_kind == "e2" else V2D0MaterializationResult
        for _, raw, raw_line in _iter_jsonl_objects(root / "results.jsonl"):
            try:
                result = model.model_validate(raw)
            except ValueError as exc:
                raise CompositionPolarityFrontierError(
                    f"invalid materialization result under {root}: {exc}"
                ) from exc
            if raw_line != _canonical_line(result):
                raise CompositionPolarityFrontierError("non-canonical materialization result")
            theorem = result.candidate_theorem
            representation = result.candidate_representation
            if theorem is None or theorem.theorem_id not in wanted_theorem_ids:
                continue
            if representation is None:
                raise CompositionPolarityFrontierError("wanted candidate lacks representation")
            if (
                result.terminal_status != "provisional_variant"
                or result.draft is None
                or theorem.theorem_id != representation.theorem_id
                or theorem.context_id != representation.context_id
                or representation.normalization_version != NORMALIZATION_VERSION
                or representation.content_hash != _representation_payload_hash(representation)
                or hashlib.sha256(theorem.proof_stripped_declaration.encode("utf-8")).hexdigest()
                != result.draft.candidate_code_hash
            ):
                raise CompositionPolarityFrontierError("wanted candidate lineage is incomplete")
            previous = candidates.get(theorem.theorem_id)
            current = (theorem, representation)
            if previous is not None and previous != current:
                raise CompositionPolarityFrontierError(
                    "candidate theorem ID has conflicting payloads"
                )
            candidates[theorem.theorem_id] = current
    if found_bindings != expected_bindings:
        raise CompositionPolarityFrontierError("second-hop root bindings are incomplete")
    missing = wanted_theorem_ids - set(candidates)
    if missing:
        raise CompositionPolarityFrontierError(
            f"frontier candidates are missing from exact roots: {len(missing)}"
        )
    return candidates, tuple(sorted(found_bindings))


def _build_record(
    *,
    unique_pair: DeterministicCompositionUniquePairRecord,
    chain_set_id: str,
    candidates: Mapping[str, tuple[TheoremRecord, RepresentationRecord]],
) -> tuple[
    DeterministicCompositionPolarityFrontierRecord,
    TheoremRecord,
    RepresentationRecord,
]:
    if len(unique_pair.chain_kinds) != 1:
        raise CompositionPolarityFrontierError("mixed-intention pair cannot enter frontier")
    theorem_id = min(unique_pair.final_theorem_ids)
    theorem, representation = candidates[theorem_id]
    peer_payloads = tuple(candidates[item] for item in unique_pair.final_theorem_ids)
    if any(
        peer_theorem.proof_stripped_declaration != theorem.proof_stripped_declaration
        or peer_representation.alpha_identity_fingerprint
        != representation.alpha_identity_fingerprint
        or peer_representation.signature_explicit != representation.signature_explicit
        for peer_theorem, peer_representation in peer_payloads
    ):
        raise CompositionPolarityFrontierError("deduplicated frontier candidates disagree")
    representation_ids = tuple(
        sorted(candidates[item][1].representation_id for item in unique_pair.final_theorem_ids)
    )
    if unique_pair.final_representation_ids != representation_ids:
        raise CompositionPolarityFrontierError("unique-pair representation lineage differs")
    if (
        hashlib.sha256(theorem.proof_stripped_declaration.encode("utf-8")).hexdigest()
        != unique_pair.final_candidate_code_hash
        or representation.alpha_identity_fingerprint != unique_pair.final_alpha_identity_fingerprint
        or theorem.root_ancestry_ids != unique_pair.root_ancestry_ids
        or theorem.context_id != unique_pair.context_id
    ):
        raise CompositionPolarityFrontierError("frontier candidate differs from unique pair")
    kind = unique_pair.chain_kinds[0]
    intention: PreservedIntention = (
        "equivalent_candidate" if kind == "P_to_P" else "near_miss_candidate"
    )
    data: dict[str, object] = {
        "input_unique_pair_id": unique_pair.unique_pair_id,
        "input_chain_set_id": chain_set_id,
        "context_id": unique_pair.context_id,
        "root_ancestry_ids": unique_pair.root_ancestry_ids,
        "original_source_theorem_id": unique_pair.original_source_theorem_id,
        "original_source_representation_id": unique_pair.original_source_representation_id,
        "depth_two_theorem_ids": unique_pair.final_theorem_ids,
        "depth_two_representation_ids": unique_pair.final_representation_ids,
        "selected_frontier_theorem_id": theorem.theorem_id,
        "selected_frontier_representation_id": representation.representation_id,
        "final_candidate_code_hash": unique_pair.final_candidate_code_hash,
        "final_alpha_identity_fingerprint": unique_pair.final_alpha_identity_fingerprint,
        "parent_chain_ids": unique_pair.chain_ids,
        "parent_chain_sequences": unique_pair.chain_sequences,
        "parent_chain_kind": kind,
        "preserved_intention": intention,
        "semantic_negative_hop_count": 0 if kind == "P_to_P" else 1,
    }
    placeholder = DeterministicCompositionPolarityFrontierRecord.model_construct(
        _fields_set=None,
        frontier_id=f"detcomp_frontier:{'0' * 64}",
        **data,
    )
    frontier_id = "detcomp_frontier:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "frontier_id")
    )
    record = DeterministicCompositionPolarityFrontierRecord.model_validate(
        {"frontier_id": frontier_id, **data}
    )
    return record, theorem, representation


def _verify_existing_output(output: _HeldDirectory, payloads: Mapping[str, bytes]) -> None:
    with _snapshot_exact_directory(output, expected_names=_OUTPUT_FILES) as snapshot:
        for name, expected in payloads.items():
            if snapshot.files[name].payload != expected:
                raise CompositionPolarityFrontierError(
                    f"existing frontier output differs: {snapshot.directory.path / name}"
                )
        _verify_held_snapshot(snapshot, verify_path_identity=False)


def _publish_or_verify_frontier(
    *,
    output_dir: Path,
    payloads: Mapping[str, bytes],
    forbidden_input_identities: frozenset[tuple[int, int]],
) -> bool:
    """Publish with no-follow descriptors and no-replace rename semantics."""

    output_dir = _absolute_path(output_dir)
    if output_dir == Path(output_dir.anchor) or not output_dir.name:
        raise CompositionPolarityFrontierError("frontier output must name a child directory")
    try:
        with _open_held_directory(
            output_dir.parent,
            label="frontier output parent",
            create=True,
            forbidden_identities=forbidden_input_identities,
        ) as parent:
            existing = _child_directory_metadata(parent, output_dir.name)
            if existing is not None:
                with _open_child_directory(parent, output_dir.name) as output:
                    _verify_child_identity(parent, output_dir.name, output.identity)
                    _verify_directory_path_identity(parent)
                    _verify_directory_path_identity(output)
                    _verify_existing_output(output, payloads)
                return True

            temporary_name = f".{output_dir.name}.{secrets.token_hex(16)}"
            os.mkdir(temporary_name, mode=0o700, dir_fd=parent.fd)
            try:
                temporary = _open_child_directory(parent, temporary_name)
            except BaseException:
                metadata = _child_directory_metadata(parent, temporary_name)
                if metadata is not None:
                    os.rmdir(temporary_name, dir_fd=parent.fd)
                raise
            renamed = False
            try:
                for name, payload in payloads.items():
                    _write_new_file_at(temporary.fd, name, payload)
                os.fsync(temporary.fd)
                _verify_child_identity(parent, temporary_name, temporary.identity)
                if _rename_noreplace_at(parent.fd, temporary_name, output_dir.name):
                    renamed = True
                    temporary.path = output_dir
                    os.fsync(parent.fd)
                    _verify_child_identity(parent, output_dir.name, temporary.identity)
                    _verify_directory_path_identity(parent)
                    _verify_directory_path_identity(temporary)
                    _verify_existing_output(temporary, payloads)
                    return False
                _cleanup_private_directory(
                    parent,
                    temporary,
                    authoritative_names=frozenset({temporary_name}),
                )
                with _open_child_directory(parent, output_dir.name) as output:
                    _verify_child_identity(parent, output_dir.name, output.identity)
                    _verify_directory_path_identity(parent)
                    _verify_directory_path_identity(output)
                    _verify_existing_output(output, payloads)
                return True
            except BaseException:
                names = {temporary_name}
                if renamed:
                    names.add(output_dir.name)
                _cleanup_private_directory(
                    parent,
                    temporary,
                    authoritative_names=frozenset(names),
                )
                raise
            finally:
                temporary.close()
    except CompositionUniquePairError as exc:
        raise CompositionPolarityFrontierError(str(exc)) from exc


def prepare_deterministic_v2_polarity_frontier(
    *,
    chain_dir: Path,
    unique_pair_dir: Path,
    second_hop_roots: Sequence[Path],
    output_dir: Path,
) -> CompositionPolarityFrontierArtifacts:
    """Freeze alpha-novel depth-two finals for one positive-only third hop."""
    chain_dir = _absolute_path(chain_dir)
    unique_pair_dir = _absolute_path(unique_pair_dir)
    root_paths = tuple(_absolute_path(item) for item in second_hop_roots)
    output_dir = _absolute_path(output_dir)
    protected_paths = (chain_dir, unique_pair_dir, *root_paths)
    if any(output_dir == root or output_dir.is_relative_to(root) for root in protected_paths):
        raise CompositionPolarityFrontierError("output directory overlaps an immutable input")

    try:
        with ExitStack() as stack:
            chain_root = stack.enter_context(
                _open_held_directory(chain_dir, label="composition chain directory")
            )
            unique_root = stack.enter_context(
                _open_held_directory(unique_pair_dir, label="composition unique-pair directory")
            )
            held_roots = tuple(
                stack.enter_context(_open_held_directory(path, label=f"second-hop root {index}"))
                for index, path in enumerate(root_paths)
            )
            chain_snapshot = stack.enter_context(
                _snapshot_exact_directory(chain_root, expected_names=_CHAIN_FILES)
            )
            unique_snapshot = stack.enter_context(
                _snapshot_exact_directory(unique_root, expected_names=_UNIQUE_FILES)
            )
            chain_manifest, chains, unique_manifest, unique_pairs = _load_inputs(
                chain_snapshot=chain_snapshot,
                unique_snapshot=unique_snapshot,
            )
            _verify_unique_pairs_against_chains(
                chain_manifest=chain_manifest,
                chains=chains,
                unique_manifest=unique_manifest,
                unique_pairs=unique_pairs,
            )
            excluded: Counter[str] = Counter()
            admitted: list[DeterministicCompositionUniquePairRecord] = []
            for item in unique_pairs:
                if item.source_alpha_return:
                    excluded["source_alpha_return"] += 1
                elif len(item.chain_kinds) != 1:
                    excluded["mixed_intention"] += 1
                elif not item.alpha_novel:
                    raise CompositionPolarityFrontierError("alpha novelty flags are inconsistent")
                else:
                    admitted.append(item)
            if not admitted:
                raise CompositionPolarityFrontierError("no alpha-novel intention-consistent pairs")
            wanted = {theorem_id for item in admitted for theorem_id in item.final_theorem_ids}
            candidates, root_binding_ids = _candidate_inventory(
                root_paths=tuple(item.path for item in held_roots),
                chain_manifest=chain_manifest,
                wanted_theorem_ids=wanted,
            )
            built = tuple(
                _build_record(
                    unique_pair=item,
                    chain_set_id=chain_manifest.chain_set_id,
                    candidates=candidates,
                )
                for item in admitted
            )
            built = tuple(sorted(built, key=lambda item: item[0].frontier_id))
            records = tuple(item[0] for item in built)
            theorems = tuple(item[1] for item in built)
            representations = tuple(item[2] for item in built)
            if len({item.frontier_id for item in records}) != len(records):
                raise CompositionPolarityFrontierError("frontier IDs are duplicated")
            if len({item.theorem_id for item in theorems}) != len(theorems):
                raise CompositionPolarityFrontierError(
                    "selected frontier theorem IDs are duplicated"
                )
            if len({item.representation_id for item in representations}) != len(representations):
                raise CompositionPolarityFrontierError(
                    "selected frontier representation IDs are duplicated"
                )
            for record, theorem, representation in zip(
                records, theorems, representations, strict=True
            ):
                if (
                    record.selected_frontier_theorem_id != theorem.theorem_id
                    or record.selected_frontier_representation_id
                    != representation.representation_id
                    or representation.theorem_id != theorem.theorem_id
                ):
                    raise CompositionPolarityFrontierError("frontier output ordering differs")

            frontier_payload = _canonical_jsonl(records)
            theorem_payload = _canonical_jsonl(theorems)
            representation_payload = _canonical_jsonl(representations)
            intentions = Counter(item.preserved_intention for item in records)
            manifest_data: dict[str, object] = {
                "input_chain_set_id": chain_manifest.chain_set_id,
                "input_chain_manifest_sha256": chain_snapshot.files["manifest.json"].sha256,
                "input_chain_records_sha256": chain_manifest.chain_output_sha256,
                "input_unique_pair_set_id": unique_manifest.unique_pair_set_id,
                "input_unique_manifest_sha256": unique_snapshot.files["manifest.json"].sha256,
                "input_unique_records_sha256": unique_manifest.unique_output_sha256,
                "input_root_binding_ids": root_binding_ids,
                "input_unique_pair_count": len(unique_pairs),
                "excluded_counts": dict(sorted(excluded.items())),
                "frontier_count": len(records),
                "intention_counts": dict(sorted(intentions.items())),
                "frontier_output_sha256": hashlib.sha256(frontier_payload).hexdigest(),
                "theorem_output_sha256": hashlib.sha256(theorem_payload).hexdigest(),
                "representation_output_sha256": hashlib.sha256(representation_payload).hexdigest(),
            }
            placeholder = DeterministicCompositionPolarityFrontierManifest.model_construct(
                _fields_set=None,
                frontier_set_id=f"detcomp_frontier_set:{'0' * 64}",
                **manifest_data,
            )
            frontier_set_id = "detcomp_frontier_set:" + hash_canonical(
                _without_id(placeholder.model_dump(mode="json"), "frontier_set_id")
            )
            manifest = DeterministicCompositionPolarityFrontierManifest.model_validate(
                {"frontier_set_id": frontier_set_id, **manifest_data}
            )
            payloads = {
                "frontier.jsonl": frontier_payload,
                "theorems.jsonl": theorem_payload,
                "representations.jsonl": representation_payload,
                "manifest.json": _canonical_line(manifest),
            }

            _verify_held_snapshot(chain_snapshot)
            _verify_held_snapshot(unique_snapshot)
            for root_handle in held_roots:
                _verify_directory_path_identity(root_handle)
            input_identities = frozenset(
                (item.identity.device, item.identity.inode)
                for item in (chain_root, unique_root, *held_roots)
            )
            replayed = _publish_or_verify_frontier(
                output_dir=output_dir,
                payloads=payloads,
                forbidden_input_identities=input_identities,
            )
    except CompositionUniquePairError as exc:
        raise CompositionPolarityFrontierError(str(exc)) from exc
    return CompositionPolarityFrontierArtifacts(
        output_dir=output_dir,
        manifest_path=output_dir / "manifest.json",
        frontier_path=output_dir / "frontier.jsonl",
        theorem_path=output_dir / "theorems.jsonl",
        representation_path=output_dir / "representations.jsonl",
        frontier_set_id=frontier_set_id,
        frontier_count=len(records),
        replayed=replayed,
    )


__all__ = [
    "CompositionPolarityFrontierArtifacts",
    "CompositionPolarityFrontierError",
    "DeterministicCompositionPolarityFrontierManifest",
    "DeterministicCompositionPolarityFrontierRecord",
    "prepare_deterministic_v2_polarity_frontier",
]
