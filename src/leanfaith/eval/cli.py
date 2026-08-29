"""`leanfaith-eval` console script (refocus Track A).

Plain run manifests only: config + seed + git revision + input/output hashes.
No gate or attestation machinery.
"""

from __future__ import annotations

import datetime
import json
import math
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

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
from leanfaith.eval.schema import EvalPrediction, GoldenPair, PartitionManifest

app = typer.Typer(no_args_is_help=True, add_completion=False)

_DEFAULT_RAW = Path("/storage/milikic/leanfaith/golden/raw")
_DEFAULT_GTED = Path("/localhome/milikic/lean_theorem_equivalence/GTED/experiment")
_DEFAULT_PNV = Path(
    "/storage/milikic/leanfaith/hf_cache/hub/datasets--PAug--ProofNetVerif/snapshots/"
    "91183e5b12d64374827bf2782db629b5b0f8f319"
)
_EPLA_SHA = "bc7933547d8a6d1aaee41ccf56d68bc1f0fc575d"
_BEQ_SHA = "5ce3b814a5d0213429cc92244e5467425b22297a"
_FROZEN_PARTITION_MANIFEST = (
    Path(__file__).resolve().parents[3] / "data/benchmarks/golden_partition_v1.json"
)
_MIXED_CANONICAL_PAIRS = Path("/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl")
_GOLDEN_SPLIT_SCHEMA_VERSION = 1
_SNAPSHOT_PROVENANCE_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)


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


def _snapshot_provenance(snapshot: Path) -> dict[str, Any]:
    files = {
        name: hash_file(path)
        for name in _SNAPSHOT_PROVENANCE_FILES
        if (path := snapshot / name).is_file()
    }
    if not files:
        raise typer.BadParameter(f"snapshot has no recognized config/tokenizer files: {snapshot}")
    return {"path": str(snapshot), "files": files}


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
    pairs_path: Annotated[
        Path | None,
        typer.Option(help="Explicit trusted split-only golden pair JSONL."),
    ] = None,
    split_manifest_path: Annotated[
        Path | None,
        typer.Option(
            "--split-manifest",
            help="Sidecar binding the split file to the frozen canonical parent.",
        ),
    ] = None,
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
    if pairs_path is None or split_manifest_path is None:
        raise typer.BadParameter(
            "evaluate requires explicit --pairs-path and --split-manifest split-only inputs"
        )
    pairs, split_contract = _load_trusted_split_pairs(
        pairs_path,
        split_manifest_path,
        partition,
    )
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
                        "partition": partition,
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
    predictions_out = out_dir / "predictions.jsonl"
    metrics_out = out_dir / "metrics.json"
    metrics_out.write_bytes(canonical_json_bytes(metrics_payload))
    _write_run_manifest(
        out_dir,
        "evaluate",
        {
            "checkpoint": {"path": str(checkpoint), "sha256": checkpoint_digest},
            "snapshot": _snapshot_provenance(snapshot),
            "pairs": {"path": str(pairs_path), "sha256": hash_file(pairs_path)},
            "split_manifest": {
                "path": str(split_manifest_path),
                "sha256": hash_file(split_manifest_path),
            },
            "split_contract": split_contract,
            "frozen_partition_manifest": {
                "path": str(_FROZEN_PARTITION_MANIFEST),
                "sha256": hash_file(_FROZEN_PARTITION_MANIFEST),
            },
            "partition": partition,
            "threshold": threshold,
            "batch_size": batch_size,
            "device": device,
            "outputs": {
                "predictions": {
                    "path": str(predictions_out),
                    "sha256": hash_file(predictions_out),
                },
                "metrics": {"path": str(metrics_out), "sha256": hash_file(metrics_out)},
            },
        },
    )
    typer.echo(json.dumps(breakdowns["headline_expert"], indent=2, sort_keys=True))
    typer.echo(json.dumps({"ci": ci, "baselines": baselines}, indent=2, sort_keys=True))
    typer.echo(f"full results -> {out_dir}")


@dataclass(frozen=True, slots=True)
class _PostHocScore:
    probability: float | None
    abstained: bool


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise typer.BadParameter(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise typer.BadParameter(f"expected one JSON object in {path}")
    return cast(dict[str, Any], value)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_trusted_split_pairs(
    pairs_path: Path,
    split_manifest_path: Path,
    partition: str,
) -> tuple[list[GoldenPair], dict[str, Any]]:
    """Hash-check and parse one complete golden split without touching the mixed file."""

    if pairs_path.resolve() == _MIXED_CANONICAL_PAIRS.resolve():
        raise typer.BadParameter("refusing the mixed canonical golden pair path")

    frozen = _load_json_object(_FROZEN_PARTITION_MANIFEST)
    known_mixed_sha256 = frozen.get("canonical_pairs_sha256")
    if not _is_sha256(known_mixed_sha256):
        raise typer.BadParameter("frozen partition manifest has an invalid canonical pairs hash")

    contract = _load_json_object(split_manifest_path)
    required_fields = {
        "schema_version",
        "parent_canonical_sha256",
        "split_sha256",
        "partition",
        "row_count",
        "group_count",
    }
    if set(contract) != required_fields or contract.get("schema_version") != (
        _GOLDEN_SPLIT_SCHEMA_VERSION
    ):
        raise typer.BadParameter("invalid golden split sidecar schema")
    if contract.get("parent_canonical_sha256") != known_mixed_sha256:
        raise typer.BadParameter(
            "golden split parent hash does not match the frozen canonical hash"
        )
    split_sha256 = contract.get("split_sha256")
    if not _is_sha256(split_sha256):
        raise typer.BadParameter("golden split sidecar has an invalid split hash")
    if split_sha256 == known_mixed_sha256:
        raise typer.BadParameter("refusing the mixed canonical golden pair hash")
    if contract.get("partition") != partition:
        raise typer.BadParameter(
            "golden split sidecar partition does not match the requested split"
        )
    for field_name in ("row_count", "group_count"):
        value = contract.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise typer.BadParameter(f"golden split sidecar has invalid {field_name}")

    try:
        pair_bytes = pairs_path.read_bytes()
    except OSError as error:
        raise typer.BadParameter(f"cannot read golden split {pairs_path}: {error}") from error
    actual_sha256 = sha256_hex(pair_bytes)
    if actual_sha256 == known_mixed_sha256:
        raise typer.BadParameter("refusing the mixed canonical golden pair hash")
    if actual_sha256 != split_sha256:
        raise typer.BadParameter("golden split file hash does not match its sidecar")

    pairs: list[GoldenPair] = []
    try:
        text = pair_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise typer.BadParameter(f"golden split is not UTF-8: {error}") from error
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            pair = GoldenPair.model_validate_json(line)
        except ValueError as error:
            raise typer.BadParameter(
                f"invalid golden pair at {pairs_path}:{line_number}: {error}"
            ) from error
        if pair.partition != partition:
            raise typer.BadParameter(
                f"golden split row {line_number} has partition {pair.partition!r}, "
                f"expected {partition!r}"
            )
        pairs.append(pair)

    if len(pairs) != contract["row_count"]:
        raise typer.BadParameter("golden split row count does not match its sidecar")
    if len({pair.pair_id for pair in pairs}) != len(pairs):
        raise typer.BadParameter("golden split contains duplicate pair_id values")
    actual_groups = {pair.group_key for pair in pairs}
    if len(actual_groups) != contract["group_count"]:
        raise typer.BadParameter("golden split group count does not match its sidecar")

    counts = frozen.get("counts")
    partition_counts_raw = counts.get(partition) if isinstance(counts, dict) else None
    expected_rows = (
        partition_counts_raw.get("canonical_pairs")
        if isinstance(partition_counts_raw, dict)
        else None
    )
    if expected_rows != len(pairs):
        raise typer.BadParameter("golden split is not the complete frozen partition")
    group_partitions = frozen.get("group_partitions")
    if not isinstance(group_partitions, dict):
        raise typer.BadParameter("frozen partition manifest has invalid group assignments")
    expected_groups = {
        group
        for group, assigned_partition in group_partitions.items()
        if assigned_partition == partition
    }
    if actual_groups != expected_groups:
        raise typer.BadParameter("golden split groups do not exactly match the frozen partition")
    return pairs, contract


def _load_dev_strict_predictions(
    strict_run: Path,
) -> tuple[list[EvalPrediction], dict[str, Any], dict[str, Any]]:
    """Fail closed on the dev-only boundary before reading any predictions."""

    resolved_run = strict_run.resolve()
    if (
        strict_run.is_symlink()
        or not strict_run.name.startswith("dev_")
        or not resolved_run.name.startswith("dev_")
    ):
        raise typer.BadParameter("calibrate accepts only evaluate output directories named dev_*")
    metrics_path = strict_run / "metrics.json"
    manifest_path = strict_run / "evaluate_run_manifest.json"
    predictions_path = strict_run / "predictions.jsonl"
    if not manifest_path.is_file():
        raise typer.BadParameter(f"strict evaluate artifact is missing: {manifest_path}")
    strict_manifest = _load_json_object(manifest_path)
    if strict_manifest.get("command") != "evaluate" or strict_manifest.get("partition") != "dev":
        raise typer.BadParameter("calibration is dev-only and requires an evaluate manifest")
    if strict_manifest.get("threshold") != 0.5:
        raise typer.BadParameter("strict evaluate manifest must declare threshold 0.5")

    partition_manifest = _load_json_object(_FROZEN_PARTITION_MANIFEST)
    pairs_input = strict_manifest.get("pairs")
    canonical_sha256 = partition_manifest.get("canonical_pairs_sha256")
    split_contract = strict_manifest.get("split_contract")
    if not isinstance(pairs_input, dict) or not isinstance(canonical_sha256, str):
        raise typer.BadParameter("evaluate manifest has invalid golden pair linkage")
    if split_contract is None:
        # Preserve calibration of pre-hardening dev predictions, which were emitted
        # from the hash-bound mixed canonical file and contain no pair text.
        if pairs_input.get("sha256") != canonical_sha256:
            raise typer.BadParameter(
                "evaluate manifest pairs hash does not match the frozen partition manifest"
            )
    elif (
        not isinstance(split_contract, dict)
        or split_contract.get("schema_version") != _GOLDEN_SPLIT_SCHEMA_VERSION
        or split_contract.get("partition") != "dev"
        or split_contract.get("parent_canonical_sha256") != canonical_sha256
        or split_contract.get("split_sha256") != pairs_input.get("sha256")
    ):
        raise typer.BadParameter("evaluate manifest has invalid dev split linkage")

    for path in (metrics_path, predictions_path):
        if not path.is_file():
            raise typer.BadParameter(f"strict evaluate artifact is missing: {path}")
    recorded_outputs = strict_manifest.get("outputs")
    if recorded_outputs is not None:
        if not isinstance(recorded_outputs, dict):
            raise typer.BadParameter("evaluate manifest outputs field is invalid")
        for name, path in (("metrics", metrics_path), ("predictions", predictions_path)):
            entry = recorded_outputs.get(name)
            if not isinstance(entry, dict) or entry.get("sha256") != hash_file(path):
                raise typer.BadParameter(f"evaluate manifest hash mismatch for {name}")
    strict_metrics = _load_json_object(metrics_path)
    if strict_metrics.get("partition") != "dev":
        raise typer.BadParameter("calibration is dev-only; strict metrics must declare dev")
    if strict_metrics.get("track") != "strict_zero_shot":
        raise typer.BadParameter("metrics.json is not a strict-zero-shot evaluation artifact")
    if strict_metrics.get("threshold") != 0.5:
        raise typer.BadParameter("strict metrics must declare threshold 0.5")
        raise typer.BadParameter("evaluate_run_manifest.json does not describe evaluate")

    predictions: list[EvalPrediction] = []
    try:
        with predictions_path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    prediction = EvalPrediction.model_validate_json(line)
                except ValueError as error:
                    raise typer.BadParameter(
                        f"invalid prediction at {predictions_path}:{line_number}: {error}"
                    ) from error
                if prediction.abstained != (prediction.probability is None):
                    raise typer.BadParameter(
                        f"abstention flag disagrees with probability at "
                        f"{predictions_path}:{line_number}"
                    )
                if prediction.partition not in (None, "dev"):
                    raise typer.BadParameter(
                        f"non-dev prediction at {predictions_path}:{line_number}"
                    )
                predictions.append(prediction)
    except OSError as error:
        raise typer.BadParameter(f"cannot read predictions {predictions_path}: {error}") from error
    if not predictions:
        raise typer.BadParameter("strict evaluate artifact contains no predictions")
    pair_ids = {prediction.pair_id for prediction in predictions}
    if len(pair_ids) != len(predictions):
        raise typer.BadParameter("strict evaluate artifact contains duplicate pair_id values")
    counts = partition_manifest.get("counts")
    dev_counts = counts.get("dev") if isinstance(counts, dict) else None
    expected_count = dev_counts.get("canonical_pairs") if isinstance(dev_counts, dict) else None
    if not isinstance(expected_count, int) or len(predictions) != expected_count:
        raise typer.BadParameter("prediction count does not match frozen dev canonical-pair count")
    group_partitions = partition_manifest.get("group_partitions")
    if not isinstance(group_partitions, dict) or any(
        group_partitions.get(prediction.group_key) != "dev" for prediction in predictions
    ):
        raise typer.BadParameter("predictions contain a group outside the frozen dev partition")
    headline = [
        index
        for index, prediction in enumerate(predictions)
        if not prediction.label_conflict
        and prediction.label_provenance == "expert_human"
        and any(dataset != "proofnetverif" for dataset in prediction.datasets)
    ]
    strict_scores = [
        _PostHocScore(
            probability=prediction.probability,
            abstained=prediction.abstained,
        )
        for prediction in predictions
    ]
    recomputed = _prediction_subset_metrics(predictions, strict_scores, headline, 0.5)
    breakdowns = strict_metrics.get("breakdowns")
    recorded = breakdowns.get("headline_expert") if isinstance(breakdowns, dict) else None
    if recomputed is None or not isinstance(recorded, dict):
        raise typer.BadParameter("strict metrics lack a verifiable headline_expert breakdown")
    comparison_fields = (
        "accuracy",
        "balanced_accuracy",
        "f1",
        "auprc",
        "roc_auc",
        "brier",
        "nll",
        "ece",
        "coverage",
        "total_count",
        "scored_count",
        "abstained_count",
        "n_pairs",
    )
    for field in comparison_fields:
        expected = recomputed.get(field)
        observed = recorded.get(field)
        if not isinstance(expected, int | float) or not isinstance(observed, int | float):
            raise typer.BadParameter(f"strict headline metric {field!r} is missing or invalid")
        if not math.isclose(float(expected), float(observed), rel_tol=1e-12, abs_tol=1e-12):
            raise typer.BadParameter(
                f"strict predictions disagree with metrics.json for headline {field}"
            )
    return predictions, strict_metrics, strict_manifest


def _prediction_subset_metrics(
    predictions: list[EvalPrediction],
    scores: list[_PostHocScore],
    indices: list[int],
    threshold: float,
) -> dict[str, Any] | None:
    from leanfaith.eval.metrics import coverage_aware_summary

    if not indices:
        return None
    subset_scores = [scores[index] for index in indices]
    if all(score.abstained for score in subset_scores):
        return None
    subset_labels = [predictions[index].label for index in indices]
    summary = dict(coverage_aware_summary(subset_scores, subset_labels, threshold))
    summary["n_pairs"] = len(indices)
    summary["prevalence"] = sum(subset_labels) / len(subset_labels)
    return summary


@app.command("calibrate")
def calibrate(
    strict_run: Annotated[
        Path,
        typer.Option(help="One dev_* directory emitted by strict-zero-shot evaluate."),
    ],
    out: Annotated[
        Path | None,
        typer.Option(help="Output directory; defaults beside the strict run."),
    ] = None,
    min_temperature: Annotated[float, typer.Option()] = 1e-3,
    max_temperature: Annotated[float, typer.Option()] = 1e3,
    temperature_iterations: Annotated[int, typer.Option()] = 200,
    n_boot: Annotated[int, typer.Option()] = 1000,
    seed: Annotated[int, typer.Option()] = 20260828,
) -> None:
    """Fit scalar temperature + balanced-accuracy threshold on expert dev pairs."""

    from leanfaith.eval.metrics import (
        apply_temperature,
        compute_classification_metrics,
        fit_temperature,
        group_bootstrap_ci,
        select_balanced_accuracy_threshold,
    )

    predictions, strict_metrics, strict_manifest = _load_dev_strict_predictions(strict_run)
    if n_boot <= 0:
        raise typer.BadParameter("n_boot must be positive")
    out_dir = out or strict_run.with_name(f"{strict_run.name}_gold_calibrated")
    if out_dir.is_symlink() or out_dir.resolve() == strict_run.resolve():
        raise typer.BadParameter("calibration output must be a distinct, non-symlink directory")
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise typer.BadParameter("calibration output directory must be new or empty")
    headline = [
        index
        for index, prediction in enumerate(predictions)
        if not prediction.label_conflict
        and prediction.label_provenance == "expert_human"
        and any(dataset != "proofnetverif" for dataset in prediction.datasets)
    ]
    fit_indices = [index for index in headline if predictions[index].probability is not None]
    if len(fit_indices) != len(headline):
        typer.echo(
            f"warning: fitting on {len(fit_indices)}/{len(headline)} scored headline pairs; "
            "overlength pairs remain abstentions",
            err=True,
        )
    fit_labels = [predictions[index].label for index in fit_indices]
    fit_probabilities = [
        probability
        for index in fit_indices
        if (probability := predictions[index].probability) is not None
    ]
    try:
        temperature = fit_temperature(
            fit_labels,
            fit_probabilities,
            min_temperature=min_temperature,
            max_temperature=max_temperature,
            iterations=temperature_iterations,
        )
        calibrated_fit_probabilities = apply_temperature(fit_probabilities, temperature)
        threshold = select_balanced_accuracy_threshold(fit_labels, calibrated_fit_probabilities)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    calibrated_scores: list[_PostHocScore] = []
    for prediction in predictions:
        probability = prediction.probability
        calibrated_probability = (
            None if probability is None else apply_temperature([probability], temperature)[0]
        )
        calibrated_scores.append(
            _PostHocScore(
                probability=calibrated_probability,
                abstained=calibrated_probability is None,
            )
        )

    breakdowns: dict[str, Any] = {
        "headline_expert": _prediction_subset_metrics(
            predictions, calibrated_scores, headline, threshold
        ),
        "all_non_conflicted": _prediction_subset_metrics(
            predictions,
            calibrated_scores,
            [
                index
                for index, prediction in enumerate(predictions)
                if not prediction.label_conflict
            ],
            threshold,
        ),
    }
    datasets = {dataset for prediction in predictions for dataset in prediction.datasets}
    for dataset in sorted(datasets):
        indices = [
            index
            for index, prediction in enumerate(predictions)
            if not prediction.label_conflict and dataset in prediction.datasets
        ]
        breakdowns[f"dataset:{dataset}"] = _prediction_subset_metrics(
            predictions, calibrated_scores, indices, threshold
        )

    fit_groups = [predictions[index].group_key for index in fit_indices]

    def _balanced_accuracy(y: Any, p: Any) -> float:
        return float(compute_classification_metrics(y, p, threshold)["balanced_accuracy"])

    def _accuracy(y: Any, p: Any) -> float:
        return float(compute_classification_metrics(y, p, threshold)["accuracy"])

    ci: dict[str, Any] = {}
    for name, metric_function in (
        ("balanced_accuracy", _balanced_accuracy),
        ("accuracy", _accuracy),
    ):
        point, lo, hi = group_bootstrap_ci(
            fit_labels,
            calibrated_fit_probabilities,
            fit_groups,
            metric_function,
            n_boot=n_boot,
            seed=seed,
        )
        ci[name] = {"point": point, "lo95": lo, "hi95": hi}

    strict_fit_metrics = compute_classification_metrics(
        fit_labels, fit_probabilities, threshold=0.5
    )
    temperature_only_metrics = compute_classification_metrics(
        fit_labels, calibrated_fit_probabilities, threshold=0.5
    )
    calibrated_fit_metrics = compute_classification_metrics(
        fit_labels, calibrated_fit_probabilities, threshold=threshold
    )
    if math.isclose(temperature, min_temperature, rel_tol=1e-12, abs_tol=0.0):
        temperature_fit_status = "min_temperature_boundary"
    elif math.isclose(temperature, max_temperature, rel_tol=1e-12, abs_tol=0.0):
        temperature_fit_status = "max_temperature_boundary"
    else:
        temperature_fit_status = "interior_optimum"
    comparison_metric_names = (
        "balanced_accuracy",
        "accuracy",
        "precision",
        "recall",
        "f1",
        "auprc",
        "roc_auc",
        "brier",
        "nll",
        "ece",
    )
    strict_metric_values = dict(strict_fit_metrics)
    calibrated_metric_values = dict(calibrated_fit_metrics)
    comparison = {
        "strict_zero_shot": {
            "threshold": 0.5,
            **{name: strict_metric_values[name] for name in comparison_metric_names},
        },
        "gold_calibrated": {
            "temperature": temperature,
            "temperature_fit_status": temperature_fit_status,
            "threshold": threshold,
            **{name: calibrated_metric_values[name] for name in comparison_metric_names},
        },
    }
    calibration_payload = {
        "fit_partition": "dev",
        "fit_subset": "headline_expert_scored",
        "fit_count": len(fit_indices),
        "fit_positive_count": sum(fit_labels),
        "fit_negative_count": len(fit_labels) - sum(fit_labels),
        "temperature": temperature,
        "inverse_temperature": 1.0 / temperature,
        "temperature_objective": "mean_binary_nll",
        "temperature_optimizer": "bounded_inverse_temperature_derivative_bisection",
        "temperature_bounds": [min_temperature, max_temperature],
        "temperature_iterations": temperature_iterations,
        "temperature_fit_status": temperature_fit_status,
        "threshold": threshold,
        "threshold_objective": "balanced_accuracy",
        "threshold_candidates": "all_distinct_decision_intervals",
        "threshold_tie_break": "closest_to_0.5_then_lower",
        "strict_nll_at_0.5": strict_fit_metrics["nll"],
        "temperature_scaled_nll": temperature_only_metrics["nll"],
        "strict_balanced_accuracy_at_0.5": strict_fit_metrics["balanced_accuracy"],
        "gold_calibrated_balanced_accuracy": calibrated_fit_metrics["balanced_accuracy"],
    }
    metrics_payload = {
        "partition": "dev",
        "fit_partition": "dev",
        "track": "gold_calibrated",
        "temperature": temperature,
        "threshold": threshold,
        "comparison_headline_expert": comparison,
        "breakdowns": breakdowns,
        "bootstrap_ci_headline": ci,
        "trivial_baselines": strict_metrics.get("trivial_baselines"),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    predictions_out = out_dir / "calibrated_predictions.jsonl"
    with predictions_out.open("w", encoding="utf-8") as stream:
        for prediction, score in zip(predictions, calibrated_scores, strict=True):
            stream.write(
                json.dumps(
                    {
                        **prediction.model_dump(mode="json", exclude={"probability"}),
                        "partition": "dev",
                        "strict_probability": prediction.probability,
                        "calibrated_probability": score.probability,
                        "decision": (
                            None if score.probability is None else score.probability >= threshold
                        ),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    calibration_out = out_dir / "calibration.json"
    metrics_out = out_dir / "metrics.json"
    calibration_out.write_bytes(canonical_json_bytes(calibration_payload))
    metrics_out.write_bytes(canonical_json_bytes(metrics_payload))
    strict_predictions = strict_run / "predictions.jsonl"
    strict_metrics_path = strict_run / "metrics.json"
    strict_manifest_path = strict_run / "evaluate_run_manifest.json"
    _write_run_manifest(
        out_dir,
        "calibrate",
        {
            "seed": seed,
            "bootstrap_samples": n_boot,
            "strict_run": str(strict_run),
            "source_linkage": (
                "evaluate_manifest_output_hashes"
                if strict_manifest.get("outputs") is not None
                else "legacy_recomputed_metrics_plus_frozen_partition"
            ),
            "inputs": {
                "predictions": {
                    "path": str(strict_predictions),
                    "sha256": hash_file(strict_predictions),
                },
                "metrics": {
                    "path": str(strict_metrics_path),
                    "sha256": hash_file(strict_metrics_path),
                },
                "evaluate_manifest": {
                    "path": str(strict_manifest_path),
                    "sha256": hash_file(strict_manifest_path),
                },
                "frozen_partition_manifest": {
                    "path": str(_FROZEN_PARTITION_MANIFEST),
                    "sha256": hash_file(_FROZEN_PARTITION_MANIFEST),
                },
            },
            "fit": calibration_payload,
            "outputs": {
                "predictions": {
                    "path": str(predictions_out),
                    "sha256": hash_file(predictions_out),
                },
                "calibration": {
                    "path": str(calibration_out),
                    "sha256": hash_file(calibration_out),
                },
                "metrics": {"path": str(metrics_out), "sha256": hash_file(metrics_out)},
            },
        },
    )
    typer.echo(json.dumps(calibration_payload, indent=2, sort_keys=True))
    typer.echo(json.dumps(comparison, indent=2, sort_keys=True))
    typer.echo(f"gold-calibrated results -> {out_dir}")
