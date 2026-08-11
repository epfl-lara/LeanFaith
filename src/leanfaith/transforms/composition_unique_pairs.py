"""Audit-only unique-pair postprocessing for deterministic composition chains.

This module is deliberately separate from :mod:`composition_chain`: chain-v1
remains an immutable gross-lineage receipt.  The postprocessor binds one exact
seed directory to one exact chain directory and groups chains by the canonical
identity ``(original source theorem, final candidate code hash)``.  Reversible
cycles therefore remain auditable without being counted as novel pairs.

No semantic label, promotion, training/evaluation eligibility, or gate credit
is created here.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.transforms.composition_chain import (
    CompositionChainError,
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
    _canonical_jsonl,
    _canonical_line,
    _load_canonical_jsonl,
    _load_seed_inventory,
    _verify_seed_snapshot,
    _without_id,
    _write_new_file,
)
from leanfaith.transforms.composition_seed import CompositionSeedManifest, CompositionSeedRecord

_HEX64 = r"^[0-9a-f]{64}$"
_UNIQUE_PAIR_ID = r"^detcomp_unique_pair:[0-9a-f]{64}$"
_UNIQUE_PAIR_SET_ID = r"^detcomp_unique_pair_set:[0-9a-f]{64}$"
_CHAIN_FILES = frozenset({"chains.jsonl", "manifest.json"})
_OUTPUT_FILES = frozenset({"unique_pairs.jsonl", "manifest.json"})

type ChainKind = Literal["P_to_P", "P_to_N"]


class CompositionUniquePairError(ValueError):
    """The bound composition inputs or immutable unique-pair replay failed."""


class DeterministicCompositionUniquePairRecord(StrictModel):
    """One exact source/final-code pair with all gross chain provenance."""

    schema_version: Literal[1] = 1
    unique_pair_id: str = Field(pattern=_UNIQUE_PAIR_ID)
    canonical_unique_key: str = Field(pattern=_HEX64)
    input_seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    context_id: str = Field(min_length=1)
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    original_source_theorem_id: str = Field(min_length=1)
    original_source_representation_id: str = Field(min_length=1)
    source_statement_content_hash: str = Field(pattern=_HEX64)
    source_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    intermediate_theorem_ids: tuple[str, ...] = Field(min_length=1)
    intermediate_representation_ids: tuple[str, ...] = Field(min_length=1)
    final_theorem_ids: tuple[str, ...] = Field(min_length=1)
    final_representation_ids: tuple[str, ...] = Field(min_length=1)
    final_candidate_code_hash: str = Field(pattern=_HEX64)
    final_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    chain_ids: tuple[str, ...] = Field(min_length=1)
    chain_sequences: tuple[str, ...] = Field(min_length=1)
    chain_kinds: tuple[ChainKind, ...] = Field(min_length=1)
    gross_chain_count: int = Field(ge=1)
    duplicate_excess_count: int = Field(ge=0)
    source_content_return: bool
    source_alpha_return: bool
    alpha_novel: bool
    quality_tier: Literal["provisional"] = "provisional"
    audit_only: Literal[True] = True
    intention_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> DeterministicCompositionUniquePairRecord:
        expected_key = hash_canonical(
            {
                "schema": "deterministic_v2_composition_unique_pair_key_v1",
                "original_source_theorem_id": self.original_source_theorem_id,
                "final_candidate_code_hash": self.final_candidate_code_hash,
            }
        )
        if self.canonical_unique_key != expected_key:
            raise ValueError("canonical unique key does not match source/final code")
        if self.unique_pair_id != f"detcomp_unique_pair:{expected_key}":
            raise ValueError("unique_pair_id does not match canonical unique key")
        for field_name in (
            "root_ancestry_ids",
            "intermediate_theorem_ids",
            "intermediate_representation_ids",
            "final_theorem_ids",
            "final_representation_ids",
            "chain_ids",
            "chain_sequences",
            "chain_kinds",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if self.gross_chain_count != len(self.chain_ids):
            raise ValueError("gross chain count does not match chain IDs")
        if self.duplicate_excess_count != self.gross_chain_count - 1:
            raise ValueError("pair duplicate excess does not reconcile")
        if self.source_content_return != (
            self.source_statement_content_hash == self.final_candidate_code_hash
        ):
            raise ValueError("source content return does not reconcile")
        if self.source_alpha_return != (
            self.source_alpha_identity_fingerprint == self.final_alpha_identity_fingerprint
        ):
            raise ValueError("source alpha return does not reconcile")
        if self.alpha_novel == self.source_alpha_return:
            raise ValueError("alpha novelty must be the inverse of source alpha return")
        return self


class DeterministicCompositionUniquePairManifest(StrictModel):
    """Self-authenticating audit manifest for chain-v1 unique pairs."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_composition_unique_pair_set"] = (
        "deterministic_v2_composition_unique_pair_set"
    )
    method_version: Literal["deterministic_v2_composition_unique_pairs_v1"] = (
        "deterministic_v2_composition_unique_pairs_v1"
    )
    unique_pair_set_id: str = Field(pattern=_UNIQUE_PAIR_SET_ID)
    input_seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    input_seed_manifest_sha256: str = Field(pattern=_HEX64)
    input_seed_records_sha256: str = Field(pattern=_HEX64)
    input_seed_theorems_sha256: str = Field(pattern=_HEX64)
    input_seed_representations_sha256: str = Field(pattern=_HEX64)
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    input_chain_manifest_sha256: str = Field(pattern=_HEX64)
    input_chain_records_sha256: str = Field(pattern=_HEX64)
    gross_chain_count: int = Field(ge=0)
    unique_pair_count: int = Field(ge=0)
    duplicate_group_count: int = Field(ge=0)
    duplicate_excess_count: int = Field(ge=0)
    gross_source_content_return_count: int = Field(ge=0)
    unique_source_content_return_count: int = Field(ge=0)
    gross_source_alpha_return_count: int = Field(ge=0)
    unique_source_alpha_return_count: int = Field(ge=0)
    gross_alpha_novel_count: int = Field(ge=0)
    unique_alpha_novel_count: int = Field(ge=0)
    gross_chain_kind_counts: dict[str, int]
    unique_pair_chain_kind_membership_counts: dict[str, int]
    gross_sequence_counts: dict[str, int]
    unique_pair_sequence_membership_counts: dict[str, int]
    unique_output: Literal["unique_pairs.jsonl"] = "unique_pairs.jsonl"
    unique_output_sha256: str = Field(pattern=_HEX64)
    audit_only: Literal[True] = True
    reversible_cycles_are_novel: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _reconciles(self) -> DeterministicCompositionUniquePairManifest:
        expected = "detcomp_unique_pair_set:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "unique_pair_set_id")
        )
        if self.unique_pair_set_id != expected:
            raise ValueError("unique_pair_set_id does not match immutable payload")
        if self.unique_pair_count > self.gross_chain_count:
            raise ValueError("unique pair count exceeds gross chain count")
        if self.duplicate_excess_count != self.gross_chain_count - self.unique_pair_count:
            raise ValueError("duplicate excess does not reconcile")
        if self.duplicate_group_count > self.unique_pair_count:
            raise ValueError("duplicate group count exceeds unique pairs")
        counted_distributions = (
            self.gross_chain_kind_counts,
            self.unique_pair_chain_kind_membership_counts,
            self.gross_sequence_counts,
            self.unique_pair_sequence_membership_counts,
        )
        if any(count < 0 for counts in counted_distributions for count in counts.values()):
            raise ValueError("composition distribution counts cannot be negative")
        if sum(self.gross_chain_kind_counts.values()) != self.gross_chain_count:
            raise ValueError("gross chain-kind counts do not reconcile")
        if sum(self.gross_sequence_counts.values()) != self.gross_chain_count:
            raise ValueError("gross sequence counts do not reconcile")
        if (
            self.gross_source_alpha_return_count + self.gross_alpha_novel_count
            != self.gross_chain_count
        ):
            raise ValueError("gross alpha return/novel counts do not reconcile")
        if (
            self.unique_source_alpha_return_count + self.unique_alpha_novel_count
            != self.unique_pair_count
        ):
            raise ValueError("unique alpha return/novel counts do not reconcile")
        if self.gross_source_content_return_count > self.gross_chain_count:
            raise ValueError("gross source-content returns exceed gross chains")
        if self.unique_source_content_return_count > self.unique_pair_count:
            raise ValueError("unique source-content returns exceed unique pairs")
        membership_counts = (
            self.unique_pair_chain_kind_membership_counts,
            self.unique_pair_sequence_membership_counts,
        )
        if any(
            count > self.unique_pair_count
            for counts in membership_counts
            for count in counts.values()
        ):
            raise ValueError("unique-pair membership count exceeds unique pairs")
        return self


@dataclass(frozen=True, slots=True)
class CompositionUniquePairArtifacts:
    """Paths and counts returned by a new postprocess or exact replay."""

    output_dir: Path
    manifest_path: Path
    unique_pairs_path: Path
    unique_pair_set_id: str
    gross_chain_count: int
    unique_pair_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _ChainInventory:
    root: Path
    manifest: DeterministicCompositionChainManifest
    manifest_sha256: str
    chains: tuple[DeterministicCompositionChainRecord, ...]
    file_snapshot: tuple[tuple[str, str, int], ...]


def _chain_snapshot(chain_dir: Path) -> tuple[tuple[str, str, int], ...]:
    snapshot: list[tuple[str, str, int]] = []
    for name in sorted(_CHAIN_FILES):
        path = chain_dir / name
        if not path.is_file() or path.is_symlink():
            raise CompositionUniquePairError(
                f"composition chain input is not a regular file: {name}"
            )
        snapshot.append((name, hash_file(path), path.stat().st_size))
    return tuple(snapshot)


def _verify_chain_snapshot(inventory: _ChainInventory) -> None:
    if _chain_snapshot(inventory.root) != inventory.file_snapshot:
        raise CompositionUniquePairError("composition chain files changed during postprocessing")


def _load_chain_inventory(
    chain_dir: Path,
    *,
    seed_manifest_sha256: str,
    seed_manifest: CompositionSeedManifest,
) -> _ChainInventory:
    try:
        chain_dir = chain_dir.resolve(strict=True)
    except OSError as exc:
        raise CompositionUniquePairError(
            f"composition chain directory is unavailable: {exc}"
        ) from exc
    if not chain_dir.is_dir() or chain_dir.is_symlink():
        raise CompositionUniquePairError("composition chain input is not a regular directory")
    if {path.name for path in chain_dir.iterdir()} != _CHAIN_FILES:
        raise CompositionUniquePairError("composition chain directory is not exact")
    snapshot = _chain_snapshot(chain_dir)
    manifest_path = chain_dir / "manifest.json"
    try:
        raw_manifest = manifest_path.read_bytes()
        manifest = DeterministicCompositionChainManifest.model_validate_json(raw_manifest)
    except (OSError, ValueError) as exc:
        raise CompositionUniquePairError(f"invalid composition chain manifest: {exc}") from exc
    if raw_manifest != _canonical_line(manifest):
        raise CompositionUniquePairError("composition chain manifest is not canonical")

    if (
        manifest.input_seed_set_id != seed_manifest.seed_set_id
        or manifest.input_seed_manifest_sha256 != seed_manifest_sha256
        or manifest.input_seed_records_sha256 != seed_manifest.seed_output_sha256
        or manifest.input_seed_theorems_sha256 != seed_manifest.theorem_output_sha256
        or manifest.input_seed_representations_sha256 != seed_manifest.representation_output_sha256
        or manifest.input_seed_count != seed_manifest.seed_count
    ):
        raise CompositionUniquePairError("composition chain does not bind the exact seed set")

    chains_path = chain_dir / manifest.chain_output
    if (
        not chains_path.is_file()
        or chains_path.is_symlink()
        or hash_file(chains_path) != manifest.chain_output_sha256
    ):
        raise CompositionUniquePairError("composition chain partition differs from manifest")
    try:
        chains = _load_canonical_jsonl(chains_path, DeterministicCompositionChainRecord)
    except CompositionChainError as exc:
        raise CompositionUniquePairError(str(exc)) from exc
    if len(chains) != manifest.chain_count:
        raise CompositionUniquePairError("composition chain count differs from manifest")
    if len({item.chain_id for item in chains}) != len(chains):
        raise CompositionUniquePairError("composition chain IDs are duplicated")
    if dict(sorted(Counter(item.chain_kind for item in chains).items())) != (
        manifest.chain_kind_counts
    ):
        raise CompositionUniquePairError("composition chain-kind counts differ from manifest")
    if dict(sorted(Counter(item.second_hop_rule_id for item in chains).items())) != (
        manifest.second_hop_rule_counts
    ):
        raise CompositionUniquePairError("composition chain rule counts differ from manifest")
    inventory = _ChainInventory(
        root=chain_dir,
        manifest=manifest,
        manifest_sha256=hash_file(manifest_path),
        chains=chains,
        file_snapshot=snapshot,
    )
    _verify_chain_snapshot(inventory)
    return inventory


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CompositionUniquePairError(message)


def _verify_chain_seed_binding(
    chain: DeterministicCompositionChainRecord,
    seed: CompositionSeedRecord,
) -> None:
    # The seed-set binding is checked at manifest and per-chain level by the
    # caller.  Every remaining mechanical lineage field is checked here.
    expected: tuple[tuple[object, object, str], ...] = (
        (chain.seed_id, seed.seed_id, "seed ID"),
        (chain.context_id, seed.context_id, "context"),
        (chain.root_ancestry_ids, seed.root_ancestry_ids, "root ancestry"),
        (chain.original_source_theorem_id, seed.source_theorem_id, "source theorem"),
        (
            chain.original_source_representation_id,
            seed.source_representation_id,
            "source representation",
        ),
        (chain.intermediate_theorem_id, seed.intermediate_theorem_id, "intermediate theorem"),
        (
            chain.intermediate_representation_id,
            seed.intermediate_representation_id,
            "intermediate representation",
        ),
        (
            chain.first_hop_root_binding_id,
            seed.first_hop_root_binding_id,
            "first-hop root",
        ),
        (chain.first_hop_result_id, seed.first_hop_result_id, "first-hop result"),
        (chain.first_hop_rule_id, seed.first_hop_rule_id, "first-hop rule"),
        (chain.first_hop_attempt_id, seed.first_hop_attempt_id, "first-hop attempt"),
        (chain.first_hop_draft_id, seed.first_hop_draft_id, "first-hop draft"),
        (chain.first_hop_audit_id, seed.first_hop_audit_id, "first-hop audit"),
        (chain.first_hop_variant_id, seed.first_hop_variant_id, "first-hop variant"),
        (
            chain.first_hop_certificate_kind,
            seed.certificate_kind,
            "first-hop certificate kind",
        ),
        (
            chain.first_hop_certificate_sha256,
            seed.certificate_sha256,
            "first-hop certificate hash",
        ),
    )
    for actual, wanted, description in expected:
        _require(actual == wanted, f"composition chain {description} differs from seed")
    _require(chain.intention_only is True, "composition chain is not intention-only")
    _require(chain.semantic_label_id is None, "composition chain embeds a semantic label")
    _require(chain.resolved_label_count == 0, "composition chain carries resolved labels")
    _require(chain.promoted_item_count == 0, "composition chain carries promoted items")
    _require(chain.training_eligible is False, "composition chain is training eligible")
    _require(chain.evaluation_eligible is False, "composition chain is evaluation eligible")
    _require(chain.gate_credit is False, "composition chain carries gate credit")


def _chain_sequence(chain: DeterministicCompositionChainRecord) -> str:
    return f"{chain.first_hop_rule_id}->{chain.second_hop_rule_id}"


def _canonical_unique_key(chain: DeterministicCompositionChainRecord) -> str:
    return hash_canonical(
        {
            "schema": "deterministic_v2_composition_unique_pair_key_v1",
            "original_source_theorem_id": chain.original_source_theorem_id,
            "final_candidate_code_hash": chain.final_candidate_code_hash,
        }
    )


def _unique_pairs(
    *,
    seed_set_id: str,
    chain_set_id: str,
    chains: Sequence[DeterministicCompositionChainRecord],
    seeds_by_id: Mapping[str, CompositionSeedRecord],
) -> tuple[DeterministicCompositionUniquePairRecord, ...]:
    grouped: dict[str, list[tuple[DeterministicCompositionChainRecord, CompositionSeedRecord]]] = (
        defaultdict(list)
    )
    for chain in chains:
        if chain.seed_set_id != seed_set_id:
            raise CompositionUniquePairError("composition chain seed-set ID differs")
        seed = seeds_by_id.get(chain.seed_id)
        if seed is None:
            raise CompositionUniquePairError("composition chain references a foreign seed")
        _verify_chain_seed_binding(chain, seed)
        grouped[_canonical_unique_key(chain)].append((chain, seed))

    output: list[DeterministicCompositionUniquePairRecord] = []
    for key, members in sorted(grouped.items()):
        first_chain, first_seed = members[0]
        invariants = (
            "context_id",
            "root_ancestry_ids",
            "original_source_theorem_id",
            "original_source_representation_id",
            "final_candidate_code_hash",
            "final_alpha_identity_fingerprint",
        )
        for chain, seed in members[1:]:
            if any(getattr(chain, name) != getattr(first_chain, name) for name in invariants):
                raise CompositionUniquePairError("canonical unique pair key collision detected")
            if (
                seed.source_statement_content_hash != first_seed.source_statement_content_hash
                or seed.source_alpha_identity_fingerprint
                != first_seed.source_alpha_identity_fingerprint
            ):
                raise CompositionUniquePairError("canonical unique pair source identity differs")
        data: dict[str, object] = {
            "canonical_unique_key": key,
            "input_seed_set_id": seed_set_id,
            "input_chain_set_id": chain_set_id,
            "context_id": first_chain.context_id,
            "root_ancestry_ids": first_chain.root_ancestry_ids,
            "original_source_theorem_id": first_chain.original_source_theorem_id,
            "original_source_representation_id": first_chain.original_source_representation_id,
            "source_statement_content_hash": first_seed.source_statement_content_hash,
            "source_alpha_identity_fingerprint": first_seed.source_alpha_identity_fingerprint,
            "intermediate_theorem_ids": tuple(
                sorted({chain.intermediate_theorem_id for chain, _ in members})
            ),
            "intermediate_representation_ids": tuple(
                sorted({chain.intermediate_representation_id for chain, _ in members})
            ),
            "final_theorem_ids": tuple(sorted({chain.final_theorem_id for chain, _ in members})),
            "final_representation_ids": tuple(
                sorted({chain.final_representation_id for chain, _ in members})
            ),
            "final_candidate_code_hash": first_chain.final_candidate_code_hash,
            "final_alpha_identity_fingerprint": first_chain.final_alpha_identity_fingerprint,
            "chain_ids": tuple(sorted(chain.chain_id for chain, _ in members)),
            "chain_sequences": tuple(sorted({_chain_sequence(chain) for chain, _ in members})),
            "chain_kinds": tuple(sorted({chain.chain_kind for chain, _ in members})),
            "gross_chain_count": len(members),
            "duplicate_excess_count": len(members) - 1,
            "source_content_return": (
                first_seed.source_statement_content_hash == first_chain.final_candidate_code_hash
            ),
            "source_alpha_return": (
                first_seed.source_alpha_identity_fingerprint
                == first_chain.final_alpha_identity_fingerprint
            ),
            "alpha_novel": (
                first_seed.source_alpha_identity_fingerprint
                != first_chain.final_alpha_identity_fingerprint
            ),
        }
        output.append(
            DeterministicCompositionUniquePairRecord.model_validate(
                {"unique_pair_id": f"detcomp_unique_pair:{key}", **data}
            )
        )
    return tuple(output)


def _verify_existing(output_dir: Path, payloads: Mapping[str, bytes]) -> None:
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise CompositionUniquePairError("existing unique-pair output is not a regular directory")
    if {path.name for path in output_dir.iterdir()} != _OUTPUT_FILES:
        raise CompositionUniquePairError("existing unique-pair output is not an exact replay")
    for name, payload in payloads.items():
        path = output_dir / name
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise CompositionUniquePairError(f"existing unique-pair output differs: {path}")


def postprocess_deterministic_v2_composition_unique_pairs(
    *,
    seed_dir: Path,
    chain_dir: Path,
    output_dir: Path,
) -> CompositionUniquePairArtifacts:
    """Revalidate exact chain-v1 inputs and emit immutable unique pairs."""

    try:
        seed_inventory = _load_seed_inventory(seed_dir)
    except CompositionChainError as exc:
        raise CompositionUniquePairError(str(exc)) from exc
    chain_inventory = _load_chain_inventory(
        chain_dir,
        seed_manifest_sha256=seed_inventory.manifest_sha256,
        seed_manifest=seed_inventory.manifest,
    )
    seeds_by_id = {item.seed_id: item for item in seed_inventory.seeds}
    if len(seeds_by_id) != len(seed_inventory.seeds):
        raise CompositionUniquePairError("composition seed IDs are duplicated")
    unique_pairs = _unique_pairs(
        seed_set_id=seed_inventory.manifest.seed_set_id,
        chain_set_id=chain_inventory.manifest.chain_set_id,
        chains=chain_inventory.chains,
        seeds_by_id=seeds_by_id,
    )

    unique_payload = _canonical_jsonl(unique_pairs)
    gross_sequence_counts = Counter(_chain_sequence(item) for item in chain_inventory.chains)
    gross_kind_counts = Counter(item.chain_kind for item in chain_inventory.chains)
    unique_kind_membership = Counter(kind for item in unique_pairs for kind in item.chain_kinds)
    unique_sequence_membership = Counter(
        sequence for item in unique_pairs for sequence in item.chain_sequences
    )
    manifest_data: dict[str, object] = {
        "input_seed_set_id": seed_inventory.manifest.seed_set_id,
        "input_seed_manifest_sha256": seed_inventory.manifest_sha256,
        "input_seed_records_sha256": seed_inventory.manifest.seed_output_sha256,
        "input_seed_theorems_sha256": seed_inventory.manifest.theorem_output_sha256,
        "input_seed_representations_sha256": (seed_inventory.manifest.representation_output_sha256),
        "input_chain_set_id": chain_inventory.manifest.chain_set_id,
        "input_chain_manifest_sha256": chain_inventory.manifest_sha256,
        "input_chain_records_sha256": chain_inventory.manifest.chain_output_sha256,
        "gross_chain_count": len(chain_inventory.chains),
        "unique_pair_count": len(unique_pairs),
        "duplicate_group_count": sum(item.gross_chain_count > 1 for item in unique_pairs),
        "duplicate_excess_count": len(chain_inventory.chains) - len(unique_pairs),
        "gross_source_content_return_count": sum(
            seeds_by_id[item.seed_id].source_statement_content_hash
            == item.final_candidate_code_hash
            for item in chain_inventory.chains
        ),
        "unique_source_content_return_count": sum(
            item.source_content_return for item in unique_pairs
        ),
        "gross_source_alpha_return_count": sum(
            seeds_by_id[item.seed_id].source_alpha_identity_fingerprint
            == item.final_alpha_identity_fingerprint
            for item in chain_inventory.chains
        ),
        "unique_source_alpha_return_count": sum(item.source_alpha_return for item in unique_pairs),
        "gross_alpha_novel_count": sum(
            seeds_by_id[item.seed_id].source_alpha_identity_fingerprint
            != item.final_alpha_identity_fingerprint
            for item in chain_inventory.chains
        ),
        "unique_alpha_novel_count": sum(item.alpha_novel for item in unique_pairs),
        "gross_chain_kind_counts": dict(sorted(gross_kind_counts.items())),
        "unique_pair_chain_kind_membership_counts": dict(sorted(unique_kind_membership.items())),
        "gross_sequence_counts": dict(sorted(gross_sequence_counts.items())),
        "unique_pair_sequence_membership_counts": dict(sorted(unique_sequence_membership.items())),
        "unique_output_sha256": hashlib.sha256(unique_payload).hexdigest(),
    }
    placeholder = DeterministicCompositionUniquePairManifest.model_construct(
        _fields_set=None,
        unique_pair_set_id=f"detcomp_unique_pair_set:{'0' * 64}",
        **manifest_data,
    )
    unique_pair_set_id = "detcomp_unique_pair_set:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "unique_pair_set_id")
    )
    manifest = DeterministicCompositionUniquePairManifest.model_validate(
        {"unique_pair_set_id": unique_pair_set_id, **manifest_data}
    )
    payloads = {
        "unique_pairs.jsonl": unique_payload,
        "manifest.json": _canonical_line(manifest),
    }

    _verify_seed_snapshot(seed_inventory)
    _verify_chain_snapshot(chain_inventory)
    output_dir = output_dir.resolve(strict=False)
    input_roots = (seed_inventory.root, chain_inventory.root)
    if any(output_dir == root or output_dir.is_relative_to(root) for root in input_roots):
        raise CompositionUniquePairError("unique-pair output cannot be inside an input")
    if output_dir.exists():
        _verify_existing(output_dir, payloads)
        replayed = True
    else:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
        try:
            for name, payload in payloads.items():
                _write_new_file(temporary / name, payload)
            try:
                os.rename(temporary, output_dir)
            except FileExistsError:
                _verify_existing(output_dir, payloads)
                replayed = True
            else:
                replayed = False
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return CompositionUniquePairArtifacts(
        output_dir=output_dir,
        manifest_path=output_dir / "manifest.json",
        unique_pairs_path=output_dir / "unique_pairs.jsonl",
        unique_pair_set_id=unique_pair_set_id,
        gross_chain_count=len(chain_inventory.chains),
        unique_pair_count=len(unique_pairs),
        replayed=replayed,
    )


__all__ = [
    "CompositionUniquePairArtifacts",
    "CompositionUniquePairError",
    "DeterministicCompositionUniquePairManifest",
    "DeterministicCompositionUniquePairRecord",
    "postprocess_deterministic_v2_composition_unique_pairs",
]
