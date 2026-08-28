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
