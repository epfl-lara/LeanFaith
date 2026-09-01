"""Private-first Hugging Face publication of a compacted sprint run.

Uploads are additive: a remote prefix is written exactly once with one
immutable commit, verified by a fresh download of every file, and recorded in
a local receipt.  The token is read from the environment by ``huggingface_hub``
and is never printed or stored.
"""

from __future__ import annotations

import argparse
import datetime
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.sprint.runner import RunPaths, load_sprint_config
from leanfaith.sft1.sprint.store import read_json_object, write_atomic

DEFAULT_REPO_ID = "Lemmy00/leanfaith-sft1-deterministic-v1"
REPO_TYPE = "dataset"


class PublishError(RuntimeError):
    """Fail-closed publication error."""


def dataset_card(run_id: str, manifest: dict[str, Any], release: dict[str, Any] | None) -> str:
    operations = manifest.get("operations", {})
    labels = manifest.get("labels", {})
    engine = manifest.get("engine") or {}
    lines = [
        "---",
        "license: apache-2.0",
        "pretty_name: LeanFaith SFT1 deterministic theorem-equivalence pairs (sprint v1)",
        "language: [en]",
        "tags: [lean4, mathlib, theorem-equivalence, autoformalization]",
        "configs:",
        f"  - config_name: {run_id}",
        "    data_files:",
        f'      - split: train\n        path: "sprint_v1/{run_id}/shard-*/rows.jsonl"',
        "---",
        "",
        f"# LeanFaith SFT1 sprint v1 — run `{run_id}`",
        "",
        "Private-first release of deterministic, proof-backed theorem-equivalence pairs derived",
        "from Mathlib theorem statements. Each core row is `{pair_id, root_id,"
        " reference, candidate,",
        "label, operation_id}` where `reference` and `candidate` are `goal_v1.0` renderings of two",
        "closed propositions and `label` is `true` only when a Meta- and kernel-checked",
        "`Iff reference candidate` witness replayed, and `false` only when the"
        " loaded Mathlib proof of",
        "the reference and a kernel-checked `Not candidate` refutation under a complete ground",
        "assignment replayed. No label uses an LLM or a rubric-only mutation.",
        "",
        "## Counts",
        "",
        f"- retained rows: {manifest.get('retained_rows')}",
        f"- labels: positive {labels.get('positive')}, negative {labels.get('negative')}",
        f"- roots: {manifest.get('roots')}",
        "- operations:",
        *(f"  - `{name}`: {count}" for name, count in sorted(operations.items())),
        "",
        "## Provenance",
        "",
        "- source: Mathlib theorem statements (Apache-2.0), revision recorded in every sidecar",
        f"- engine: `{engine.get('semantic_version')}` (source sha256"
        f" `{engine.get('source_sha256')}`)",
        f"- implementation commit: `{manifest.get('implementation_commit')}`",
        f"- config semantic hash: `{manifest.get('config_semantic_hash')}`",
        f"- gold blocklist sha256: `{manifest.get('gold_blocklist_sha256')}`",
        f"- duplicates removed: {manifest.get('duplicates_removed')}, conflicting classes rejected:"
        f" {manifest.get('conflicting_classes_rejected')}",
        "",
        "## Files",
        "",
        "- `shard-*/rows.jsonl`: core training rows",
        "- `shard-*/sidecars.jsonl`: keyed sidecars with site certificates, proof/refutation",
        "  evidence, frozen REPR records for both endpoints, project pins, and cache identity",
        "- `shard-*/manifest.json`, `manifest.json`: counts and content hashes",
    ]
    if release is not None:
        checks = release.get("checks", {})
        lines.extend(
            [
                "- `release_report.json`: 10K release gate (shortcut screens and projection)",
                "",
                "## Release gate",
                "",
                *(f"- {name}: {'pass' if value else 'FAIL'}" for name, value in checks.items()),
            ]
        )
    lines.extend(
        [
            "",
            "This repository is private-first; redistribution review is recorded in the LeanFaith",
            "task brief `plans/30_sft1_deterministic.md`.",
            "",
        ]
    )
    return "\n".join(lines)


def local_files(compacted: Path) -> list[Path]:
    files: list[Path] = []
    for shard in sorted(compacted.glob("shard-*")):
        for name in ("rows.jsonl", "sidecars.jsonl", "manifest.json"):
            path = shard / name
            if not path.is_file():
                raise PublishError(f"shard file missing: {path}")
            files.append(path)
    for name in ("manifest.json", "release_report.json"):
        path = compacted / name
        if path.is_file():
            files.append(path)
    return files


def publish_run(
    repo_root: Path,
    *,
    run_id: str,
    repo_id: str = DEFAULT_REPO_ID,
    remote_prefix: str | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

    loaded = load_sprint_config(repo_root, config_path)
    paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
    compacted = paths.compacted
    manifest = read_json_object(compacted / "manifest.json")
    release_path = compacted / "release_report.json"
    release = read_json_object(release_path) if release_path.is_file() else None
    prefix = remote_prefix or f"sprint_v1/{run_id}"
    receipt_path = compacted / "publication_receipt.json"
    if receipt_path.is_file():
        receipt = read_json_object(receipt_path)
        if receipt.get("remote_prefix") == prefix and receipt.get("repo_id") == repo_id:
            return receipt
        raise PublishError("publication receipt exists for a different prefix or repo")
    card_path = compacted / "README.md"
    write_atomic(card_path, dataset_card(run_id, manifest, release).encode("utf-8"))
    files = [*local_files(compacted), card_path]
    hashes = {path.relative_to(compacted).as_posix(): hash_file(path) for path in files}
    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type=REPO_TYPE, private=True, exist_ok=True)
    info = api.repo_info(repo_id=repo_id, repo_type=REPO_TYPE)
    if not bool(info.private):
        raise PublishError("refusing to publish SFT1 sprint data to a public repository")
    parent = str(info.sha)
    existing = set(api.list_repo_files(repo_id=repo_id, repo_type=REPO_TYPE, revision=parent))
    remote_paths = {relative: f"{prefix}/{relative}" for relative in hashes}
    occupied = {name for name in existing if name.startswith(f"{prefix}/")}
    if occupied:
        raise PublishError(f"remote prefix {prefix!r} is already occupied; refusing overwrite")
    operations = [
        CommitOperationAdd(
            path_in_repo=remote_paths[relative], path_or_fileobj=compacted / relative
        )
        for relative in sorted(hashes)
    ]
    if "README.md" not in existing:
        operations.append(CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=card_path))
    commit = api.create_commit(
        repo_id=repo_id,
        repo_type=REPO_TYPE,
        operations=operations,
        commit_message=f"sft1 sprint v1: publish {run_id} ({manifest.get('retained_rows')} rows)",
        parent_commit=parent,
    )
    revision = str(commit.oid)
    if len(revision) != 40:
        raise PublishError("Hub publication did not return an immutable revision")
    verified: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="sft1-sprint-verify-") as fresh:
        for relative, remote in sorted(remote_paths.items()):
            downloaded = hf_hub_download(
                repo_id=repo_id,
                filename=remote,
                repo_type=REPO_TYPE,
                revision=revision,
                cache_dir=fresh,
            )
            digest = hash_file(Path(downloaded))
            if digest != hashes[relative]:
                raise PublishError(f"fresh download hash mismatch for {remote}")
            verified[remote] = digest
    receipt = {
        "schema_version": 1,
        "run_id": run_id,
        "repo_id": repo_id,
        "repo_type": REPO_TYPE,
        "private": True,
        "revision": revision,
        "parent_revision": parent,
        "remote_prefix": prefix,
        "file_sha256": verified,
        "retained_rows": manifest.get("retained_rows"),
        "published_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "fresh_verification": True,
    }
    write_atomic(receipt_path, canonical_json_bytes(receipt) + b"\n")
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--remote-prefix")
    args = parser.parse_args(argv)
    receipt = publish_run(
        args.repo_root.resolve(),
        run_id=args.run_id,
        repo_id=args.repo_id,
        remote_prefix=args.remote_prefix,
        config_path=args.config.resolve() if args.config else None,
    )
    print(json.dumps({k: v for k, v in receipt.items() if k != "file_sha256"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
