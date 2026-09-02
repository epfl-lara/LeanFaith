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
from collections.abc import Mapping, Sequence
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
    provenance = manifest.get("provenance") or {}
    segments = provenance.get("segments") or []
    status = manifest.get("artifact_status", "diagnostic_gate_evidence_not_a_training_release")
    screens = (release or {}).get("shortcut", {}).get("screens", [])
    lines = [
        "---",
        "license: apache-2.0",
        "pretty_name: LeanFaith SFT1 sprint v1 diagnostic gate evidence",
        "language: [en]",
        "tags: [lean4, mathlib, theorem-equivalence, autoformalization, diagnostic-evidence]",
        "configs:",
        f"  - config_name: {run_id}",
        "    data_files:",
        "      - split: diagnostic",
        f'        path: "sprint_v1/{run_id}/shard-*/rows.jsonl"',
        "---",
        "",
        f"# LeanFaith SFT1 sprint v1 — diagnostic gate evidence, run `{run_id}`",
        "",
        "**This artifact is diagnostic gate evidence, not a training release.** Its labels are",
        "individually certified, but the composition failed the sprint's shortcut screens (see",
        "the release-gate section), so it must not be used as model-facing training data.",
        "",
        "Each core row is `{pair_id, root_id, reference, candidate, label, operation_id}` where",
        "`reference` and `candidate` are `goal_v1.0` renderings of two closed propositions.",
        "`label` is `true` only when a Meta- and kernel-checked `Iff reference candidate` witness",
        "was constructed during original generation, and `false` only when the loaded Mathlib",
        "proof of the reference and a kernel-checked `Not candidate` refutation under a complete",
        "ground assignment were constructed during original generation. No label uses an LLM or",
        "a rubric-only mutation.",
        "",
        "## Replay semantics",
        "",
        "Proof checks occurred during original generation. The recorded zero-Lean-call replay is a",
        "journal/cache replay of stored terminals and cached artifacts, not a fresh kernel replay.",
        "",
        "## Counts",
        "",
        f"- artifact status: `{status}`",
        f"- retained rows: {manifest.get('retained_rows')}",
        f"- labels: positive {labels.get('positive')}, negative {labels.get('negative')}",
        f"- roots: {manifest.get('roots')}",
        "- operations:",
        *(f"  - `{name}`: {count}" for name, count in sorted(operations.items())),
        "",
        "## Provenance (derived from sidecars)",
        "",
        "- source: Mathlib theorem statements (Apache-2.0), revision recorded in every sidecar",
        f"- engine semantic versions: {provenance.get('engine_semantic_versions')}",
        "- implementation segments (a resumed run may span several):",
    ]
    for segment in segments:
        commits = ", ".join(f"`{c[:12]}`" for c in segment.get("engine_commits", [])) or "unknown"
        lines.append(
            f"  - engine `{segment.get('engine_source_sha256')}` / compile context"
            f" `{segment.get('compile_context_id')}` / cache schema {segment.get('cache_schema')}:"
            f" {segment.get('rows')} rows, {segment.get('roots')} roots; engine commits {commits}"
        )
    lines.extend(
        [
            f"- config semantic hash: `{manifest.get('config_semantic_hash')}`",
            f"- gold blocklist sha256: `{manifest.get('gold_blocklist_sha256')}`",
            f"- duplicates removed: {manifest.get('duplicates_removed')}, conflicting rows"
            f" rejected: {manifest.get('conflicting_rows_rejected')}, view-dropped rows:"
            f" {manifest.get('view_dropped')}",
            "",
            "## Files",
            "",
            "- `shard-*/rows.jsonl`: core rows",
            "- `shard-*/sidecars.jsonl`: keyed sidecars with site certificates, proof/refutation",
            "  evidence, frozen REPR records for both endpoints, project pins, engine identity,",
            "  and cache identity",
            "- `shard-*/manifest.json`, `manifest.json`: counts, content hashes, sidecar-derived",
            "  provenance segments",
        ]
    )
    if release is not None:
        checks = release.get("checks", {})
        lines.extend(
            [
                "- `release_report.json`: release-gate report (shortcut screens and projection)",
                "",
                "## Release gate",
                "",
                *(f"- {name}: {'pass' if value else 'FAIL'}" for name, value in checks.items()),
                *(
                    f"- {screen.get('name')}: balanced accuracy {screen.get('balanced_accuracy')}"
                    f" (95% upper bound {screen.get('upper_bound_95')}, threshold"
                    f" {screen.get('threshold')}) → {'pass' if screen.get('passed') else 'FAIL'}"
                    for screen in screens
                ),
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


def index_card(prefixes: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "---",
        "license: apache-2.0",
        "pretty_name: LeanFaith SFT1 deterministic theorem-equivalence pairs",
        "language: [en]",
        "tags: [lean4, mathlib, theorem-equivalence, autoformalization]",
        "---",
        "",
        "# LeanFaith SFT1 deterministic pairs (private-first)",
        "",
        "Index of published prefixes. Each prefix carries its own README with counts, provenance,",
        "and gate status. Proof checks occurred during original generation; replay receipts are",
        "journal/cache replays of stored terminals, not fresh kernel replays.",
        "",
        "| prefix | rows | status |",
        "| --- | --- | --- |",
    ]
    for item in prefixes:
        lines.append(f"| `{item['prefix']}` | {item['rows']} | {item['status']} |")
    lines.append("")
    return "\n".join(lines)


def local_files(compacted: Path) -> list[Path]:
    files: list[Path] = []
    for shard in sorted(compacted.glob("shard-*")):
        for name in ("rows.jsonl", "sidecars.jsonl", "manifest.json"):
            path = shard / name
            if not path.is_file():
                raise PublishError(f"shard file missing: {path}")
            files.append(path)
    for name in ("manifest.json", "release_report.json", "integrity_report.json", "verdict.json"):
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


def _upload_verified(
    api: Any,
    *,
    repo_id: str,
    local_root: Path,
    files: Sequence[Path],
    remote_prefix: str,
    commit_message: str,
    extra_operations: Sequence[Any] = (),
) -> tuple[str, str, dict[str, str]]:
    from huggingface_hub import CommitOperationAdd, hf_hub_download

    hashes = {path.relative_to(local_root).as_posix(): hash_file(path) for path in files}
    info = api.repo_info(repo_id=repo_id, repo_type=REPO_TYPE)
    if not bool(info.private):
        raise PublishError("refusing to publish SFT1 sprint data to a public repository")
    parent = str(info.sha)
    existing = set(api.list_repo_files(repo_id=repo_id, repo_type=REPO_TYPE, revision=parent))
    if any(name.startswith(f"{remote_prefix}/") for name in existing):
        raise PublishError(f"remote prefix {remote_prefix!r} is already occupied")
    remote_paths = {relative: f"{remote_prefix}/{relative}" for relative in hashes}
    operations = [
        CommitOperationAdd(
            path_in_repo=remote_paths[relative], path_or_fileobj=local_root / relative
        )
        for relative in sorted(hashes)
    ]
    operations.extend(extra_operations)
    commit = api.create_commit(
        repo_id=repo_id,
        repo_type=REPO_TYPE,
        operations=operations,
        commit_message=commit_message,
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
    return revision, parent, verified


def publish_windows(
    repo_root: Path,
    *,
    run_id: str,
    repo_id: str = DEFAULT_REPO_ID,
    config_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Publish every compacted root window that has no publication receipt yet."""

    from huggingface_hub import HfApi

    loaded = load_sprint_config(repo_root, config_path)
    paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type=REPO_TYPE, private=True, exist_ok=True)
    receipts: list[dict[str, Any]] = []
    for window_dir in sorted(paths.compacted.glob("window-*")):
        receipt_path = window_dir / "publication_receipt.json"
        if receipt_path.is_file():
            continue
        manifest = read_json_object(window_dir / "manifest.json")
        files = [window_dir / name for name in ("rows.jsonl", "sidecars.jsonl", "manifest.json")]
        if any(not path.is_file() for path in files):
            raise PublishError(f"window files missing in {window_dir}")
        prefix = f"sprint_v1/{run_id}/{window_dir.name}"
        revision, parent, verified = _upload_verified(
            api,
            repo_id=repo_id,
            local_root=window_dir,
            files=files,
            remote_prefix=prefix,
            commit_message=(
                f"sft1 sprint v1: publish {run_id} {window_dir.name} "
                f"({manifest.get('row_count')} rows)"
            ),
        )
        receipt = {
            "schema_version": 1,
            "run_id": run_id,
            "window": manifest.get("window"),
            "repo_id": repo_id,
            "repo_type": REPO_TYPE,
            "private": True,
            "revision": revision,
            "parent_revision": parent,
            "remote_prefix": prefix,
            "file_sha256": verified,
            "row_count": manifest.get("row_count"),
            "published_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "fresh_verification": True,
        }
        write_atomic(receipt_path, canonical_json_bytes(receipt) + b"\n")
        receipts.append(receipt)
    return receipts


def update_cards(
    repo_root: Path,
    *,
    run_ids: Sequence[str],
    repo_id: str = DEFAULT_REPO_ID,
    config_path: Path | None = None,
    index: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Replace only the README cards of already-published prefixes in one commit."""

    from huggingface_hub import CommitOperationAdd, HfApi

    loaded = load_sprint_config(repo_root, config_path)
    api = HfApi()
    info = api.repo_info(repo_id=repo_id, repo_type=REPO_TYPE)
    if not bool(info.private):
        raise PublishError("refusing to update cards on a public repository")
    parent = str(info.sha)
    operations = []
    updated: dict[str, str] = {}
    for run_id in run_ids:
        paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
        compacted = paths.compacted
        receipt_path = compacted / "publication_receipt.json"
        if not receipt_path.is_file():
            raise PublishError(f"{run_id} has no publication receipt")
        receipt = read_json_object(receipt_path)
        manifest = read_json_object(compacted / "manifest.json")
        release_path = compacted / "release_report.json"
        release = read_json_object(release_path) if release_path.is_file() else None
        card = dataset_card(run_id, manifest, release)
        card_path = compacted / "README.md"
        write_atomic(card_path, card.encode("utf-8"))
        remote = f"{receipt['remote_prefix']}/README.md"
        operations.append(CommitOperationAdd(path_in_repo=remote, path_or_fileobj=card_path))
        updated[remote] = hash_file(card_path)
    if index:
        index_path = Path(loaded.config.output.staging_root) / "compacted" / "README.index.md"
        write_atomic(index_path, index_card(index).encode("utf-8"))
        operations.append(CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=index_path))
        updated["README.md"] = hash_file(index_path)
    commit = api.create_commit(
        repo_id=repo_id,
        repo_type=REPO_TYPE,
        operations=operations,
        commit_message="sft1 sprint v1: correct dataset cards (diagnostic gate evidence)",
        parent_commit=parent,
    )
    revision = str(commit.oid)
    for run_id in run_ids:
        paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
        receipt_path = paths.compacted / "publication_receipt.json"
        receipt = read_json_object(receipt_path)
        history = list(receipt.get("card_revisions", []))
        history.append(
            {
                "revision": revision,
                "at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            }
        )
        receipt["card_revisions"] = history
        write_atomic(receipt_path, canonical_json_bytes(receipt) + b"\n")
    return {"revision": revision, "parent_revision": parent, "updated": updated}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--repo-id", default=DEFAULT_REPO_ID)
    parser.add_argument("--remote-prefix")
    parser.add_argument("--windows", action="store_true", help="publish compacted root windows")
    parser.add_argument(
        "--update-cards", nargs="*", metavar="RUN_ID", help="replace README cards of published runs"
    )
    parser.add_argument("--index-json", type=Path, help="JSON list of {prefix, rows, status}")
    args = parser.parse_args(argv)
    if args.update_cards is not None:
        index = json.loads(args.index_json.read_text(encoding="utf-8")) if args.index_json else []
        result = update_cards(
            args.repo_root.resolve(),
            run_ids=args.update_cards,
            repo_id=args.repo_id,
            config_path=args.config.resolve() if args.config else None,
            index=index,
        )
        print(json.dumps(result, indent=1))
        return 0
    if not args.run_id and args.update_cards is None:
        parser.error("--run-id is required unless --update-cards is given")
    if args.windows:
        receipts = publish_windows(
            args.repo_root.resolve(),
            run_id=args.run_id,
            repo_id=args.repo_id,
            config_path=args.config.resolve() if args.config else None,
        )
        print(
            json.dumps(
                [{k: v for k, v in item.items() if k != "file_sha256"} for item in receipts],
                indent=1,
            )
        )
        return 0
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
