"""Additive Phase-3 representation signatures for resolved benchmarks.

The Phase-2 identity registry (``data/benchmarks/frozen_ids.json``) is an
immutable input.  This module elaborates the Lean statements of locally
resolved benchmarks through the canonical Lean backend, records explicit
per-statement failures, builds hash-to-statement retrieval indexes, and writes
a *new* versioned registry whose identity/text fields are unchanged.

Only the backend adapter may import LeanInteract.  This module depends on the
``LeanBackend`` protocol and the existing Lean-backed extraction/
representation builders.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import (
    FrozenBenchmark,
    FrozenRegistry,
    append_representation_signatures,
    lean_hash,
    load_frozen_registry,
    nl_hash,
)
from leanfaith.lean.extraction import (
    PROPOSITION_KINDS,
    SourceIdentity,
    extract_from_declarations,
    reconstruct_for_revalidation,
)
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.response_normalization import normalize_exception
from leanfaith.lean.session_policy import RetryPolicy, run_with_retries
from leanfaith.representations.pipeline import (
    RepresentationBatch,
    TheoremForRepresentation,
    build_representation_batch,
)
from leanfaith.representations.views import NORMALIZATION_VERSION, signature_near_dup_hash
from leanfaith.schemas.enums import ValidationStatus
from leanfaith.schemas.ids import HEX64_PATTERN
from leanfaith.schemas.manifest import require_utc

BENCHMARK_SIGNATURE_SCHEMA_VERSION: Literal[1] = 1
BENCHMARK_SIGNATURE_SELECTION_VERSION: Literal["benchmark_signature_inputs_v1"] = (
    "benchmark_signature_inputs_v1"
)
BENCHMARK_SIGNATURE_REGISTRY_FILENAME = "frozen_ids.representations_v1.json"
BENCHMARK_SIGNATURE_INDEX_FILENAME = "frozen_representation_signatures_v1.json"

_SIGNATURE_VIEWS = (
    "headless",
    "signature_pp",
    "signature_explicit",
    "alpha_identity_fingerprint",
)

_BENCHMARK_RETRY_POLICY = RetryPolicy(
    max_attempts=2,
    retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.INTERNAL_ERROR, LeanStatus.TIMEOUT}),
)


def _benchmark_request_hash(request: LeanRequest) -> str:
    """Deterministic fallback hash for an exception escaping a backend.

    Production adapters normally normalize exceptions and persist raw responses
    themselves. This hash is used only when a non-conforming backend raises;
    attempt metadata is intentionally excluded so retries retain one semantic
    request identity while raw artifacts use the attempt lineage.
    """

    return hash_canonical(
        {
            "allow_sorry": request.allow_sorry,
            "code": request.code,
            "context_id": request.context_id,
            "declarations": request.declarations,
            "file_path": str(request.file_path) if request.file_path is not None else None,
            "infotree": request.infotree,
            "root_goals": request.root_goals,
            "timeout_seconds": request.timeout_seconds,
        }
    )


def _normalize_backend_exception(request: LeanRequest, exc: BaseException) -> LeanResult:
    context_fingerprint = request.context_id.removeprefix("ctx:")
    return normalize_exception(
        request,
        exc,
        request_hash=_benchmark_request_hash(request),
        context_fingerprint=context_fingerprint,
        elapsed_ms=0,
        raw_response_path=None,
    )


class _RetryingBenchmarkBackend:
    """Bounded infrastructure-only retry boundary for benchmark processing.

    The canonical backend should already return one normalized ``LeanResult``
    per request. The defensive exception normalization here also covers test
    doubles and third-party protocol implementations. Attempt numbers are
    added to request metadata, so a production adapter stores append-only raw
    response lineage without changing the semantic request hash.
    """

    def __init__(self, delegate: LeanBackend) -> None:
        self._delegate = delegate

    def run(self, request: LeanRequest) -> LeanResult:
        def safe_run(attempt_request: LeanRequest) -> LeanResult:
            try:
                return self._delegate.run(attempt_request)
            except Exception as exc:  # normalized infrastructure result, never a label
                return _normalize_backend_exception(attempt_request, exc)

        if "attempt" in request.metadata:
            return safe_run(request)
        return run_with_retries(safe_run, request, _BENCHMARK_RETRY_POLICY).result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        if not requests:
            return []

        final: list[LeanResult | None] = [None] * len(requests)
        pending = list(range(len(requests)))
        for attempt_index in range(_BENCHMARK_RETRY_POLICY.max_attempts):
            attempt_requests = [
                replace(
                    requests[index],
                    metadata={
                        **dict(requests[index].metadata),
                        "attempt": str(attempt_index),
                    },
                )
                for index in pending
            ]
            try:
                batch_results: Sequence[object] = self._delegate.run_batch(attempt_requests)
                if len(batch_results) != len(attempt_requests):
                    raise RuntimeError(
                        "benchmark backend returned a batch with the wrong result count: "
                        f"expected {len(attempt_requests)}, got {len(batch_results)}"
                    )
            except Exception as exc:
                batch_results = tuple(
                    _normalize_backend_exception(attempt_request, exc)
                    for attempt_request in attempt_requests
                )

            retry: list[int] = []
            for index, attempt_request, item in zip(
                pending, attempt_requests, batch_results, strict=True
            ):
                if isinstance(item, BaseException):
                    result = _normalize_backend_exception(attempt_request, item)
                elif isinstance(item, LeanResult):
                    result = item
                else:
                    result = _normalize_backend_exception(
                        attempt_request,
                        TypeError(
                            "benchmark backend returned an unsupported batch item "
                            f"of type {type(item).__name__}"
                        ),
                    )
                if (
                    result.status in _BENCHMARK_RETRY_POLICY.retry_statuses
                    and attempt_index + 1 < _BENCHMARK_RETRY_POLICY.max_attempts
                ):
                    retry.append(index)
                else:
                    final[index] = result
            pending = retry
            if not pending:
                break

        if pending or any(result is None for result in final):
            raise AssertionError("benchmark retry orchestration left requests without results")
        return [result for result in final if result is not None]

    def close(self) -> None:
        # This proxy does not own the shared production backend.
        return None


class BenchmarkSide(StrEnum):
    REFERENCE = "reference"
    CANDIDATE = "candidate"


class BenchmarkViewStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


class BenchmarkSignatureFailureStage(StrEnum):
    EXTRACTION = "extraction"
    TARGET_SELECTION = "target_selection"
    REPRESENTATION = "representation"
    INTERNAL = "internal"


class BenchmarkStatementInput(StrictModel):
    """One benchmark Lean statement; no label or diagnosis is retained."""

    registry_key: str
    source_id: str
    revision: str
    split: str
    row_id: str
    row_ordinal: int = Field(default=0, ge=0)
    side: BenchmarkSide
    header: str
    statement: str
    statement_id: str = Field(pattern=HEX64_PATTERN)
    input_content_hash: str = Field(pattern=HEX64_PATTERN)

    @classmethod
    def create(
        cls,
        *,
        registry_key: str,
        source_id: str,
        revision: str,
        split: str,
        row_id: str,
        side: BenchmarkSide,
        row_ordinal: int = 0,
        header: str,
        statement: str,
    ) -> BenchmarkStatementInput:
        locator = {
            "registry_key": registry_key,
            "source_id": source_id,
            "revision": revision,
            "split": split,
            "row_id": row_id,
            "row_ordinal": row_ordinal,
            "side": side.value,
        }
        return cls(
            registry_key=registry_key,
            source_id=source_id,
            revision=revision,
            split=split,
            row_id=row_id,
            row_ordinal=row_ordinal,
            side=side,
            header=header,
            statement=statement,
            statement_id=hash_canonical(locator),
            input_content_hash=hash_canonical(
                {**locator, "header": header, "statement": statement}
            ),
        )


class BenchmarkSignatureFailure(StrictModel):
    statement_id: str = Field(pattern=HEX64_PATTERN)
    stage: BenchmarkSignatureFailureStage
    code: str
    detail: str


class BenchmarkSignatureRecord(StrictModel):
    """Hash-only representation result for one benchmark statement."""

    schema_version: Literal[1]
    statement_id: str = Field(pattern=HEX64_PATTERN)
    input_content_hash: str = Field(pattern=HEX64_PATTERN)
    registry_key: str
    source_id: str
    revision: str
    split: str
    row_id: str
    row_ordinal: int = Field(default=0, ge=0)
    side: BenchmarkSide
    context_id: str
    normalization_version: str = NORMALIZATION_VERSION
    elaboration_status: str
    theorem_id: str | None = None
    headless_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    signature_pp_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    signature_explicit_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    alpha_identity_fingerprint: str | None = Field(default=None, pattern=HEX64_PATTERN)
    view_status: dict[str, BenchmarkViewStatus]
    failure_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _views_are_complete_and_consistent(self) -> BenchmarkSignatureRecord:
        if set(self.view_status) != set(_SIGNATURE_VIEWS):
            raise ValueError(f"view_status must contain exactly {_SIGNATURE_VIEWS!r}")
        values = {
            "headless": self.headless_hash,
            "signature_pp": self.signature_pp_hash,
            "signature_explicit": self.signature_explicit_hash,
            "alpha_identity_fingerprint": self.alpha_identity_fingerprint,
        }
        for view, value in values.items():
            status = self.view_status[view]
            if value is None and status == BenchmarkViewStatus.OK:
                raise ValueError(f"{view} hash is null but marked ok")
            if value is not None and status != BenchmarkViewStatus.OK:
                raise ValueError(f"{view} hash is present but marked {status}")
        if tuple(sorted(set(self.failure_codes))) != self.failure_codes:
            raise ValueError("failure_codes must be sorted and unique")
        return self

    def representation_hashes(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.headless_hash,
                self.signature_pp_hash,
                self.signature_explicit_hash,
                self.alpha_identity_fingerprint,
            )
            if value is not None
        )


class BenchmarkSignatureAccounting(StrictModel):
    attempted: int = Field(ge=0)
    elaborated: int = Field(ge=0)
    all_views_ok: int = Field(ge=0)
    records_with_failures: int = Field(ge=0)
    by_benchmark: dict[str, int]
    view_success: dict[str, int]
    failure_counts: dict[str, int]


class BenchmarkSignatureArtifact(StrictModel):
    """Detailed additive artifact and retrieval indexes for Phase 3."""

    schema_version: Literal[1]
    selection_version: Literal["benchmark_signature_inputs_v1"]
    identity_registry_sha256: str = Field(pattern=HEX64_PATTERN)
    context_id: str
    normalization_version: str = NORMALIZATION_VERSION
    generated_at: datetime.datetime
    input_checksums: dict[str, str]
    records: tuple[BenchmarkSignatureRecord, ...]
    failures: tuple[BenchmarkSignatureFailure, ...]
    retrieval_indexes: dict[str, dict[str, tuple[str, ...]]]
    accounting: BenchmarkSignatureAccounting

    @model_validator(mode="after")
    def _artifact_reconciles(self) -> BenchmarkSignatureArtifact:
        require_utc(self.generated_at)
        if not self.input_checksums:
            raise ValueError("input_checksums must not be empty")
        if list(self.input_checksums) != sorted(self.input_checksums):
            raise ValueError("input_checksums must be sorted by key")
        for key, digest in self.input_checksums.items():
            if not key:
                raise ValueError("input_checksums contains an empty key")
            if re.fullmatch(HEX64_PATTERN, digest) is None:
                raise ValueError(f"input_checksums has an invalid SHA-256 digest for {key!r}")
        statement_ids = [record.statement_id for record in self.records]
        if statement_ids != sorted(set(statement_ids)):
            raise ValueError("records must be sorted by unique statement_id")
        if any(record.context_id != self.context_id for record in self.records):
            raise ValueError("all records must use the artifact context_id")
        expected_indexes = _build_retrieval_indexes(self.records)
        if self.retrieval_indexes != expected_indexes:
            raise ValueError("retrieval indexes do not reconcile with records")
        expected_accounting = _build_accounting(self.records)
        if self.accounting != expected_accounting:
            raise ValueError("accounting does not reconcile with records")
        expected_failure_pairs = sorted(
            (record.statement_id, code) for record in self.records for code in record.failure_codes
        )
        actual_failure_pairs = [(failure.statement_id, failure.code) for failure in self.failures]
        if actual_failure_pairs != sorted(actual_failure_pairs):
            raise ValueError("failures must be sorted by statement_id and code")
        if len(actual_failure_pairs) != len(set(actual_failure_pairs)):
            raise ValueError("failures must be unique by statement_id and code")
        if actual_failure_pairs != expected_failure_pairs:
            raise ValueError("failure objects do not exactly match record.failure_codes")
        return self


class BenchmarkSignatureWorkManifest(StrictModel):
    schema_version: Literal[1]
    selection_version: Literal["benchmark_signature_inputs_v1"]
    identity_registry_sha256: str = Field(pattern=HEX64_PATTERN)
    context_id: str
    normalization_version: str = NORMALIZATION_VERSION
    generated_at: datetime.datetime
    ordered_inputs: tuple[tuple[str, str], ...]

    @model_validator(mode="after")
    def _manifest_reconciles(self) -> BenchmarkSignatureWorkManifest:
        require_utc(self.generated_at)
        ordered_inputs = list(self.ordered_inputs)
        if ordered_inputs != sorted(ordered_inputs):
            raise ValueError("ordered_inputs must be sorted")
        if len(ordered_inputs) != len(set(ordered_inputs)):
            raise ValueError("ordered_inputs must be unique")
        statement_ids = [statement_id for statement_id, _ in ordered_inputs]
        if len(statement_ids) != len(set(statement_ids)):
            raise ValueError("ordered_inputs statement IDs must be unique")
        for statement_id, content_hash in ordered_inputs:
            if re.fullmatch(HEX64_PATTERN, statement_id) is None:
                raise ValueError("ordered_inputs contains an invalid statement_id")
            if re.fullmatch(HEX64_PATTERN, content_hash) is None:
                raise ValueError("ordered_inputs contains an invalid input_content_hash")
        return self


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: benchmark row must be an object")
            rows.append(value)
    return rows


def _resolved_benchmark(registry: FrozenRegistry, key: str) -> FrozenBenchmark:
    matches = [benchmark for benchmark in registry.benchmarks if benchmark.registry_key == key]
    if len(matches) != 1:
        raise ValueError(f"identity registry must contain exactly one {key!r} entry")
    benchmark = matches[0]
    if not benchmark.resolved or benchmark.source_id is None or benchmark.revision is None:
        raise ValueError(f"benchmark {key!r} is not locally resolved")
    return benchmark


def load_resolved_benchmark_inputs(
    *,
    identity_registry_path: Path,
    proofnet_dir: Path,
    formalrx_jsonl: Path,
) -> tuple[tuple[BenchmarkStatementInput, ...], dict[str, str]]:
    """Load only benchmark inputs; labels/diagnoses are deliberately ignored."""

    registry = load_frozen_registry(identity_registry_path)
    proofnet = _resolved_benchmark(registry, "proofnetverif")
    formalrx = _resolved_benchmark(registry, "formalrx_test")
    assert proofnet.source_id is not None and proofnet.revision is not None
    assert formalrx.source_id is not None and formalrx.revision is not None

    proofnet_manifest_path = proofnet_dir / "manifest.json"
    proofnet_manifest = json.loads(proofnet_manifest_path.read_text(encoding="utf-8"))
    if proofnet_manifest.get("source_revision") != proofnet.revision:
        raise ValueError("ProofNetVerif local manifest revision does not match identity registry")

    statements: list[BenchmarkStatementInput] = []
    observed_proofnet_rows: list[str] = []
    observed_proofnet_nl_hashes: set[str] = set()
    observed_proofnet_text_hashes: set[str] = set()
    input_checksums: dict[str, str] = {
        str(identity_registry_path): hash_file(identity_registry_path),
        str(proofnet_manifest_path): hash_file(proofnet_manifest_path),
        str(formalrx_jsonl): hash_file(formalrx_jsonl),
    }
    for split in sorted(proofnet.splits):
        path = proofnet_dir / f"{split}.jsonl"
        input_checksums[str(path)] = hash_file(path)
        for row_ordinal, row in enumerate(_read_jsonl(path)):
            row_id = f"{split}:{row['problem_id']}"
            observed_proofnet_rows.append(row_id)
            observed_proofnet_nl_hashes.add(nl_hash(str(row["nl_statement"])))
            observed_proofnet_text_hashes.add(lean_hash(str(row["reference_lean"])))
            observed_proofnet_text_hashes.add(lean_hash(str(row["candidate_lean"])))
            for side, statement_key in (
                (BenchmarkSide.REFERENCE, "reference_lean"),
                (BenchmarkSide.CANDIDATE, "candidate_lean"),
            ):
                statements.append(
                    BenchmarkStatementInput.create(
                        registry_key=proofnet.registry_key,
                        source_id=proofnet.source_id,
                        revision=proofnet.revision,
                        split=split,
                        row_id=row_id,
                        row_ordinal=row_ordinal,
                        side=side,
                        header=str(row["lean_header"]),
                        statement=str(row[statement_key]),
                    )
                )
    if Counter(observed_proofnet_rows) != Counter(proofnet.row_ids):
        raise ValueError("ProofNetVerif local row IDs do not match the identity registry")
    if observed_proofnet_nl_hashes != set(proofnet.nl_hashes):
        raise ValueError("ProofNetVerif local NL hashes do not match the identity registry")
    if observed_proofnet_text_hashes != set(proofnet.text_hashes):
        raise ValueError("ProofNetVerif local Lean hashes do not match the identity registry")

    observed_formalrx_rows: list[str] = []
    observed_formalrx_nl_hashes: set[str] = set()
    observed_formalrx_text_hashes: set[str] = set()
    for row_ordinal, row in enumerate(_read_jsonl(formalrx_jsonl)):
        row_id = str(row["idx"])
        observed_formalrx_rows.append(row_id)
        observed_formalrx_nl_hashes.add(nl_hash(str(row["informal_statement"])))
        observed_formalrx_text_hashes.add(lean_hash(str(row["header"])))
        observed_formalrx_text_hashes.add(lean_hash(str(row["formal_statement"])))
        statements.append(
            BenchmarkStatementInput.create(
                registry_key=formalrx.registry_key,
                source_id=formalrx.source_id,
                revision=formalrx.revision,
                split="test",
                row_id=row_id,
                row_ordinal=row_ordinal,
                side=BenchmarkSide.CANDIDATE,
                header=str(row["header"]),
                statement=str(row["formal_statement"]),
            )
        )
    if Counter(observed_formalrx_rows) != Counter(formalrx.row_ids):
        raise ValueError("FormalRx-Test local row IDs do not match the identity registry")
    if observed_formalrx_nl_hashes != set(formalrx.nl_hashes):
        raise ValueError("FormalRx-Test local NL hashes do not match the identity registry")
    if observed_formalrx_text_hashes != set(formalrx.text_hashes):
        raise ValueError("FormalRx-Test local Lean hashes do not match the identity registry")

    statements.sort(key=lambda item: item.statement_id)
    if len({item.statement_id for item in statements}) != len(statements):
        raise ValueError("benchmark statement IDs are not unique")
    return tuple(statements), dict(sorted(input_checksums.items()))


def _source_with_placeholder(item: BenchmarkStatementInput) -> tuple[str, int]:
    header = item.header.rstrip()
    statement = item.statement.rstrip()
    if statement.endswith(":="):
        statement += " by sorry"
    prefix = f"{header}\n\n" if header else ""
    return prefix + statement + "\n", prefix.count("\n") + 1


def _failed_record(
    item: BenchmarkStatementInput,
    *,
    context_id: str,
    elaboration_status: str,
    failure_codes: tuple[str, ...],
) -> BenchmarkSignatureRecord:
    return BenchmarkSignatureRecord(
        schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
        statement_id=item.statement_id,
        input_content_hash=item.input_content_hash,
        registry_key=item.registry_key,
        source_id=item.source_id,
        revision=item.revision,
        split=item.split,
        row_id=item.row_id,
        row_ordinal=item.row_ordinal,
        side=item.side,
        context_id=context_id,
        elaboration_status=elaboration_status,
        view_status=dict.fromkeys(_SIGNATURE_VIEWS, BenchmarkViewStatus.NOT_ATTEMPTED),
        failure_codes=tuple(sorted(set(failure_codes))),
    )


def _target_declarations(
    declarations: tuple[dict[str, Any], ...], statement_start_line: int
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for declaration in declarations:
        start = (declaration.get("range") or {}).get("start") or {}
        line = start.get("line")
        if isinstance(line, int) and line >= statement_start_line:
            selected.append(dict(declaration))
    return selected


def build_benchmark_signature_record(
    backend: LeanBackend,
    item: BenchmarkStatementInput,
    *,
    context_id: str,
    created_at: datetime.datetime,
) -> tuple[BenchmarkSignatureRecord, tuple[BenchmarkSignatureFailure, ...]]:
    """Elaborate and hash one statement; every failure yields a record."""

    require_utc(created_at)
    retrying_backend = _RetryingBenchmarkBackend(backend)
    source, statement_start_line = _source_with_placeholder(item)
    request = LeanRequest(
        request_id=f"benchmark-signature-extract-{item.statement_id[:20]}",
        context_id=context_id,
        code=source,
        declarations=True,
        allow_sorry=True,
        timeout_seconds=600.0,
    )
    try:
        result = retrying_backend.run(request)
    except Exception as exc:  # protocol implementations must normally normalize this
        code = f"backend_exception:{type(exc).__name__}"
        failure = BenchmarkSignatureFailure(
            statement_id=item.statement_id,
            stage=BenchmarkSignatureFailureStage.INTERNAL,
            code=code,
            detail=str(exc)[:2000],
        )
        return (
            _failed_record(
                item,
                context_id=context_id,
                elaboration_status=LeanStatus.INTERNAL_ERROR.value,
                failure_codes=(code,),
            ),
            (failure,),
        )

    if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
        code = f"source_{result.status.value}"
        failure = BenchmarkSignatureFailure(
            statement_id=item.statement_id,
            stage=BenchmarkSignatureFailureStage.EXTRACTION,
            code=code,
            detail=result.infrastructure_error or "Lean did not elaborate the benchmark statement",
        )
        return (
            _failed_record(
                item,
                context_id=context_id,
                elaboration_status=result.status.value,
                failure_codes=(code,),
            ),
            (failure,),
        )

    target_declarations = _target_declarations(result.declarations, statement_start_line)
    proposition_declarations = [
        declaration
        for declaration in target_declarations
        if str(declaration.get("kind", "")) in PROPOSITION_KINDS
    ]
    if len(proposition_declarations) != 1:
        code = (
            "target_theorem_missing"
            if not proposition_declarations
            else "ambiguous_target_theorems"
        )
        detail = f"found {len(proposition_declarations)} target theorem/lemma declarations"
        failure = BenchmarkSignatureFailure(
            statement_id=item.statement_id,
            stage=BenchmarkSignatureFailureStage.TARGET_SELECTION,
            code=code,
            detail=detail,
        )
        return (
            _failed_record(
                item,
                context_id=context_id,
                elaboration_status=result.status.value,
                failure_codes=(code,),
            ),
            (failure,),
        )

    identity = SourceIdentity(
        source=f"benchmark:{item.registry_key}",
        source_revision=item.revision,
        source_record=f"{item.row_id}:{item.side.value}",
        source_record_id=item.statement_id,
        context_id=context_id,
        source_split=item.split,
    )
    extraction = extract_from_declarations(
        identity,
        source,
        proposition_declarations,
        created_at=created_at,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        lean_result_id=result.request_hash,
    )
    if len(extraction.accepted) != 1:
        failure_codes = tuple(
            sorted({f"extract_{failure.code.value}" for failure in extraction.failures})
        ) or ("extract_no_accepted_target",)
        failure = BenchmarkSignatureFailure(
            statement_id=item.statement_id,
            stage=BenchmarkSignatureFailureStage.EXTRACTION,
            code=failure_codes[0],
            detail="; ".join(failure.detail for failure in extraction.failures)[:2000],
        )
        return (
            _failed_record(
                item,
                context_id=context_id,
                elaboration_status=result.status.value,
                failure_codes=failure_codes,
            ),
            (failure,),
        )

    extracted = extraction.accepted[0]
    theorem = extracted.theorem
    full_name = theorem.declaration_full_name
    if full_name is None:
        raise AssertionError("accepted extraction has no full declaration name")
    inline_source = reconstruct_for_revalidation(
        source, extracted.declaration, extracted.proof_stripped
    )
    representation_result = build_representation_batch(
        retrying_backend,  # type: ignore[arg-type]
        RepresentationBatch(
            context_id=context_id,
            import_header="",
            ordered_theorem_inputs=(
                TheoremForRepresentation(
                    theorem_id=theorem.theorem_id,
                    full_name=full_name,
                    proof_stripped=extracted.proof_stripped,
                    context_id=context_id,
                    source_signature=str(
                        (extracted.declaration.get("signature") or {}).get("pp", "")
                    ).strip()
                    or None,
                    inline_declaration=True,
                    inline_source=inline_source,
                ),
            ),
        ),
        created_at=created_at,
    )
    representation = representation_result.ordered_representation_records[0]
    hashes = {
        "headless": (
            signature_near_dup_hash(representation.headless)
            if representation.headless is not None
            else None
        ),
        "signature_pp": (
            signature_near_dup_hash(representation.signature_pp)
            if representation.signature_pp is not None
            else None
        ),
        "signature_explicit": (
            signature_near_dup_hash(representation.signature_explicit)
            if representation.signature_explicit is not None
            else None
        ),
        "alpha_identity_fingerprint": representation.alpha_identity_fingerprint,
    }
    statuses = {
        "headless": BenchmarkViewStatus.OK
        if hashes["headless"] is not None
        else BenchmarkViewStatus.FAILED,
        "signature_pp": BenchmarkViewStatus.OK
        if hashes["signature_pp"] is not None
        else BenchmarkViewStatus.FAILED,
        "signature_explicit": BenchmarkViewStatus.OK
        if hashes["signature_explicit"] is not None
        else BenchmarkViewStatus.FAILED,
        "alpha_identity_fingerprint": BenchmarkViewStatus.OK
        if hashes["alpha_identity_fingerprint"] is not None
        else BenchmarkViewStatus.FAILED,
    }
    failure_codes = tuple(
        sorted(
            {
                f"representation_{failure.view}_{failure.status}"
                for failure in representation_result.per_theorem_failures
            }
        )
    )
    failures = tuple(
        BenchmarkSignatureFailure(
            statement_id=item.statement_id,
            stage=BenchmarkSignatureFailureStage.REPRESENTATION,
            code=f"representation_{failure.view}_{failure.status}",
            detail=failure.detail,
        )
        for failure in representation_result.per_theorem_failures
    )
    return (
        BenchmarkSignatureRecord(
            schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
            statement_id=item.statement_id,
            input_content_hash=item.input_content_hash,
            registry_key=item.registry_key,
            source_id=item.source_id,
            revision=item.revision,
            split=item.split,
            row_id=item.row_id,
            row_ordinal=item.row_ordinal,
            side=item.side,
            context_id=context_id,
            elaboration_status=result.status.value,
            theorem_id=theorem.theorem_id,
            headless_hash=hashes["headless"],
            signature_pp_hash=hashes["signature_pp"],
            signature_explicit_hash=hashes["signature_explicit"],
            alpha_identity_fingerprint=hashes["alpha_identity_fingerprint"],
            view_status=statuses,
            failure_codes=failure_codes,
        ),
        failures,
    )


def _build_retrieval_indexes(
    records: tuple[BenchmarkSignatureRecord, ...],
) -> dict[str, dict[str, tuple[str, ...]]]:
    indexes: dict[str, dict[str, list[str]]] = {view: {} for view in _SIGNATURE_VIEWS}
    attrs = {
        "headless": "headless_hash",
        "signature_pp": "signature_pp_hash",
        "signature_explicit": "signature_explicit_hash",
        "alpha_identity_fingerprint": "alpha_identity_fingerprint",
    }
    for record in records:
        for view, attr in attrs.items():
            digest = getattr(record, attr)
            if digest is not None:
                indexes[view].setdefault(digest, []).append(record.statement_id)
    return {
        view: {
            digest: tuple(sorted(statement_ids)) for digest, statement_ids in sorted(index.items())
        }
        for view, index in indexes.items()
    }


def _build_accounting(
    records: tuple[BenchmarkSignatureRecord, ...],
) -> BenchmarkSignatureAccounting:
    by_benchmark: dict[str, int] = {}
    view_success = dict.fromkeys(_SIGNATURE_VIEWS, 0)
    failure_counts: dict[str, int] = {}
    elaborated = 0
    all_views_ok = 0
    records_with_failures = 0
    for record in records:
        by_benchmark[record.registry_key] = by_benchmark.get(record.registry_key, 0) + 1
        if record.elaboration_status in {LeanStatus.VALID.value, LeanStatus.VALID_WITH_SORRY.value}:
            elaborated += 1
        if all(status == BenchmarkViewStatus.OK for status in record.view_status.values()):
            all_views_ok += 1
        if record.failure_codes:
            records_with_failures += 1
        for view, status in record.view_status.items():
            if status == BenchmarkViewStatus.OK:
                view_success[view] += 1
        for code in record.failure_codes:
            failure_counts[code] = failure_counts.get(code, 0) + 1
    return BenchmarkSignatureAccounting(
        attempted=len(records),
        elaborated=elaborated,
        all_views_ok=all_views_ok,
        records_with_failures=records_with_failures,
        by_benchmark=dict(sorted(by_benchmark.items())),
        view_success=dict(sorted(view_success.items())),
        failure_counts=dict(sorted(failure_counts.items())),
    )


def build_benchmark_signature_artifact(
    *,
    identity_registry_sha256: str,
    context_id: str,
    generated_at: datetime.datetime,
    input_checksums: dict[str, str],
    records: tuple[BenchmarkSignatureRecord, ...],
    failures: tuple[BenchmarkSignatureFailure, ...],
) -> BenchmarkSignatureArtifact:
    ordered_records = tuple(sorted(records, key=lambda record: record.statement_id))
    ordered_failures = tuple(
        sorted(failures, key=lambda failure: (failure.statement_id, failure.stage, failure.code))
    )
    return BenchmarkSignatureArtifact(
        schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
        selection_version=BENCHMARK_SIGNATURE_SELECTION_VERSION,
        identity_registry_sha256=identity_registry_sha256,
        context_id=context_id,
        generated_at=generated_at,
        input_checksums=dict(sorted(input_checksums.items())),
        records=ordered_records,
        failures=ordered_failures,
        retrieval_indexes=_build_retrieval_indexes(ordered_records),
        accounting=_build_accounting(ordered_records),
    )


def _write_immutable_json(model: StrictModel, path: Path) -> str:
    payload = canonical_json_bytes(model.model_dump(mode="json")) + b"\n"
    if path.exists():
        if path.read_bytes() != payload:
            raise FileExistsError(f"versioned artifact already exists with different bytes: {path}")
        return hash_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_bytes(payload)
    os.replace(partial, path)
    return hash_file(path)


def _load_work_record(path: Path) -> BenchmarkSignatureRecord:
    return BenchmarkSignatureRecord.model_validate_json(path.read_text(encoding="utf-8"))


def process_benchmark_signature_inputs(
    backend: LeanBackend,
    inputs: tuple[BenchmarkStatementInput, ...],
    *,
    identity_registry_sha256: str,
    context_id: str,
    created_at: datetime.datetime,
    work_dir: Path,
) -> tuple[tuple[BenchmarkSignatureRecord, ...], tuple[BenchmarkSignatureFailure, ...]]:
    """Process all inputs with content-bound, per-statement resume records."""

    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / "input_manifest.json"
    if manifest_path.is_file():
        existing_manifest = BenchmarkSignatureWorkManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        effective_created_at = existing_manifest.generated_at
    else:
        effective_created_at = created_at
    work_manifest = BenchmarkSignatureWorkManifest(
        schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
        selection_version=BENCHMARK_SIGNATURE_SELECTION_VERSION,
        identity_registry_sha256=identity_registry_sha256,
        context_id=context_id,
        generated_at=effective_created_at,
        ordered_inputs=tuple((item.statement_id, item.input_content_hash) for item in inputs),
    )
    _write_immutable_json(work_manifest, manifest_path)

    records: list[BenchmarkSignatureRecord] = []
    failures: list[BenchmarkSignatureFailure] = []
    for item in inputs:
        record_path = work_dir / "records" / f"{item.statement_id}.json"
        failure_path = work_dir / "failures" / f"{item.statement_id}.json"
        if record_path.is_file():
            record = _load_work_record(record_path)
            if (
                record.input_content_hash != item.input_content_hash
                or record.context_id != context_id
            ):
                raise ValueError(f"stale benchmark signature resume record: {record_path}")
            records.append(record)
            if record.failure_codes and not failure_path.is_file():
                raise ValueError(f"failure resume artifact is missing: {failure_path}")
            if not record.failure_codes and failure_path.exists():
                raise ValueError(f"unexpected failure resume artifact: {failure_path}")
            if failure_path.is_file():
                payload = json.loads(failure_path.read_text(encoding="utf-8"))
                if not isinstance(payload, list):
                    raise ValueError(f"failure resume artifact is not a list: {failure_path}")
                failures.extend(
                    BenchmarkSignatureFailure.model_validate(value) for value in payload
                )
            continue

        record, item_failures = build_benchmark_signature_record(
            backend,
            item,
            context_id=context_id,
            created_at=effective_created_at,
        )
        _write_immutable_json(record, record_path)
        if item_failures:
            failure_path.parent.mkdir(parents=True, exist_ok=True)
            failure_payload = (
                canonical_json_bytes([failure.model_dump(mode="json") for failure in item_failures])
                + b"\n"
            )
            partial = failure_path.with_suffix(".json.partial")
            partial.write_bytes(failure_payload)
            os.replace(partial, failure_path)
        records.append(record)
        failures.extend(item_failures)
    return tuple(records), tuple(failures)


def build_additive_registry(
    identity_registry: FrozenRegistry,
    artifact: BenchmarkSignatureArtifact,
) -> FrozenRegistry:
    """Copy the identity registry and append only representation hashes."""

    updated = identity_registry
    by_benchmark: dict[str, set[str]] = {}
    for record in artifact.records:
        by_benchmark.setdefault(record.registry_key, set()).update(record.representation_hashes())
    for registry_key, digests in sorted(by_benchmark.items()):
        updated = append_representation_signatures(
            updated,
            registry_key,
            tuple(sorted(digests)),
        )
    return updated


def write_benchmark_signature_artifacts(
    *,
    identity_registry_path: Path,
    output_dir: Path,
    artifact: BenchmarkSignatureArtifact,
) -> tuple[Path, str, Path, str]:
    """Write a new registry and detailed index; never mutate Phase-2 input."""

    original_bytes = identity_registry_path.read_bytes()
    if hash_file(identity_registry_path) != artifact.identity_registry_sha256:
        raise ValueError("identity registry hash changed since input loading")
    identity_registry = load_frozen_registry(identity_registry_path)
    updated = build_additive_registry(identity_registry, artifact)
    registry_path = output_dir / BENCHMARK_SIGNATURE_REGISTRY_FILENAME
    index_path = output_dir / BENCHMARK_SIGNATURE_INDEX_FILENAME
    if registry_path.resolve() == identity_registry_path.resolve():
        raise ValueError("additive registry output may not overwrite identity registry")
    registry_digest = _write_immutable_json(updated, registry_path)
    index_digest = _write_immutable_json(artifact, index_path)
    if identity_registry_path.read_bytes() != original_bytes:
        raise RuntimeError("identity registry was modified while writing additive artifacts")
    return registry_path, registry_digest, index_path, index_digest


def _effective_freeze_time(work_dir: Path, requested: datetime.datetime) -> datetime.datetime:
    manifest_path = work_dir / "input_manifest.json"
    if not manifest_path.is_file():
        return requested
    manifest = BenchmarkSignatureWorkManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    return manifest.generated_at


def run_benchmark_signature_freeze(
    backend: LeanBackend,
    *,
    identity_registry_path: Path,
    proofnet_dir: Path,
    formalrx_jsonl: Path,
    context_id: str,
    generated_at: datetime.datetime,
    work_dir: Path,
    output_dir: Path,
    additional_input_checksums: dict[str, str] | None = None,
) -> tuple[Path, str, Path, str, BenchmarkSignatureAccounting]:
    """Execute the complete resolved-benchmark Phase-3 additive freeze."""

    inputs, input_checksums = load_resolved_benchmark_inputs(
        identity_registry_path=identity_registry_path,
        proofnet_dir=proofnet_dir,
        formalrx_jsonl=formalrx_jsonl,
    )
    if additional_input_checksums:
        overlap = set(input_checksums) & set(additional_input_checksums)
        if overlap:
            raise ValueError(
                f"duplicate benchmark-signature input checksum keys: {sorted(overlap)}"
            )
        input_checksums.update(additional_input_checksums)
    effective_generated_at = _effective_freeze_time(work_dir, generated_at)
    identity_registry_sha256 = hash_file(identity_registry_path)
    records, failures = process_benchmark_signature_inputs(
        backend,
        inputs,
        identity_registry_sha256=identity_registry_sha256,
        context_id=context_id,
        created_at=effective_generated_at,
        work_dir=work_dir,
    )
    artifact = build_benchmark_signature_artifact(
        identity_registry_sha256=identity_registry_sha256,
        context_id=context_id,
        generated_at=effective_generated_at,
        input_checksums=input_checksums,
        records=records,
        failures=failures,
    )
    registry_path, registry_digest, index_path, index_digest = write_benchmark_signature_artifacts(
        identity_registry_path=identity_registry_path,
        output_dir=output_dir,
        artifact=artifact,
    )
    return (
        registry_path,
        registry_digest,
        index_path,
        index_digest,
        artifact.accounting,
    )
