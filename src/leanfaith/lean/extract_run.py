"""Extraction orchestration and partition writing (PLAN.md §12, LF-012).

Drives the LeanInteract backend over repository files (FileCommand) and
dataset snippets (Command), applies the range-based proof strip, revalidates,
and writes JSONL partitions plus an OutputManifest. A full run is idempotent:
each source's partition is truncated at the start so re-running the same
inputs reproduces the same output rather than append-duplicating (§10 rule 4).
Crash-resume (skip already-processed shards) is a later refinement.
"""

from __future__ import annotations

import datetime
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.lean.extraction import (
    ExtractionResult,
    SourceIdentity,
    extract_from_declarations,
    reconstruct_for_revalidation,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.schemas import (
    ArtifactClass,
    CodeState,
    DataStage,
    OutputManifest,
    ValidationStatus,
    write_manifest,
)

_VALID = (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY)


@dataclass(slots=True)
class ExtractStats:
    sources_processed: int = 0
    declarations_seen: int = 0
    accepted: int = 0
    failures: int = 0
    revalidated_ok: int = 0
    revalidation_failed: int = 0
    source_not_elaborating: int = 0
    failure_codes: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, object]:
        return {
            "sources_processed": self.sources_processed,
            "declarations_seen": self.declarations_seen,
            "accepted": self.accepted,
            "failures": self.failures,
            "revalidated_ok": self.revalidated_ok,
            "revalidation_failed": self.revalidation_failed,
            "source_not_elaborating": self.source_not_elaborating,
            "failure_codes": dict(self.failure_codes),
        }


def _write_records(result: ExtractionResult, theorems_path: Path, failures_path: Path) -> None:
    if not result.accepted and not result.failures:
        return
    theorems_path.parent.mkdir(parents=True, exist_ok=True)
    if result.accepted:
        _append_theorems(result, theorems_path)
    if result.failures:
        _append_failures(result, failures_path)


def _append_theorems(result: ExtractionResult, theorems_path: Path) -> None:
    with theorems_path.open("a", encoding="utf-8") as fh:
        for extracted in result.accepted:
            fh.write(
                json.dumps(
                    {
                        "theorem": extracted.theorem.model_dump(mode="json"),
                        "representation": extracted.representation.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def _append_failures(result: ExtractionResult, failures_path: Path) -> None:
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    with failures_path.open("a", encoding="utf-8") as fh:
        for failure in result.failures:
            fh.write(
                json.dumps(
                    {
                        "source_record": failure.source_record,
                        "declaration_name": failure.declaration_name,
                        "code": failure.code.value,
                        "detail": failure.detail,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def _reset_partition(out_dir: Path, source: str) -> tuple[Path, Path]:
    """Truncate a source's partitions so a full re-run is idempotent rather
    than append-duplicating (§10 rule 4)."""
    theorems_path = out_dir / "theorems" / f"{source}.jsonl"
    failures_path = out_dir / "failures" / f"{source}.jsonl"
    for path in (theorems_path, failures_path):
        if path.exists():
            path.unlink()
    return theorems_path, failures_path


def extract_repository_files(
    backend: LeanInteractBackend,
    checkout: Path,
    rel_paths: list[str],
    *,
    source: str,
    source_revision: str,
    context_id: str,
    out_dir: Path,
    timeout_seconds: float = 300.0,
) -> ExtractStats:
    """Extract every proposition declaration from each file via FileCommand."""
    stats = ExtractStats()
    created_at = datetime.datetime.now(tz=datetime.UTC)
    theorems_path, failures_path = _reset_partition(out_dir, source)
    for index, rel in enumerate(rel_paths):
        result = backend.run(
            LeanRequest(
                request_id=f"{source}-file-{index}",
                context_id=context_id,
                file_path=Path(rel),
                declarations=True,
                timeout_seconds=timeout_seconds,
            )
        )
        stats.sources_processed += 1
        if result.status not in _VALID:
            stats.source_not_elaborating += 1
            continue
        source_text = (checkout / rel).read_text(encoding="utf-8")
        extraction = extract_from_declarations(
            SourceIdentity(
                source=source,
                source_revision=source_revision,
                source_record=rel,
                context_id=context_id,
                source_file=rel,
            ),
            source_text,
            list(result.declarations),
            created_at=created_at,
            elaboration_status=ValidationStatus.ELABORATES,
            lean_result_id=result.request_hash,
        )
        stats.declarations_seen += len(result.declarations)
        stats.accepted += len(extraction.accepted)
        stats.failures += len(extraction.failures)
        for failure in extraction.failures:
            stats.failure_codes[failure.code.value] += 1
        _write_records(extraction, theorems_path, failures_path)
    return stats


def extract_dataset_snippets(
    backend: LeanInteractBackend,
    rows: list[dict[str, object]],
    *,
    source: str,
    source_revision: str,
    context_id: str,
    out_dir: Path,
    id_key: str = "uuid",
    lean_key: str = "lean_code",
    revalidate: bool = True,
    timeout_seconds: float = 180.0,
) -> ExtractStats:
    """Extract the theorem from each self-contained snippet, revalidating the
    stripped statement re-elaborates in its own context (§12.2 step 7)."""
    stats = ExtractStats()
    created_at = datetime.datetime.now(tz=datetime.UTC)
    theorems_path, failures_path = _reset_partition(out_dir, source)
    for index, row in enumerate(rows):
        snippet = str(row.get(lean_key, ""))
        record_id = str(row.get(id_key, f"row-{index}"))
        stats.sources_processed += 1
        decl_result = backend.run(
            LeanRequest(
                request_id=f"{source}-snip-{index}",
                context_id=context_id,
                code=snippet,
                declarations=True,
                timeout_seconds=timeout_seconds,
            )
        )
        if decl_result.status not in _VALID or not decl_result.declarations:
            stats.source_not_elaborating += 1
            continue
        declarations = list(decl_result.declarations)
        extraction = extract_from_declarations(
            SourceIdentity(
                source=source,
                source_revision=source_revision,
                source_record=record_id,
                context_id=context_id,
            ),
            snippet,
            declarations,
            created_at=created_at,
            elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
            lean_result_id=decl_result.request_hash,
        )
        stats.declarations_seen += len(declarations)
        stats.failures += len(extraction.failures)
        for failure in extraction.failures:
            stats.failure_codes[failure.code.value] += 1
        confirmed = []
        for extracted in extraction.accepted:
            if revalidate:
                reval = backend.run(
                    LeanRequest(
                        request_id=f"{source}-reval-{index}-{extracted.theorem.declaration_name}",
                        context_id=context_id,
                        code=reconstruct_for_revalidation(
                            snippet, extracted.declaration, extracted.proof_stripped
                        ),
                        allow_sorry=True,
                        timeout_seconds=timeout_seconds,
                    )
                )
                if reval.status != LeanStatus.VALID_WITH_SORRY:
                    stats.revalidation_failed += 1
                    continue
                stats.revalidated_ok += 1
            confirmed.append(extracted)
        stats.accepted += len(confirmed)
        _write_records(ExtractionResult(accepted=tuple(confirmed)), theorems_path, failures_path)
    return stats


def write_extraction_manifest(
    stats: ExtractStats,
    *,
    source: str,
    source_revision: str,
    run_id: str,
    code: CodeState,
    out_dir: Path,
    root: Path,
) -> Path:
    theorems_path = out_dir / "theorems" / f"{source}.jsonl"
    checksums = {}
    if theorems_path.is_file():
        checksums[str(theorems_path.relative_to(root))] = hash_file(theorems_path)
    from leanfaith.config.hashing import hash_canonical

    manifest = OutputManifest(
        stage=DataStage.ELABORATED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id=run_id,
        source=source,
        source_revision=source_revision,
        config_hash=hash_canonical(
            {"source": source, "revision": source_revision, "adapter": "extract_v1"}
        ),
        record_schema_version=1,
        row_count=stats.accepted,
        file_checksums=checksums,
        code=code,
        created_at=datetime.datetime.now(tz=datetime.UTC),
        notes=json.dumps(stats.as_dict(), sort_keys=True),
    )
    manifest_path = out_dir / "manifests" / f"{source}.json"
    write_manifest(manifest, manifest_path)
    return manifest_path
