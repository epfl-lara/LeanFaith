"""One-source ReForm generation, Lean rendering, voting, and restart smoke."""

from __future__ import annotations

import json
import os
import resource
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.host_resources import claim_resources, release_resources
from leanfaith.lean.leaninteract_backend import METHOD_VERSION
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult
from leanfaith.representations.goal_v1 import CompileContext
from leanfaith.sft2b.durable import (
    AppendOnlyJournal,
    immutable_write,
    read_model,
    write_json,
    write_jsonl,
    write_model,
)
from leanfaith.sft2b.formalizer import FormalizerRunResult, run_reform_8b_generation
from leanfaith.sft2b.judges import (
    JudgeCallResult,
    LoadedJudges,
    judge_input_hash,
    load_judges,
    run_judge,
    vote_cache_key,
)
from leanfaith.sft2b.lean import (
    PropositionEndpoint,
    compile_context_from_source,
    endpoint_cache_key,
    make_mathlib_backend,
    render_propositions_tolerant,
)
from leanfaith.sft2b.new_source import load_new_source
from leanfaith.sft2b.numina_source import load_numina_source
from leanfaith.sft2b.pins import RuntimePins, verify_runtime_pins
from leanfaith.sft2b.pipeline import SmokeConfig, load_smoke_config, run_existing_smoke
from leanfaith.sft2b.schemas import (
    CandidateRecord,
    CompilationEvidence,
    CompileStatus,
    CoreRow,
    EndpointCacheRecord,
    FormalizerAttempt,
    FormalizerInvalidAttemptView,
    InvalidAttempt,
    JudgeId,
    JudgeVote,
    MajorityDecision,
    MajorityOutcome,
    ReformRenderBatchTerminal,
    RunManifest,
    SourceRecord,
    UnknownCandidate,
    majority_outcome,
    stable_id,
)


class ReformPipelineError(RuntimeError):
    """Raised when the bounded ReForm smoke cannot form a coherent terminal."""


@dataclass(frozen=True, slots=True)
class CandidateSemanticResult:
    compilation: CompilationEvidence
    votes: tuple[JudgeVote, ...]
    majority: MajorityOutcome | None


@dataclass(frozen=True, slots=True)
class ReformPassResult:
    candidates: tuple[CandidateSemanticResult, ...]
    lean_requests: int
    judge_calls: int
    lean_peak_rss_bytes: int


@dataclass(frozen=True, slots=True)
class ReformSmokeResult:
    manifest: RunManifest
    output_root: Path
    generation_root: Path
    resumed_existing_manifest: bool


class _CapturingBackend:
    def __init__(self, backend: LeanBackend) -> None:
        self.backend = backend
        self.requests: list[LeanRequest] = []
        self.results: list[LeanResult] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        result = self.backend.run(request)
        self.results.append(result)
        return result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        raise ReformPipelineError("ReForm smoke uses one persistent Lean request")

    def close(self) -> None:
        self.backend.close()


def _load_source(
    repo_root: Path,
    *,
    source_config_path: Path,
    helper_path: Path,
    pins: RuntimePins,
) -> tuple[SourceRecord, dict[str, object]]:
    raw = json.loads(source_config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ReformPipelineError("source config must be a JSON object")
    if raw.get("schema_version") == "sft2b_numina_source_smoke_v1":
        source, receipt = load_numina_source(
            repo_root,
            config_path=source_config_path,
            helper_path=helper_path,
            pins=pins,
        )
        return source, receipt.to_dict()
    source, new_receipt = load_new_source(
        repo_root,
        config_path=source_config_path,
        helper_path=helper_path,
        pins=pins,
    )
    return source, new_receipt.to_dict()


def _compile_context(
    source: SourceRecord, *, helper_path: Path, pins: RuntimePins
) -> CompileContext:
    context, _ = compile_context_from_source(
        source_context_path=Path(source.compile_context.source_context_path),
        helper_path=helper_path,
        pins=pins,
    )
    if context.compile_context_id != source.compile_context.render_compile_context_id:
        raise ReformPipelineError("new-source render compile context drifted")
    return context


def _endpoints(
    source: SourceRecord, candidates: tuple[CandidateRecord, ...]
) -> tuple[PropositionEndpoint, ...]:
    reference = PropositionEndpoint(
        endpoint_id="reference",
        endpoint_role="reference",
        proposition=source.reference_proposition,
        source_id=source.source_id,
    )
    candidate_endpoints = tuple(
        PropositionEndpoint(
            endpoint_id=item.candidate_id,
            endpoint_role="candidate",
            proposition=item.raw_proof_free_signature,
            source_id=source.source_id,
            candidate_id=item.candidate_id,
        )
        for item in candidates
    )
    return (reference, *candidate_endpoints)


def _valid_endpoint_record(
    *,
    endpoint: PropositionEndpoint,
    cache_key: str,
    source_context_id: str,
    context: CompileContext,
    pins: RuntimePins,
    sidecar: object,
) -> EndpointCacheRecord:
    sidecar_value = cast(dict[str, object], sidecar.to_dict())  # type: ignore[attr-defined]
    goal = str(sidecar.core_text())  # type: ignore[attr-defined]
    if "[anonymous]" in goal or "⋯" in goal:
        raise ReformPipelineError("REPR output contains a forbidden model-facing placeholder")
    return EndpointCacheRecord(
        endpoint_cache_key=cache_key,
        endpoint_id=endpoint.endpoint_id,
        endpoint_role=endpoint.endpoint_role,  # type: ignore[arg-type]
        source_id=endpoint.source_id,
        candidate_id=endpoint.candidate_id,
        proposition_sha256=endpoint.proposition_sha256,
        source_context_id=source_context_id,
        render_compile_context_id=context.compile_context_id,
        project_revision=context.project_revision,
        lean_version=context.lean_version,
        helper_sha256=pins.sft2b_helper_hash,
        repr_spec_sha256=pins.repr_spec_hash,
        repr_implementation_set_sha256=pins.repr_implementation_set_hash,
        status=CompileStatus.VALID,
        goal_v1=goal,
        goal_v1_sha256=sha256_hex(goal.encode("utf-8")),
        repr_sidecar=sidecar_value,
    )


def _invalid_endpoint_record(
    *,
    endpoint: PropositionEndpoint,
    cache_key: str,
    source_context_id: str,
    context: CompileContext,
    pins: RuntimePins,
    detail: str,
) -> EndpointCacheRecord:
    return EndpointCacheRecord(
        endpoint_cache_key=cache_key,
        endpoint_id=endpoint.endpoint_id,
        endpoint_role="candidate",
        source_id=endpoint.source_id,
        candidate_id=endpoint.candidate_id,
        proposition_sha256=endpoint.proposition_sha256,
        source_context_id=source_context_id,
        render_compile_context_id=context.compile_context_id,
        project_revision=context.project_revision,
        lean_version=context.lean_version,
        helper_sha256=pins.sft2b_helper_hash,
        repr_spec_sha256=pins.repr_spec_hash,
        repr_implementation_set_sha256=pins.repr_implementation_set_hash,
        status=CompileStatus.INVALID,
        error_class="lean_elaboration_invalid",
        error_detail=detail,
    )


def _rss_high_water_bytes() -> int:
    self_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return int(max(self_rss, child_rss) * 1024)


def _batch_cache_path(root: Path, batch_key: str) -> Path:
    return root / "cache/render_batches" / f"{batch_key}.json"


def _endpoint_cache_path(root: Path, key: str) -> Path:
    return root / "cache/endpoints" / f"{key}.json"


def _ensure_render_batch(
    *,
    repo_root: Path,
    root: Path,
    source: SourceRecord,
    candidates: tuple[CandidateRecord, ...],
    config: SmokeConfig,
    pins: RuntimePins,
    journal: AppendOnlyJournal,
    pass_name: str,
) -> tuple[tuple[CompilationEvidence, ...], int, int]:
    if not candidates:
        return (), 0, 0
    context = _compile_context(source, helper_path=config.helper_path, pins=pins)
    endpoints = _endpoints(source, candidates)
    keys = tuple(
        endpoint_cache_key(
            endpoint,
            source_context_id=source.compile_context.source_context_id,
            compile_context=context,
            pins=pins,
        )
        for endpoint in endpoints
    )
    batch_key = hash_canonical(
        {
            "schema_version": "sft2b_reform_render_batch_key_v1",
            "endpoint_cache_keys": keys,
            "candidate_ids": [item.candidate_id for item in candidates],
            "render_scope": source.source_id,
        }
    )
    batch_path = _batch_cache_path(root, batch_key)
    endpoint_paths = tuple(_endpoint_cache_path(root, key) for key in keys)
    if batch_path.exists():
        terminal = read_model(batch_path, ReformRenderBatchTerminal)
        if terminal.batch_key != batch_key:
            raise ReformPipelineError("render batch cache identity mismatch")
        reference = terminal.compilation_evidence[0].reference
        if reference is None:
            raise ReformPipelineError("cached render batch lacks its trusted reference")
        write_model(endpoint_paths[0], reference)
        for path, evidence in zip(endpoint_paths[1:], terminal.compilation_evidence, strict=True):
            if evidence.candidate is None:
                raise ReformPipelineError("cached render evidence lacks candidate endpoint")
            write_model(path, evidence.candidate)
        journal.append(
            stage="render_cache_hit",
            terminal_key=f"{pass_name}:render:{batch_key}",
            artifact_path=batch_path,
        )
        return terminal.compilation_evidence, 0, terminal.peak_rss_bytes
    if any(path.exists() for path in endpoint_paths):
        raise ReformPipelineError("endpoint cache exists without atomic ReForm batch terminal")
    reservation = claim_resources(
        task="SFT2B",
        lean_workers=1,
        lean_rss_gib=4.0,
        gpu=False,
        pid=os.getpid(),
        owner_session=config.owner_session,
        worktree=repo_root,
    )
    backend = make_mathlib_backend(
        compile_context=context,
        project_dir=config.mathlib_project_path,
        raw_response_dir=root / "lean/raw_responses",
    )
    capturing = _CapturingBackend(backend)
    try:
        rendered = render_propositions_tolerant(
            capturing,
            endpoints=endpoints,
            compile_context=context,
            render_scope_id=f"scope:{source.source_id}",
            request_id=f"sft2b-reform-smoke:{source.source_id}",
            timeout_seconds=config.lean_timeout_seconds,
        )
    finally:
        capturing.close()
        released = release_resources(task="SFT2B")
        if released != reservation:
            raise ReformPipelineError("released Lean claim differs from acquired claim")
    peak_rss = _rss_high_water_bytes()
    if len(capturing.requests) != 1 or len(capturing.results) != 1:
        raise ReformPipelineError("ReForm smoke did not issue exactly one Lean request")
    raw_path = Path(rendered.raw_response_path) if rendered.raw_response_path else None
    raw_hash = hash_file(raw_path) if raw_path is not None and raw_path.is_file() else None
    failures = {item.endpoint_id: item.detail for item in rendered.failures}
    if "reference" in failures:
        raise ReformPipelineError(f"trusted reference did not elaborate: {failures['reference']}")
    by_id = {sidecar.record.endpoint_id: sidecar for sidecar in rendered.sidecars}
    if "reference" not in by_id:
        raise ReformPipelineError("trusted reference did not produce a REPR sidecar")
    reference = _valid_endpoint_record(
        endpoint=endpoints[0],
        cache_key=keys[0],
        source_context_id=source.compile_context.source_context_id,
        context=context,
        pins=pins,
        sidecar=by_id["reference"],
    )
    evidence_rows: list[CompilationEvidence] = []
    for candidate, endpoint, key in zip(candidates, endpoints[1:], keys[1:], strict=True):
        if candidate.candidate_id in failures:
            detail = failures[candidate.candidate_id]
            candidate_record = _invalid_endpoint_record(
                endpoint=endpoint,
                cache_key=key,
                source_context_id=source.compile_context.source_context_id,
                context=context,
                pins=pins,
                detail=detail,
            )
            status = CompileStatus.INVALID
        else:
            sidecar = by_id.get(candidate.candidate_id)
            if sidecar is None:
                raise ReformPipelineError("candidate lacks both sidecar and failure marker")
            candidate_record = _valid_endpoint_record(
                endpoint=endpoint,
                cache_key=key,
                source_context_id=source.compile_context.source_context_id,
                context=context,
                pins=pins,
                sidecar=sidecar,
            )
            status = CompileStatus.VALID
        evidence_id = stable_id(
            "sft2b_compile",
            {
                "reference_cache_key": reference.endpoint_cache_key,
                "candidate_cache_key": candidate_record.endpoint_cache_key,
                "request_hash": rendered.request_hash,
                "status": status,
            },
        )
        evidence_rows.append(
            CompilationEvidence(
                evidence_id=evidence_id,
                source_id=source.source_id,
                candidate_id=candidate.candidate_id,
                reference_cache_key=reference.endpoint_cache_key,
                candidate_cache_key=candidate_record.endpoint_cache_key,
                request_hash=rendered.request_hash,
                status=status,
                elapsed_ms=rendered.elapsed_ms,
                raw_response_path=str(raw_path) if raw_path is not None else None,
                raw_response_sha256=raw_hash,
                backend_method_version=METHOD_VERSION,
                cache_hit=False,
                reference=reference,
                candidate=candidate_record,
                failure_class=(None if status == CompileStatus.VALID else "lean_invalid"),
                failure_detail=(
                    None if status == CompileStatus.VALID else failures[candidate.candidate_id]
                ),
            )
        )
    terminal = ReformRenderBatchTerminal(
        batch_key=batch_key,
        source_id=source.source_id,
        reference_cache_key=reference.endpoint_cache_key,
        candidate_ids=tuple(item.candidate_id for item in candidates),
        compilation_evidence=tuple(evidence_rows),
        request_hash=rendered.request_hash,
        elapsed_ms=rendered.elapsed_ms,
        raw_response_path=str(raw_path) if raw_path is not None else None,
        raw_response_sha256=raw_hash,
        peak_rss_bytes=peak_rss,
    )
    write_model(batch_path, terminal)
    write_model(endpoint_paths[0], reference)
    for path, evidence in zip(endpoint_paths[1:], evidence_rows, strict=True):
        assert evidence.candidate is not None
        write_model(path, evidence.candidate)
    journal.append(
        stage="render_completed",
        terminal_key=f"{pass_name}:render:{batch_key}",
        artifact_path=batch_path,
    )
    return tuple(evidence_rows), 1, peak_rss


def _vote_path(root: Path, cache_key: str) -> Path:
    return root / "cache/votes" / cache_key / "vote.json"


def _ensure_votes(
    *,
    root: Path,
    source: SourceRecord,
    candidate: CandidateRecord,
    compilation: CompilationEvidence,
    loaded: LoadedJudges,
    journal: AppendOnlyJournal,
    pass_name: str,
) -> tuple[tuple[JudgeVote, ...], int]:
    if compilation.status != CompileStatus.VALID:
        return (), 0
    if compilation.reference is None or compilation.candidate is None:
        raise ReformPipelineError("valid compilation lacks judge-facing endpoints")
    reference = cast(str, compilation.reference.goal_v1)
    candidate_goal = cast(str, compilation.candidate.goal_v1)
    input_hash = judge_input_hash(
        nl_statement=source.nl_statement,
        reference=reference,
        candidate=candidate_goal,
    )
    votes: list[JudgeVote] = []
    calls = 0
    for judge in JudgeId:
        provider = loaded.provider(judge)
        cache_key = vote_cache_key(
            loaded,
            provider,
            candidate_id=candidate.candidate_id,
            input_sha256=input_hash,
        )
        path = _vote_path(root, cache_key)
        if path.exists():
            vote = read_model(path, JudgeVote)
            journal.append(
                stage="vote_cache_hit",
                terminal_key=f"{pass_name}:vote:{judge.value}:{cache_key}",
                artifact_path=path,
                candidate_id=candidate.candidate_id,
            )
        else:
            result: JudgeCallResult = run_judge(
                loaded,
                judge=judge,
                candidate_id=candidate.candidate_id,
                nl_statement=source.nl_statement,
                reference=reference,
                candidate=candidate_goal,
                working_dir=root / "judge_work",
            )
            vote = result.vote
            cell = path.parent
            immutable_write(cell / "stdout.bin", result.stdout)
            immutable_write(cell / "stderr.bin", result.stderr)
            immutable_write(cell / "provider_payload.json", result.provider_payload)
            write_json(
                cell / "receipt.json",
                {
                    "schema_version": "sft2b_judge_call_receipt_v1",
                    "judge": judge.value,
                    "candidate_id": candidate.candidate_id,
                    "cache_key": cache_key,
                    "elapsed_seconds": result.elapsed_seconds,
                    "stdout_sha256": hash_file(cell / "stdout.bin"),
                    "stderr_sha256": hash_file(cell / "stderr.bin"),
                    "provider_payload_sha256": hash_file(cell / "provider_payload.json"),
                },
            )
            write_model(path, vote)
            journal.append(
                stage="vote_completed",
                terminal_key=f"{pass_name}:vote:{judge.value}:{cache_key}",
                artifact_path=path,
                candidate_id=candidate.candidate_id,
            )
            calls += 1
        if vote.judge != judge or vote.candidate_id != candidate.candidate_id:
            raise ReformPipelineError("vote cache identity mismatch")
        votes.append(vote)
    return tuple(votes), calls


def _run_pass(
    *,
    repo_root: Path,
    root: Path,
    source: SourceRecord,
    candidates: tuple[CandidateRecord, ...],
    config: SmokeConfig,
    pins: RuntimePins,
    loaded: LoadedJudges,
    journal: AppendOnlyJournal,
    pass_name: str,
) -> ReformPassResult:
    evidence, lean_calls, peak_rss = _ensure_render_batch(
        repo_root=repo_root,
        root=root,
        source=source,
        candidates=candidates,
        config=config,
        pins=pins,
        journal=journal,
        pass_name=pass_name,
    )
    results: list[CandidateSemanticResult] = []
    judge_calls = 0
    for candidate, compilation in zip(candidates, evidence, strict=True):
        votes, calls = _ensure_votes(
            root=root,
            source=source,
            candidate=candidate,
            compilation=compilation,
            loaded=loaded,
            journal=journal,
            pass_name=pass_name,
        )
        judge_calls += calls
        majority = None
        if compilation.status == CompileStatus.VALID:
            if len(votes) != 3:
                raise ReformPipelineError("valid candidate did not receive three votes")
            majority = majority_outcome(candidate.candidate_id, votes)
        results.append(
            CandidateSemanticResult(
                compilation=compilation,
                votes=votes,
                majority=majority,
            )
        )
    return ReformPassResult(
        candidates=tuple(results),
        lean_requests=lean_calls,
        judge_calls=judge_calls,
        lean_peak_rss_bytes=peak_rss,
    )


def _source_snapshot(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": hash_file(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _formalizer_invalid(attempt: FormalizerAttempt) -> FormalizerInvalidAttemptView:
    return FormalizerInvalidAttemptView(
        attempt_id=attempt.attempt_id,
        source_id=attempt.source_id,
        slot=attempt.slot,
        validity_label=False,
        failure_class=cast(str, attempt.failure_class),
        failure_detail=cast(str, attempt.failure_detail),
        raw_output_sha256=attempt.raw_output_sha256,
    )


def _compact(
    *,
    root: Path,
    run_id: str,
    source: SourceRecord,
    generation: FormalizerRunResult,
    input_receipt_hash: str,
    source_before: dict[str, object],
    source_after: dict[str, object],
    pins: RuntimePins,
    loaded: LoadedJudges,
    first: ReformPassResult,
    restart: ReformPassResult,
    journal: AppendOnlyJournal,
) -> RunManifest:
    outputs = root / "outputs"
    attempts: list[object] = [item.model_dump(mode="json") for item in generation.attempts]
    formalizer_invalid: list[object] = [
        _formalizer_invalid(item).model_dump(mode="json")
        for item in generation.attempts
        if item.extraction_status == "invalid"
    ]
    compilation: list[object] = [
        item.compilation.model_dump(mode="json") for item in first.candidates
    ]
    votes: list[object] = [
        vote.model_dump(mode="json") for item in first.candidates for vote in item.votes
    ]
    majority: list[object] = [
        item.majority.model_dump(mode="json")
        for item in first.candidates
        if item.majority is not None
    ]
    core: list[object] = []
    invalid: list[object] = []
    unknowns: list[object] = []
    for candidate, result in zip(generation.candidates, first.candidates, strict=True):
        if result.compilation.status != CompileStatus.VALID:
            invalid.append(
                InvalidAttempt(
                    candidate_id=candidate.candidate_id,
                    source_id=source.source_id,
                    compilation_evidence_id=result.compilation.evidence_id,
                    validity_label=False,
                    failure_class=cast(str, result.compilation.failure_class),
                    failure_detail=cast(str, result.compilation.failure_detail),
                ).model_dump(mode="json")
            )
            continue
        if result.majority is None:
            raise ReformPipelineError("valid candidate lacks majority")
        if result.majority.decision == MajorityDecision.UNKNOWN:
            unknowns.append(
                UnknownCandidate(
                    candidate_id=candidate.candidate_id,
                    source_id=source.source_id,
                    compilation_evidence_id=result.compilation.evidence_id,
                    majority_outcome_id=result.majority.outcome_id,
                    vote_ids=result.majority.vote_ids,
                    reason="three-voter policy produced no two-vote semantic majority",
                ).model_dump(mode="json")
            )
        else:
            assert result.compilation.reference is not None
            assert result.compilation.candidate is not None
            core.append(
                CoreRow(
                    reference=cast(str, result.compilation.reference.goal_v1),
                    candidate=cast(str, result.compilation.candidate.goal_v1),
                    label=cast(bool, result.majority.label),
                ).model_dump(mode="json")
            )
    output_rows: dict[str, list[object]] = {
        "sources.jsonl": [source.model_dump(mode="json")],
        "formalizer_attempts.jsonl": attempts,
        "formalizer_invalid_attempts.jsonl": formalizer_invalid,
        "candidates.jsonl": [item.model_dump(mode="json") for item in generation.candidates],
        "compilation_evidence.jsonl": compilation,
        "votes.jsonl": votes,
        "majority.jsonl": majority,
        "core.jsonl": core,
        "invalid_attempts.jsonl": invalid,
        "unknowns.jsonl": unknowns,
    }
    for name, rows in output_rows.items():
        write_jsonl(outputs / name, rows)
    write_json(
        outputs / "performance.json",
        {
            "schema_version": "sft2b_reform_performance_v1",
            "formalizer_attempts": [
                {
                    "slot": item.slot.value,
                    "elapsed_ms": item.elapsed_ms,
                    "prompt_tokens": item.prompt_tokens,
                    "completion_tokens": item.completion_tokens,
                    "peak_cuda_allocated_bytes": item.peak_cuda_allocated_bytes,
                    "peak_cuda_reserved_bytes": item.peak_cuda_reserved_bytes,
                }
                for item in generation.attempts
            ],
            "lean_elapsed_ms": [item.compilation.elapsed_ms for item in first.candidates],
            "lean_peak_rss_bytes": first.lean_peak_rss_bytes,
        },
    )
    write_json(
        outputs / "source_immutability.json",
        {
            "schema_version": "sft2b_source_immutability_v1",
            "before": source_before,
            "after": source_after,
            "unchanged": source_before == source_after,
        },
    )
    journal.append(
        stage="compacted",
        terminal_key="compaction:reform-outputs-v1",
        artifact_path=outputs / "majority.jsonl",
    )
    output_files = [outputs / name for name in output_rows]
    output_files.extend((outputs / "performance.json", outputs / "source_immutability.json"))
    output_hashes = {path.name: hash_file(path) for path in output_files}
    prompt_hashes = {item.judge: item.prompt_sha256 for item in loaded.config.providers}
    valid_count = sum(item.compilation.status == CompileStatus.VALID for item in first.candidates)
    manifest = RunManifest(
        run_id=run_id,
        run_kind="reform_8b_smoke",
        source_ids=(source.source_id,),
        candidate_ids=tuple(item.candidate_id for item in generation.candidates),
        repr_freeze_commit=pins.repr_freeze_commit,
        repr_spec_sha256=pins.repr_spec_hash,
        repr_implementation_set_sha256=pins.repr_implementation_set_hash,
        repr_api_sha256=pins.repr_api_hash,
        helper_sha256=pins.sft2b_helper_hash,
        prompt_hashes=prompt_hashes,
        input_receipt_sha256=input_receipt_hash,
        journal_sha256=hash_file(journal.path),
        output_hashes=output_hashes,
        counts={
            "sources": 1,
            "formalizer_attempts": len(generation.attempts),
            "formalizer_candidates": len(generation.candidates),
            "formalizer_invalid": len(formalizer_invalid),
            "valid": valid_count,
            "core": len(core),
            "invalid": len(invalid),
            "unknown": len(unknowns),
            "votes": len(votes),
            "formalizer_calls": len(generation.attempts),
            "restart_formalizer_calls": 0,
        },
        lean_request_count=first.lean_requests,
        judge_call_count=first.judge_calls,
        restart_lean_request_count=restart.lean_requests,
        restart_judge_call_count=restart.judge_calls,
        publication_performed=False,
        training_performed=False,
    )
    write_model(root / "manifest.json", manifest)
    return manifest


def run_reform_smoke(
    repo_root: Path,
    *,
    existing_config_path: Path,
    source_config_path: Path,
    model_config_path: Path,
) -> ReformSmokeResult:
    """Run and immediately replay one complete ReForm source without duplicate calls."""

    existing = run_existing_smoke(repo_root, existing_config_path)
    if existing.manifest.counts.get("core") != 1:
        raise ReformPipelineError("existing-candidate gate is not accepted")
    config = load_smoke_config(repo_root, existing_config_path)
    pins = verify_runtime_pins(repo_root, helper_path=config.helper_path)
    loaded = load_judges(repo_root, config.judges_config_path)
    source, source_receipt = _load_source(
        repo_root,
        source_config_path=source_config_path,
        helper_path=config.helper_path,
        pins=pins,
    )
    generation = run_reform_8b_generation(
        repo_root,
        config_path=model_config_path,
        source=source,
    )
    if len(generation.attempts) != 4:
        raise ReformPipelineError("ReForm generation did not produce four durable attempts")
    run_id = stable_id(
        "sft2b_run",
        {
            "run_kind": "reform_8b_smoke",
            "source_id": source.source_id,
            "generation_run_id": generation.run_id,
            "pins": pins.to_dict(),
            "judge_config_sha256": hash_file(config.judges_config_path),
        },
    )
    root = config.staging_root / "smokes" / run_id
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = read_model(manifest_path, RunManifest)
        for name, expected in manifest.output_hashes.items():
            if hash_file(root / "outputs" / name) != expected:
                raise ReformPipelineError(f"ReForm manifest output drift: {name}")
        return ReformSmokeResult(
            manifest=manifest,
            output_root=root,
            generation_root=generation.root,
            resumed_existing_manifest=True,
        )
    root.mkdir(parents=True, exist_ok=True)
    write_model(root / "inputs/source.json", source)
    write_json(root / "inputs/source_receipt.json", source_receipt)
    write_json(root / "inputs/runtime_pins.json", pins.to_dict())
    write_json(
        root / "inputs/formalizer.json",
        {
            "schema_version": "sft2b_formalizer_run_receipt_v1",
            "run_id": generation.run_id,
            "root": str(generation.root),
            "model_config_sha256": hash_file(model_config_path),
            "attempt_ids": [item.attempt_id for item in generation.attempts],
            "candidate_ids": [item.candidate_id for item in generation.candidates],
            "raw_output_sha256": [item.raw_output_sha256 for item in generation.attempts],
        },
    )
    input_receipt_path = root / "inputs/input_receipt.json"
    write_json(
        input_receipt_path,
        {
            "schema_version": "sft2b_reform_input_receipt_v1",
            "source_sha256": hash_file(root / "inputs/source.json"),
            "source_receipt_sha256": hash_file(root / "inputs/source_receipt.json"),
            "runtime_pins_sha256": hash_file(root / "inputs/runtime_pins.json"),
            "formalizer_sha256": hash_file(root / "inputs/formalizer.json"),
            "judge_config_sha256": hash_file(config.judges_config_path),
        },
    )
    journal = AppendOnlyJournal(
        root / "journal/events.jsonl", run_id=run_id, source_id=source.source_id
    )
    journal.append(
        stage="source_recovered",
        terminal_key="source:recovered",
        artifact_path=root / "inputs/source.json",
    )
    for attempt in generation.attempts:
        journal.append(
            stage="formalizer_completed",
            terminal_key=f"formalizer:{attempt.slot.value}:{attempt.attempt_id}",
            artifact_path=generation.root / "slots" / attempt.slot.value / "attempt.json",
            candidate_id=attempt.candidate_id,
        )
    source_path = Path(source.provenance.source_path)
    source_before = _source_snapshot(source_path)
    first = _run_pass(
        repo_root=repo_root,
        root=root,
        source=source,
        candidates=generation.candidates,
        config=config,
        pins=pins,
        loaded=loaded,
        journal=journal,
        pass_name="first",
    )
    generation_restart = run_reform_8b_generation(
        repo_root,
        config_path=model_config_path,
        source=source,
    )
    if generation_restart.model_calls != 0 or generation_restart.model_loaded:
        raise ReformPipelineError("restart duplicated a formalizer call or model load")
    restart = _run_pass(
        repo_root=repo_root,
        root=root,
        source=source,
        candidates=generation_restart.candidates,
        config=config,
        pins=pins,
        loaded=loaded,
        journal=journal,
        pass_name="restart",
    )
    if restart.lean_requests != 0 or restart.judge_calls != 0:
        raise ReformPipelineError("restart duplicated a Lean or judge call")
    if first.candidates != restart.candidates:
        raise ReformPipelineError("restart semantic terminals differ")
    source_after = _source_snapshot(source_path)
    if source_before != source_after:
        raise ReformPipelineError("source artifact changed during ReForm smoke")
    manifest = _compact(
        root=root,
        run_id=run_id,
        source=source,
        generation=generation,
        input_receipt_hash=hash_file(input_receipt_path),
        source_before=source_before,
        source_after=source_after,
        pins=pins,
        loaded=loaded,
        first=first,
        restart=restart,
        journal=journal,
    )
    return ReformSmokeResult(
        manifest=manifest,
        output_root=root,
        generation_root=generation.root,
        resumed_existing_manifest=False,
    )
