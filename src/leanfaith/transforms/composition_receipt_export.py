"""Receipt-bound provisional export for complete deterministic depth-two runs.

The exporter is intentionally downstream of the immutable chain and unique-pair
audits.  It admits exactly the thirteen P14--P18/N11--N18 second-hop roots,
retains alpha-return cycles for audit, quarantines mixed mechanical intentions,
and exposes only alpha-novel, intention-consistent pairs in a *provisional*,
non-training inventory.  It creates no semantic labels or promotion credit.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import (
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
    audit_deterministic_v2_composition_chains,
)
from leanfaith.transforms.composition_full_launcher import (
    CompositionFullLaunchSpec,
    CompositionFullReceipt,
)
from leanfaith.transforms.composition_seed import CompositionSeedManifest
from leanfaith.transforms.composition_smoke_launcher import FAMILY_DEFINITIONS
from leanfaith.transforms.composition_unique_pairs import (
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
    postprocess_deterministic_v2_composition_unique_pairs,
)
from leanfaith.transforms.provisional_pair_combine import (
    ProvisionalPairCombineError,
    _load_root,
    _root_tree,
)
from leanfaith.transforms.v2_d0_materializer import V2D0MaterializationResult
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult

_HEX64 = r"^[0-9a-f]{64}$"
_EXPECTED_FAMILIES = frozenset({f"p{i}" for i in range(14, 19)}) | frozenset(
    {f"n{i}" for i in range(11, 19)}
)
_P_RULES = (
    "p14_independent_binder_permutation",
    "p15_root_iff_reversal",
    "p16_conjunction_reassociation",
    "p17_hypothesis_packing",
    "p18_root_equality_symmetry",
)
_N_RULES = (
    "n11_bound_variable_substitution",
    "n12_implication_converse",
    "n13_witness_dependency",
    "n14_negation_scope",
    "n15_conjunct_omission",
    "n16_domain_guard_removal",
    "n17_role_sensitive_arguments",
    "n18_root_equality_polarity",
)
_SECOND_RULES = _P_RULES + _N_RULES
_EXPECTED_SEQUENCES = tuple(f"{first}->{second}" for first in _P_RULES for second in _SECOND_RULES)
_OUTPUT_FILES = frozenset(
    {"inventory.jsonl", "cycles.jsonl", "quarantine.jsonl", "manifest.json", "report.md"}
)


class CompositionReceiptExportError(ValueError):
    """A receipt, audited input, text join, or immutable replay failed closed."""


def _without_id(payload: Mapping[str, object], field: str) -> dict[str, object]:
    output = dict(payload)
    output.pop(field, None)
    return output


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(item) for item in records)


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute normalized locator without dereferencing symlinks."""

    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_components(path: Path, *, allow_missing: bool) -> Path:
    """Reject a symlink anywhere in an input or output locator.

    ``Path.resolve`` cannot be used for this boundary because it erases the fact
    that a caller supplied a symlink.  For prospective output paths, checking
    stops at the first absent component and is repeated after parent creation.
    """

    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise CompositionReceiptExportError(f"required path is absent: {current}") from None
        except OSError as exc:
            raise CompositionReceiptExportError(
                f"cannot inspect path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CompositionReceiptExportError(f"path contains a symlink: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise CompositionReceiptExportError(
                f"path parent component is not a directory: {current}"
            )
    return absolute


def _require_regular_file(path: Path) -> Path:
    safe = _reject_symlink_components(path, allow_missing=False)
    if not safe.is_file():
        raise CompositionReceiptExportError(f"required input is not a regular file: {safe}")
    return safe


def _require_real_directory(path: Path) -> Path:
    safe = _reject_symlink_components(path, allow_missing=False)
    if not safe.is_dir():
        raise CompositionReceiptExportError(f"required input is not a real directory: {safe}")
    return safe


class DeterministicCompositionExportRecord(StrictModel):
    """One source/final Lean pair, still provisional and intention-only."""

    schema_version: Literal[1] = 1
    export_record_id: str = Field(pattern=r"^detcomp_export:[0-9a-f]{64}$")
    disposition: Literal["provisional_inventory", "cycle_audit", "mixed_intention_quarantine"]
    input_unique_pair_id: str = Field(pattern=r"^detcomp_unique_pair:[0-9a-f]{64}$")
    original_source_theorem_id: str
    original_source_representation_id: str
    source_dataset: str = Field(min_length=1)
    private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    source_lean: str = Field(min_length=1)
    source_statement_content_hash: str = Field(pattern=_HEX64)
    source_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    final_theorem_ids: tuple[str, ...] = Field(min_length=1)
    final_representation_ids: tuple[str, ...] = Field(min_length=1)
    final_lean: str = Field(min_length=1)
    final_candidate_code_hash: str = Field(pattern=_HEX64)
    final_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    chain_ids: tuple[str, ...] = Field(min_length=1)
    chain_sequences: tuple[str, ...] = Field(min_length=1)
    chain_kinds: tuple[Literal["P_to_P", "P_to_N"], ...] = Field(min_length=1)
    mechanical_intention: Literal[
        "equivalent_candidate", "near_miss_candidate", "conflicting_intentions"
    ]
    alpha_novel: bool
    source_alpha_return: bool
    mixed_intention_collision: bool
    quality_tier: Literal["provisional"] = "provisional"
    intention_only: Literal[True] = True
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> DeterministicCompositionExportRecord:
        expected = "detcomp_export:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "export_record_id")
        )
        if self.export_record_id != expected:
            raise ValueError("export record ID does not match its immutable payload")
        for name in (
            "final_theorem_ids",
            "final_representation_ids",
            "chain_ids",
            "chain_sequences",
            "chain_kinds",
        ):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
        mixed = set(self.chain_kinds) == {"P_to_N", "P_to_P"}
        if self.mixed_intention_collision != mixed:
            raise ValueError("mixed-intention collision flag does not reconcile")
        if self.alpha_novel == self.source_alpha_return:
            raise ValueError("alpha novelty and source return must be complements")
        expected_disposition = (
            "mixed_intention_quarantine"
            if mixed
            else "provisional_inventory"
            if self.alpha_novel
            else "cycle_audit"
        )
        if self.disposition != expected_disposition:
            raise ValueError("export disposition does not reconcile")
        expected_intention = (
            "conflicting_intentions"
            if mixed
            else "equivalent_candidate"
            if self.chain_kinds == ("P_to_P",)
            else "near_miss_candidate"
        )
        if self.mechanical_intention != expected_intention:
            raise ValueError("mechanical intention does not reconcile")
        if not set(self.chain_sequences).issubset(_EXPECTED_SEQUENCES):
            raise ValueError("export record contains a sequence outside the 65-sequence matrix")
        expected_kinds = tuple(
            sorted(
                {
                    "P_to_P" if sequence.split("->", 1)[1].startswith("p") else "P_to_N"
                    for sequence in self.chain_sequences
                }
            )
        )
        if self.chain_kinds != expected_kinds:
            raise ValueError("chain kinds do not match chain sequences")
        expected_policy = _source_policy(self.source_dataset)
        observed_policy = (
            self.private_source_content,
            self.redistribution_allowed,
            self.external_transmission_allowed,
            self.release_eligible,
        )
        if observed_policy != expected_policy:
            raise ValueError("source privacy flags do not match the registered dataset policy")
        return self


class DeterministicCompositionReceiptExportManifest(StrictModel):
    """Self-authenticating manifest for one complete receipt-bound export."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_composition_receipt_export"] = (
        "deterministic_v2_composition_receipt_export"
    )
    export_set_id: str = Field(pattern=r"^detcomp_export_set:[0-9a-f]{64}$")
    full_receipt_id: str = Field(pattern=r"^detcomp_full_receipt:[0-9a-f]{64}$")
    full_receipt_sha256: str = Field(pattern=_HEX64)
    full_launch_id: str = Field(pattern=r"^detcomp_full_launch:[0-9a-f]{64}$")
    full_launch_spec_sha256: str = Field(pattern=_HEX64)
    input_seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    input_seed_manifest_sha256: str = Field(pattern=_HEX64)
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    input_chain_manifest_sha256: str = Field(pattern=_HEX64)
    input_unique_pair_set_id: str = Field(pattern=r"^detcomp_unique_pair_set:[0-9a-f]{64}$")
    input_unique_pair_manifest_sha256: str = Field(pattern=_HEX64)
    source_theorem_partition_sha256s: tuple[str, ...] = Field(min_length=1)
    source_representation_partition_sha256s: tuple[str, ...] = Field(min_length=1)
    source_datasets: tuple[str, ...] = ()
    contains_private_source_content: bool
    contains_mixed_source_privacy: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    required_families: tuple[str, ...]
    required_sequence_count: Literal[65] = 65
    gross_chain_count: int = Field(ge=0)
    unique_pair_count: int = Field(ge=0)
    provisional_inventory_count: int = Field(ge=0)
    cycle_audit_count: int = Field(ge=0)
    mixed_intention_quarantine_count: int = Field(ge=0)
    sequence_counts: dict[str, int]
    sequence_inventory_counts: dict[str, int]
    sequence_cycle_counts: dict[str, int]
    sequence_quarantine_counts: dict[str, int]
    inventory_sha256: str = Field(pattern=_HEX64)
    cycles_sha256: str = Field(pattern=_HEX64)
    quarantine_sha256: str = Field(pattern=_HEX64)
    report_sha256: str = Field(pattern=_HEX64)
    quality_tier: Literal["provisional"] = "provisional"
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _reconciles(self) -> DeterministicCompositionReceiptExportManifest:
        expected = "detcomp_export_set:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "export_set_id")
        )
        if self.export_set_id != expected:
            raise ValueError("export set ID does not match its immutable payload")
        if set(self.required_families) != _EXPECTED_FAMILIES or len(self.required_families) != 13:
            raise ValueError("export manifest does not bind exactly thirteen required families")
        expected_sequences = set(_EXPECTED_SEQUENCES)
        for counts in (
            self.sequence_counts,
            self.sequence_inventory_counts,
            self.sequence_cycle_counts,
            self.sequence_quarantine_counts,
        ):
            if set(counts) != expected_sequences or any(value < 0 for value in counts.values()):
                raise ValueError("sequence table must contain exactly all 65 nonnegative rows")
        if sum(self.sequence_counts.values()) != self.gross_chain_count:
            raise ValueError("gross sequence counts do not reconcile")
        if self.unique_pair_count > self.gross_chain_count:
            raise ValueError("unique-pair count exceeds gross chain count")
        if (
            self.provisional_inventory_count
            + self.cycle_audit_count
            + self.mixed_intention_quarantine_count
            != self.unique_pair_count
        ):
            raise ValueError("mutually exclusive export partitions do not reconcile")
        for sequence in _EXPECTED_SEQUENCES:
            partition_memberships = (
                self.sequence_inventory_counts[sequence]
                + self.sequence_cycle_counts[sequence]
                + self.sequence_quarantine_counts[sequence]
            )
            if partition_memberships > self.sequence_counts[sequence]:
                raise ValueError("unique sequence memberships exceed gross sequence chains")
            if (
                self.sequence_inventory_counts[sequence] > self.provisional_inventory_count
                or self.sequence_cycle_counts[sequence] > self.cycle_audit_count
                or self.sequence_quarantine_counts[sequence] > self.mixed_intention_quarantine_count
            ):
                raise ValueError("sequence partition count exceeds its record partition")
        for field_name in (
            "source_theorem_partition_sha256s",
            "source_representation_partition_sha256s",
            "source_datasets",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        source_policies = tuple(_source_policy(source) for source in self.source_datasets)
        private_values = {policy[0] for policy in source_policies}
        expected_private = any(private for private, *_ in source_policies)
        expected_mixed = len(private_values) > 1
        expected_redistribution = all(policy[1] for policy in source_policies)
        expected_external = all(policy[2] for policy in source_policies)
        expected_release = all(policy[3] for policy in source_policies)
        if (
            self.contains_private_source_content != expected_private
            or self.contains_mixed_source_privacy != expected_mixed
            or self.redistribution_allowed != expected_redistribution
            or self.external_transmission_allowed != expected_external
            or self.release_eligible != expected_release
        ):
            raise ValueError("manifest privacy flags do not match registered source policies")
        return self


@dataclass(frozen=True, slots=True)
class CompositionReceiptExportArtifacts:
    output_dir: Path
    manifest_path: Path
    inventory_path: Path
    cycles_path: Path
    quarantine_path: Path
    report_path: Path
    export_set_id: str
    provisional_inventory_count: int
    cycle_audit_count: int
    mixed_intention_quarantine_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _FullBinding:
    spec: CompositionFullLaunchSpec
    receipt: CompositionFullReceipt
    spec_path: Path
    receipt_path: Path
    root_paths: tuple[Path, ...]


def _load_canonical(path: Path, model: type[StrictModel]) -> StrictModel:
    safe_path = _require_regular_file(path)
    raw = safe_path.read_bytes()
    try:
        item = model.model_validate_json(raw)
    except ValueError as exc:
        raise CompositionReceiptExportError(f"invalid {model.__name__}: {exc}") from exc
    if raw != _canonical_line(item):
        raise CompositionReceiptExportError(f"non-canonical {model.__name__}: {safe_path}")
    return item


def _load_canonical_jsonl(path: Path, model: type[StrictModel]) -> tuple[StrictModel, ...]:
    safe_path = _require_regular_file(path)
    output: list[StrictModel] = []
    for line_number, raw in enumerate(safe_path.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n") or not raw.strip():
            raise CompositionReceiptExportError(
                f"invalid JSONL framing at {safe_path}:{line_number}"
            )
        try:
            item = model.model_validate_json(raw)
        except ValueError as exc:
            raise CompositionReceiptExportError(
                f"invalid {model.__name__} at {safe_path}:{line_number}: {exc}"
            ) from exc
        if raw != _canonical_line(item):
            raise CompositionReceiptExportError(
                f"non-canonical {model.__name__} at {safe_path}:{line_number}"
            )
        output.append(item)
    return tuple(output)


def _validate_family_coverage(
    spec: CompositionFullLaunchSpec, receipt: CompositionFullReceipt
) -> None:
    expected_order = tuple(item.key for item in FAMILY_DEFINITIONS)
    if (
        tuple(item.family for item in spec.families) != expected_order
        or tuple(item.family for item in receipt.roots) != expected_order
        or set(expected_order) != _EXPECTED_FAMILIES
        or len(receipt.roots) != 13
    ):
        raise CompositionReceiptExportError("full receipt must contain exactly P14-P18 and N11-N18")


def _verify_full_binding(full_run_root: Path, seed_dir: Path) -> _FullBinding:
    root = _require_real_directory(full_run_root)
    orchestration = root / "orchestration"
    spec_path = orchestration / "launch_spec.json"
    receipt_path = orchestration / "receipt.json"
    status_path = orchestration / "status.json"
    spec = _load_canonical(spec_path, CompositionFullLaunchSpec)
    receipt = _load_canonical(receipt_path, CompositionFullReceipt)
    assert isinstance(spec, CompositionFullLaunchSpec)
    assert isinstance(receipt, CompositionFullReceipt)
    if receipt.launch_id != spec.launch_id or receipt.launch_spec_sha256 != hash_file(spec_path):
        raise CompositionReceiptExportError("full receipt does not bind the launch spec")
    status_path = _require_regular_file(status_path)
    if receipt.final_status_sha256 != hash_file(status_path):
        raise CompositionReceiptExportError("full receipt does not bind final status")
    _validate_family_coverage(spec, receipt)
    resolved_seed = _require_real_directory(seed_dir)
    if _require_real_directory(Path(spec.seed_dir)) != resolved_seed:
        raise CompositionReceiptExportError("full launch seed directory differs")
    seed_manifest = _load_canonical(resolved_seed / "manifest.json", CompositionSeedManifest)
    assert isinstance(seed_manifest, CompositionSeedManifest)
    if (
        spec.seed_manifest_sha256 != hash_file(resolved_seed / "manifest.json")
        or spec.seed_set_id != seed_manifest.seed_set_id
        or spec.seed_partition_sha256 != seed_manifest.seed_output_sha256
        or spec.theorem_partition_sha256 != seed_manifest.theorem_output_sha256
        or spec.representation_partition_sha256 != seed_manifest.representation_output_sha256
        or hash_file(_require_regular_file(resolved_seed / seed_manifest.seed_output))
        != seed_manifest.seed_output_sha256
        or hash_file(_require_regular_file(resolved_seed / seed_manifest.theorem_output))
        != seed_manifest.theorem_output_sha256
        or hash_file(_require_regular_file(resolved_seed / seed_manifest.representation_output))
        != seed_manifest.representation_output_sha256
    ):
        raise CompositionReceiptExportError("full launch does not bind the exact seed set")
    root_paths: list[Path] = []
    seen_bindings: set[str] = set()
    for plan, bound in zip(spec.families, receipt.roots, strict=True):
        path = _require_real_directory(Path(bound.root_path))
        if (
            plan.family != bound.family
            or plan.run_kind != bound.run_kind
            or plan.profile_id != bound.profile_id
            or _require_real_directory(Path(plan.output_root)) != path
            or hash_file(_require_regular_file(path / "run_spec.json")) != bound.run_spec_sha256
            or hash_file(_require_regular_file(path / "manifest.json")) != bound.manifest_sha256
            or hash_file(_require_regular_file(path / "results.jsonl")) != bound.results_sha256
            or hash_file(_require_regular_file(orchestration / "logs" / f"{bound.family}.log"))
            != bound.log_sha256
        ):
            raise CompositionReceiptExportError(f"receipt root binding differs for {bound.family}")
        try:
            loaded = _load_root(path)
        except (OSError, ProvisionalPairCombineError) as exc:
            raise CompositionReceiptExportError(f"receipt root failed replay: {exc}") from exc
        if (
            loaded.binding.root_binding_id != bound.root_binding_id
            or loaded.binding.root_tree_hash != bound.root_tree_hash
            or loaded.binding.run_kind != bound.run_kind
            or loaded.binding.profile_id != bound.profile_id
            or loaded.binding.provisional_count != bound.provisional_count
        ):
            raise CompositionReceiptExportError(f"receipt root content differs for {bound.family}")
        if bound.root_binding_id in seen_bindings:
            raise CompositionReceiptExportError("full receipt repeats a root binding")
        seen_bindings.add(bound.root_binding_id)
        root_paths.append(path)
    return _FullBinding(spec, receipt, spec_path, receipt_path, tuple(root_paths))


def _load_source_inventory(
    theorem_paths: Sequence[Path], representation_paths: Sequence[Path]
) -> tuple[dict[str, TheoremRecord], dict[str, RepresentationRecord]]:
    if not theorem_paths or not representation_paths:
        raise CompositionReceiptExportError(
            "at least one source partition of each kind is required"
        )
    theorems = tuple(
        item
        for path in theorem_paths
        for item in _load_source_partition(path, TheoremRecord, wrapper_key="theorem")
    )
    representations = tuple(
        item
        for path in representation_paths
        for item in _load_source_partition(path, RepresentationRecord, wrapper_key="representation")
    )
    by_theorem = {item.theorem_id: item for item in theorems}
    by_representation = {item.representation_id: item for item in representations}
    if len(by_theorem) != len(theorems) or len(by_representation) != len(representations):
        raise CompositionReceiptExportError("source inventory contains duplicate identities")
    return by_theorem, by_representation


def _load_source_partition[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
    *,
    wrapper_key: str,
) -> tuple[ModelT, ...]:
    """Load an existing direct or extraction-wrapper source partition.

    These upstream partitions are content-hash bound but are not all encoded as
    canonical standalone records: extraction theorem files contain
    ``{"theorem": ..., "representation": ...}`` rows.  The exporter validates
    the selected nested model and binds the unchanged full file hash.
    """

    safe_path = _require_regular_file(path)
    output: list[ModelT] = []
    for line_number, raw in enumerate(safe_path.read_bytes().splitlines(keepends=True), start=1):
        if not raw.endswith(b"\n") or not raw.strip():
            raise CompositionReceiptExportError(
                f"invalid source JSONL framing at {safe_path}:{line_number}"
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompositionReceiptExportError(
                f"invalid source JSON at {safe_path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CompositionReceiptExportError(
                f"source row is not an object at {safe_path}:{line_number}"
            )
        selected = payload.get(wrapper_key, payload)
        if not isinstance(selected, dict):
            raise CompositionReceiptExportError(
                f"source wrapper is invalid at {safe_path}:{line_number}"
            )
        try:
            output.append(model.model_validate(selected))
        except ValueError as exc:
            raise CompositionReceiptExportError(
                f"invalid {model.__name__} at {safe_path}:{line_number}: {exc}"
            ) from exc
    return tuple(output)


def _source_policy(source: str) -> tuple[bool, bool, bool, bool]:
    normalized = source.casefold()
    if "sft_classic" in normalized or "formalmathatepfl/sft_classic" in normalized:
        return True, False, False, False
    if "mathlib" in normalized:
        return False, True, True, True
    raise CompositionReceiptExportError(
        f"source privacy policy is not registered for dataset {source!r}"
    )


def _join_source(
    pair: DeterministicCompositionUniquePairRecord,
    theorem_by_id: Mapping[str, TheoremRecord],
    representation_by_id: Mapping[str, RepresentationRecord],
) -> tuple[str, str, bool, bool, bool, bool]:
    theorem = theorem_by_id.get(pair.original_source_theorem_id)
    representation = representation_by_id.get(pair.original_source_representation_id)
    if theorem is None or representation is None:
        raise CompositionReceiptExportError("source inventory lacks a receipt-bound pair member")
    if (
        representation.theorem_id != theorem.theorem_id
        or theorem.statement_content_hash != pair.source_statement_content_hash
        or theorem.context_id != pair.context_id
        or theorem.root_ancestry_ids != pair.root_ancestry_ids
        or representation.context_id != pair.context_id
        or representation.alpha_identity_fingerprint != pair.source_alpha_identity_fingerprint
        or representation.raw_proof_stripped != theorem.proof_stripped_declaration
    ):
        raise CompositionReceiptExportError("source Lean join differs from bound seed identity")
    private, redistribution, external, release = _source_policy(theorem.source)
    return (
        theorem.proof_stripped_declaration,
        theorem.source,
        private,
        redistribution,
        external,
        release,
    )


def _join_final_texts(
    chains: Sequence[DeterministicCompositionChainRecord],
    roots_by_binding: Mapping[str, tuple[Path, str]],
) -> dict[str, str]:
    requested: dict[str, set[int]] = defaultdict(set)
    for chain in chains:
        if chain.second_hop_root_binding_id not in roots_by_binding:
            raise CompositionReceiptExportError("chain references a root outside the full receipt")
        requested[chain.second_hop_root_binding_id].add(chain.second_hop_result_line_number)

    results_by_line: dict[
        tuple[str, int], V2E2MaterializationResult | V2D0MaterializationResult
    ] = {}
    for root_binding_id, line_numbers in requested.items():
        root, run_kind = roots_by_binding[root_binding_id]
        model = V2E2MaterializationResult if run_kind == "e2" else V2D0MaterializationResult
        remaining = set(line_numbers)
        results_path = _require_regular_file(root / "results.jsonl")
        with results_path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if line_number not in remaining:
                    continue
                try:
                    result = model.model_validate_json(raw)
                except ValueError as exc:
                    raise CompositionReceiptExportError(f"invalid final result: {exc}") from exc
                if raw != _canonical_line(result):
                    raise CompositionReceiptExportError("final result is not canonical")
                results_by_line[(root_binding_id, line_number)] = result
                remaining.remove(line_number)
                if not remaining:
                    break
        if remaining:
            raise CompositionReceiptExportError("chain result line is absent from receipt root")

    output: dict[str, str] = {}
    for chain in chains:
        cache_key = (chain.second_hop_root_binding_id, chain.second_hop_result_line_number)
        result = results_by_line[cache_key]
        if (
            result.result_id != chain.second_hop_result_id
            or result.profile_id != chain.second_hop_profile_id
            or result.rule_id != chain.second_hop_rule_id
            or result.terminal_status != "provisional_variant"
            or result.draft is None
            or result.candidate_theorem is None
            or result.candidate_representation is None
            or result.audit is None
            or result.variant is None
            or result.attempt.attempt_id != chain.second_hop_attempt_id
            or result.attempt.source_theorem_ids != (chain.intermediate_theorem_id,)
            or result.attempt.source_representation_ids != (chain.intermediate_representation_id,)
            or result.draft.draft_id != chain.second_hop_draft_id
            or result.draft.source_theorem_ids != (chain.intermediate_theorem_id,)
            or result.draft.source_representation_ids != (chain.intermediate_representation_id,)
            or result.audit.audit_id != chain.second_hop_audit_id
            or result.variant.variant_id != chain.second_hop_variant_id
            or result.draft.candidate_code_hash != chain.final_candidate_code_hash
            or result.candidate_theorem.theorem_id != chain.final_theorem_id
            or result.candidate_representation.representation_id != chain.final_representation_id
            or result.candidate_representation.theorem_id != result.candidate_theorem.theorem_id
            or result.candidate_theorem.parent_theorem_ids != (chain.intermediate_theorem_id,)
            or result.candidate_theorem.context_id != chain.context_id
            or result.candidate_representation.context_id != chain.context_id
            or result.candidate_theorem.root_ancestry_ids != chain.root_ancestry_ids
            or result.candidate_representation.alpha_identity_fingerprint
            != chain.final_alpha_identity_fingerprint
            or result.candidate_theorem.statement_content_hash != chain.final_candidate_code_hash
            or result.candidate_theorem.proof_stripped_declaration != result.draft.candidate_code
        ):
            raise CompositionReceiptExportError("final Lean join differs from chain receipt")
        prior = output.setdefault(chain.chain_id, result.draft.candidate_code)
        if prior != result.draft.candidate_code:
            raise CompositionReceiptExportError("one chain identity maps to different final Lean")
    return output


def _export_record(
    pair: DeterministicCompositionUniquePairRecord,
    *,
    source_lean: str,
    source_dataset: str,
    private_source_content: bool,
    redistribution_allowed: bool,
    external_transmission_allowed: bool,
    release_eligible: bool,
    final_lean: str,
) -> DeterministicCompositionExportRecord:
    mixed = set(pair.chain_kinds) == {"P_to_N", "P_to_P"}
    disposition = (
        "mixed_intention_quarantine"
        if mixed
        else "provisional_inventory"
        if pair.alpha_novel
        else "cycle_audit"
    )
    intention = (
        "conflicting_intentions"
        if mixed
        else "equivalent_candidate"
        if pair.chain_kinds == ("P_to_P",)
        else "near_miss_candidate"
    )
    payload: dict[str, object] = {
        "export_record_id": f"detcomp_export:{'0' * 64}",
        "disposition": disposition,
        "input_unique_pair_id": pair.unique_pair_id,
        "original_source_theorem_id": pair.original_source_theorem_id,
        "original_source_representation_id": pair.original_source_representation_id,
        "source_dataset": source_dataset,
        "private_source_content": private_source_content,
        "redistribution_allowed": redistribution_allowed,
        "external_transmission_allowed": external_transmission_allowed,
        "release_eligible": release_eligible,
        "source_lean": source_lean,
        "source_statement_content_hash": pair.source_statement_content_hash,
        "source_alpha_identity_fingerprint": pair.source_alpha_identity_fingerprint,
        "final_theorem_ids": pair.final_theorem_ids,
        "final_representation_ids": pair.final_representation_ids,
        "final_lean": final_lean,
        "final_candidate_code_hash": pair.final_candidate_code_hash,
        "final_alpha_identity_fingerprint": pair.final_alpha_identity_fingerprint,
        "chain_ids": pair.chain_ids,
        "chain_sequences": pair.chain_sequences,
        "chain_kinds": pair.chain_kinds,
        "mechanical_intention": intention,
        "alpha_novel": pair.alpha_novel,
        "source_alpha_return": pair.source_alpha_return,
        "mixed_intention_collision": mixed,
    }
    placeholder = DeterministicCompositionExportRecord.model_construct(_fields_set=None, **payload)
    payload["export_record_id"] = "detcomp_export:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "export_record_id")
    )
    return DeterministicCompositionExportRecord.model_validate(payload)


def _sequence_counts(
    records: Sequence[DeterministicCompositionExportRecord],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in records:
        counts.update(item.chain_sequences)
    return {sequence: counts[sequence] for sequence in _EXPECTED_SEQUENCES}


def _gross_sequence_counts(
    chains: Sequence[DeterministicCompositionChainRecord],
) -> dict[str, int]:
    counts = Counter(f"{item.first_hop_rule_id}->{item.second_hop_rule_id}" for item in chains)
    return {sequence: counts[sequence] for sequence in _EXPECTED_SEQUENCES}


def _report(
    *,
    binding: _FullBinding,
    chain_manifest: DeterministicCompositionChainManifest,
    unique_manifest: DeterministicCompositionUniquePairManifest,
    gross_sequence_counts: Mapping[str, int],
    inventory: Sequence[DeterministicCompositionExportRecord],
    cycles: Sequence[DeterministicCompositionExportRecord],
    quarantine: Sequence[DeterministicCompositionExportRecord],
) -> bytes:
    all_records = tuple(inventory) + tuple(cycles) + tuple(quarantine)
    membership = _sequence_counts(all_records)
    inventory_counts = _sequence_counts(inventory)
    cycle_counts = _sequence_counts(cycles)
    quarantine_counts = _sequence_counts(quarantine)
    lines = [
        "# Deterministic depth-two composition readiness report",
        "",
        "**Status: PROVISIONAL / NOT TRAINING READY.** This export contains mechanical ",
        "intentions only. It creates no semantic labels, promotions, evaluation eligibility, "
        "or gate credit.",
        "",
        f"- Full receipt: `{binding.receipt.receipt_id}`",
        f"- Chain set: `{chain_manifest.chain_set_id}` ({chain_manifest.chain_count} gross chains)",
        f"- Unique-pair set: `{unique_manifest.unique_pair_set_id}` "
        f"({unique_manifest.unique_pair_count} pairs)",
        f"- Alpha-novel, intention-consistent provisional inventory: **{len(inventory)}**",
        f"- Alpha-return cycles retained for audit: **{len(cycles)}**",
        f"- Mixed P-to-P/P-to-N intention collisions quarantined: **{len(quarantine)}**",
        "- **Privacy:** this inventory may contain private `sft_classic` source text; the "
        "manifest controls release and external transmission fail-closed.",
        "",
        "## 65-sequence coverage and readiness",
        "",
        "| First hop | Second hop | Kind | Gross chains | Unique memberships | Export | "
        "Cycles | Quarantine | Readiness |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for sequence in _EXPECTED_SEQUENCES:
        first, second = sequence.split("->", 1)
        kind = "P_to_P" if second.startswith("p") else "P_to_N"
        exported = inventory_counts[sequence]
        quarantined = quarantine_counts[sequence]
        readiness = (
            "provisional + conflict"
            if exported and quarantined
            else "provisional"
            if exported
            else "conflict"
            if quarantined
            else "audit/no novel pair"
        )
        lines.append(
            f"| `{first}` | `{second}` | {kind} | {gross_sequence_counts[sequence]} | "
            f"{membership[sequence]} | "
            f"{exported} | {cycle_counts[sequence]} | {quarantined} | {readiness} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `Export` includes only alpha-novel pairs with one consistent mechanical intention.",
            "- `Cycles` are source-alpha returns and remain visible only for audit; a mixed-"
            "intention cycle is retained in `Quarantine` instead.",
            "- `Quarantine` contains every pair reached by both P-to-P and P-to-N chains.",
            "- None of these partitions is a resolved F1 label or a training dataset.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _verify_existing_output(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    safe_output = _reject_symlink_components(output_dir, allow_missing=False)
    if not safe_output.is_dir():
        raise CompositionReceiptExportError("existing export output is not a real directory")
    if {item.name for item in safe_output.iterdir()} != _OUTPUT_FILES:
        raise CompositionReceiptExportError("existing export output is not exact")
    if any(
        _require_regular_file(safe_output / name).read_bytes() != payload
        for name, payload in payloads.items()
    ):
        raise CompositionReceiptExportError("existing export output differs")
    return True


def _write_or_replay(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _OUTPUT_FILES:
        raise CompositionReceiptExportError("export payload set is not exact")
    output_dir = _reject_symlink_components(output_dir, allow_missing=True)
    if output_dir.exists():
        return _verify_existing_output(output_dir, payloads)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    _require_real_directory(output_dir.parent)
    output_dir = _reject_symlink_components(output_dir, allow_missing=True)
    if output_dir.exists():
        return _verify_existing_output(output_dir, payloads)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        for name, payload in payloads.items():
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        os.rename(temporary, output_dir)
    except FileExistsError:
        if temporary.exists():
            shutil.rmtree(temporary)
        return _verify_existing_output(output_dir, payloads)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return False


def _verify_receipt_boundary_unchanged(binding: _FullBinding, seed_dir: Path) -> None:
    """Re-hash every receipt/seed/root binding immediately before publication."""

    orchestration = binding.spec_path.parent
    seed_root = _require_real_directory(seed_dir)
    seed_manifest_path = _require_regular_file(seed_root / "manifest.json")
    seed_manifest = _load_canonical(seed_manifest_path, CompositionSeedManifest)
    assert isinstance(seed_manifest, CompositionSeedManifest)
    if (
        hash_file(_require_regular_file(binding.spec_path)) != binding.receipt.launch_spec_sha256
        or _require_regular_file(binding.receipt_path).read_bytes()
        != _canonical_line(binding.receipt)
        or hash_file(_require_regular_file(orchestration / "status.json"))
        != binding.receipt.final_status_sha256
        or hash_file(seed_manifest_path) != binding.spec.seed_manifest_sha256
        or seed_manifest.seed_set_id != binding.spec.seed_set_id
        or hash_file(_require_regular_file(seed_root / seed_manifest.seed_output))
        != binding.spec.seed_partition_sha256
        or hash_file(_require_regular_file(seed_root / seed_manifest.theorem_output))
        != binding.spec.theorem_partition_sha256
        or hash_file(_require_regular_file(seed_root / seed_manifest.representation_output))
        != binding.spec.representation_partition_sha256
    ):
        raise CompositionReceiptExportError("receipt or seed binding changed during export")

    for bound, root in zip(binding.receipt.roots, binding.root_paths, strict=True):
        safe_root = _require_real_directory(root)
        try:
            current_tree_hash = _root_tree(safe_root)[1]
        except (OSError, ProvisionalPairCombineError) as exc:
            raise CompositionReceiptExportError(
                f"receipt root changed during export: {bound.family}: {exc}"
            ) from exc
        if (
            hash_file(_require_regular_file(safe_root / "run_spec.json")) != bound.run_spec_sha256
            or hash_file(_require_regular_file(safe_root / "manifest.json"))
            != bound.manifest_sha256
            or hash_file(_require_regular_file(safe_root / "results.jsonl")) != bound.results_sha256
            or current_tree_hash != bound.root_tree_hash
            or hash_file(_require_regular_file(orchestration / "logs" / f"{bound.family}.log"))
            != bound.log_sha256
        ):
            raise CompositionReceiptExportError(
                f"receipt root binding changed during export: {bound.family}"
            )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _require_disjoint_write_roots(
    *,
    chain_dir: Path,
    unique_pair_dir: Path,
    output_dir: Path,
    protected_inputs: Sequence[Path],
) -> None:
    """Reject any nesting between scratch/publication roots and bound inputs."""

    write_roots = (
        ("chain audit", chain_dir),
        ("unique-pair audit", unique_pair_dir),
        ("receipt export", output_dir),
    )
    for index, (left_label, left) in enumerate(write_roots):
        for right_label, right in write_roots[index + 1 :]:
            if _paths_overlap(left, right):
                raise CompositionReceiptExportError(
                    f"{left_label} and {right_label} write roots must be disjoint"
                )
    for write_label, write_root in write_roots:
        for protected in protected_inputs:
            if _paths_overlap(write_root, protected):
                raise CompositionReceiptExportError(
                    f"{write_label} write root overlaps a bound input: {protected}"
                )


def export_deterministic_v2_composition_receipt(
    *,
    full_run_root: Path,
    seed_dir: Path,
    source_theorems: Sequence[Path],
    source_representations: Sequence[Path],
    chain_dir: Path,
    unique_pair_dir: Path,
    output_dir: Path,
) -> CompositionReceiptExportArtifacts:
    """Verify a complete run and emit a label-free provisional export/report."""

    full_run_root = _require_real_directory(full_run_root)
    seed_dir = _require_real_directory(seed_dir)
    source_theorems = tuple(_require_regular_file(path) for path in source_theorems)
    source_representations = tuple(_require_regular_file(path) for path in source_representations)
    chain_dir = _reject_symlink_components(chain_dir, allow_missing=True)
    unique_pair_dir = _reject_symlink_components(unique_pair_dir, allow_missing=True)
    safe_output = _reject_symlink_components(output_dir, allow_missing=True)
    binding = _verify_full_binding(full_run_root, seed_dir)
    protected_inputs = (
        full_run_root,
        seed_dir,
        *binding.root_paths,
        *source_theorems,
        *source_representations,
    )
    _require_disjoint_write_roots(
        chain_dir=chain_dir,
        unique_pair_dir=unique_pair_dir,
        output_dir=safe_output,
        protected_inputs=protected_inputs,
    )
    source_theorem_hashes = tuple((path, hash_file(path)) for path in source_theorems)
    source_representation_hashes = tuple((path, hash_file(path)) for path in source_representations)
    audited = audit_deterministic_v2_composition_chains(
        seed_dir=seed_dir,
        second_hop_roots=binding.root_paths,
        output_dir=chain_dir,
    )
    postprocessed = postprocess_deterministic_v2_composition_unique_pairs(
        seed_dir=seed_dir,
        chain_dir=chain_dir,
        output_dir=unique_pair_dir,
    )
    chain_manifest = _load_canonical(audited.manifest_path, DeterministicCompositionChainManifest)
    unique_manifest = _load_canonical(
        postprocessed.manifest_path, DeterministicCompositionUniquePairManifest
    )
    assert isinstance(chain_manifest, DeterministicCompositionChainManifest)
    assert isinstance(unique_manifest, DeterministicCompositionUniquePairManifest)
    receipt_root_ids = {item.root_binding_id for item in binding.receipt.roots}
    chain_root_ids = {item.root_binding_id for item in chain_manifest.second_hop_roots}
    if receipt_root_ids != chain_root_ids or chain_manifest.second_hop_root_count != 13:
        raise CompositionReceiptExportError("chain audit does not bind all and only receipt roots")
    chains = tuple(
        item
        for item in _load_canonical_jsonl(audited.chains_path, DeterministicCompositionChainRecord)
        if isinstance(item, DeterministicCompositionChainRecord)
    )
    unique_pairs = tuple(
        item
        for item in _load_canonical_jsonl(
            postprocessed.unique_pairs_path, DeterministicCompositionUniquePairRecord
        )
        if isinstance(item, DeterministicCompositionUniquePairRecord)
    )
    gross_sequence_counts = _gross_sequence_counts(chains)
    observed_gross = {key: value for key, value in gross_sequence_counts.items() if value}
    if observed_gross != unique_manifest.gross_sequence_counts:
        raise CompositionReceiptExportError("gross chain sequences differ from unique-pair audit")
    membership_counter = Counter(
        sequence for pair in unique_pairs for sequence in pair.chain_sequences
    )
    observed_memberships = dict(sorted(membership_counter.items()))
    if observed_memberships != unique_manifest.unique_pair_sequence_membership_counts:
        raise CompositionReceiptExportError(
            "unique-pair sequence memberships differ from unique-pair audit"
        )
    chain_by_id = {item.chain_id: item for item in chains}
    if len(chain_by_id) != len(chains):
        raise CompositionReceiptExportError("chain audit repeats an identity")
    referenced_chain_ids = [chain_id for pair in unique_pairs for chain_id in pair.chain_ids]
    if len(referenced_chain_ids) != len(set(referenced_chain_ids)) or set(
        referenced_chain_ids
    ) != set(chain_by_id):
        raise CompositionReceiptExportError(
            "unique-pair partition does not cover every chain exactly once"
        )
    theorem_by_id, representation_by_id = _load_source_inventory(
        source_theorems, source_representations
    )
    roots_by_binding = {
        receipt.root_binding_id: (root, receipt.run_kind)
        for receipt, root in zip(binding.receipt.roots, binding.root_paths, strict=True)
    }
    final_by_chain = _join_final_texts(chains, roots_by_binding)
    records: list[DeterministicCompositionExportRecord] = []
    for pair in unique_pairs:
        (
            source_lean,
            source_dataset,
            private_source_content,
            redistribution_allowed,
            external_transmission_allowed,
            release_eligible,
        ) = _join_source(pair, theorem_by_id, representation_by_id)
        texts: set[str] = set()
        for chain_id in pair.chain_ids:
            chain = chain_by_id.get(chain_id)
            if chain is None:
                raise CompositionReceiptExportError("unique pair references a foreign chain")
            texts.add(final_by_chain[chain_id])
        if len(texts) != 1:
            raise CompositionReceiptExportError("one unique pair maps to different final Lean text")
        records.append(
            _export_record(
                pair,
                source_lean=source_lean,
                source_dataset=source_dataset,
                private_source_content=private_source_content,
                redistribution_allowed=redistribution_allowed,
                external_transmission_allowed=external_transmission_allowed,
                release_eligible=release_eligible,
                final_lean=texts.pop(),
            )
        )
    records.sort(key=lambda item: item.export_record_id)
    inventory = tuple(item for item in records if item.disposition == "provisional_inventory")
    cycles = tuple(item for item in records if item.disposition == "cycle_audit")
    quarantine = tuple(item for item in records if item.disposition == "mixed_intention_quarantine")
    inventory_payload = _canonical_jsonl(inventory)
    cycles_payload = _canonical_jsonl(cycles)
    quarantine_payload = _canonical_jsonl(quarantine)
    report_payload = _report(
        binding=binding,
        chain_manifest=chain_manifest,
        unique_manifest=unique_manifest,
        gross_sequence_counts=gross_sequence_counts,
        inventory=inventory,
        cycles=cycles,
        quarantine=quarantine,
    )
    manifest_data: dict[str, object] = {
        "export_set_id": f"detcomp_export_set:{'0' * 64}",
        "full_receipt_id": binding.receipt.receipt_id,
        "full_receipt_sha256": hash_file(binding.receipt_path),
        "full_launch_id": binding.spec.launch_id,
        "full_launch_spec_sha256": hash_file(binding.spec_path),
        "input_seed_set_id": binding.spec.seed_set_id,
        "input_seed_manifest_sha256": hash_file(_require_regular_file(seed_dir / "manifest.json")),
        "input_chain_set_id": chain_manifest.chain_set_id,
        "input_chain_manifest_sha256": hash_file(audited.manifest_path),
        "input_unique_pair_set_id": unique_manifest.unique_pair_set_id,
        "input_unique_pair_manifest_sha256": hash_file(postprocessed.manifest_path),
        "source_theorem_partition_sha256s": tuple(
            sorted({expected for _, expected in source_theorem_hashes})
        ),
        "source_representation_partition_sha256s": tuple(
            sorted({expected for _, expected in source_representation_hashes})
        ),
        "source_datasets": tuple(sorted({item.source_dataset for item in records})),
        "contains_private_source_content": any(item.private_source_content for item in records),
        "contains_mixed_source_privacy": (
            bool(records)
            and any(item.private_source_content for item in records)
            and any(not item.private_source_content for item in records)
        ),
        "redistribution_allowed": all(item.redistribution_allowed for item in records),
        "external_transmission_allowed": all(
            item.external_transmission_allowed for item in records
        ),
        "release_eligible": all(item.release_eligible for item in records),
        "required_families": tuple(sorted(_EXPECTED_FAMILIES)),
        "gross_chain_count": len(chains),
        "unique_pair_count": len(records),
        "provisional_inventory_count": len(inventory),
        "cycle_audit_count": len(cycles),
        "mixed_intention_quarantine_count": len(quarantine),
        "sequence_counts": gross_sequence_counts,
        "sequence_inventory_counts": _sequence_counts(inventory),
        "sequence_cycle_counts": _sequence_counts(cycles),
        "sequence_quarantine_counts": _sequence_counts(quarantine),
        "inventory_sha256": hashlib.sha256(inventory_payload).hexdigest(),
        "cycles_sha256": hashlib.sha256(cycles_payload).hexdigest(),
        "quarantine_sha256": hashlib.sha256(quarantine_payload).hexdigest(),
        "report_sha256": hashlib.sha256(report_payload).hexdigest(),
    }
    placeholder = DeterministicCompositionReceiptExportManifest.model_construct(
        _fields_set=None, **manifest_data
    )
    manifest_data["export_set_id"] = "detcomp_export_set:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "export_set_id")
    )
    manifest = DeterministicCompositionReceiptExportManifest.model_validate(manifest_data)
    payloads = {
        "inventory.jsonl": inventory_payload,
        "cycles.jsonl": cycles_payload,
        "quarantine.jsonl": quarantine_payload,
        "report.md": report_payload,
        "manifest.json": _canonical_line(manifest),
    }
    if any(
        hash_file(_require_regular_file(path)) != expected
        for path, expected in source_theorem_hashes
    ) or any(
        hash_file(_require_regular_file(path)) != expected
        for path, expected in source_representation_hashes
    ):
        raise CompositionReceiptExportError("source inventory changed during export")
    _verify_receipt_boundary_unchanged(binding, seed_dir)
    if (
        hash_file(_require_regular_file(binding.spec_path)) != manifest.full_launch_spec_sha256
        or hash_file(_require_regular_file(binding.receipt_path)) != manifest.full_receipt_sha256
        or hash_file(_require_regular_file(audited.manifest_path))
        != manifest.input_chain_manifest_sha256
        or hash_file(_require_regular_file(audited.chains_path))
        != chain_manifest.chain_output_sha256
        or hash_file(_require_regular_file(postprocessed.manifest_path))
        != manifest.input_unique_pair_manifest_sha256
        or hash_file(_require_regular_file(postprocessed.unique_pairs_path))
        != unique_manifest.unique_output_sha256
    ):
        raise CompositionReceiptExportError("receipt-bound input changed during export")
    replayed = _write_or_replay(safe_output, payloads)
    return CompositionReceiptExportArtifacts(
        output_dir=safe_output,
        manifest_path=safe_output / "manifest.json",
        inventory_path=safe_output / "inventory.jsonl",
        cycles_path=safe_output / "cycles.jsonl",
        quarantine_path=safe_output / "quarantine.jsonl",
        report_path=safe_output / "report.md",
        export_set_id=manifest.export_set_id,
        provisional_inventory_count=len(inventory),
        cycle_audit_count=len(cycles),
        mixed_intention_quarantine_count=len(quarantine),
        replayed=replayed,
    )


__all__ = [
    "CompositionReceiptExportArtifacts",
    "CompositionReceiptExportError",
    "DeterministicCompositionExportRecord",
    "DeterministicCompositionReceiptExportManifest",
    "export_deterministic_v2_composition_receipt",
]
