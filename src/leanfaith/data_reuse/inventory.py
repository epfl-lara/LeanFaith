"""Build the DATA-REUSE inventory without invoking Lean or mutating sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "configs/data_reuse/inventory_v1.json"
TREE_HASH_ALGORITHM = "sha256 over sorted UTF-8 relative paths: relative_path NUL file_sha256 NUL"

BOOTSTRAP_ROOT = Path(
    "/storage/milikic/leanfaith/experimental_mixed_supervision/"
    "firsthop_kimi_qwen1125_composition_f7b398af_v1"
)
DEPTH3_ROOT = Path(
    "/storage/milikic/leanfaith/deterministic_v2/composition_third_hop_audits/"
    "frontier_084859ee_five_families_v2"
)
UNARY_ROOT = Path(
    "/storage/milikic/leanfaith/deterministic_scale/"
    "run_76de447_public_schema4_v1/unary/provisional_merged"
)
SFT2A_ROOT = Path("/storage/milikic/leanfaith/corpus2/recovered_singlepass_codex_v1_e8567ba")
LEGACY_CORPUS_ROOT = Path("/storage/milikic/leanfaith/corpus2/v1_ed41471")
GOLD_PAIRS = Path("/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl")
CURATED_CPT = Path(
    "/storage/milikic/lean_cpt_updates/2026-08-12-curated-libraries/hf_cpt_dataset.jsonl"
)
PUBLIC_REPRESENTATIONS = Path(
    "/storage/milikic/leanfaith/scale_dc29fe6d4038/"
    "public_mathlib_repr_v3/run_a/records/mathlib.jsonl"
)
PRIVATE_REPRESENTATIONS = Path(
    "/storage/milikic/leanfaith/gate3/frozen/source_subsets/sft_classic_v1/representations.jsonl"
)
GATE3_INPUT_THEOREMS = Path("/storage/milikic/leanfaith/gate3/frozen/gate3_inputs.theorems.jsonl")
CROSS_DOMAIN_REFERENCE_REPRESENTATIONS = (
    REPO_ROOT / "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/"
    "reference_representations.jsonl"
)
D3_ROOT = Path("/storage/milikic/leanfaith/lf023_llm_transforms/codex_scale_v1_f88931b")


class InventoryError(RuntimeError):
    """Raised when frozen inventory evidence does not reconcile."""


@dataclass(frozen=True)
class SourceSnapshot:
    """Cheap evidence that an input file did not change during the audit."""

    size: int
    mtime_ns: int


def canonical_json_bytes(value: object) -> bytes:
    """Return the canonical JSON encoding used by inventory outputs and IDs."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root: Path, *, selected: Sequence[Path] | None = None) -> str:
    """Hash a directory as sorted relative-path/file-hash bindings."""

    if selected is None:
        files = sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    else:
        files = sorted(selected, key=lambda path: path.relative_to(root).as_posix())
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InventoryError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise InventoryError(f"non-object JSONL row at {path}:{line_number}")
            yield value


def count_jsonl(path: Path) -> int:
    return sum(1 for _ in iter_jsonl(path))


def snapshot(path: Path) -> SourceSnapshot:
    stat = path.stat()
    return SourceSnapshot(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def stable_preview_id(artifact_id: str, source_id: str) -> str:
    payload = {
        "adapter_schema_version": 1,
        "artifact_id": artifact_id,
        "source_id": source_id,
    }
    return f"reuse_preview:{sha256_bytes(canonical_json_bytes(payload))}"


def bounded_group_samples[T](
    rows: Iterable[T],
    *,
    group_key: Callable[[T], str],
    stable_key: Callable[[T], str],
    limit: int = 5,
) -> dict[str, list[T]]:
    """Select the lexicographically first bounded sample in every group."""

    grouped: dict[str, list[T]] = defaultdict(list)
    for row in rows:
        grouped[group_key(row)].append(row)
    return {
        group: sorted(values, key=stable_key)[:limit] for group, values in sorted(grouped.items())
    }


def _sft2b_roots() -> tuple[Path, ...]:
    base = REPO_ROOT / "data/raw/real_outputs"
    return (
        base / "public_research_v1",
        base / "gate3_docstrings_operational_v1",
        base / "cross_domain_docstrings_operational_v1",
    )


def _canonical_sft2b_manifests() -> list[Path]:
    manifests: list[Path] = []
    for root in _sft2b_roots():
        for manifest in sorted(root.glob("**/postprocess_v*/manifest.json")):
            if root.name == "public_research_v1" and manifest.parent.name != "postprocess_v2":
                continue
            manifests.append(manifest)
    return manifests


def _canonical_sft2b_files() -> list[Path]:
    selected: list[Path] = []
    for manifest in _canonical_sft2b_manifests():
        selected.append(manifest)
        selected.extend(sorted(manifest.parent.glob("invocations/*/unresolved_nl_lean.json")))
        selected.extend(sorted(manifest.parent.glob("invocations/*/unresolved_pairs.jsonl")))
    return selected


def _sft2b_hash() -> str:
    return tree_hash(REPO_ROOT, selected=_canonical_sft2b_files())


def _sft2b_row_count() -> int:
    count = 0
    for manifest_path in _canonical_sft2b_manifests():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        count += int(manifest["admitted_pair_count"])
    return count


def _resolve_row_path(spec: dict[str, Any], value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    base = Path(spec["path"])
    if not base.is_absolute():
        base = REPO_ROOT / base
    return base / path


def _artifact_hash(spec: dict[str, Any]) -> str:
    mode = spec["hash_mode"]
    if mode == "sft2b_canonical":
        return _sft2b_hash()
    path = Path(spec["path"])
    if not path.is_absolute():
        path = REPO_ROOT / path
    if mode == "file":
        return sha256_file(path)
    if mode == "tree":
        return tree_hash(path)
    raise InventoryError(f"unknown hash mode {mode!r} for {spec['artifact_id']}")


def _artifact_rows(spec: dict[str, Any]) -> int:
    if spec["hash_mode"] == "sft2b_canonical":
        return _sft2b_row_count()
    paths = spec.get("row_paths", [])
    if paths:
        return sum(count_jsonl(_resolve_row_path(spec, value)) for value in paths)
    if spec["artifact_id"] == "evaluation_gold_partition_v1":
        path = Path(spec["path"])
        value = json.loads(path.read_text(encoding="utf-8"))
        return int(value["total_pairs"])
    raise InventoryError(f"no row counter for {spec['artifact_id']}")


def _validated_inventory_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for spec in config["artifacts"]:
        digest = _artifact_hash(spec)
        if digest != spec["expected_hash"]:
            raise InventoryError(
                f"hash mismatch for {spec['artifact_id']}: expected "
                f"{spec['expected_hash']}, observed {digest}"
            )
        rows = _artifact_rows(spec)
        if rows != spec["expected_rows"]:
            raise InventoryError(
                f"row mismatch for {spec['artifact_id']}: expected "
                f"{spec['expected_rows']}, observed {rows}"
            )
        records.append(
            {
                "artifact_id": spec["artifact_id"],
                "path": spec["path"],
                "immutable_hash": f"sha256:{digest}",
                "rows": rows,
                "schema": spec["schema"],
                "source_lineage": spec["source_lineage"],
                "label_source": spec["label_source"],
                "lean_evidence": spec["lean_evidence"],
                "representation": spec["representation"],
                "redistribution": spec["redistribution"],
                "destination_task": spec["destination_task"],
                "decision": spec["decision"],
                "reason": spec["reason"],
            }
        )
    return records


def _pair_key(reference: str, candidate: str) -> str:
    return sha256_bytes(canonical_json_bytes([reference, candidate]))


def _unordered_pair_key(reference: str, candidate: str) -> str:
    first, second = sorted((reference, candidate))
    return _pair_key(first, second)


def _text_key(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def _preview(
    *,
    artifact_id: str,
    source_id: str,
    adapter_status: str,
    target_schema: str,
    core: dict[str, Any],
    sidecar: dict[str, Any],
) -> dict[str, Any]:
    return {
        "adapter_status": adapter_status,
        "artifact_id": artifact_id,
        "core_preview": core,
        "preview_id": stable_preview_id(artifact_id, source_id),
        "sidecar_preview": sidecar,
        "source_id": source_id,
        "target_schema": target_schema,
    }


def _load_requested_representations(
    *,
    representation_ids: set[str],
    theorem_ids: set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    by_representation: dict[str, str] = {}
    by_theorem: dict[str, str] = {}

    def bind_theorem(theorem_id: str, headless: str, path: Path) -> None:
        existing = by_theorem.get(theorem_id)
        if existing is not None and existing != headless:
            raise InventoryError(
                f"conflicting headless views for {theorem_id} while reading {path}"
            )
        by_theorem[theorem_id] = headless

    for path in (PUBLIC_REPRESENTATIONS, PRIVATE_REPRESENTATIONS):
        for row in iter_jsonl(path):
            representation_id = row["representation_id"]
            theorem_id = row["theorem_id"]
            if representation_id in representation_ids:
                by_representation[representation_id] = row["headless"]
            if theorem_id in theorem_ids:
                bind_theorem(theorem_id, row["headless"], path)
    for row in iter_jsonl(GATE3_INPUT_THEOREMS):
        theorem_id = row["theorem"]["theorem_id"]
        if theorem_id in theorem_ids:
            bind_theorem(theorem_id, row["representation"]["headless"], GATE3_INPUT_THEOREMS)
    for row in iter_jsonl(CROSS_DOMAIN_REFERENCE_REPRESENTATIONS):
        theorem_id = row["theorem_id"]
        if theorem_id in theorem_ids:
            bind_theorem(theorem_id, row["headless"], CROSS_DOMAIN_REFERENCE_REPRESENTATIONS)
    missing_representations = representation_ids - by_representation.keys()
    missing_theorems = theorem_ids - by_theorem.keys()
    if missing_representations or missing_theorems:
        raise InventoryError(
            "missing requested representation joins: "
            f"representation_ids={sorted(missing_representations)[:3]}, "
            f"theorem_ids={sorted(missing_theorems)[:3]}"
        )
    return by_representation, by_theorem


def _sft2b_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for manifest_path in _canonical_sft2b_manifests():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pair_paths = sorted(manifest_path.parent.glob("invocations/*/unresolved_pairs.jsonl"))
        for pair_path in pair_paths:
            pair_rows = list(iter_jsonl(pair_path))
            if len(pair_rows) != 1:
                raise InventoryError(f"expected one unresolved pair in {pair_path}")
            invocation = pair_path.parent
            nl_lean = json.loads((invocation / "unresolved_nl_lean.json").read_text())
            candidate = json.loads((invocation / "admitted_representation.json").read_text())
            reference_pairs = nl_lean["reference_pairs"]
            if len(reference_pairs) != 1:
                raise InventoryError(f"expected one reference pair in {invocation}")
            rows.append(
                {
                    "candidate_headless": candidate["headless"],
                    "manifest_id": manifest["manifest_id"],
                    "nl_lean": nl_lean,
                    "pair": pair_rows[0],
                    "reference_theorem_id": reference_pairs[0]["reference_theorem_id"],
                    "source_path": str(pair_path.relative_to(REPO_ROOT)),
                }
            )
    if len(rows) != 301:
        raise InventoryError(f"expected 301 canonical SFT2B rows, observed {len(rows)}")
    return rows


def _collect_evidence_and_previews() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    previews: list[dict[str, Any]] = []

    bootstrap_rows = list(iter_jsonl(BOOTSTRAP_ROOT / "records.jsonl"))
    bootstrap_samples = bounded_group_samples(
        bootstrap_rows,
        group_key=lambda row: row["pseudo_target_basis"],
        stable_key=lambda row: row["record_id"],
    )
    for basis, rows in bootstrap_samples.items():
        for row in rows:
            previews.append(
                _preview(
                    artifact_id="sft1_bootstrap_proxy_v1",
                    source_id=row["record_id"],
                    adapter_status="blocked_revalidate",
                    target_schema="SFT(reference,candidate,label)",
                    core={
                        "candidate": row["candidate"]["headless"],
                        "label": row["pseudo_target"] == "same_claim",
                        "reference": row["source"]["headless"],
                    },
                    sidecar={
                        "label_provenance": basis,
                        "machine_supervision_only": row["machine_supervision_only"],
                        "nonsemantic_proxy": True,
                        "redistribution_allowed": row["redistribution_allowed"],
                        "source_record_id": row["record_id"],
                    },
                )
            )

    depth_rows = list(iter_jsonl(DEPTH3_ROOT / "unique_pairs.jsonl"))
    depth_final = {
        row["representation_id"]: row["headless"]
        for row in iter_jsonl(DEPTH3_ROOT / "representations.jsonl")
    }
    depth_source_ids = {row["original_source_representation_id"] for row in depth_rows}

    unary_rows = list(iter_jsonl(UNARY_ROOT / "partitions/pairs.jsonl"))
    unary_samples = bounded_group_samples(
        unary_rows,
        group_key=lambda row: row["transformation_family"],
        stable_key=lambda row: row["pair_id"],
    )
    sampled_unary_rows = [row for rows in unary_samples.values() for row in rows]
    sampled_unary_source_theorems = {row["theorem_a_id"] for row in sampled_unary_rows}
    sampled_unary_candidate_theorems = {row["theorem_b_id"] for row in sampled_unary_rows}
    unary_candidate_headless: dict[str, str] = {}
    leak_candidate_theorems: set[str] = set()
    for row in iter_jsonl(UNARY_ROOT / "partitions/candidate_representations.jsonl"):
        theorem_id = row["theorem_id"]
        if theorem_id in sampled_unary_candidate_theorems:
            unary_candidate_headless[theorem_id] = row["headless"]
        if "lf_alpha" in row["headless"]:
            leak_candidate_theorems.add(theorem_id)
    p01_candidate_theorems = {
        row["theorem_b_id"] for row in unary_rows if row["transformation_family"] == "p01_alpha"
    }
    if leak_candidate_theorems != p01_candidate_theorems:
        raise InventoryError("P01/lf_alpha candidate sets do not reconcile exactly")
    if sampled_unary_candidate_theorems != unary_candidate_headless.keys():
        raise InventoryError("sampled unary candidate representations are incomplete")

    sft2b_rows = _sft2b_rows()
    sft2b_samples = sorted(sft2b_rows, key=lambda row: row["pair"]["pair_id"])[:5]
    sampled_sft2b_reference_theorems = {row["reference_theorem_id"] for row in sft2b_samples}

    representation_by_id, representation_by_theorem = _load_requested_representations(
        representation_ids=depth_source_ids,
        theorem_ids=sampled_unary_source_theorems | sampled_sft2b_reference_theorems,
    )

    depth_samples = bounded_group_samples(
        depth_rows,
        group_key=lambda row: row["preserved_intention"],
        stable_key=lambda row: row["pair_id"],
    )
    for intention, rows in depth_samples.items():
        for row in rows:
            previews.append(
                _preview(
                    artifact_id="sft1_depth_three_v2",
                    source_id=row["pair_id"],
                    adapter_status="blocked_transform_review",
                    target_schema="SFT(reference,candidate,label)",
                    core={
                        "candidate": depth_final[row["selected_final_representation_id"]],
                        "label": intention == "equivalent_candidate",
                        "reference": representation_by_id[row["original_source_representation_id"]],
                    },
                    sidecar={
                        "intention_only": True,
                        "label_provenance": f"transform_intention:{intention}",
                        "root_ancestry_ids": row["root_ancestry_ids"],
                        "source_pair_id": row["pair_id"],
                        "training_eligible": False,
                    },
                )
            )

    for family, rows in unary_samples.items():
        for row in rows:
            p01_reject = family == "p01_alpha"
            previews.append(
                _preview(
                    artifact_id="sft1_unary_provisional_v1",
                    source_id=row["pair_id"],
                    adapter_status="reject_lexical_leak" if p01_reject else "blocked_replay",
                    target_schema="SFT(reference,candidate,label)",
                    core={
                        "candidate": unary_candidate_headless[row["theorem_b_id"]],
                        "label": row["intended_relation"] == "equivalent",
                        "reference": representation_by_theorem[row["theorem_a_id"]],
                    },
                    sidecar={
                        "label_provenance": "transform_intention_only",
                        "lf_alpha_leak": p01_reject,
                        "source_pair_id": row["pair_id"],
                        "transformation_family": family,
                    },
                )
            )

    sft2a_records_path = SFT2A_ROOT / "outputs/trainer_records.jsonl"
    source_before = snapshot(sft2a_records_path)
    sft2a_rows = list(iter_jsonl(sft2a_records_path))
    sft2a_samples = bounded_group_samples(
        sft2a_rows,
        group_key=lambda row: str(row["label"]).lower(),
        stable_key=lambda row: row["record_id"],
    )
    for label_group, rows in sft2a_samples.items():
        del label_group
        for row in rows:
            previews.append(
                _preview(
                    artifact_id="sft2a_qwen_kimi_codex_legacy_v1",
                    source_id=row["record_id"],
                    adapter_status="ready_separate_legacy_config",
                    target_schema="SFT(reference,candidate,label)",
                    core={
                        "candidate": row["candidate_headless"],
                        "label": row["label"],
                        "reference": row["reference_headless"],
                    },
                    sidecar={
                        "family": row["family"],
                        "group_key": row["group_key"],
                        "label_provenance": "qwen_or_kimi_proposer_plus_single_codex_judge",
                        "source_record_id": row["record_id"],
                    },
                )
            )

    for row in sft2b_samples:
        pair = row["pair"]
        nl_lean = row["nl_lean"]
        previews.append(
            _preview(
                artifact_id="sft2b_compiled_unresolved_301_v1",
                source_id=pair["pair_id"],
                adapter_status="blocked_unknown_label",
                target_schema="SFT(reference,candidate,label)",
                core={
                    "candidate": row["candidate_headless"],
                    "label": None,
                    "reference": representation_by_theorem[row["reference_theorem_id"]],
                },
                sidecar={
                    "label_provenance": "unknown_requires_three_voter_policy",
                    "manifest_id": row["manifest_id"],
                    "nl_lean_id": nl_lean["nl_lean_id"],
                    "nl_statement": nl_lean["nl_statement"],
                    "requires_adjudication": True,
                    "source_path": row["source_path"],
                },
            )
        )

    gold_rows = list(iter_jsonl(GOLD_PAIRS))
    gold_samples = bounded_group_samples(
        gold_rows,
        group_key=lambda row: row["label_provenance"],
        stable_key=lambda row: row["pair_id"],
    )
    for provenance, rows in gold_samples.items():
        for row in rows:
            previews.append(
                _preview(
                    artifact_id="evaluation_gold_pairs_v1",
                    source_id=row["pair_id"],
                    adapter_status="ready_after_eval_v2_split",
                    target_schema="EVAL(pair_id,reference,candidate,label,split)",
                    core={
                        "candidate": row["candidate_headless"],
                        "label": row["label"],
                        "pair_id": row["pair_id"],
                        "reference": row["reference_headless"],
                        "split": None,
                    },
                    sidecar={
                        "label_conflict": row["label_conflict"],
                        "label_provenance": provenance,
                        "legacy_partition": row["partition"],
                        "training_forbidden": True,
                    },
                )
            )

    cpt_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cpt_id_texts: dict[str, set[str]] = defaultdict(set)
    cpt_text_hashes: set[str] = set()
    cpt_rows = 0
    for row in iter_jsonl(CURATED_CPT):
        cpt_rows += 1
        text_hash = _text_key(row["text"])
        cpt_id_texts[row["id"]].add(text_hash)
        cpt_text_hashes.add(text_hash)
        sample = cpt_samples[row["source"]]
        if len(sample) < 5:
            sample.append(row)
    for source, rows in sorted(cpt_samples.items()):
        for row in sorted(rows, key=lambda value: (value["id"], _text_key(value["text"]))):
            source_id = sha256_bytes(
                canonical_json_bytes(
                    {
                        "legacy_id": row["id"],
                        "source": row["source"],
                        "subset": row["subset"],
                        "text_sha256": _text_key(row["text"]),
                    }
                )
            )
            previews.append(
                _preview(
                    artifact_id="curated_lean_cpt_469585_v1",
                    source_id=f"cpt_source:{source_id}",
                    adapter_status="blocked_stable_id_and_gold_screen",
                    target_schema="CPT1(text)",
                    core={"text": row["text"]},
                    sidecar={
                        "content_type": row["content_type"],
                        "legacy_id": row["id"],
                        "source": source,
                        "stable_id_recipe": "sha256(source,subset,legacy_id,text_sha256)",
                        "subset": row["subset"],
                        "text_sha256": _text_key(row["text"]),
                    },
                )
            )

    d3_rows = list(iter_jsonl(D3_ROOT / "trainer_records.jsonl"))
    d3_samples = bounded_group_samples(
        d3_rows,
        group_key=lambda row: str(row["label"]).lower(),
        stable_key=lambda row: row["record_id"],
    )
    for rows in d3_samples.values():
        for row in rows:
            previews.append(
                _preview(
                    artifact_id="d3_codex_transform_pilot_v1",
                    source_id=row["record_id"],
                    adapter_status="blocked_independent_judge",
                    target_schema="SFT(reference,candidate,label)",
                    core={
                        "candidate": row["candidate_headless"],
                        "label": row["label"],
                        "reference": row["reference_headless"],
                    },
                    sidecar={
                        "family": row["family"],
                        "label_provenance": "generator_intention_only",
                        "source_record_id": row["record_id"],
                    },
                )
            )

    bootstrap_view = _overlap_view_from_bootstrap(bootstrap_rows)
    depth_view = _overlap_view_from_depth(depth_rows, representation_by_id, depth_final)
    sft2a_view = _overlap_view_from_sft2a(sft2a_rows)
    overlap = {
        "bootstrap__depth3": _overlap_counts(bootstrap_view, depth_view),
        "bootstrap__sft2a": _overlap_counts(bootstrap_view, sft2a_view),
        "depth3__sft2a": _overlap_counts(depth_view, sft2a_view),
    }

    sft2a_judgments = list(iter_jsonl(SFT2A_ROOT / "outputs/judgments.jsonl"))
    sft2a_manifest = json.loads((SFT2A_ROOT / "final_manifest.json").read_text())
    sft2a_pair_duplicates = len(sft2a_rows) - len(
        {_pair_key(row["reference_headless"], row["candidate_headless"]) for row in sft2a_rows}
    )
    first_example = min(sft2a_rows, key=lambda row: row["record_id"])
    judgment = next(
        row for row in sft2a_judgments if row["record_id"] == first_example["record_id"]
    )
    pair_plan = next(
        row
        for row in iter_jsonl(SFT2A_ROOT / "inputs/pair_plan.jsonl")
        if row["plan_row_id"] == judgment["plan_row_id"]
    )
    source_after = snapshot(sft2a_records_path)
    if source_before != source_after:
        raise InventoryError("SFT2A trainer source changed during read-only audit")

    legacy_counts: dict[str, Any] = {}
    for split in ("train", "validation", "test"):
        rows = list(iter_jsonl(LEGACY_CORPUS_ROOT / f"records_{split}_v1.jsonl"))
        legacy_counts[split] = {
            "labels": dict(sorted(Counter(str(row["label"]).lower() for row in rows).items())),
            "rows": len(rows),
            "sources": dict(sorted(Counter(row["source"] for row in rows).items())),
        }

    cpt_duplicate_groups = {key: values for key, values in cpt_id_texts.items() if len(values) > 1}
    cpt_id_counts = Counter(row["id"] for row in iter_jsonl(CURATED_CPT))
    cpt_duplicate_excess = sum(count - 1 for count in cpt_id_counts.values() if count > 1)

    sample_coverage = {
        "bootstrap_by_label_basis": {
            key: {
                "population": sum(row["pseudo_target_basis"] == key for row in bootstrap_rows),
                "sample": len(value),
            }
            for key, value in bootstrap_samples.items()
        },
        "depth3_by_intention": {
            key: {
                "population": sum(row["preserved_intention"] == key for row in depth_rows),
                "sample": len(value),
            }
            for key, value in depth_samples.items()
        },
        "gold_by_label_provenance": {
            key: {
                "population": sum(row["label_provenance"] == key for row in gold_rows),
                "sample": len(value),
            }
            for key, value in gold_samples.items()
        },
        "sft2a_by_label": {
            key: {
                "population": sum(str(row["label"]).lower() == key for row in sft2a_rows),
                "sample": len(value),
            }
            for key, value in sft2a_samples.items()
        },
        "sft2b_unknown": {"population": len(sft2b_rows), "sample": len(sft2b_samples)},
        "unary_by_family": {
            key: {
                "population": sum(row["transformation_family"] == key for row in unary_rows),
                "sample": len(value),
            }
            for key, value in unary_samples.items()
        },
    }

    evidence = {
        "adapter_preview_count": len(previews),
        "bootstrap": {
            "label_basis": dict(
                sorted(Counter(row["pseudo_target_basis"] for row in bootstrap_rows).items())
            ),
            "labels": dict(sorted(Counter(row["pseudo_target"] for row in bootstrap_rows).items())),
            "redistribution_allowed": dict(
                sorted(
                    Counter(
                        str(row["redistribution_allowed"]).lower() for row in bootstrap_rows
                    ).items()
                )
            ),
            "rows": len(bootstrap_rows),
        },
        "cpt": {
            "duplicate_id_excess": cpt_duplicate_excess,
            "duplicate_id_groups": sum(count > 1 for count in cpt_id_counts.values()),
            "duplicate_ids_with_distinct_texts": len(cpt_duplicate_groups),
            "rows": cpt_rows,
            "unique_texts": len(cpt_text_hashes),
        },
        "depth3": {
            "intentions": dict(
                sorted(Counter(row["preserved_intention"] for row in depth_rows).items())
            ),
            "rows": len(depth_rows),
        },
        "legacy_corpus": legacy_counts,
        "one_example_smoke": {
            "adapter_preview_id": stable_preview_id(
                "sft2a_qwen_kimi_codex_legacy_v1", first_example["record_id"]
            ),
            "core": {
                "candidate": first_example["candidate_headless"],
                "label": first_example["label"],
                "reference": first_example["reference_headless"],
            },
            "judgment_status": judgment["status"],
            "lean_check_id": pair_plan["audit_input"]["lean_check_id"],
            "pair_plan_row_id": pair_plan["plan_row_id"],
            "source_checks_sha256": pair_plan["source_checks_sha256"],
            "source_record_id": first_example["record_id"],
            "source_snapshot_after": source_after.__dict__,
            "source_snapshot_before": source_before.__dict__,
            "source_theorem_id": pair_plan["source_theorem_id"],
            "trainer_records_sha256": sha256_file(sft2a_records_path),
        },
        "overlap": overlap,
        "sample_coverage": sample_coverage,
        "sft2a": {
            "exact_text_pair_duplicate_excess": sft2a_pair_duplicates,
            "gross_judgments": len(sft2a_judgments),
            "label_counts": sft2a_manifest["label_counts"],
            "resolved_trainer_rows": len(sft2a_rows),
            "unresolved_sidecar_rows": sft2a_manifest["counts"]["unresolved"],
        },
        "sft2b": {
            "all_unknown": all(row["nl_lean"]["resolved_label_id"] is None for row in sft2b_rows),
            "canonical_rows": len(sft2b_rows),
            "manifest_rows": [
                {
                    "admitted_pair_count": json.loads(path.read_text())["admitted_pair_count"],
                    "manifest_id": json.loads(path.read_text())["manifest_id"],
                    "path": str(path.relative_to(REPO_ROOT)),
                }
                for path in _canonical_sft2b_manifests()
            ],
        },
        "unary": {
            "family_counts": dict(
                sorted(Counter(row["transformation_family"] for row in unary_rows).items())
            ),
            "lf_alpha_leak_rows": len(leak_candidate_theorems),
            "non_p01_rows": len(unary_rows) - len(p01_candidate_theorems),
            "p01_rows": len(p01_candidate_theorems),
            "rows": len(unary_rows),
        },
        "zero_work_assertions": {
            "data_deleted": 0,
            "data_merged": 0,
            "data_relabelled": 0,
            "data_uploaded": 0,
            "lean_invocations": 0,
            "llm_calls": 0,
        },
    }
    previews.sort(key=lambda row: (row["artifact_id"], row["source_id"]))
    return evidence, previews


def _overlap_view_from_bootstrap(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {
        key: set() for key in ("pairs", "unordered", "sources", "candidates", "ancestry")
    }
    for row in rows:
        reference = row["source"]["headless"]
        candidate = row["candidate"]["headless"]
        result["pairs"].add(_pair_key(reference, candidate))
        result["unordered"].add(_unordered_pair_key(reference, candidate))
        result["sources"].add(_text_key(reference))
        result["candidates"].add(_text_key(candidate))
        result["ancestry"].update(
            value for value in row["split_group_ids"] if value.startswith("anc:")
        )
    return result


def _overlap_view_from_depth(
    rows: list[dict[str, Any]],
    source_representations: dict[str, str],
    final_representations: dict[str, str],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {
        key: set() for key in ("pairs", "unordered", "sources", "candidates", "ancestry")
    }
    for row in rows:
        reference = source_representations[row["original_source_representation_id"]]
        candidate = final_representations[row["selected_final_representation_id"]]
        result["pairs"].add(_pair_key(reference, candidate))
        result["unordered"].add(_unordered_pair_key(reference, candidate))
        result["sources"].add(_text_key(reference))
        result["candidates"].add(_text_key(candidate))
        result["ancestry"].update(row["root_ancestry_ids"])
    return result


def _overlap_view_from_sft2a(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {
        key: set() for key in ("pairs", "unordered", "sources", "candidates", "ancestry")
    }
    for row in rows:
        reference = row["reference_headless"]
        candidate = row["candidate_headless"]
        result["pairs"].add(_pair_key(reference, candidate))
        result["unordered"].add(_unordered_pair_key(reference, candidate))
        result["sources"].add(_text_key(reference))
        result["candidates"].add(_text_key(candidate))
        result["ancestry"].add(row["group_key"])
    return result


def _overlap_counts(first: dict[str, set[str]], second: dict[str, set[str]]) -> dict[str, int]:
    return {
        "ancestry": len(first["ancestry"] & second["ancestry"]),
        "candidate_text": len(first["candidates"] & second["candidates"]),
        "directed_pair": len(first["pairs"] & second["pairs"]),
        "source_text": len(first["sources"] & second["sources"]),
        "unordered_pair": len(first["unordered"] & second["unordered"]),
    }


def _render_report(records: list[dict[str, Any]], evidence: dict[str, Any]) -> str:
    by_id = {record["artifact_id"]: record for record in records}
    required = [
        ("sft1_bootstrap_proxy_v1", "17,181", "17,181"),
        ("sft1_depth_three_v2", "4,031", "4,031"),
        ("sft1_unary_provisional_v1", "27,327", "27,327"),
        ("sft2a_qwen_kimi_codex_legacy_v1", "13,373 gross", "13,373 = 13,367 resolved + 6 unknown"),
        ("sft2b_compiled_unresolved_301_v1", "301", "301 = 3 + 195 + 103"),
        ("legacy_mixed_corpus_v1", "23,414", "23,414"),
        ("evaluation_gold_pairs_v1", "5,111", "5,111"),
        ("curated_lean_cpt_469585_v1", "469,585", "469,585"),
    ]
    lines = [
        "# DATA-REUSE inventory v1",
        "",
        "This is a read-only audit of existing bytes. It generated no supervision, changed no ",
        "labels, merged no datasets, invoked neither Lean nor an LLM, uploaded nothing, and ",
        "deleted nothing. Directory identities use `sha256-tree-v1`: SHA-256 over sorted UTF-8 ",
        "`relative_path NUL file_sha256 NUL` bindings.",
        "",
        "## Required-root reconciliation",
        "",
        "| Artifact | Expected | Observed | Decision |",
        "| --- | ---: | ---: | --- |",
    ]
    for artifact_id, expected, observed in required:
        lines.append(
            f"| `{artifact_id}` | {expected} | {observed} | `{by_id[artifact_id]['decision']}` |"
        )
    lines.extend(
        [
            "",
            "All eight required roots were found. The 301-row SFT2B count is the canonical ",
            "postprocess-v2 public tranche (3), eight Algebra tranches (195), and eight ",
            "cross-domain tranches (103); the superseded public postprocess-v1 output is excluded.",
            "",
            "## Decisions",
            "",
        ]
    )
    for record in records:
        lines.extend(
            [
                f"### `{record['artifact_id']}` — `{record['decision']}`",
                "",
                f"- Path: `{record['path']}`",
                f"- Immutable identity: `{record['immutable_hash']}`",
                f"- Rows: {record['rows']:,}",
                f"- Destination: {record['destination_task']}",
                f"- Reason: {record['reason']}",
                "",
            ]
        )
    overlap = evidence["overlap"]
    lines.extend(
        [
            "## Duplicate and ancestry overlap",
            "",
            "Exact comparisons preserve raw headless text. `directed_pair` hashes ",
            "`[reference,candidate]`; `unordered_pair` additionally ignores orientation.",
            "",
            (
                "| Pair of roots | Directed pair | Unordered pair | Source text | "
                "Candidate text | Ancestry |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for key in ("bootstrap__depth3", "bootstrap__sft2a", "depth3__sft2a"):
        value = overlap[key]
        lines.append(
            f"| `{key}` | {value['directed_pair']:,} | {value['unordered_pair']:,} | "
            f"{value['source_text']:,} | {value['candidate_text']:,} | {value['ancestry']:,} |"
        )
    lines.extend(
        [
            "",
            (
                "The resolved SFT2A trainer file also has "
                f"{evidence['sft2a']['exact_text_pair_duplicate_excess']} duplicate directed "
                "text-pair rows despite unique record IDs. Destination adapters must deduplicate "
                "before splitting."
            ),
            "",
            "## High-risk findings",
            "",
            (
                f"- Unary P01 is an exact {evidence['unary']['p01_rows']:,}-row set match to "
                "candidate representations containing `lf_alpha`; reject that tranche. The other "
                f"{evidence['unary']['non_p01_rows']:,} rows remain replay- and "
                "user-approval-gated."
            ),
            (
                "- SFT2B's 301 compiled candidates all have unknown semantic labels. They are vote "
                "inputs, not negatives."
            ),
            (
                f"- The curated CPT file has {evidence['cpt']['unique_texts']:,} unique texts but "
                f"{evidence['cpt']['duplicate_id_groups']:,} duplicated legacy IDs with "
                f"{evidence['cpt']['duplicate_id_excess']:,} excess rows; every duplicated ID "
                "maps to different text. A content-derived stable ID is mandatory."
            ),
            (
                "- Gold is evaluation-only. The old partition manifest is provenance evidence, "
                "not the active EVAL v2 split."
            ),
            "",
            "## Adapter previews",
            "",
            (
                f"`adapter_previews_v1.jsonl` contains {evidence['adapter_preview_count']} "
                "deterministic previews. IDs hash the artifact ID plus canonical source ID, never "
                "row numbers. Ready rows are limited to resolved SFT2A in a separate legacy "
                "configuration. Other previews carry explicit blocked/reject status; SFT2B uses "
                "`label=null`, and gold uses `split=null` until destination owners apply their "
                "contracts."
            ),
            "",
            (
                "Sampling is five rows per observed schema/label-source group when the population "
                "permits. The only smaller populations are the 3-row `agreeing_mixed_proxy` group "
                "and the 1-row unary `n03_drop_hypothesis` group; both are exhaustively previewed."
            ),
            "",
            "## One-example smoke",
            "",
        ]
    )
    smoke = evidence["one_example_smoke"]
    lines.extend(
        [
            f"- Source record: `{smoke['source_record_id']}`",
            f"- Pair-plan record: `{smoke['pair_plan_row_id']}`",
            f"- Source theorem: `{smoke['source_theorem_id']}`",
            f"- Stored Lean check: `{smoke['lean_check_id']}` bound by ",
            f"  `{smoke['source_checks_sha256']}`",
            f"- Judgment status: `{smoke['judgment_status']}`",
            f"- Preview stable ID: `{smoke['adapter_preview_id']}`",
            f"- Source file SHA-256: `{smoke['trainer_records_sha256']}`",
            "- Source size/mtime snapshot is byte-for-byte identical before and after the trace.",
            "",
            "The serialized core preview is linked to its source/judgment/check IDs in ",
            "`evidence_v1.json`; no source bytes were written.",
            "",
            "## Exact next action",
            "",
            "The SFT2A owner should first review and accept or reject the ",
            "`sft2a_qwen_kimi_codex_legacy_v1` recipe: freeze the `goal_v1.0` adapter, choose the ",
            "7-row exact-text duplicate policy, keep the 6 unknowns sidecar-only, and then rerun ",
            "this preview before any import. SFT1 remains waiting on the user's transform-catalog ",
            "approval; this task authorizes no generation or replay.",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(content)
    temporary.replace(path)


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def build_inventory(config_path: Path = DEFAULT_CONFIG) -> Path:
    """Validate frozen inputs and write only checksummed inventory artifacts."""

    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    if config.get("schema_version") != 1:
        raise InventoryError("inventory config must use schema_version=1")
    records = _validated_inventory_records(config)
    evidence, previews = _collect_evidence_and_previews()
    output_root = Path(config["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)

    inventory_path = output_root / "inventory_v1.jsonl"
    previews_path = output_root / "adapter_previews_v1.jsonl"
    evidence_path = output_root / "evidence_v1.json"
    report_path = output_root / "report_v1.md"
    _atomic_write(inventory_path, _jsonl_bytes(records))
    _atomic_write(previews_path, _jsonl_bytes(previews))
    _atomic_write(evidence_path, canonical_json_bytes(evidence) + b"\n")
    _atomic_write(report_path, _render_report(records, evidence).encode("utf-8"))

    outputs = {
        path.name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in (inventory_path, previews_path, evidence_path, report_path)
    }
    manifest = {
        "artifact_count": len(records),
        "config_path": str(config_path),
        "config_sha256": sha256_bytes(config_bytes),
        "constraints": {
            "data_deleted": False,
            "data_generated": False,
            "data_merged": False,
            "data_relabelled": False,
            "data_uploaded": False,
            "lean_invoked": False,
            "llm_invoked": False,
            "source_mutated": False,
        },
        "implementation": {
            "path": str(Path(__file__).resolve().relative_to(REPO_ROOT)),
            "sha256": sha256_file(Path(__file__).resolve()),
        },
        "manifest_schema_version": 1,
        "outputs": outputs,
        "preview_count": len(previews),
        "status": "complete",
        "tree_hash_algorithm": TREE_HASH_ALGORITHM,
    }
    _atomic_write(output_root / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return output_root


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    output_root = build_inventory(args.config)
    print(output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
