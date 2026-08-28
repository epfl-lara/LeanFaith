"""`leanfaith-eval` console script (refocus Track A).

Plain run manifests only: config + seed + git revision + input/output hashes.
No gate or attestation machinery.
"""

from __future__ import annotations

import datetime
import json
import platform
import subprocess
from pathlib import Path
from typing import Annotated, Any

import typer

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.eval.ingest import (
    build_canonical_pairs,
    load_beq,
    load_epla,
    load_gted,
    load_proofnetverif,
)
from leanfaith.eval.partition import assign_partitions, build_blocklist, partition_counts
from leanfaith.eval.schema import GoldenPair, PartitionManifest

app = typer.Typer(no_args_is_help=True, add_completion=False)

_DEFAULT_RAW = Path("/storage/milikic/leanfaith/golden/raw")
_DEFAULT_GTED = Path("/localhome/milikic/lean_theorem_equivalence/GTED/experiment")
_DEFAULT_PNV = Path(
    "/storage/milikic/leanfaith/hf_cache/hub/datasets--PAug--ProofNetVerif/snapshots/"
    "91183e5b12d64374827bf2782db629b5b0f8f319"
)
_EPLA_SHA = "bc7933547d8a6d1aaee41ccf56d68bc1f0fc575d"
_BEQ_SHA = "5ce3b814a5d0213429cc92244e5467425b22297a"


def _git_revision(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def _write_run_manifest(out_dir: Path, name: str, payload: dict[str, Any]) -> None:
    manifest = {
        "command": name,
        "created_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "git_revision": _git_revision(Path(__file__).resolve().parents[3]),
        "python": platform.python_version(),
        **payload,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}_run_manifest.json").write_bytes(canonical_json_bytes(manifest))


def _write_pairs(pairs: list[GoldenPair], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for pair in pairs:
            stream.write(json.dumps(pair.model_dump(), ensure_ascii=False, sort_keys=True))
            stream.write("\n")
    return hash_file(path)


def load_pairs(path: Path) -> list[GoldenPair]:
    pairs: list[GoldenPair] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                pairs.append(GoldenPair.model_validate_json(line))
    return pairs


@app.command("ingest-golden")
def ingest_golden(
    raw_root: Annotated[Path, typer.Option()] = _DEFAULT_RAW,
    gted_root: Annotated[Path, typer.Option()] = _DEFAULT_GTED,
    proofnetverif_snapshot: Annotated[Path, typer.Option()] = _DEFAULT_PNV,
    out: Annotated[Path, typer.Option()] = Path(
        "/storage/milikic/leanfaith/golden/canonical/golden_pairs_unpartitioned_v1.jsonl"
    ),
) -> None:
    """Ingest EPLA + BEq + GTED + ProofNetVerif into canonical pairs."""

    epla_dir = raw_root / "epla" / _EPLA_SHA
    beq_dir = raw_root / "beq" / _BEQ_SHA
    rows = []
    rows += load_epla(epla_dir / "EPLA-miniF2F.json", epla_dir / "EPLA-ProofNet.json")
    rows += load_beq(beq_dir, beq_dir / "proofnet" / "benchmark.jsonl")
    rows += load_gted(gted_root)
    rows += load_proofnetverif(proofnetverif_snapshot)
    pairs = build_canonical_pairs(rows)
    digest = _write_pairs(pairs, out)
    per_dataset: dict[str, int] = {}
    for pair in pairs:
        for membership in pair.memberships:
            per_dataset[membership.dataset] = per_dataset.get(membership.dataset, 0) + 1
    conflicts = sum(1 for pair in pairs if pair.label_conflict)
    _write_run_manifest(
        out.parent,
        "ingest_golden",
        {
            "raw_rows": len(rows),
            "canonical_pairs": len(pairs),
            "membership_counts": per_dataset,
            "label_conflicts": conflicts,
            "output": {"path": str(out), "sha256": digest},
        },
    )
    typer.echo(
        f"rows={len(rows)} canonical_pairs={len(pairs)} conflicts={conflicts} "
        f"memberships={json.dumps(per_dataset, sort_keys=True)} -> {out}"
    )


@app.command("partition-golden")
def partition_golden(
    pairs_path: Annotated[Path, typer.Option()] = Path(
        "/storage/milikic/leanfaith/golden/canonical/golden_pairs_unpartitioned_v1.jsonl"
    ),
    seed: Annotated[int, typer.Option()] = 20260828,
    out_pairs: Annotated[Path, typer.Option()] = Path(
        "/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl"
    ),
    manifest_out: Annotated[Path, typer.Option()] = Path(
        "data/benchmarks/golden_partition_v1.json"
    ),
    blocklist_out: Annotated[Path, typer.Option()] = Path(
        "data/benchmarks/golden_blocklist_v1.json"
    ),
) -> None:
    """Freeze the group-first stratified partition + contamination blocklist."""

    pairs = load_pairs(pairs_path)
    result = assign_partitions(pairs, seed=seed)
    digest = _write_pairs(result.pairs, out_pairs)
    counts = partition_counts(result.pairs)
    manifest = PartitionManifest(
        seed=seed,
        created_utc=datetime.datetime.now(datetime.UTC).isoformat(),
        git_revision=_git_revision(Path(__file__).resolve().parents[3]),
        group_partitions=dict(sorted(result.group_partitions.items())),
        counts=counts,
        canonical_pairs_sha256=digest,
        canonical_pairs_path=str(out_pairs),
        total_pairs=len(result.pairs),
        conflicted_pairs=sum(1 for pair in result.pairs if pair.label_conflict),
    )
    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.write_bytes(canonical_json_bytes(manifest.model_dump()))
    blocklist = build_blocklist(result.pairs)
    blocklist_out.write_bytes(canonical_json_bytes(blocklist))
    _write_run_manifest(
        out_pairs.parent,
        "partition_golden",
        {
            "seed": seed,
            "input": {"path": str(pairs_path), "sha256": hash_file(pairs_path)},
            "output": {"path": str(out_pairs), "sha256": digest},
            "manifest": {
                "path": str(manifest_out),
                "sha256": sha256_hex(manifest_out.read_bytes()),
            },
            "blocklist": {
                "path": str(blocklist_out),
                "sha256": sha256_hex(blocklist_out.read_bytes()),
                "hashes": len(blocklist["near_dup_hashes"]),
            },
            "counts": counts,
        },
    )
    typer.echo(json.dumps(counts, indent=2, sort_keys=True))
    typer.echo(f"partition manifest -> {manifest_out}; blocklist -> {blocklist_out}")


def main() -> None:
    app()


_DEFAULT_CHECKPOINT = Path(
    "/storage/milikic/leanfaith/m1_proxy_training/"
    "firsthop_kimi_qwen_composition_8d815af_v1/model.safetensors"
)
_DEFAULT_SNAPSHOT = Path(
    "/storage/milikic/models/hub/models--answerdotai--ModernBERT-base/snapshots/"
    "8949b909ec900327062f0ebf497f51aef5e6f0c8"
)


def _subset_metrics(
    pairs: list[GoldenPair],
    scores: list[Any],
    indices: list[int],
    threshold: float,
) -> dict[str, Any] | None:
    from leanfaith.eval.metrics import coverage_aware_summary

    if not indices:
        return None
    subset_scores = [scores[i] for i in indices]
    subset_labels = [pairs[i].label for i in indices]
    if all(score.abstained for score in subset_scores):
        return None
    summary = dict(coverage_aware_summary(subset_scores, subset_labels, threshold))
    summary["n_pairs"] = len(indices)
    summary["prevalence"] = sum(subset_labels) / len(subset_labels)
    return summary


@app.command("evaluate")
def evaluate(
    checkpoint: Annotated[Path, typer.Option()] = _DEFAULT_CHECKPOINT,
    snapshot: Annotated[Path, typer.Option()] = _DEFAULT_SNAPSHOT,
    pairs_path: Annotated[Path, typer.Option()] = Path(
        "/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl"
    ),
    partition: Annotated[str, typer.Option()] = "dev",
    unseal_final_test: Annotated[bool, typer.Option("--unseal-final-test")] = False,
    threshold: Annotated[float, typer.Option()] = 0.5,
    batch_size: Annotated[int, typer.Option()] = 32,
    device: Annotated[str, typer.Option()] = "cuda",
    label: Annotated[str, typer.Option(help="Run label used in the output dir name.")] = "m1",
    out_root: Annotated[Path, typer.Option()] = Path("/storage/milikic/leanfaith/golden/eval_runs"),
) -> None:
    """Score one checkpoint on a golden partition (strict zero-shot track)."""

    from leanfaith.eval.m1_runtime import load_m1_scorer, score_pairs
    from leanfaith.eval.metrics import compute_classification_metrics, group_bootstrap_ci
    from leanfaith.representations.views import signature_near_dup_hash

    if partition == "final_test" and not unseal_final_test:
        raise typer.BadParameter(
            "final_test is SEALED until the frozen comparison set is ready "
            "(PLAN.md Track A); pass --unseal-final-test only for that one run."
        )
    all_pairs = load_pairs(pairs_path)
    pairs = [pair for pair in all_pairs if pair.partition == partition]
    if not pairs:
        raise typer.BadParameter(f"no pairs in partition {partition!r}")
    scorer = load_m1_scorer(checkpoint, snapshot, device)
    scores = score_pairs(
        scorer, [(p.reference_headless, p.candidate_headless) for p in pairs], batch_size
    )

    headline = [
        i
        for i, pair in enumerate(pairs)
        if not pair.label_conflict
        and pair.label_provenance == "expert_human"
        and any(m.dataset != "proofnetverif" for m in pair.memberships)
    ]
    breakdowns: dict[str, Any] = {
        "headline_expert": _subset_metrics(pairs, scores, headline, threshold),
        "all_non_conflicted": _subset_metrics(
            pairs,
            scores,
            [i for i, p in enumerate(pairs) if not p.label_conflict],
            threshold,
        ),
    }
    for dataset in sorted({m.dataset for p in pairs for m in p.memberships}):
        indices = [
            i
            for i, p in enumerate(pairs)
            if not p.label_conflict and any(m.dataset == dataset for m in p.memberships)
        ]
        breakdowns[f"dataset:{dataset}"] = _subset_metrics(pairs, scores, indices, threshold)

    scored_headline = [i for i in headline if scores[i].probability is not None]
    ci: dict[str, Any] = {}
    if scored_headline:
        labels = [pairs[i].label for i in scored_headline]
        probs = [
            probability
            for i in scored_headline
            if (probability := scores[i].probability) is not None
        ]
        groups = [pairs[i].group_key for i in scored_headline]

        def _balanced_accuracy(y: Any, p: Any) -> float:
            return float(compute_classification_metrics(y, p, threshold)["balanced_accuracy"])

        def _accuracy(y: Any, p: Any) -> float:
            return float(compute_classification_metrics(y, p, threshold)["accuracy"])

        for name, fn in (("balanced_accuracy", _balanced_accuracy), ("accuracy", _accuracy)):
            point, lo, hi = group_bootstrap_ci(
                labels, probs, groups, fn, n_boot=1000, seed=20260828
            )
            ci[name] = {"point": point, "lo95": lo, "hi95": hi}

    majority = sum(pairs[i].label for i in headline) / max(len(headline), 1)
    identity_correct = sum(
        1
        for i in headline
        if (
            signature_near_dup_hash(pairs[i].reference_headless)
            == signature_near_dup_hash(pairs[i].candidate_headless)
        )
        == pairs[i].label
    )
    baselines = {
        "always_majority_accuracy": max(majority, 1.0 - majority),
        "identity_match_accuracy": identity_correct / max(len(headline), 1),
    }

    checkpoint_digest = hash_file(checkpoint)
    out_dir = out_root / f"{partition}_{label}_{checkpoint_digest[:12]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "predictions.jsonl").open("w", encoding="utf-8") as stream:
        for pair, score in zip(pairs, scores, strict=True):
            stream.write(
                json.dumps(
                    {
                        "pair_id": pair.pair_id,
                        "group_key": pair.group_key,
                        "datasets": sorted({m.dataset for m in pair.memberships}),
                        "label": pair.label,
                        "label_conflict": pair.label_conflict,
                        "label_provenance": pair.label_provenance,
                        "probability": score.probability,
                        "abstained": score.abstained,
                        "token_length": score.token_length,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    metrics_payload = {
        "partition": partition,
        "threshold": threshold,
        "track": "strict_zero_shot",
        "breakdowns": breakdowns,
        "bootstrap_ci_headline": ci,
        "trivial_baselines": baselines,
    }
    (out_dir / "metrics.json").write_bytes(canonical_json_bytes(metrics_payload))
    _write_run_manifest(
        out_dir,
        "evaluate",
        {
            "checkpoint": {"path": str(checkpoint), "sha256": checkpoint_digest},
            "pairs": {"path": str(pairs_path), "sha256": hash_file(pairs_path)},
            "partition": partition,
            "threshold": threshold,
            "batch_size": batch_size,
            "device": device,
        },
    )
    typer.echo(json.dumps(breakdowns["headline_expert"], indent=2, sort_keys=True))
    typer.echo(json.dumps({"ci": ci, "baselines": baselines}, indent=2, sort_keys=True))
    typer.echo(f"full results -> {out_dir}")
