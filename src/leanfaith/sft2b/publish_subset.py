"""Stage the accepted SFT2B smoke subset for a private Hub publication."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.sft2b.durable import immutable_write, read_model, write_json
from leanfaith.sft2b.pins import verify_runtime_pins
from leanfaith.sft2b.schemas import (
    CandidateRecord,
    CompilationEvidence,
    CoreRow,
    FormalizerAttempt,
    FormalizerInvalidAttemptView,
    InvalidAttempt,
    JudgeVote,
    MajorityOutcome,
    RunManifest,
    SourceRecord,
    UnknownCandidate,
)

_TOKEN_PATTERN = re.compile(rb"(?<![A-Za-z0-9])(?:hf_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,})")
_RUN_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "sources.jsonl": SourceRecord,
    "candidates.jsonl": CandidateRecord,
    "compilation_evidence.jsonl": CompilationEvidence,
    "votes.jsonl": JudgeVote,
    "majority.jsonl": MajorityOutcome,
    "core.jsonl": CoreRow,
    "invalid_attempts.jsonl": InvalidAttempt,
    "unknowns.jsonl": UnknownCandidate,
    "formalizer_attempts.jsonl": FormalizerAttempt,
    "formalizer_invalid_attempts.jsonl": FormalizerInvalidAttemptView,
}
_DATASET_CONFIG_PATHS = {
    "existing_301_core": "data/existing_301_smoke/core.jsonl",
    "reform_8b_core": "data/reform_8b_smoke/core.jsonl",
    "sources": "data/views/sources.jsonl",
    "candidates": "data/views/candidates.jsonl",
    "compilation_evidence": "data/views/compilation_evidence.jsonl",
    "votes": "data/views/votes.jsonl",
    "majority": "data/views/majority.jsonl",
    "invalid_attempts": "data/reform_8b_smoke/invalid_attempts.jsonl",
    "formalizer_attempts": "data/reform_8b_smoke/formalizer_attempts.jsonl",
    "formalizer_invalid_attempts": ("data/reform_8b_smoke/formalizer_invalid_attempts.jsonl"),
}


class SubsetPublicationError(RuntimeError):
    """Raised when accepted evidence cannot produce an exact portable release."""


@dataclass(frozen=True, slots=True)
class AcceptedRun:
    name: str
    root: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class SubsetConfig:
    repo_id: str
    private: bool
    staging_path: Path
    publication_receipt_path: Path
    accepted_runs: tuple[AcceptedRun, ...]
    workspace_paths: tuple[Path, ...]


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SubsetPublicationError(f"{label} must be a string-keyed object")
    return value


def load_subset_config(repo_root: Path, path: Path) -> SubsetConfig:
    value = _require_mapping(json.loads(path.read_text(encoding="utf-8")), "subset config")
    if value.get("schema_version") != "sft2b_hf_subset_v1":
        raise SubsetPublicationError("unsupported subset publication config")
    runs_raw = value.get("accepted_runs")
    if not isinstance(runs_raw, list):
        raise SubsetPublicationError("accepted_runs must be a list")
    runs: list[AcceptedRun] = []
    for index, raw in enumerate(runs_raw):
        item = _require_mapping(raw, f"accepted_runs[{index}]")
        runs.append(
            AcceptedRun(
                name=str(item["name"]),
                root=Path(str(item["root"])),
                manifest_sha256=str(item["manifest_sha256"]),
            )
        )
    workspace_raw = value.get("workspace_paths")
    if not isinstance(workspace_raw, list) or any(
        not isinstance(item, str) for item in workspace_raw
    ):
        raise SubsetPublicationError("workspace_paths must be a string list")
    workspace_paths = tuple(repo_root / item for item in workspace_raw)
    config = SubsetConfig(
        repo_id=str(value["repo_id"]),
        private=value.get("private") is True,
        staging_path=Path(str(value["staging_path"])),
        publication_receipt_path=Path(str(value["publication_receipt_path"])),
        accepted_runs=tuple(runs),
        workspace_paths=workspace_paths,
    )
    if config.repo_id != "Lemmy00/leanfaith-sft2-autoformalizer-v1" or not config.private:
        raise SubsetPublicationError("SFT2B subset must target the exact private Hub dataset")
    if [item.name for item in config.accepted_runs] != [
        "existing_301_smoke",
        "reform_8b_smoke",
    ]:
        raise SubsetPublicationError("accepted run names/order drifted")
    return config


def _read_jsonl(path: Path, model_type: type[BaseModel]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = model_type.model_validate_json(line)
            except Exception as exc:
                raise SubsetPublicationError(f"invalid {path}:{line_number}: {exc}") from exc
            row = value.model_dump(mode="json")
            if model_type is CoreRow and set(row) != {"reference", "candidate", "label"}:
                raise SubsetPublicationError("core row contract contains an auxiliary field")
            rows.append(row)
    return rows


def _copy_exact(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise SubsetPublicationError(f"release input is not a regular file: {source}")
    payload = source.read_bytes()
    if _TOKEN_PATTERN.search(payload):
        raise SubsetPublicationError(f"possible credential in release input: {source}")
    immutable_write(destination, payload)


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    immutable_write(
        path,
        b"".join(canonical_json_bytes(row) + b"\n" for row in rows),
    )


def _workspace_files(repo_root: Path, roots: tuple[Path, ...]) -> tuple[Path, ...]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        if not root.is_dir():
            raise SubsetPublicationError(f"workspace snapshot input is missing: {root}")
        files.extend(
            item
            for item in root.rglob("*")
            if item.is_file()
            and not item.is_symlink()
            and "__pycache__" not in item.parts
            and item.suffix not in {".pyc", ".pyo"}
        )
    relative = [item.relative_to(repo_root) for item in files]
    if len(relative) != len(set(relative)):
        raise SubsetPublicationError("workspace snapshot contains duplicate paths")
    return tuple(sorted(files))


def _dataset_card(*, counts: dict[str, int], release_id: str) -> bytes:
    configs = "\n".join(
        "\n".join(
            (
                f"- config_name: {name}",
                "  data_files:",
                "  - split: train",
                f"    path: {path}",
            )
        )
        for name, path in _DATASET_CONFIG_PATHS.items()
    )
    body = f"""---
pretty_name: LeanFaith SFT2B Autoformalizer Test Subset
language:
- en
license: other
task_categories:
- text-generation
configs:
{configs}
---

# LeanFaith SFT2B autoformalizer test subset

Private, pre-scale transfer snapshot for continuing SFT2B on another machine.

This release contains {counts["sources"]} source rows, {counts["candidates"]} extracted candidate
rows, {counts["core"]} valid labeled core pairs, {counts["invalid"]} Lean-invalid candidates,
{counts["votes"]} blinded judge votes, and {counts["formalizer_invalid"]} output-contract-invalid
formalizer attempts. There are no unknown semantic outcomes in this subset.

Core rows have exactly `{{reference, candidate, label}}`. Invalid Lean and output-contract failures
are separate auxiliary views and are never semantic label `false`. The two core configurations keep
the canonical 301-reuse smoke separate from the one-source ReForm-8B smoke. `sources`, `candidates`,
`compilation_evidence`, `votes`, and `majority` are keyed rich sidecars.

All three judges were blinded to sibling votes and expected labels. REPR is frozen to commit
`176a783842c5a73b84413dfa8347670608b615d9`, spec
`68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8`, implementation set
`9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff`, and API
`c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d`.

`release/release_manifest.json` binds every staged payload. `release/SHA256SUMS` includes the
manifest itself. `repro/workspace/` is a checksum-bound snapshot of all SFT2B-owned code, configs,
prompts, tests, and the executable brief needed to restore the current uncommitted task state.

Release identity: `{release_id}`. This is a testing subset, not the matched 500-source pilot or a
50K training release. Mixed source provenance and redistribution notes remain on each source row;
keep the repository private until the full source audit and publication gate.
"""
    return body.encode("utf-8")


def stage_subset(repo_root: Path, config_path: Path) -> dict[str, object]:
    config = load_subset_config(repo_root, config_path)
    helper_path = repo_root / "src/leanfaith/sft2b/lean_helper.lean"
    pins = verify_runtime_pins(repo_root, helper_path=helper_path)

    manifests: dict[str, RunManifest] = {}
    counts = {
        "sources": 0,
        "candidates": 0,
        "core": 0,
        "invalid": 0,
        "votes": 0,
        "formalizer_invalid": 0,
    }
    merged_filenames = {
        "sources.jsonl",
        "candidates.jsonl",
        "compilation_evidence.jsonl",
        "votes.jsonl",
        "majority.jsonl",
    }
    merged_rows: dict[str, list[dict[str, object]]] = {
        filename: [] for filename in merged_filenames
    }
    for accepted in config.accepted_runs:
        manifest_path = accepted.root / "manifest.json"
        if hash_file(manifest_path) != accepted.manifest_sha256:
            raise SubsetPublicationError(f"accepted manifest drifted: {accepted.name}")
        manifest = read_model(manifest_path, RunManifest)
        if manifest.publication_performed or manifest.training_performed:
            raise SubsetPublicationError("accepted smoke manifest has an impossible side effect")
        manifests[accepted.name] = manifest
        for filename, expected_hash in manifest.output_hashes.items():
            source = accepted.root / "outputs" / filename
            if hash_file(source) != expected_hash:
                raise SubsetPublicationError(f"accepted output drifted: {accepted.name}/{filename}")
            destination = config.staging_path / "data" / accepted.name / filename
            _copy_exact(source, destination)
            model_type = _RUN_OUTPUT_MODELS.get(filename)
            if model_type is not None:
                rows = _read_jsonl(destination, model_type)
                if filename in merged_rows:
                    merged_rows[filename].extend(rows)
                counter = {
                    "sources.jsonl": "sources",
                    "candidates.jsonl": "candidates",
                    "core.jsonl": "core",
                    "invalid_attempts.jsonl": "invalid",
                    "votes.jsonl": "votes",
                    "formalizer_invalid_attempts.jsonl": "formalizer_invalid",
                }.get(filename)
                if counter is not None:
                    counts[counter] += len(rows)
        _copy_exact(
            manifest_path,
            config.staging_path / "release/source_manifests" / f"{accepted.name}.json",
        )

    for filename, rows in sorted(merged_rows.items()):
        _write_rows(config.staging_path / "data/views" / filename, rows)

    if counts != {
        "sources": 2,
        "candidates": 4,
        "core": 3,
        "invalid": 1,
        "votes": 9,
        "formalizer_invalid": 1,
    }:
        raise SubsetPublicationError(f"release counts drifted: {counts}")
    for core_path in config.staging_path.glob("data/*/core.jsonl"):
        text = core_path.read_text(encoding="utf-8")
        if "[anonymous]" in text or "⋯" in text:
            raise SubsetPublicationError("core contains a rejected model-facing render")

    for source in _workspace_files(repo_root, config.workspace_paths):
        relative = source.relative_to(repo_root)
        _copy_exact(source, config.staging_path / "repro/workspace" / relative)

    release_identity = {
        "schema_version": "sft2b_hf_subset_release_v1",
        "layout_version": "sft2b_hf_subset_layout_v2",
        "repo_id": config.repo_id,
        "private": config.private,
        "source_run_manifest_sha256": {
            item.name: item.manifest_sha256 for item in config.accepted_runs
        },
        "source_run_ids": {name: manifest.run_id for name, manifest in sorted(manifests.items())},
        "counts": counts,
        "repr_pins": pins.to_dict(),
    }
    release_id = "sft2b_hf_subset:" + hash_canonical(release_identity)
    immutable_write(
        config.staging_path / "README.md",
        _dataset_card(counts=counts, release_id=release_id),
    )

    payload_files = tuple(
        sorted(
            path
            for path in config.staging_path.rglob("*")
            if path.is_file()
            and path.relative_to(config.staging_path).as_posix()
            not in {"release/release_manifest.json", "release/SHA256SUMS"}
        )
    )
    payload_index = {
        path.relative_to(config.staging_path).as_posix(): {
            "sha256": hash_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in payload_files
    }
    release_manifest = {
        **release_identity,
        "release_id": release_id,
        "payload_files": payload_index,
        "payload_file_count": len(payload_index),
        "payload_bytes": sum(path.stat().st_size for path in payload_files),
    }
    release_manifest_path = config.staging_path / "release/release_manifest.json"
    write_json(release_manifest_path, release_manifest)
    checksum_files = tuple(
        sorted(
            path
            for path in config.staging_path.rglob("*")
            if path.is_file()
            and path.relative_to(config.staging_path).as_posix() != "release/SHA256SUMS"
        )
    )
    checksums = "".join(
        f"{hash_file(path)}  {path.relative_to(config.staging_path).as_posix()}\n"
        for path in checksum_files
    ).encode("utf-8")
    immutable_write(config.staging_path / "release/SHA256SUMS", checksums)

    result = {
        "schema_version": "sft2b_hf_subset_stage_result_v1",
        "repo_id": config.repo_id,
        "private": config.private,
        "staging_path": str(config.staging_path),
        "release_id": release_id,
        "release_manifest_sha256": hash_file(release_manifest_path),
        "counts": counts,
        "file_count": len(checksum_files) + 1,
        "total_bytes": sum(path.stat().st_size for path in checksum_files)
        + (config.staging_path / "release/SHA256SUMS").stat().st_size,
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/sft2b/hf_subset_v1.json"),
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    result = stage_subset(repo_root, repo_root / args.config)
    print(canonical_json_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
