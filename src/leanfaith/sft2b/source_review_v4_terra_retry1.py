"""One authorized Terra transport-schema retry for the SFT2B v4 smoke.

The initial Terra request was rejected before inference because the provider's
JSON-schema subset forbids ``uniqueItems``.  This additive runner proves that
failure shape, reuses the successful Opus review, removes only that transport
keyword, and permits exactly one new Terra request on the same packet row.
"""

from __future__ import annotations

import argparse
import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.sft2b import source_review_v4 as base
from leanfaith.sft2b.durable import immutable_write
from leanfaith.sft2b.schemas import NonEmpty, Sha256, StableId, stable_id
from leanfaith.sft2b.source_review_v3 import SourceReviewPacketEntryV3


class TerraRetryError(RuntimeError):
    """Raised when retry lineage or the single-call bound fails closed."""


class PinnedFileRetry1(StrictModel):
    path: NonEmpty
    sha256: Sha256


class InitialRunEvidenceRetry1(StrictModel):
    run_id: StableId
    run_root: NonEmpty
    files: dict[str, PinnedFileRetry1]
    opus_review_id: StableId
    terra_failed_attempt_id: StableId
    terra_failed_cache_key: Sha256
    terra_raw_stdout_sha256: Sha256
    expected_provider_error_code: Literal["invalid_json_schema"]
    expected_rejected_keyword: Literal["uniqueItems"]
    terra_model_answer_produced: Literal[False]
    terra_usage_event_produced: Literal[False]

    @model_validator(mode="after")
    def validate_files(self) -> InitialRunEvidenceRetry1:
        expected = {
            "SHA256SUMS",
            "model_panel_outcomes.jsonl",
            "model_review_attempts.jsonl",
            "model_review_manifest.json",
            "model_review_unknowns.jsonl",
            "model_reviews.jsonl",
        }
        if set(self.files) != expected:
            raise ValueError("retry must pin the exact initial evidence file set")
        return self


class TerraRetryAuthorization(StrictModel):
    authorized_by: Literal["repository_owner"]
    authorization_basis: Literal["same_row_pre_inference_transport_correction_v1"]
    packet_entry_id: StableId
    source_id: StableId
    retry_provider: Literal["terra"]
    maximum_provider_calls: Literal[1]
    successful_opus_recall_authorized: Literal[False]
    ambiguous_call_retry_authorized: Literal[False]
    remaining_packet_rows_authorized: Literal[False]
    generation_authorized: Literal[False]
    lean_authorized: Literal[False]
    publication_authorized: Literal[False]
    training_authorized: Literal[False]


class TerraRetryConfigV1(StrictModel):
    schema_version: Literal["sft2b_source_review_v4_terra_retry1"]
    base_contract: PinnedFileRetry1
    base_implementation: PinnedFileRetry1
    retry_implementation: PinnedFileRetry1
    logical_output_schema: PinnedFileRetry1
    terra_transport_output_schema: PinnedFileRetry1
    initial_run: InitialRunEvidenceRetry1
    authorization: TerraRetryAuthorization
    cache_root: NonEmpty
    output_root: NonEmpty


class TerraRetryLineageV1(StrictModel):
    schema_version: Literal["sft2b_terra_retry_lineage_v1"] = "sft2b_terra_retry_lineage_v1"
    initial_run_id: StableId
    initial_manifest_sha256: Sha256
    original_opus_review_id: StableId
    original_opus_review_sha256: Sha256
    original_terra_attempt_id: StableId
    original_terra_raw_stdout_sha256: Sha256
    original_terra_failure_code: Literal["invalid_json_schema"]
    original_terra_rejected_keyword: Literal["uniqueItems"]
    original_terra_model_answer_produced: Literal[False]
    original_terra_usage_event_produced: Literal[False]
    logical_output_schema_sha256: Sha256
    terra_transport_output_schema_sha256: Sha256
    only_removed_transport_keyword: Literal["uniqueItems"]
    parser_side_unique_sorted_validation_retained: Literal[True]
    successful_opus_provider_recall_performed: Literal[False]


class TerraRetryManifestV1(StrictModel):
    schema_version: Literal["sft2b_terra_retry_manifest_v1"] = "sft2b_terra_retry_manifest_v1"
    run_id: StableId
    retry_config_sha256: Sha256
    retry_implementation_sha256: Sha256
    packet_entry_id: StableId
    source_id: StableId
    original_opus_review_id: StableId
    terra_retry_request_id: StableId
    terra_retry_cache_key: Sha256
    terra_retry_attempt_id: StableId
    terra_retry_review_id: StableId | None
    panel_outcome_id: StableId
    panel_route: base.PanelRoute
    provider_calls_total: Literal[1]
    opus_provider_calls: Literal[0]
    terra_provider_calls: Literal[1]
    model_review_only: Literal[True]
    output_sha256: dict[str, Sha256]


class TerraRetryProcessReceiptV1(StrictModel):
    schema_version: Literal["sft2b_terra_retry_process_receipt_v1"] = (
        "sft2b_terra_retry_process_receipt_v1"
    )
    run_id: StableId
    phase: Literal["initial_or_resume", "cache_only_restart"]
    started_at_utc: datetime.datetime
    completed_at_utc: datetime.datetime
    terra_calls_this_process: Annotated[int, Field(ge=0, le=1)]
    terra_cache_hits_this_process: Annotated[int, Field(ge=0, le=1)]
    opus_calls_this_process: Literal[0]
    ambiguous_request_count: Annotated[int, Field(ge=0, le=1)]
    manifest_sha256: Sha256
    journal_sha256: Sha256

    @field_validator("started_at_utc", "completed_at_utc")
    @classmethod
    def validate_utc(cls, value: datetime.datetime) -> datetime.datetime:
        if value.tzinfo is None or value.utcoffset() != datetime.timedelta(0):
            raise ValueError("retry timestamps must be timezone-aware UTC")
        return value


@dataclass(frozen=True, slots=True)
class LoadedTerraRetry:
    repo_root: Path
    config_path: Path
    config_sha256: str
    config: TerraRetryConfigV1
    base_loaded: base.LoadedModelPanelV4
    retry_loaded: base.LoadedModelPanelV4
    entry: SourceReviewPacketEntryV3
    original_opus_review: base.ModelSourceReviewV4
    original_attempts: tuple[base.ModelReviewAttemptV4, base.ModelReviewAttemptV4]
    lineage: TerraRetryLineageV1


@dataclass(frozen=True, slots=True)
class TerraRetryResult:
    manifest: TerraRetryManifestV1
    receipt: TerraRetryProcessReceiptV1
    outcome: base.ModelPanelOutcomeV4


def _path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TerraRetryError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise TerraRetryError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise TerraRetryError(f"cannot read JSONL {path}: {error}") from error
    if not lines or any(not line.strip() for line in lines):
        raise TerraRetryError(f"JSONL is empty or has blank rows: {path}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise TerraRetryError(f"invalid JSONL row {path}:{number}") from error
        if not isinstance(value, dict):
            raise TerraRetryError(f"non-object JSONL row {path}:{number}")
        rows.append(cast(dict[str, Any], value))
    return tuple(rows)


def _verify_pin(path: Path, pin: PinnedFileRetry1, label: str) -> None:
    if not path.is_file() or path.is_symlink() or hash_file(path) != pin.sha256:
        raise TerraRetryError(f"{label} pin mismatch: {path}")


def _strip_unique_items(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_unique_items(child) for key, child in value.items() if key != "uniqueItems"
        }
    if isinstance(value, list):
        return [_strip_unique_items(child) for child in value]
    return value


def _prove_pre_inference_schema_rejection(
    raw_stdout: bytes,
    *,
    expected_code: str,
    expected_keyword: str,
) -> None:
    if not raw_stdout or not raw_stdout.endswith(b"\n"):
        raise TerraRetryError("initial Terra stdout is empty or partial")
    events: list[dict[str, object]] = []
    for number, line in enumerate(raw_stdout.splitlines()):
        value = base._strict_json(line, label=f"initial Terra event {number}")
        if not isinstance(value, dict):
            raise TerraRetryError("initial Terra event is not an object")
        events.append(cast(dict[str, object], value))
    types = [event.get("type") for event in events]
    if types != ["thread.started", "turn.started", "error", "turn.failed"]:
        raise TerraRetryError("initial Terra failure is not the exact pre-inference shape")
    if any(str(event_type).startswith("item.") for event_type in types):
        raise TerraRetryError("initial Terra request produced a model item")
    if any("usage" in event for event in events):
        raise TerraRetryError("initial Terra request produced a usage event")
    detail = canonical_json_bytes(events).decode("utf-8")
    if (
        expected_code not in detail
        or expected_keyword not in detail
        or "not permitted" not in detail
    ):
        raise TerraRetryError("initial Terra error is not the frozen schema rejection")


def _initial_evidence(
    repo_root: Path,
    config: TerraRetryConfigV1,
    base_loaded: base.LoadedModelPanelV4,
) -> tuple[
    base.ModelSourceReviewV4,
    tuple[base.ModelReviewAttemptV4, base.ModelReviewAttemptV4],
    TerraRetryLineageV1,
]:
    initial = config.initial_run
    root = _path(repo_root, initial.run_root)
    for name, pin in initial.files.items():
        if pin.path != name:
            raise TerraRetryError("initial evidence pin names and paths differ")
        _verify_pin(root / name, pin, f"initial {name}")
    manifest = base.ModelReviewRunManifestV4.model_validate(
        _object(root / "model_review_manifest.json")
    )
    if manifest.run_id != initial.run_id:
        raise TerraRetryError("initial run ID drifted")
    base.verify_smoke_output(base_loaded)
    reviews = tuple(
        base.ModelSourceReviewV4.model_validate(row) for row in _jsonl(root / "model_reviews.jsonl")
    )
    attempts = tuple(
        base.ModelReviewAttemptV4.model_validate(row)
        for row in _jsonl(root / "model_review_attempts.jsonl")
    )
    if len(reviews) != 1 or reviews[0].reviewer_slot != "opus":
        raise TerraRetryError("initial run lacks exactly one successful Opus review")
    opus = reviews[0]
    if opus.review_id != initial.opus_review_id:
        raise TerraRetryError("initial Opus review ID drifted")
    if len(attempts) != 2 or tuple(row.reviewer_slot for row in attempts) != base.REVIEWER_ORDER:
        raise TerraRetryError("initial attempt set/order drifted")
    terra_attempt = attempts[1]
    if (
        terra_attempt.attempt_id != initial.terra_failed_attempt_id
        or terra_attempt.cache_key != initial.terra_failed_cache_key
        or terra_attempt.status != "provider_error"
        or terra_attempt.review_id is not None
    ):
        raise TerraRetryError("initial Terra failure binding drifted")
    raw_stdout_path = (
        _path(repo_root, base_loaded.config.cache_root)
        / "terra"
        / terra_attempt.cache_key
        / "raw_stdout.bin"
    )
    if hash_file(raw_stdout_path) != initial.terra_raw_stdout_sha256:
        raise TerraRetryError("initial Terra raw stdout hash drifted")
    raw_stdout = raw_stdout_path.read_bytes()
    _prove_pre_inference_schema_rejection(
        raw_stdout,
        expected_code=initial.expected_provider_error_code,
        expected_keyword=initial.expected_rejected_keyword,
    )
    logical_path = _path(repo_root, config.logical_output_schema.path)
    transport_path = _path(repo_root, config.terra_transport_output_schema.path)
    _verify_pin(logical_path, config.logical_output_schema, "logical output schema")
    _verify_pin(transport_path, config.terra_transport_output_schema, "Terra transport schema")
    if _strip_unique_items(_object(logical_path)) != _object(transport_path):
        raise TerraRetryError("Terra transport schema changes more than uniqueItems")
    if "uniqueItems" not in logical_path.read_text(encoding="utf-8"):
        raise TerraRetryError("logical schema lacks the rejected uniqueItems keyword")
    lineage = TerraRetryLineageV1(
        initial_run_id=initial.run_id,
        initial_manifest_sha256=initial.files["model_review_manifest.json"].sha256,
        original_opus_review_id=opus.review_id,
        original_opus_review_sha256=sha256_hex(base._model_bytes(opus)),
        original_terra_attempt_id=terra_attempt.attempt_id,
        original_terra_raw_stdout_sha256=initial.terra_raw_stdout_sha256,
        original_terra_failure_code="invalid_json_schema",
        original_terra_rejected_keyword="uniqueItems",
        original_terra_model_answer_produced=False,
        original_terra_usage_event_produced=False,
        logical_output_schema_sha256=config.logical_output_schema.sha256,
        terra_transport_output_schema_sha256=config.terra_transport_output_schema.sha256,
        only_removed_transport_keyword="uniqueItems",
        parser_side_unique_sorted_validation_retained=True,
        successful_opus_provider_recall_performed=False,
    )
    return opus, attempts, lineage


def load_retry(repo_root: Path, config_path: Path) -> LoadedTerraRetry:
    config = TerraRetryConfigV1.model_validate(_object(config_path))
    base_contract = _path(repo_root, config.base_contract.path)
    base_implementation = _path(repo_root, config.base_implementation.path)
    retry_implementation = _path(repo_root, config.retry_implementation.path)
    _verify_pin(base_contract, config.base_contract, "base contract")
    _verify_pin(base_implementation, config.base_implementation, "base implementation")
    _verify_pin(retry_implementation, config.retry_implementation, "retry implementation")
    base_loaded = base.load_model_panel(repo_root, base_contract)
    entry = next(
        row
        for row in base_loaded.packet_entries
        if row.packet_entry_id == config.authorization.packet_entry_id
        and row.source_id == config.authorization.source_id
    )
    opus, attempts, lineage = _initial_evidence(repo_root, config, base_loaded)

    derived_raw = base_loaded.config.model_dump(mode="json")
    derived_raw["implementation"] = config.retry_implementation.model_dump(mode="json")
    derived_raw["output_schema"] = config.terra_transport_output_schema.model_dump(mode="json")
    derived_raw["cache_root"] = config.cache_root
    derived_raw["output_root"] = config.output_root
    derived = base.SourceReviewModelPanelConfigV4.model_validate(derived_raw)
    retry_loaded = base.LoadedModelPanelV4(
        repo_root=repo_root,
        config_path=config_path,
        config_sha256=hash_file(config_path),
        config=derived,
        packet_dir=base_loaded.packet_dir,
        packet_entries=base_loaded.packet_entries,
        output_schema_path=_path(repo_root, config.terra_transport_output_schema.path),
    )
    return LoadedTerraRetry(
        repo_root=repo_root,
        config_path=config_path,
        config_sha256=hash_file(config_path),
        config=config,
        base_loaded=base_loaded,
        retry_loaded=retry_loaded,
        entry=entry,
        original_opus_review=opus,
        original_attempts=attempts,
        lineage=lineage,
    )


def _run_id(loaded: LoadedTerraRetry, request: base.ModelReviewRequestV4) -> str:
    return stable_id(
        "sft2b_terra_retry1_run",
        {
            "retry_config_sha256": loaded.config_sha256,
            "initial_run_id": loaded.config.initial_run.run_id,
            "packet_entry_id": loaded.entry.packet_entry_id,
            "terra_request_id": request.request_id,
        },
    )


def _retry_request(loaded: LoadedTerraRetry) -> base.ModelReviewRequestV4:
    provider = loaded.retry_loaded.config.provider("terra")
    prompt, projection = base.render_review_prompt(loaded.retry_loaded, provider, loaded.entry)
    return base.build_review_request(
        loaded.retry_loaded,
        provider,
        loaded.entry,
        rendered_prompt=prompt,
        projection_bytes=projection,
    )


def _jsonl_bytes(rows: tuple[StrictModel, ...]) -> bytes:
    return b"".join(base._model_bytes(row) for row in rows)


def _compact(
    loaded: LoadedTerraRetry,
    *,
    run_id: str,
    request: base.ModelReviewRequestV4,
    retry_attempt: base.ModelReviewAttemptV4,
    retry_review: base.ModelSourceReviewV4 | None,
    outcome: base.ModelPanelOutcomeV4,
) -> TerraRetryManifestV1:
    root = _path(loaded.repo_root, loaded.config.output_root) / run_id
    root.mkdir(parents=True, exist_ok=True)
    reviews = (loaded.original_opus_review,) + ((retry_review,) if retry_review is not None else ())
    attempts = (*loaded.original_attempts, retry_attempt)
    artifacts = {
        "model_reviews.jsonl": _jsonl_bytes(cast(tuple[StrictModel, ...], reviews)),
        "model_review_attempts.jsonl": _jsonl_bytes(cast(tuple[StrictModel, ...], attempts)),
        "model_panel_outcomes.jsonl": base._model_bytes(outcome),
        "terra_retry_lineage.json": base._model_bytes(loaded.lineage),
    }
    for name, payload in artifacts.items():
        immutable_write(root / name, payload)
    output_hashes = {name: hash_file(root / name) for name in sorted(artifacts)}
    manifest = TerraRetryManifestV1(
        run_id=run_id,
        retry_config_sha256=loaded.config_sha256,
        retry_implementation_sha256=loaded.config.retry_implementation.sha256,
        packet_entry_id=loaded.entry.packet_entry_id,
        source_id=loaded.entry.source_id,
        original_opus_review_id=loaded.original_opus_review.review_id,
        terra_retry_request_id=request.request_id,
        terra_retry_cache_key=request.cache_key,
        terra_retry_attempt_id=retry_attempt.attempt_id,
        terra_retry_review_id=retry_review.review_id if retry_review is not None else None,
        panel_outcome_id=outcome.panel_outcome_id,
        panel_route=outcome.route,
        provider_calls_total=1,
        opus_provider_calls=0,
        terra_provider_calls=1,
        model_review_only=True,
        output_sha256=output_hashes,
    )
    immutable_write(root / "terra_retry_manifest.json", base._model_bytes(manifest))
    checksum_names = (*sorted(artifacts), "terra_retry_manifest.json")
    immutable_write(
        root / "SHA256SUMS",
        "".join(f"{hash_file(root / name)}  {name}\n" for name in checksum_names).encode(),
    )
    return manifest


def run_retry(
    loaded: LoadedTerraRetry,
    *,
    cache_only: bool,
    provider_runner: base.ProviderRunner = base.run_provider,
) -> TerraRetryResult:
    request = _retry_request(loaded)
    run_id = _run_id(loaded, request)
    root = _path(loaded.repo_root, loaded.config.output_root) / run_id
    journal = base.ModelReviewJournalV4(root / "journal/terra_retry.jsonl", run_id=run_id)
    started_at = datetime.datetime.now(datetime.UTC)
    provider = loaded.retry_loaded.config.provider("terra")
    observed_request, attempt, review, cache_hit, called = base._execute_cell(
        loaded.retry_loaded,
        provider,
        loaded.entry,
        run_id=run_id,
        journal=journal,
        cache_only=cache_only,
        provider_runner=provider_runner,
    )
    if observed_request != request or int(called) + int(cache_hit) != 1:
        raise TerraRetryError("Terra retry did not close exactly one request cell")
    reviews = (loaded.original_opus_review,) + ((review,) if review is not None else ())
    outcome = base.panel_outcome(
        loaded.entry,
        reviews,
        minimum_confidence=loaded.retry_loaded.config.panel.minimum_decisive_confidence,
    )
    manifest = _compact(
        loaded,
        run_id=run_id,
        request=request,
        retry_attempt=attempt,
        retry_review=review,
        outcome=outcome,
    )
    completed_at = datetime.datetime.now(datetime.UTC)
    manifest_path = root / "terra_retry_manifest.json"
    journal_path = root / "journal/terra_retry.jsonl"
    receipt = TerraRetryProcessReceiptV1(
        run_id=run_id,
        phase="cache_only_restart" if cache_only else "initial_or_resume",
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        terra_calls_this_process=int(called),
        terra_cache_hits_this_process=int(cache_hit),
        opus_calls_this_process=0,
        ambiguous_request_count=len(journal.ambiguous_request_ids()),
        manifest_sha256=hash_file(manifest_path),
        journal_sha256=hash_file(journal_path),
    )
    receipt_bytes = base._model_bytes(receipt)
    immutable_write(root / "process_receipts" / f"{sha256_hex(receipt_bytes)}.json", receipt_bytes)
    return TerraRetryResult(manifest=manifest, receipt=receipt, outcome=outcome)


def verify_retry(loaded: LoadedTerraRetry) -> TerraRetryManifestV1:
    request = _retry_request(loaded)
    run_id = _run_id(loaded, request)
    root = _path(loaded.repo_root, loaded.config.output_root) / run_id
    manifest = base._read_model(root / "terra_retry_manifest.json", TerraRetryManifestV1)
    if manifest.run_id != run_id or manifest.retry_config_sha256 != loaded.config_sha256:
        raise TerraRetryError("Terra retry manifest identity drifted")
    expected_files = {
        "model_reviews.jsonl",
        "model_review_attempts.jsonl",
        "model_panel_outcomes.jsonl",
        "terra_retry_lineage.json",
        "terra_retry_manifest.json",
    }
    checksums: dict[str, str] = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name in checksums:
            raise TerraRetryError("Terra retry checksum ledger is malformed")
        checksums[name] = digest
    expected = {name: hash_file(root / name) for name in sorted(expected_files)}
    if checksums != expected:
        raise TerraRetryError("Terra retry checksum replay failed")
    for name, digest in manifest.output_sha256.items():
        if hash_file(root / name) != digest:
            raise TerraRetryError("Terra retry manifest output hash drifted")
    reviews = tuple(
        base.ModelSourceReviewV4.model_validate(row) for row in _jsonl(root / "model_reviews.jsonl")
    )
    outcomes = tuple(
        base.ModelPanelOutcomeV4.model_validate(row)
        for row in _jsonl(root / "model_panel_outcomes.jsonl")
    )
    replayed = base.panel_outcome(
        loaded.entry,
        reviews,
        minimum_confidence=loaded.retry_loaded.config.panel.minimum_decisive_confidence,
    )
    if outcomes != (replayed,) or replayed.panel_outcome_id != manifest.panel_outcome_id:
        raise TerraRetryError("Terra retry panel outcome does not replay")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/sft2b/source_review_contract_v4_terra_retry1.json"),
    )
    parser.add_argument("command", choices=("preflight", "run", "restart", "verify"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    loaded = load_retry(repo_root, config_path)
    if args.command == "preflight":
        result: object = {
            "schema_version": "sft2b_terra_retry_preflight_v1",
            "retry_config_sha256": loaded.config_sha256,
            "initial_run_id": loaded.config.initial_run.run_id,
            "packet_entry_id": loaded.entry.packet_entry_id,
            "provider_calls_performed": 0,
            "opus_recall_performed": False,
        }
    elif args.command == "run":
        run = run_retry(loaded, cache_only=False)
        result = {
            "manifest": run.manifest.model_dump(mode="json"),
            "receipt": run.receipt.model_dump(mode="json"),
            "outcome": run.outcome.model_dump(mode="json"),
        }
    elif args.command == "restart":
        run = run_retry(loaded, cache_only=True)
        if (
            run.receipt.terra_calls_this_process != 0
            or run.receipt.terra_cache_hits_this_process != 1
        ):
            raise TerraRetryError("Terra retry restart did not prove a zero-call cache hit")
        result = {
            "manifest": run.manifest.model_dump(mode="json"),
            "receipt": run.receipt.model_dump(mode="json"),
            "outcome": run.outcome.model_dump(mode="json"),
        }
    else:
        result = verify_retry(loaded).model_dump(mode="json")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
