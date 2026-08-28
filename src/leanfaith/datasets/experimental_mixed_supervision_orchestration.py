"""Artifact-to-corpus orchestration for experimental mixed supervision.

The lower-level :mod:`experimental_mixed_supervision` module deliberately
accepts typed, already-verified objects.  This module is the reproducible I/O
boundary that constructs those objects from frozen artifacts:

* a complete first-hop projection;
* one or more complete, clean LF-022 Codex audits;
* canonical theorem and representation partitions for the LF-022 sources;
* the current representation-aware benchmark denylist; and
* the exact mixed-corpus policy file; and
* optionally, one complete receipt-bound deterministic composition export.

No semantic labels are created here.  The resulting corpus remains
experimental, provisional proxy supervision.  Composition is admitted only
from the full receipt/export boundary; partial roots never enter this module.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from leanfaith.config import canonical_json_bytes, hash_file, load_config, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import (
    LF016_AUTHORIZATION_PATH,
    REPRESENTATION_SIGNATURE_MANIFEST_PATH,
    ActiveBenchmarkRegistry,
    load_active_benchmark_registry,
)
from leanfaith.datasets.experimental_first_hop_projection import (
    ExperimentalFirstHopProjectionManifest,
    load_selectable_experimental_first_hop_projection,
    verify_experimental_first_hop_projection,
)
from leanfaith.datasets.experimental_mixed_supervision import (
    DeterministicCompositionReplayBinding,
    ExperimentalMixedAdapterResult,
    ExperimentalMixedCandidate,
    ExperimentalMixedExclusion,
    ExperimentalMixedInputBinding,
    ExperimentalMixedSupervisionArtifacts,
    ExperimentalMixedSupervisionConfig,
    ExperimentalMixedSupervisionError,
    ExperimentalMixedSupervisionManifest,
    adapt_deterministic_composition_export_batch,
    adapt_selectable_first_hop_projection,
    adapt_verified_lf022_codex_audit,
    bind_experimental_mixed_input,
    freeze_experimental_mixed_supervision,
    verify_experimental_mixed_supervision,
)
from leanfaith.generation.lf022_codex_audit import (
    LF022VerifiedCodexAudit,
    verify_completed_lf022_codex_audit,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import (
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
)
from leanfaith.transforms.composition_full_launcher import (
    CompositionFullLaunchSpec,
    CompositionFullReceipt,
)
from leanfaith.transforms.composition_receipt_export import (
    DeterministicCompositionExportRecord,
    DeterministicCompositionReceiptExportManifest,
)
from leanfaith.transforms.composition_seed import CompositionSeedManifest
from leanfaith.transforms.composition_unique_pairs import (
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
)
from leanfaith.transforms.v2_d0_materializer import V2D0MaterializationResult
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult

_ARTIFACT_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_THEOREM_ID_IN_JSON = re.compile(rb'"theorem_id"\s*:\s*"(thm:[0-9a-f]{64})"')


@dataclass(frozen=True, slots=True)
class ExperimentalLF022AuditSource:
    """One named audit and the repository root used by its relative artifacts."""

    name: str
    repo_root: Path
    checks_path: Path
    audit_root: Path
    parent_audit_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if _ARTIFACT_NAME.fullmatch(self.name) is None:
            raise ExperimentalMixedSupervisionError(
                "LF-022 audit name must match [a-z0-9][a-z0-9_.-]*"
            )
        resolved = tuple(path.resolve() for path in self.parent_audit_roots)
        if len(resolved) != len(set(resolved)):
            raise ExperimentalMixedSupervisionError("LF-022 parent audit roots must be unique")


@dataclass(frozen=True, slots=True)
class ExperimentalCompositionSource:
    """Immutable roots and original source partitions for one composition export.

    The seed directory contains the first-hop *intermediate* declarations used
    to launch the second hop.  It is lineage evidence, not the inventory of
    original source theorem/view records referenced by the receipt export.
    Those original partitions are therefore explicit, independently bound
    inputs.
    """

    full_run_root: Path
    seed_dir: Path
    postprocess_root: Path
    source_theorem_paths: tuple[Path, ...]
    source_representation_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        resolved = (
            self.full_run_root.resolve(),
            self.seed_dir.resolve(),
            self.postprocess_root.resolve(),
        )
        if len(set(resolved)) != 3:
            raise ExperimentalMixedSupervisionError(
                "composition full-run, seed, and postprocess roots must be distinct"
            )
        if not self.source_theorem_paths or not self.source_representation_paths:
            raise ExperimentalMixedSupervisionError(
                "composition original source theorem and representation partitions are required"
            )
        source_paths = tuple(
            path.resolve()
            for path in (*self.source_theorem_paths, *self.source_representation_paths)
        )
        if len(source_paths) != len(set(source_paths)):
            raise ExperimentalMixedSupervisionError(
                "composition original source partition paths must be unique"
            )


@dataclass(frozen=True, slots=True)
class ExperimentalMixedOrchestrationResult:
    """A frozen corpus plus transparent source-level construction counts."""

    artifacts: ExperimentalMixedSupervisionArtifacts
    first_hop_input_count: int
    first_hop_candidate_count: int
    lf022_audit_count: int
    lf022_judgment_count: int
    lf022_candidate_count: int
    adapter_exclusion_count: int
    input_binding_count: int
    composition_export_count: int = 0
    composition_candidate_count: int = 0
    composition_exclusion_count: int = 0


class _InputBindings:
    """Build deterministic, path-distinct freezer bindings."""

    def __init__(self) -> None:
        self._values: dict[str, ExperimentalMixedInputBinding] = {}

    def add(
        self,
        name: str,
        path: Path,
        *,
        partition: Literal["first_hop", "lf022_codex", "composition", "policy"],
    ) -> ExperimentalMixedInputBinding:
        if not name or name in self._values:
            raise ExperimentalMixedSupervisionError(
                f"duplicate or empty mixed input binding name: {name!r}"
            )
        binding = bind_experimental_mixed_input(path, partition=partition)
        self._values[name] = binding
        return binding

    def finish(self) -> dict[str, ExperimentalMixedInputBinding]:
        return dict(sorted(self._values.items()))


def _resolve_from(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _regular_files_below(root: Path) -> tuple[Path, ...]:
    """Return every regular file and reject symlinks anywhere in the tree."""

    if root.is_symlink() or not root.is_dir():
        raise ExperimentalMixedSupervisionError(f"artifact root is not a real directory: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ExperimentalMixedSupervisionError(f"artifact tree contains a symlink: {path}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ExperimentalMixedSupervisionError(
                f"artifact tree contains a non-file entry: {path}"
            )
    return tuple(files)


def _add_tree_bindings(
    bindings: _InputBindings,
    *,
    prefix: str,
    root: Path,
    partition: Literal["first_hop", "lf022_codex"],
) -> None:
    for path in _regular_files_below(root):
        bindings.add(
            f"{prefix}/{path.relative_to(root).as_posix()}",
            path,
            partition=partition,
        )


def _add_benchmark_bindings(
    bindings: _InputBindings,
    *,
    registry: ActiveBenchmarkRegistry,
    authorization_path: Path | None,
) -> None:
    paths = {
        "benchmark/representation_manifest": registry.manifest_path,
        "benchmark/base_registry": registry.base_registry_path,
        "benchmark/active_registry": registry.active_registry_path,
        "benchmark/detailed_index": registry.detailed_index_path,
        "benchmark/input_manifest": registry.input_manifest_path,
        "benchmark/code_bundle": registry.code_bundle_path,
    }
    if authorization_path is not None:
        paths["benchmark/authorization"] = authorization_path
    for name, path in sorted(paths.items()):
        bindings.add(name, path, partition="policy")


def _load_canonical_model[ModelT: StrictModel](path: Path, model: type[ModelT]) -> ModelT:
    if path.is_symlink() or not path.is_file():
        raise ExperimentalMixedSupervisionError(f"composition artifact is absent: {path}")
    raw = path.read_bytes()
    try:
        item = model.model_validate_json(raw)
    except ValueError as exc:
        raise ExperimentalMixedSupervisionError(
            f"invalid composition {model.__name__}: {path}: {exc}"
        ) from exc
    if raw != canonical_json_bytes(item.model_dump(mode="json")) + b"\n":
        raise ExperimentalMixedSupervisionError(
            f"non-canonical composition {model.__name__}: {path}"
        )
    return item


def _load_canonical_rows[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
) -> tuple[tuple[bytes, ModelT], ...]:
    if path.is_symlink() or not path.is_file():
        raise ExperimentalMixedSupervisionError(f"composition partition is absent: {path}")
    output: list[tuple[bytes, ModelT]] = []
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.endswith(b"\n") or not raw.strip():
                raise ExperimentalMixedSupervisionError(
                    f"invalid composition JSONL framing: {path}:{line_number}"
                )
            try:
                item = model.model_validate_json(raw)
            except ValueError as exc:
                raise ExperimentalMixedSupervisionError(
                    f"invalid composition {model.__name__}: {path}:{line_number}: {exc}"
                ) from exc
            if raw != canonical_json_bytes(item.model_dump(mode="json")) + b"\n":
                raise ExperimentalMixedSupervisionError(
                    f"non-canonical composition {model.__name__}: {path}:{line_number}"
                )
            output.append((raw, item))
    return tuple(output)


def _require_hash(path: Path, expected: str, *, label: str) -> None:
    if hash_file(path) != expected:
        raise ExperimentalMixedSupervisionError(f"composition {label} hash differs: {path}")


def _bind_composition_source_partitions(
    source: ExperimentalCompositionSource,
    *,
    export_manifest: DeterministicCompositionReceiptExportManifest,
    bindings: _InputBindings,
) -> tuple[
    tuple[ExperimentalMixedInputBinding, ...],
    tuple[ExperimentalMixedInputBinding, ...],
]:
    """Bind the original source inventory and match its receipt-export hashes.

    This deliberately does not infer the source inventory from ``seed_dir``:
    seed theorem/view records are P01/P02/P12/P18 intermediates, whereas every
    exported pair points back to its pre-transformation theorem and view.
    """

    theorem_bindings = tuple(
        bindings.add(
            f"composition/source/theorems/{index:02d}",
            path,
            partition="composition",
        )
        for index, path in enumerate(sorted(source.source_theorem_paths))
    )
    representation_bindings = tuple(
        bindings.add(
            f"composition/source/representations/{index:02d}",
            path,
            partition="composition",
        )
        for index, path in enumerate(sorted(source.source_representation_paths))
    )
    if tuple(sorted(item.sha256 for item in theorem_bindings)) != (
        export_manifest.source_theorem_partition_sha256s
    ):
        raise ExperimentalMixedSupervisionError(
            "composition original source theorem partitions differ from receipt export"
        )
    if tuple(sorted(item.sha256 for item in representation_bindings)) != (
        export_manifest.source_representation_partition_sha256s
    ):
        raise ExperimentalMixedSupervisionError(
            "composition original source representation partitions differ from receipt export"
        )
    return (
        tuple(sorted(theorem_bindings, key=lambda item: (item.sha256, item.path))),
        tuple(sorted(representation_bindings, key=lambda item: (item.sha256, item.path))),
    )


def _adapt_composition_source(
    source: ExperimentalCompositionSource,
    *,
    benchmark_registry: ActiveBenchmarkRegistry,
    bindings: _InputBindings,
) -> tuple[ExperimentalMixedAdapterResult, int]:
    """Build complete per-row joins, then invoke the one-pass receipt verifier."""

    full_root = source.full_run_root.resolve()
    seed_root = source.seed_dir.resolve()
    postprocess_root = source.postprocess_root.resolve()
    if any(
        path.is_symlink() or not path.is_dir() for path in (full_root, seed_root, postprocess_root)
    ):
        raise ExperimentalMixedSupervisionError(
            "composition source roots must be existing real directories"
        )

    orchestration_root = full_root / "orchestration"
    launch_path = orchestration_root / "launch_spec.json"
    receipt_path = orchestration_root / "receipt.json"
    status_path = orchestration_root / "status.json"
    chain_root = postprocess_root / "chains"
    unique_root = postprocess_root / "unique_pairs"
    export_root = postprocess_root / "export"
    chain_manifest_path = chain_root / "manifest.json"
    chain_records_path = chain_root / "chains.jsonl"
    unique_manifest_path = unique_root / "manifest.json"
    unique_records_path = unique_root / "unique_pairs.jsonl"
    export_manifest_path = export_root / "manifest.json"

    launch = _load_canonical_model(launch_path, CompositionFullLaunchSpec)
    receipt = _load_canonical_model(receipt_path, CompositionFullReceipt)
    seed_manifest = _load_canonical_model(seed_root / "manifest.json", CompositionSeedManifest)
    chain_manifest = _load_canonical_model(
        chain_manifest_path, DeterministicCompositionChainManifest
    )
    unique_manifest = _load_canonical_model(
        unique_manifest_path, DeterministicCompositionUniquePairManifest
    )
    export_manifest = _load_canonical_model(
        export_manifest_path, DeterministicCompositionReceiptExportManifest
    )

    if (
        Path(launch.seed_dir).resolve() != seed_root
        or Path(launch.output_root).resolve() != full_root
    ):
        raise ExperimentalMixedSupervisionError(
            "composition launch paths differ from the requested source roots"
        )
    seed_paths = {
        "records": seed_root / seed_manifest.seed_output,
        "theorems": seed_root / seed_manifest.theorem_output,
        "representations": seed_root / seed_manifest.representation_output,
    }
    if (
        receipt.launch_id != launch.launch_id
        or receipt.launch_spec_sha256 != hash_file(launch_path)
        or receipt.final_status_sha256 != hash_file(status_path)
        or launch.seed_set_id != seed_manifest.seed_set_id
        or launch.seed_manifest_sha256 != hash_file(seed_root / "manifest.json")
        or launch.seed_partition_sha256 != seed_manifest.seed_output_sha256
        or launch.theorem_partition_sha256 != seed_manifest.theorem_output_sha256
        or launch.representation_partition_sha256 != seed_manifest.representation_output_sha256
        or export_manifest.full_launch_id != launch.launch_id
        or export_manifest.full_receipt_id != receipt.receipt_id
        or export_manifest.full_launch_spec_sha256 != hash_file(launch_path)
        or export_manifest.full_receipt_sha256 != hash_file(receipt_path)
        or export_manifest.input_seed_set_id != seed_manifest.seed_set_id
        or export_manifest.input_seed_manifest_sha256 != hash_file(seed_root / "manifest.json")
        or export_manifest.input_chain_set_id != chain_manifest.chain_set_id
        or export_manifest.input_chain_manifest_sha256 != hash_file(chain_manifest_path)
        or export_manifest.input_unique_pair_set_id != unique_manifest.unique_pair_set_id
        or export_manifest.input_unique_pair_manifest_sha256 != hash_file(unique_manifest_path)
        or unique_manifest.input_chain_set_id != chain_manifest.chain_set_id
        or unique_manifest.input_chain_manifest_sha256 != hash_file(chain_manifest_path)
    ):
        raise ExperimentalMixedSupervisionError(
            "composition launch/receipt/seed/export manifests do not form one exact lineage"
        )
    _require_hash(seed_paths["records"], seed_manifest.seed_output_sha256, label="seed")
    _require_hash(seed_paths["theorems"], seed_manifest.theorem_output_sha256, label="seed theorem")
    _require_hash(
        seed_paths["representations"],
        seed_manifest.representation_output_sha256,
        label="seed representation",
    )
    _require_hash(chain_records_path, chain_manifest.chain_output_sha256, label="chain records")
    _require_hash(
        unique_records_path, unique_manifest.unique_output_sha256, label="unique-pair records"
    )

    launch_binding = bindings.add(
        "composition/full_run/launch_spec", launch_path, partition="composition"
    )
    receipt_binding = bindings.add(
        "composition/full_run/receipt", receipt_path, partition="composition"
    )
    status_binding = bindings.add(
        "composition/full_run/status", status_path, partition="composition"
    )
    seed_manifest_binding = bindings.add(
        "composition/seed/manifest", seed_root / "manifest.json", partition="composition"
    )
    bindings.add("composition/seed/records", seed_paths["records"], partition="composition")
    bindings.add("composition/seed/theorems", seed_paths["theorems"], partition="composition")
    bindings.add(
        "composition/seed/representations",
        seed_paths["representations"],
        partition="composition",
    )
    theorem_bindings, representation_bindings = _bind_composition_source_partitions(
        source,
        export_manifest=export_manifest,
        bindings=bindings,
    )
    chain_manifest_binding = bindings.add(
        "composition/chains/manifest", chain_manifest_path, partition="composition"
    )
    chain_records_binding = bindings.add(
        "composition/chains/records", chain_records_path, partition="composition"
    )
    unique_manifest_binding = bindings.add(
        "composition/unique_pairs/manifest",
        unique_manifest_path,
        partition="composition",
    )
    unique_records_binding = bindings.add(
        "composition/unique_pairs/records", unique_records_path, partition="composition"
    )
    export_manifest_binding = bindings.add(
        "composition/export/manifest", export_manifest_path, partition="composition"
    )
    bindings.add("composition/export/report", export_root / "report.md", partition="composition")

    export_partition_paths = {
        "inventory": export_root / "inventory.jsonl",
        "cycles": export_root / "cycles.jsonl",
        "quarantine": export_root / "quarantine.jsonl",
    }
    export_partition_bindings = {
        partition: bindings.add(f"composition/export/{partition}", path, partition="composition")
        for partition, path in export_partition_paths.items()
    }
    for partition, expected in (
        ("inventory", export_manifest.inventory_sha256),
        ("cycles", export_manifest.cycles_sha256),
        ("quarantine", export_manifest.quarantine_sha256),
    ):
        _require_hash(export_partition_paths[partition], expected, label=f"export {partition}")
    _require_hash(export_root / "report.md", export_manifest.report_sha256, label="export report")

    receipt_root_by_id = {item.root_binding_id: item for item in receipt.roots}
    chain_root_by_id = {item.root_binding_id: item for item in chain_manifest.second_hop_roots}
    if (
        len(receipt.roots) != 13
        or set(receipt_root_by_id) != set(chain_root_by_id)
        or len(receipt_root_by_id) != 13
    ):
        raise ExperimentalMixedSupervisionError(
            "composition receipt must bind exactly the thirteen chain roots"
        )
    result_binding_by_root: dict[str, ExperimentalMixedInputBinding] = {}
    for root_id, receipt_root in sorted(receipt_root_by_id.items()):
        root_path = Path(receipt_root.root_path).resolve()
        chain_bound = chain_root_by_id[root_id]
        run_spec_path = root_path / "run_spec.json"
        manifest_path = root_path / "manifest.json"
        results_path = root_path / "results.jsonl"
        log_path = orchestration_root / "logs" / f"{receipt_root.family}.log"
        if (
            hash_file(run_spec_path) != receipt_root.run_spec_sha256
            or hash_file(manifest_path) != receipt_root.manifest_sha256
            or hash_file(results_path) != receipt_root.results_sha256
            or hash_file(log_path) != receipt_root.log_sha256
            or chain_bound.results.sha256 != receipt_root.results_sha256
            or chain_bound.results.byte_count != results_path.stat().st_size
            or chain_bound.run_kind != receipt_root.run_kind
        ):
            raise ExperimentalMixedSupervisionError(
                f"composition receipt root differs for {receipt_root.family}"
            )
        prefix = f"composition/full_run/roots/{receipt_root.family}"
        bindings.add(f"{prefix}/run_spec", run_spec_path, partition="composition")
        bindings.add(f"{prefix}/manifest", manifest_path, partition="composition")
        result_binding_by_root[root_id] = bindings.add(
            f"{prefix}/results", results_path, partition="composition"
        )
        bindings.add(f"{prefix}/log", log_path, partition="composition")

    chain_rows = _load_canonical_rows(chain_records_path, DeterministicCompositionChainRecord)
    unique_rows = _load_canonical_rows(
        unique_records_path, DeterministicCompositionUniquePairRecord
    )
    chains = tuple(item for _, item in chain_rows)
    unique_pairs = tuple(item for _, item in unique_rows)
    if len(chains) != chain_manifest.chain_count or len(unique_pairs) != (
        unique_manifest.unique_pair_count
    ):
        raise ExperimentalMixedSupervisionError(
            "composition chain/unique-pair counts differ from their manifests"
        )
    chain_by_id = {item.chain_id: item for item in chains}
    pair_by_id = {item.unique_pair_id: item for item in unique_pairs}
    if len(chain_by_id) != len(chains) or len(pair_by_id) != len(unique_pairs):
        raise ExperimentalMixedSupervisionError(
            "composition chain/unique-pair partitions repeat identities"
        )

    source_theorem_ids = frozenset(item.original_source_theorem_id for item in unique_pairs)
    theorem_by_id = _load_target_records(
        tuple(Path(item.path) for item in theorem_bindings),
        target_theorem_ids=source_theorem_ids,
        model=TheoremRecord,
        wrapper_key="theorem",
    )
    source_representations_by_theorem = _load_target_records(
        tuple(Path(item.path) for item in representation_bindings),
        target_theorem_ids=source_theorem_ids,
        model=RepresentationRecord,
        wrapper_key="representation",
    )
    representation_by_id = {
        item.representation_id: item for item in source_representations_by_theorem.values()
    }
    if len(representation_by_id) != len(source_representations_by_theorem):
        raise ExperimentalMixedSupervisionError(
            "composition original source partitions repeat representation identities"
        )

    requested_lines: dict[str, set[int]] = defaultdict(set)
    for chain in chains:
        requested_lines[chain.second_hop_root_binding_id].add(chain.second_hop_result_line_number)
    results_by_chain: dict[str, V2E2MaterializationResult | V2D0MaterializationResult] = {}
    chains_by_root_line = {
        (item.second_hop_root_binding_id, item.second_hop_result_line_number): item
        for item in chains
    }
    if len(chains_by_root_line) != len(chains):
        raise ExperimentalMixedSupervisionError(
            "composition chains repeat a second-hop result locator"
        )
    for root_id, line_numbers in sorted(requested_lines.items()):
        receipt_root = receipt_root_by_id[root_id]
        result_model = (
            V2E2MaterializationResult
            if receipt_root.run_kind == "e2"
            else V2D0MaterializationResult
        )
        remaining = set(line_numbers)
        result_path = Path(result_binding_by_root[root_id].path)
        with result_path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if line_number not in remaining:
                    continue
                if not raw.endswith(b"\n") or not raw.strip():
                    raise ExperimentalMixedSupervisionError(
                        f"invalid second-hop JSONL framing: {result_path}:{line_number}"
                    )
                try:
                    result = result_model.model_validate_json(raw)
                except ValueError as exc:
                    raise ExperimentalMixedSupervisionError(
                        f"invalid second-hop result: {result_path}:{line_number}: {exc}"
                    ) from exc
                if raw != canonical_json_bytes(result.model_dump(mode="json")) + b"\n":
                    raise ExperimentalMixedSupervisionError(
                        f"non-canonical second-hop result: {result_path}:{line_number}"
                    )
                chain = chains_by_root_line[(root_id, line_number)]
                results_by_chain[chain.chain_id] = result
                remaining.remove(line_number)
                if not remaining:
                    break
        if remaining:
            raise ExperimentalMixedSupervisionError(
                f"composition result lines are absent from {result_path}: {sorted(remaining)[:5]}"
            )

    export_rows: list[
        tuple[
            Literal["inventory", "cycles", "quarantine"],
            int,
            bytes,
            DeterministicCompositionExportRecord,
        ]
    ] = []
    for partition in ("inventory", "cycles", "quarantine"):
        typed_partition = cast(Literal["inventory", "cycles", "quarantine"], partition)
        for line_number, (raw, record) in enumerate(
            _load_canonical_rows(
                export_partition_paths[typed_partition],
                DeterministicCompositionExportRecord,
            ),
            start=1,
        ):
            export_rows.append((typed_partition, line_number, raw, record))
    if len(export_rows) != export_manifest.unique_pair_count:
        raise ExperimentalMixedSupervisionError(
            "composition export row count differs from its manifest"
        )

    replay_items: list[
        tuple[DeterministicCompositionExportRecord, DeterministicCompositionReplayBinding]
    ] = []
    for partition, line_number, raw, record in export_rows:
        pair = pair_by_id.get(record.input_unique_pair_id)
        if pair is None:
            raise ExperimentalMixedSupervisionError(
                "composition export references a missing unique pair"
            )
        selected_chains = tuple(
            sorted((chain_by_id[item] for item in pair.chain_ids), key=lambda item: item.chain_id)
        )
        selected_results = tuple(results_by_chain[item.chain_id] for item in selected_chains)
        final_theorem_by_id = {
            item.candidate_theorem.theorem_id: item.candidate_theorem
            for item in selected_results
            if item.candidate_theorem is not None
        }
        final_theorems = tuple(
            sorted(
                final_theorem_by_id.values(),
                key=lambda item: item.theorem_id,
            )
        )
        final_representation_by_id = {
            item.candidate_representation.representation_id: item.candidate_representation
            for item in selected_results
            if item.candidate_representation is not None
        }
        final_representations = tuple(
            sorted(
                final_representation_by_id.values(),
                key=lambda item: item.representation_id,
            )
        )
        source_theorem = theorem_by_id.get(pair.original_source_theorem_id)
        source_representation = representation_by_id.get(pair.original_source_representation_id)
        if source_theorem is None or source_representation is None:
            raise ExperimentalMixedSupervisionError(
                "composition seed inventory lacks an exported source theorem/view"
            )
        replay_items.append(
            (
                record,
                DeterministicCompositionReplayBinding(
                    full_launch_spec=launch,
                    full_launch_spec_artifact=launch_binding,
                    full_receipt=receipt,
                    full_receipt_artifact=receipt_binding,
                    full_status_artifact=status_binding,
                    export_manifest=export_manifest,
                    export_manifest_artifact=export_manifest_binding,
                    export_partition=partition,
                    export_partition_artifact=export_partition_bindings[partition],
                    export_line_number=line_number,
                    export_line_sha256=sha256_hex(raw),
                    chain_manifest=chain_manifest,
                    chain_manifest_artifact=chain_manifest_binding,
                    chain_records_artifact=chain_records_binding,
                    unique_pair_manifest=unique_manifest,
                    unique_pair_manifest_artifact=unique_manifest_binding,
                    unique_pair_records_artifact=unique_records_binding,
                    unique_pair=pair,
                    chains=selected_chains,
                    source_theorem_artifacts=theorem_bindings,
                    source_representation_artifacts=representation_bindings,
                    second_hop_result_artifacts={
                        root_id: result_binding_by_root[root_id]
                        for root_id in sorted(
                            {item.second_hop_root_binding_id for item in selected_chains}
                        )
                    },
                    source_theorem=source_theorem,
                    source_representation=source_representation,
                    final_theorems=final_theorems,
                    final_representations=final_representations,
                ),
            )
        )

    adapted = adapt_deterministic_composition_export_batch(
        tuple(replay_items),
        export_partition_artifacts=cast(
            dict[Literal["inventory", "cycles", "quarantine"], ExperimentalMixedInputBinding],
            export_partition_bindings,
        ),
        benchmark_registry=benchmark_registry,
    )
    # Bind the seed manifest object even though it is already represented by
    # its path binding; retaining the local name makes the lineage check above
    # explicit and prevents an accidental removal as dead code.
    if seed_manifest_binding.sha256 != launch.seed_manifest_sha256:
        raise ExperimentalMixedSupervisionError("composition seed binding changed")
    return adapted, len(export_rows)


def _load_target_records[ModelT: TheoremRecord | RepresentationRecord](
    paths: Sequence[Path],
    *,
    target_theorem_ids: frozenset[str],
    model: type[ModelT],
    wrapper_key: Literal["theorem", "representation"],
) -> dict[str, ModelT]:
    """Stream huge JSONL partitions and parse only requested theorem rows.

    Representation partitions can contain exceptionally large operator trees.
    A byte-level theorem-ID prefilter prevents unrelated records from ever
    becoming Python/Pydantic objects.
    """

    found: dict[str, ModelT] = {}
    if not target_theorem_ids:
        return found
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ExperimentalMixedSupervisionError(f"source partition is absent: {path}")
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.endswith(b"\n") or not raw.strip():
                    raise ExperimentalMixedSupervisionError(
                        f"invalid source JSONL framing at {path}:{line_number}"
                    )
                matches = {item.decode("ascii") for item in _THEOREM_ID_IN_JSON.findall(raw)}
                selected_ids = matches & target_theorem_ids
                if not selected_ids:
                    continue
                if len(selected_ids) != 1:
                    raise ExperimentalMixedSupervisionError(
                        f"source row contains multiple requested theorem IDs at "
                        f"{path}:{line_number}"
                    )
                selected_id = next(iter(selected_ids))
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise TypeError("row is not an object")
                    selected = payload.get(wrapper_key, payload)
                    if not isinstance(selected, dict):
                        raise TypeError(f"{wrapper_key} wrapper is not an object")
                    record = cast(ModelT, model.model_validate(selected))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ExperimentalMixedSupervisionError(
                        f"invalid {model.__name__} at {path}:{line_number}: {exc}"
                    ) from exc
                theorem_id = record.theorem_id
                if theorem_id != selected_id:
                    raise ExperimentalMixedSupervisionError(
                        f"prefilter/model theorem ID mismatch at {path}:{line_number}"
                    )
                if theorem_id in found:
                    raise ExperimentalMixedSupervisionError(
                        f"canonical source theorem ID appears more than once: {theorem_id}"
                    )
                found[theorem_id] = record
    missing = sorted(target_theorem_ids - set(found))
    if missing:
        preview = ", ".join(missing[:5])
        raise ExperimentalMixedSupervisionError(
            f"canonical source partition lacks {len(missing)} requested theorem IDs: {preview}"
        )
    return found


def _verify_audits(
    audit_sources: Sequence[ExperimentalLF022AuditSource],
) -> tuple[LF022VerifiedCodexAudit, ...]:
    names = [source.name for source in audit_sources]
    if len(names) != len(set(names)):
        raise ExperimentalMixedSupervisionError("LF-022 audit names must be unique")
    verified: list[LF022VerifiedCodexAudit] = []
    for source in sorted(audit_sources, key=lambda item: item.name):
        verified.append(
            verify_completed_lf022_codex_audit(
                repo_root=source.repo_root,
                checks_path=source.checks_path,
                audit_root=source.audit_root,
                require_complete_clean=True,
                parent_audit_roots=source.parent_audit_roots,
            )
        )
    return tuple(verified)


def _source_theorem_ids(
    audits: Sequence[LF022VerifiedCodexAudit],
) -> frozenset[str]:
    theorem_ids: set[str] = set()
    for audit in audits:
        for judgment in audit.judgments:
            selected = tuple(
                value for value in judgment.source_record_ids if value.startswith("thm:")
            )
            if len(selected) != 1:
                raise ExperimentalMixedSupervisionError(
                    "verified LF-022 judgment lacks exactly one canonical source theorem ID"
                )
            theorem_ids.add(selected[0])
    return frozenset(theorem_ids)


def _bind_lf022_source_artifacts(
    bindings: _InputBindings,
    *,
    audit_source: ExperimentalLF022AuditSource,
    verified: LF022VerifiedCodexAudit,
) -> None:
    """Bind audit bytes plus variant/task/raw-Lean artifacts used by verification."""

    _add_tree_bindings(
        bindings,
        prefix=f"lf022/{audit_source.name}/audit",
        root=audit_source.audit_root,
        partition="lf022_codex",
    )
    verified_parents = {
        Path(binding.audit_root).resolve(): binding for binding in verified.parent_audit_bindings
    }
    declared_parents = tuple(path.resolve() for path in audit_source.parent_audit_roots)
    if set(verified_parents) != set(declared_parents):
        raise ExperimentalMixedSupervisionError(
            "verified LF-022 parent bindings differ from declared parent audit roots"
        )
    for index, parent_root in enumerate(sorted(declared_parents)):
        parent_binding = verified_parents[parent_root]
        parent_manifest = parent_root / "manifest.json"
        if hash_file(parent_manifest) != parent_binding.manifest_sha256:
            raise ExperimentalMixedSupervisionError(
                f"LF-022 parent manifest hash differs: {parent_manifest}"
            )
        _add_tree_bindings(
            bindings,
            prefix=f"lf022/{audit_source.name}/parent{index:02d}",
            root=parent_root,
            partition="lf022_codex",
        )
    bindings.add(
        f"lf022/{audit_source.name}/checks",
        audit_source.checks_path,
        partition="lf022_codex",
    )
    extra_paths: dict[str, Path] = {}
    for check in verified.checks:
        variant_path = _resolve_from(audit_source.repo_root, Path(check.source_variant_artifact))
        extra_paths[f"variant/{check.source_variant_artifact_sha256}"] = variant_path
        task_path = variant_path.with_name("task.json")
        extra_paths[f"task/{hash_file(task_path)}"] = task_path
        for attempt in check.attempts:
            if attempt.raw_response_path is None:
                continue
            raw_path = _resolve_from(audit_source.repo_root, Path(attempt.raw_response_path))
            if hash_file(raw_path) != attempt.raw_response_sha256:
                raise ExperimentalMixedSupervisionError(
                    f"LF-022 Lean raw response hash differs: {raw_path}"
                )
            extra_paths[f"lean_raw/{attempt.raw_response_sha256}"] = raw_path
    for suffix, path in sorted(extra_paths.items()):
        bindings.add(
            f"lf022/{audit_source.name}/{suffix}",
            path,
            partition="lf022_codex",
        )


def _load_registry(
    *,
    repo_root: Path,
    manifest_path: Path | None,
    expected_manifest_sha256: str | None,
    authorization_path: Path | None,
) -> tuple[ActiveBenchmarkRegistry, Path | None]:
    effective_manifest = _resolve_from(
        repo_root,
        manifest_path or REPRESENTATION_SIGNATURE_MANIFEST_PATH,
    )
    effective_authorization: Path | None = None
    if expected_manifest_sha256 is None:
        effective_authorization = _resolve_from(
            repo_root,
            authorization_path or LF016_AUTHORIZATION_PATH,
        )
    registry = load_active_benchmark_registry(
        effective_manifest,
        repo_root=repo_root,
        expected_manifest_sha256=expected_manifest_sha256,
        authorization_path=effective_authorization,
    )
    return registry, effective_authorization


def _assemble_and_freeze(
    *,
    repo_root: Path,
    output_dir: Path,
    config_path: Path,
    first_hop_projection_dir: Path,
    lf022_audits: Sequence[ExperimentalLF022AuditSource],
    source_theorem_paths: Sequence[Path],
    source_representation_paths: Sequence[Path],
    composition_source: ExperimentalCompositionSource | None,
    benchmark_manifest_path: Path | None,
    benchmark_expected_manifest_sha256: str | None,
    benchmark_authorization_path: Path | None,
) -> ExperimentalMixedOrchestrationResult:
    repo_root = repo_root.resolve()
    config_path = _resolve_from(repo_root, config_path)
    first_hop_projection_dir = first_hop_projection_dir.resolve()
    theorem_paths = tuple(path.resolve() for path in source_theorem_paths)
    representation_paths = tuple(path.resolve() for path in source_representation_paths)
    if not theorem_paths or not representation_paths:
        raise ExperimentalMixedSupervisionError(
            "LF-022 orchestration requires theorem and representation partitions"
        )

    loaded = load_config(config_path, ExperimentalMixedSupervisionConfig)
    config = loaded.config
    if config.first_hop_partition != "included" or config.lf022_codex_partition != "included":
        raise ExperimentalMixedSupervisionError(
            "this orchestration requires included first-hop and LF-022 partitions"
        )
    if (config.composition_partition == "included") != (composition_source is not None):
        raise ExperimentalMixedSupervisionError(
            "composition source presence must exactly match composition_partition=included"
        )
    if not lf022_audits:
        raise ExperimentalMixedSupervisionError("at least one complete LF-022 audit is required")

    registry, effective_authorization = _load_registry(
        repo_root=repo_root,
        manifest_path=benchmark_manifest_path,
        expected_manifest_sha256=benchmark_expected_manifest_sha256,
        authorization_path=benchmark_authorization_path,
    )
    first_hop_manifest: ExperimentalFirstHopProjectionManifest = (
        verify_experimental_first_hop_projection(
            first_hop_projection_dir,
            verify_external_inputs=True,
        )
    )
    current_registry_sha256 = hash_file(registry.active_registry_path)
    if first_hop_manifest.config.benchmark_active_registry_sha256 != current_registry_sha256:
        raise ExperimentalMixedSupervisionError(
            "first-hop projection was screened against a different active benchmark registry"
        )
    first_hop_records = load_selectable_experimental_first_hop_projection(
        first_hop_projection_dir,
        allow_experimental_first_hop_projection=True,
        purpose="mixed_proxy_construction",
    )
    if len(first_hop_records) != first_hop_manifest.selectable_count:
        raise ExperimentalMixedSupervisionError(
            "first-hop selectable partition differs from its verified manifest"
        )

    verified_audits = _verify_audits(lf022_audits)
    target_ids = _source_theorem_ids(verified_audits)
    theorem_models = _load_target_records(
        theorem_paths,
        target_theorem_ids=target_ids,
        model=TheoremRecord,
        wrapper_key="theorem",
    )
    representation_models = _load_target_records(
        representation_paths,
        target_theorem_ids=target_ids,
        model=RepresentationRecord,
        wrapper_key="representation",
    )
    source_theorems = dict(theorem_models)
    source_representations = dict(representation_models)

    candidates: list[ExperimentalMixedCandidate] = []
    exclusions: list[ExperimentalMixedExclusion] = []
    first_hop_candidate_count = 0
    for record in first_hop_records:
        adapted = adapt_selectable_first_hop_projection(
            record,
            benchmark_registry=registry,
        )
        candidates.extend(adapted.candidates)
        exclusions.extend(adapted.exclusions)
        first_hop_candidate_count += len(adapted.candidates)

    lf022_candidate_count = 0
    for verified in verified_audits:
        adapted = adapt_verified_lf022_codex_audit(
            verified,
            source_theorems=source_theorems,
            source_representations=source_representations,
            benchmark_registry=registry,
        )
        candidates.extend(adapted.candidates)
        exclusions.extend(adapted.exclusions)
        lf022_candidate_count += len(adapted.candidates)

    bindings = _InputBindings()
    bindings.add("policy/mixed_config", config_path, partition="policy")
    _add_benchmark_bindings(
        bindings,
        registry=registry,
        authorization_path=effective_authorization,
    )
    _add_tree_bindings(
        bindings,
        prefix="first_hop/projection",
        root=first_hop_projection_dir,
        partition="first_hop",
    )
    for name, binding in sorted(first_hop_manifest.inputs.items()):
        bindings.add(
            f"first_hop/upstream/{name}",
            Path(binding.path),
            partition="first_hop",
        )
    for audit_source, verified in zip(
        sorted(lf022_audits, key=lambda item: item.name),
        verified_audits,
        strict=True,
    ):
        _bind_lf022_source_artifacts(
            bindings,
            audit_source=audit_source,
            verified=verified,
        )
    for index, path in enumerate(theorem_paths):
        bindings.add(
            f"lf022/source_theorems/{index:04d}",
            path,
            partition="lf022_codex",
        )
    for index, path in enumerate(representation_paths):
        bindings.add(
            f"lf022/source_representations/{index:04d}",
            path,
            partition="lf022_codex",
        )
    composition_export_count = 0
    composition_candidate_count = 0
    composition_exclusion_count = 0
    if composition_source is not None:
        adapted_composition, composition_export_count = _adapt_composition_source(
            composition_source,
            benchmark_registry=registry,
            bindings=bindings,
        )
        candidates.extend(adapted_composition.candidates)
        exclusions.extend(adapted_composition.exclusions)
        composition_candidate_count = len(adapted_composition.candidates)
        composition_exclusion_count = len(adapted_composition.exclusions)
    frozen_bindings = bindings.finish()
    artifacts = freeze_experimental_mixed_supervision(
        repo_root=repo_root,
        output_dir=output_dir,
        config=config,
        candidates=tuple(candidates),
        adapter_exclusions=tuple(exclusions),
        inputs=frozen_bindings,
    )
    return ExperimentalMixedOrchestrationResult(
        artifacts=artifacts,
        first_hop_input_count=len(first_hop_records),
        first_hop_candidate_count=first_hop_candidate_count,
        lf022_audit_count=len(verified_audits),
        lf022_judgment_count=sum(len(audit.judgments) for audit in verified_audits),
        lf022_candidate_count=lf022_candidate_count,
        adapter_exclusion_count=len(exclusions),
        input_binding_count=len(frozen_bindings),
        composition_export_count=composition_export_count,
        composition_candidate_count=composition_candidate_count,
        composition_exclusion_count=composition_exclusion_count,
    )


def freeze_experimental_mixed_supervision_from_artifacts(
    *,
    repo_root: Path,
    output_dir: Path,
    config_path: Path,
    first_hop_projection_dir: Path,
    lf022_audits: Sequence[ExperimentalLF022AuditSource],
    source_theorem_paths: Sequence[Path],
    source_representation_paths: Sequence[Path],
    composition_source: ExperimentalCompositionSource | None = None,
    benchmark_manifest_path: Path | None = None,
    benchmark_expected_manifest_sha256: str | None = None,
    benchmark_authorization_path: Path | None = None,
) -> ExperimentalMixedOrchestrationResult:
    """Verify every source and freeze (or exactly replay) the mixed corpus."""

    return _assemble_and_freeze(
        repo_root=repo_root,
        output_dir=output_dir,
        config_path=config_path,
        first_hop_projection_dir=first_hop_projection_dir,
        lf022_audits=lf022_audits,
        source_theorem_paths=source_theorem_paths,
        source_representation_paths=source_representation_paths,
        composition_source=composition_source,
        benchmark_manifest_path=benchmark_manifest_path,
        benchmark_expected_manifest_sha256=benchmark_expected_manifest_sha256,
        benchmark_authorization_path=benchmark_authorization_path,
    )


def replay_verify_experimental_mixed_supervision_from_artifacts(
    *,
    repo_root: Path,
    output_dir: Path,
    config_path: Path,
    first_hop_projection_dir: Path,
    lf022_audits: Sequence[ExperimentalLF022AuditSource],
    source_theorem_paths: Sequence[Path],
    source_representation_paths: Sequence[Path],
    composition_source: ExperimentalCompositionSource | None = None,
    benchmark_manifest_path: Path | None = None,
    benchmark_expected_manifest_sha256: str | None = None,
    benchmark_authorization_path: Path | None = None,
) -> ExperimentalMixedOrchestrationResult:
    """Reassemble all sources and require byte-identical existing output."""

    result = _assemble_and_freeze(
        repo_root=repo_root,
        output_dir=output_dir,
        config_path=config_path,
        first_hop_projection_dir=first_hop_projection_dir,
        lf022_audits=lf022_audits,
        source_theorem_paths=source_theorem_paths,
        source_representation_paths=source_representation_paths,
        composition_source=composition_source,
        benchmark_manifest_path=benchmark_manifest_path,
        benchmark_expected_manifest_sha256=benchmark_expected_manifest_sha256,
        benchmark_authorization_path=benchmark_authorization_path,
    )
    if not result.artifacts.replayed:
        raise ExperimentalMixedSupervisionError(
            "replay verification unexpectedly created a new corpus"
        )
    verify_experimental_mixed_supervision(output_dir, verify_external_inputs=True)
    return result


def verify_frozen_experimental_mixed_supervision(
    output_dir: Path,
) -> ExperimentalMixedSupervisionManifest:
    """Verify frozen bytes, external bindings, split unions, and policy invariants."""

    return verify_experimental_mixed_supervision(
        output_dir,
        verify_external_inputs=True,
    )


__all__ = [
    "ExperimentalCompositionSource",
    "ExperimentalLF022AuditSource",
    "ExperimentalMixedOrchestrationResult",
    "freeze_experimental_mixed_supervision_from_artifacts",
    "replay_verify_experimental_mixed_supervision_from_artifacts",
    "verify_frozen_experimental_mixed_supervision",
]
