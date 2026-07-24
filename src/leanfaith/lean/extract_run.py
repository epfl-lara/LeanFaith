"""Extraction orchestration and partition writing (PLAN.md §12, LF-012).

Drives the LeanInteract backend over repository files (FileCommand) and
dataset snippets (Command), applies the range-based proof strip, revalidates,
and writes JSONL partitions plus an OutputManifest. Direct runs are
idempotent; scale orchestration adds content-bound completed-chunk markers so
interrupted runs can resume without changing the frozen input order.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from leanfaith.config.hashing import hash_file
from leanfaith.lean.extraction import (
    ExtractedDeclaration,
    ExtractionFailure,
    ExtractionFailureCode,
    ExtractionResult,
    SourceIdentity,
    extract_from_declarations,
    reconstruct_for_revalidation,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import run_with_retries
from leanfaith.representations import (
    RepresentationBatch,
    TheoremForRepresentation,
    build_representation_batch,
)
from leanfaith.schemas import (
    ArtifactClass,
    CodeState,
    DataStage,
    OutputManifest,
    ValidationStatus,
    write_manifest,
)
from leanfaith.sources.hf_sft_classic import (
    ParsedRow,
    strip_completed_proof,
)
from leanfaith.sources.hf_sft_classic import (
    parse_row as parse_sft_classic_row,
)

_VALID = (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY)


def _run_request(backend: LeanInteractBackend, request: LeanRequest) -> LeanResult:
    """Apply the fixed infrastructure-only retry policy to extraction requests."""

    return run_with_retries(backend.run, request).result


def _failure_code_for_status(status: LeanStatus) -> ExtractionFailureCode:
    if status == LeanStatus.TIMEOUT:
        return ExtractionFailureCode.TIMEOUT
    if status == LeanStatus.CRASH:
        return ExtractionFailureCode.WORKER_CRASH
    if status == LeanStatus.SETUP_ERROR:
        return ExtractionFailureCode.IMPORT_FAILURE
    if status == LeanStatus.UNSUPPORTED:
        return ExtractionFailureCode.UNSUPPORTED_STRUCTURE
    return ExtractionFailureCode.SOURCE_NON_ELABORATION


def _persist_source_failure(
    stats: ExtractStats,
    failures_path: Path,
    source_record: str,
    code: ExtractionFailureCode,
    detail: str,
) -> None:
    failure = ExtractionFailure(source_record, None, code, detail, outcome_level="row")
    stats.failures += 1
    stats.failure_codes[code.value] += 1
    _append_failures(ExtractionResult(failures=(failure,)), failures_path)


@dataclass(slots=True)
class ExtractStats:
    sources_processed: int = 0
    declarations_seen: int = 0
    accepted: int = 0
    failures: int = 0
    revalidated_ok: int = 0
    revalidation_failed: int = 0
    source_not_elaborating: int = 0
    elaborating_no_declarations: int = 0
    partial_declarations_reported: int = 0
    failure_codes: Counter[str] = field(default_factory=Counter)
    row_outcomes: Counter[str] = field(default_factory=Counter)
    declaration_outcomes: Counter[str] = field(default_factory=Counter)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ExtractStats:
        """Rehydrate a completed chunk marker for deterministic resume."""

        def integer(name: str) -> int:
            value = payload.get(name, 0)
            if not isinstance(value, int):
                raise ValueError(f"chunk stats {name} must be an integer")
            return value

        def counter(name: str) -> Counter[str]:
            value = payload.get(name, {})
            if not isinstance(value, dict):
                raise ValueError(f"chunk stats {name} must be an object")
            parsed: Counter[str] = Counter()
            for key, item in value.items():
                if not isinstance(item, int):
                    raise ValueError(f"chunk stats {name}.{key} must be an integer")
                parsed[str(key)] = item
            return parsed

        return cls(
            sources_processed=integer("sources_processed"),
            declarations_seen=integer("declarations_seen"),
            accepted=integer("accepted"),
            failures=integer("failures"),
            revalidated_ok=integer("revalidated_ok"),
            revalidation_failed=integer("revalidation_failed"),
            source_not_elaborating=integer("source_not_elaborating"),
            elaborating_no_declarations=integer("elaborating_no_declarations"),
            partial_declarations_reported=integer("partial_declarations_reported"),
            failure_codes=counter("failure_codes"),
            row_outcomes=counter("row_outcomes"),
            declaration_outcomes=counter("declaration_outcomes"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "sources_processed": self.sources_processed,
            "declarations_seen": self.declarations_seen,
            "accepted": self.accepted,
            "failures": self.failures,
            "revalidated_ok": self.revalidated_ok,
            "revalidation_failed": self.revalidation_failed,
            "source_not_elaborating": self.source_not_elaborating,
            "elaborating_no_declarations": self.elaborating_no_declarations,
            "partial_declarations_reported": self.partial_declarations_reported,
            "failure_codes": dict(self.failure_codes),
            "row_outcomes": dict(self.row_outcomes),
            "declaration_outcomes": dict(self.declaration_outcomes),
        }

    def validate_accounting(self) -> None:
        if sum(self.row_outcomes.values()) != self.sources_processed:
            raise ValueError(
                "row terminal outcomes do not reconcile: "
                f"{sum(self.row_outcomes.values())} outcomes for "
                f"{self.sources_processed} attempted sources"
            )
        if sum(self.declaration_outcomes.values()) != self.declarations_seen:
            raise ValueError(
                "declaration terminal outcomes do not reconcile: "
                f"{sum(self.declaration_outcomes.values())} outcomes for "
                f"{self.declarations_seen} declarations"
            )

    def merge(self, other: ExtractStats) -> None:
        """Accumulate one independently written extraction chunk."""

        for field_name in (
            "sources_processed",
            "declarations_seen",
            "accepted",
            "failures",
            "revalidated_ok",
            "revalidation_failed",
            "source_not_elaborating",
            "elaborating_no_declarations",
            "partial_declarations_reported",
        ):
            setattr(self, field_name, getattr(self, field_name) + getattr(other, field_name))
        self.failure_codes.update(other.failure_codes)
        self.row_outcomes.update(other.row_outcomes)
        self.declaration_outcomes.update(other.declaration_outcomes)


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
                        "outcome_level": failure.outcome_level,
                        "extraction_route": failure.extraction_route,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


def _open_partitions(out_dir: Path, source: str) -> tuple[Path, Path]:
    """Fresh ``.partial`` staging files for a run. Writing to partials and
    atomically renaming at the end (``_finalize_partitions``) keeps any prior
    good partition intact until the run completes, so a crashed or degraded
    re-run cannot destroy it (idempotent AND crash-safe, §10 rule 4)."""
    theorems_partial = out_dir / "theorems" / f"{source}.jsonl.partial"
    failures_partial = out_dir / "failures" / f"{source}.jsonl.partial"
    for path in (theorems_partial, failures_partial):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return theorems_partial, failures_partial


def _finalize_partition(partial: Path) -> None:
    """Atomically move one staged partition into place, or drop it (and any
    stale prior version) when the run produced nothing for it — no zero-byte
    files, and the swap only happens after all writes succeed."""
    final = partial.with_suffix("")  # strip .partial
    if partial.stat().st_size == 0:
        partial.unlink()
        if final.exists():
            final.unlink()
    else:
        os.replace(partial, final)


def _finalize_partitions(theorems_partial: Path, failures_partial: Path) -> None:
    _finalize_partition(theorems_partial)
    _finalize_partition(failures_partial)


def merge_extraction_partitions(
    chunk_out_dirs: list[Path],
    *,
    out_dir: Path,
    source: str,
) -> None:
    """Merge ordered worker partitions through the same atomic finalization path."""

    theorem_partial, failure_partial = _open_partitions(out_dir, source)
    for chunk_out_dir in chunk_out_dirs:
        for destination, partition_kind in (
            (theorem_partial, "theorems"),
            (failure_partial, "failures"),
        ):
            source_path = chunk_out_dir / partition_kind / f"{source}.jsonl"
            if not source_path.is_file():
                continue
            with (
                source_path.open("rb") as source_handle,
                destination.open("ab") as destination_handle,
            ):
                shutil.copyfileobj(source_handle, destination_handle)
    _finalize_partitions(theorem_partial, failure_partial)


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
    theorems_path, failures_path = _open_partitions(out_dir, source)
    for index, rel in enumerate(rel_paths):
        result = _run_request(
            backend,
            LeanRequest(
                request_id=f"{source}-file-{index}",
                context_id=context_id,
                file_path=Path(rel),
                declarations=True,
                timeout_seconds=timeout_seconds,
            ),
        )
        stats.sources_processed += 1
        if result.status not in _VALID:
            stats.source_not_elaborating += 1
            _persist_source_failure(
                stats,
                failures_path,
                rel,
                _failure_code_for_status(result.status),
                f"file elaboration ended as {result.status.value}",
            )
            stats.row_outcomes[_failure_code_for_status(result.status).value] += 1
            continue
        if not result.declarations:
            # Elaborates but yields no declarations: e.g. a `mutual ... end`
            # block, whose inner theorems LeanInteract does not report. Counted
            # distinctly so this known extraction gap is auditable, never
            # silently folded into non-elaborating (§12.7).
            stats.elaborating_no_declarations += 1
            _persist_source_failure(
                stats,
                failures_path,
                rel,
                ExtractionFailureCode.ELABORATING_NO_DECLARATIONS,
                "file elaborated but yielded no declarations",
            )
            stats.row_outcomes[ExtractionFailureCode.ELABORATING_NO_DECLARATIONS.value] += 1
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
        stats.declaration_outcomes["accepted"] += len(extraction.accepted)
        stats.declaration_outcomes["failed_or_skipped"] += len(result.declarations) - len(
            extraction.accepted
        )
        for failure in extraction.failures:
            stats.failure_codes[failure.code.value] += 1
        _write_records(extraction, theorems_path, failures_path)
        stats.row_outcomes["accepted" if extraction.accepted else "no_accepted_proposition"] += 1
    _finalize_partitions(theorems_path, failures_path)
    stats.validate_accounting()
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
    theorems_path, failures_path = _open_partitions(out_dir, source)
    for index, row in enumerate(rows):
        snippet = str(row.get(lean_key, ""))
        record_id = str(row.get(id_key, f"row-{index}"))
        stats.sources_processed += 1
        decl_result = _run_request(
            backend,
            LeanRequest(
                request_id=f"{source}-snip-{index}",
                context_id=context_id,
                code=snippet,
                declarations=True,
                timeout_seconds=timeout_seconds,
            ),
        )
        if decl_result.status not in _VALID:
            stats.source_not_elaborating += 1
            _persist_source_failure(
                stats,
                failures_path,
                record_id,
                _failure_code_for_status(decl_result.status),
                f"snippet elaboration ended as {decl_result.status.value}",
            )
            stats.row_outcomes[_failure_code_for_status(decl_result.status).value] += 1
            continue
        if not decl_result.declarations:
            # Elaborates but reports no declarations (e.g. a mutual block);
            # counted distinctly rather than conflated with non-elaborating.
            stats.elaborating_no_declarations += 1
            _persist_source_failure(
                stats,
                failures_path,
                record_id,
                ExtractionFailureCode.ELABORATING_NO_DECLARATIONS,
                "snippet elaborated but yielded no declarations",
            )
            stats.row_outcomes[ExtractionFailureCode.ELABORATING_NO_DECLARATIONS.value] += 1
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
        reval_failures: list[ExtractionFailure] = []
        for extracted in extraction.accepted:
            if revalidate:
                reval = _run_request(
                    backend,
                    LeanRequest(
                        request_id=f"{source}-reval-{index}-{extracted.theorem.declaration_name}",
                        context_id=context_id,
                        code=reconstruct_for_revalidation(
                            snippet, extracted.declaration, extracted.proof_stripped
                        ),
                        allow_sorry=True,
                        timeout_seconds=timeout_seconds,
                    ),
                )
                if reval.status != LeanStatus.VALID_WITH_SORRY:
                    stats.revalidation_failed += 1
                    stats.failure_codes[ExtractionFailureCode.REVALIDATION_FAILED.value] += 1
                    reval_failures.append(
                        ExtractionFailure(
                            record_id,
                            extracted.theorem.declaration_name,
                            ExtractionFailureCode.REVALIDATION_FAILED,
                            f"stripped statement re-elaborated as {reval.status.value}",
                        )
                    )
                    stats.failures += 1
                    stats.declaration_outcomes["revalidation_failed"] += 1
                    continue
                stats.revalidated_ok += 1
            confirmed.append(extracted)
            stats.declaration_outcomes["accepted"] += 1
        stats.declaration_outcomes["failed_or_skipped"] += len(declarations) - len(
            extraction.accepted
        )
        stats.accepted += len(confirmed)
        # Every excluded declaration is persisted as an explicit failure record
        # (§10 rule 5), symmetric with the repository path.
        _write_records(
            ExtractionResult(
                accepted=tuple(confirmed),
                failures=extraction.failures + tuple(reval_failures),
            ),
            theorems_path,
            failures_path,
        )
        stats.row_outcomes["accepted" if confirmed else "no_accepted_proposition"] += 1
    _finalize_partitions(theorems_path, failures_path)
    stats.validate_accounting()
    return stats


def _with_route_outcome(
    extracted: ExtractedDeclaration,
    *,
    agreement: str,
    question_status: str,
    lean_code_status: str,
    parsed: ParsedRow,
    fallback_strip_status: str,
    inline_elaboration_source: str,
) -> ExtractedDeclaration:
    metadata = dict(extracted.theorem.metadata)
    metadata.update(
        {
            "question_route_status": question_status,
            "lean_code_route_status": lean_code_status,
            "question_parse_status": parsed.parse_status,
            "data_source": parsed.data_source,
            "source_valid": parsed.source_valid,
            "proof_repair": parsed.proof_repair,
            "fallback_strip_status": fallback_strip_status,
            **parsed.metadata,
        }
    )
    theorem = extracted.theorem.model_copy(
        update={
            "question_lean_code_agreement": agreement,
            "inline_elaboration_source": inline_elaboration_source,
            "metadata": metadata,
        }
    )
    return ExtractedDeclaration(
        theorem=theorem,
        representation=extracted.representation,
        proof_stripped=extracted.proof_stripped,
        declaration=extracted.declaration,
    )


def _route_alpha_fingerprints(
    backend: LeanInteractBackend,
    extraction: ExtractionResult,
    *,
    context_id: str,
    created_at: datetime.datetime,
    snippet: str,
) -> tuple[str, ...] | None:
    """Compute the Gate-3 identity fingerprints for one accepted inline route."""

    theorems = tuple(
        TheoremForRepresentation(
            theorem_id=item.theorem.theorem_id,
            full_name=item.theorem.declaration_full_name or item.theorem.declaration_name or "",
            proof_stripped=item.proof_stripped,
            context_id=context_id,
            source_signature=item.representation.headless,
            inline_declaration=True,
            inline_source=reconstruct_for_revalidation(
                snippet, item.declaration, item.proof_stripped
            ),
        )
        for item in extraction.accepted
    )
    if not theorems or any(not theorem.full_name for theorem in theorems):
        return None
    result = build_representation_batch(
        backend,
        RepresentationBatch(
            context_id=context_id,
            import_header="import Mathlib",
            ordered_theorem_inputs=theorems,
        ),
        created_at=created_at,
    )
    fingerprints = tuple(
        sorted(
            record.alpha_identity_fingerprint
            for record in result.ordered_representation_records
            if record.alpha_identity_fingerprint is not None
        )
    )
    return fingerprints if len(fingerprints) == len(theorems) else None


def _elaborate_dataset_route(
    backend: LeanInteractBackend,
    parsed: ParsedRow,
    *,
    route: str,
    snippet: str,
    source: str,
    context_id: str,
    created_at: datetime.datetime,
    timeout_seconds: float,
) -> tuple[LeanResult, ExtractionResult]:
    result = _run_request(
        backend,
        LeanRequest(
            request_id=f"{source}-{parsed.source_record_id[:16]}-{route}",
            context_id=context_id,
            code=snippet,
            declarations=True,
            allow_sorry=route == "question_statement",
            timeout_seconds=timeout_seconds,
        ),
    )
    if result.status not in _VALID or not result.declarations:
        return result, ExtractionResult()
    identity = SourceIdentity(
        source=source,
        source_revision=parsed.revision,
        source_record=parsed.source_record_id,
        source_record_id=parsed.source_record_id,
        upstream_uuid=parsed.upstream_uuid,
        raw_row_hash=parsed.raw_row_hash,
        question_hash=parsed.question_hash,
        lean_code_hash=parsed.lean_code_hash,
        extraction_route=route,
        nl_pair_eligibility=(
            "eligible" if route == "question_statement" and parsed.eligible_nl else "unverified"
        ),
        context_id=context_id,
        source_split=parsed.split,
        nl_source_link=parsed.nl_source_link,
        nl_trust=parsed.nl_trust,
    )
    extraction = extract_from_declarations(
        identity,
        snippet,
        list(result.declarations),
        created_at=created_at,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        lean_result_id=result.request_hash,
    )
    return result, extraction


def _revalidate_route(
    backend: LeanInteractBackend,
    extraction: ExtractionResult,
    *,
    snippet: str,
    source: str,
    parsed: ParsedRow,
    context_id: str,
    timeout_seconds: float,
) -> tuple[ExtractionResult, int]:
    confirmed: list[ExtractedDeclaration] = []
    failures = list(extraction.failures)
    failed_count = 0
    for item in extraction.accepted:
        result = _run_request(
            backend,
            LeanRequest(
                request_id=(
                    f"{source}-{parsed.source_record_id[:16]}-reval-{item.theorem.declaration_name}"
                ),
                context_id=context_id,
                code=reconstruct_for_revalidation(snippet, item.declaration, item.proof_stripped),
                allow_sorry=True,
                timeout_seconds=timeout_seconds,
            ),
        )
        if result.status != LeanStatus.VALID_WITH_SORRY:
            failed_count += 1
            failures.append(
                ExtractionFailure(
                    parsed.source_record_id,
                    item.theorem.declaration_name,
                    ExtractionFailureCode.REVALIDATION_FAILED,
                    f"stripped statement re-elaborated as {result.status.value}",
                )
            )
        else:
            confirmed.append(item)
    return ExtractionResult(accepted=tuple(confirmed), failures=tuple(failures)), failed_count


def extract_sft_classic_rows(
    backend: LeanInteractBackend,
    rows: list[dict[str, object]],
    *,
    source_revision: str,
    split: str,
    row_offset: int,
    source_row_indices: list[int] | None = None,
    context_id: str,
    out_dir: Path,
    source: str = "sft_classic",
    timeout_seconds: float = 180.0,
) -> ExtractStats:
    """Question-first two-route extraction for the private sft_classic source."""

    stats = ExtractStats()
    created_at = datetime.datetime.now(tz=datetime.UTC)
    theorems_path, failures_path = _open_partitions(out_dir, source)
    if source_row_indices is not None and len(source_row_indices) != len(rows):
        raise ValueError("source_row_indices must have one entry per row")
    for local_index, raw_row in enumerate(rows):
        source_row_index = (
            source_row_indices[local_index]
            if source_row_indices is not None
            else row_offset + local_index
        )
        parsed = parse_sft_classic_row(
            raw_row,
            revision=source_revision,
            split=split,
            row_index=source_row_index,
        )
        stats.sources_processed += 1
        question_snippet = parsed.question_lean_block or ""
        stripped_fallback = strip_completed_proof(parsed.lean_source)
        fallback_strip_status = "proof_stripped" if stripped_fallback is not None else "unsupported"
        fallback_snippet = stripped_fallback or ""
        question_result, question_extraction = _elaborate_dataset_route(
            backend,
            parsed,
            route="question_statement",
            snippet=question_snippet,
            source=source,
            context_id=context_id,
            created_at=created_at,
            timeout_seconds=timeout_seconds,
        )
        fallback_result, fallback_extraction = _elaborate_dataset_route(
            backend,
            parsed,
            route="lean_code_fallback",
            snippet=fallback_snippet,
            source=source,
            context_id=context_id,
            created_at=created_at,
            timeout_seconds=timeout_seconds,
        )
        question_has_declarations = question_result.status in _VALID and bool(
            question_result.declarations
        )
        if question_extraction.accepted or question_has_declarations:
            chosen_snippet = question_snippet
            chosen = question_extraction
            chosen_result = question_result
            route = "question_statement"
        else:
            chosen_snippet = fallback_snippet
            chosen = fallback_extraction
            chosen_result = fallback_result
            route = "lean_code_fallback"
        # Declaration accounting covers only declarations from a valid
        # canonical route. LeanInteract can return partial declaration info
        # alongside an INVALID response; those names are untrusted diagnostics,
        # not declarations with independently persisted terminal outcomes.
        canonical_declarations = (
            chosen_result.declarations if chosen_result.status in _VALID else ()
        )
        partial_declarations = len(chosen_result.declarations) - len(canonical_declarations)
        stats.declarations_seen += len(canonical_declarations)
        stats.partial_declarations_reported += partial_declarations

        question_status = question_result.status.value
        fallback_status = fallback_result.status.value
        if question_extraction.accepted and fallback_extraction.accepted:
            question_alpha = _route_alpha_fingerprints(
                backend,
                question_extraction,
                context_id=context_id,
                created_at=created_at,
                snippet=question_snippet,
            )
            fallback_alpha = _route_alpha_fingerprints(
                backend,
                fallback_extraction,
                context_id=context_id,
                created_at=created_at,
                snippet=fallback_snippet,
            )
            if question_alpha is None or fallback_alpha is None:
                agreement = "alpha_comparison_failed"
            elif question_alpha == fallback_alpha:
                agreement = "alpha_fingerprint_equal"
            else:
                agreement = "alpha_fingerprint_mismatch"
        elif route == "question_statement":
            agreement = "fallback_unavailable"
        elif chosen.accepted:
            agreement = "question_unavailable"
        else:
            agreement = "neither_route_accepted"

        if not chosen.accepted:
            status = chosen_result.status
            if not question_snippet:
                code = ExtractionFailureCode.MISSING_LEAN_FENCE
            elif chosen.failures:
                code = chosen.failures[0].code
            elif route == "lean_code_fallback" and stripped_fallback is None:
                code = ExtractionFailureCode.UNSUPPORTED_STRUCTURE
            elif status in _VALID and not chosen_result.declarations:
                code = ExtractionFailureCode.ELABORATING_NO_DECLARATIONS
            else:
                code = _failure_code_for_status(status)
            if chosen.failures:
                stats.failures += len(chosen.failures)
                for failure in chosen.failures:
                    stats.failure_codes[failure.code.value] += 1
                _write_records(chosen, theorems_path, failures_path)
            _persist_source_failure(
                stats,
                failures_path,
                parsed.source_record_id,
                code,
                f"question={question_status}; fallback={fallback_status}; "
                f"partial_declarations_reported={partial_declarations}",
            )
            stats.source_not_elaborating += int(
                question_result.status not in _VALID and fallback_result.status not in _VALID
            )
            if canonical_declarations:
                stats.declaration_outcomes["failed_or_skipped"] += len(canonical_declarations)
            stats.row_outcomes[code.value] += 1
            continue

        confirmed, reval_failed = _revalidate_route(
            backend,
            chosen,
            snippet=chosen_snippet,
            source=source,
            parsed=parsed,
            context_id=context_id,
            timeout_seconds=timeout_seconds,
        )
        stats.revalidation_failed += reval_failed
        stats.revalidated_ok += len(confirmed.accepted)
        stats.accepted += len(confirmed.accepted)
        stats.failures += len(confirmed.failures)
        stats.declaration_outcomes["accepted"] += len(confirmed.accepted)
        stats.declaration_outcomes["failed_or_skipped"] += len(canonical_declarations) - len(
            confirmed.accepted
        )
        for failure in confirmed.failures:
            stats.failure_codes[failure.code.value] += 1
        enriched = tuple(
            _with_route_outcome(
                item,
                agreement=agreement,
                question_status=question_status,
                lean_code_status=fallback_status,
                parsed=parsed,
                fallback_strip_status=fallback_strip_status,
                inline_elaboration_source=reconstruct_for_revalidation(
                    chosen_snippet, item.declaration, item.proof_stripped
                ),
            )
            for item in confirmed.accepted
        )
        _write_records(
            ExtractionResult(accepted=enriched, failures=confirmed.failures),
            theorems_path,
            failures_path,
        )
        stats.row_outcomes[
            "accepted_question_statement"
            if confirmed.accepted and route == "question_statement"
            else (
                "accepted_lean_code_fallback"
                if confirmed.accepted
                else ExtractionFailureCode.REVALIDATION_FAILED.value
            )
        ] += 1
    _finalize_partitions(theorems_path, failures_path)
    stats.validate_accounting()
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
    input_paths: tuple[Path, ...] = (),
    environment_hash: str | None = None,
    context_hash: str | None = None,
    config_payload: dict[str, object] | None = None,
) -> Path:
    theorems_path = out_dir / "theorems" / f"{source}.jsonl"
    failures_path = out_dir / "failures" / f"{source}.jsonl"
    output_checksums = (
        {
            str(
                theorems_path.relative_to(root)
                if theorems_path.is_relative_to(root)
                else theorems_path
            ): hash_file(theorems_path)
        }
        if theorems_path.is_file()
        else {}
    )
    failure_checksums = (
        {
            str(
                failures_path.relative_to(root)
                if failures_path.is_relative_to(root)
                else failures_path
            ): hash_file(failures_path)
        }
        if failures_path.is_file()
        else {}
    )
    checksums = {**output_checksums, **failure_checksums}
    input_checksums = {
        str(path.relative_to(root) if path.is_relative_to(root) else path): hash_file(path)
        for path in input_paths
    }
    from leanfaith.config.hashing import hash_canonical

    manifest = OutputManifest(
        stage=DataStage.ELABORATED,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id=run_id,
        source=source,
        source_revision=source_revision,
        config_hash=hash_canonical(
            {
                "source": source,
                "revision": source_revision,
                "adapter": "extract_v2",
                **(config_payload or {}),
            }
        ),
        record_schema_version=1,
        row_count=stats.accepted,
        attempted_row_count=stats.sources_processed,
        declaration_count=stats.declarations_seen,
        terminal_outcome_counts=dict(stats.row_outcomes),
        file_checksums=checksums,
        input_partition_checksums=input_checksums,
        output_partition_checksums=output_checksums,
        failure_partition_checksums=failure_checksums,
        environment_hash=environment_hash,
        context_hash=context_hash,
        code_tree_hash=code.code_tree_hash,
        code=code,
        created_at=datetime.datetime.now(tz=datetime.UTC),
        notes=json.dumps(stats.as_dict(), sort_keys=True),
    )
    manifest_path = out_dir / "manifests" / f"{source}.json"
    write_manifest(manifest, manifest_path)
    return manifest_path
