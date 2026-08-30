"""End-to-end, restart-safe SFT2B one-candidate execution pipeline."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.host_resources import claim_resources, release_resources
from leanfaith.lean.leaninteract_backend import METHOD_VERSION
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import CompileContext
from leanfaith.sft2b.durable import (
    AppendOnlyJournal,
    immutable_write,
    read_model,
    write_json,
    write_jsonl,
    write_model,
)
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
    render_propositions,
)
from leanfaith.sft2b.pins import RuntimePins, verify_runtime_pins
from leanfaith.sft2b.reuse import ExistingCandidate, load_existing_301, receipt_bytes
from leanfaith.sft2b.schemas import (
    CompilationEvidence,
    CompileStatus,
    CoreRow,
    EndpointCacheRecord,
    InvalidAttempt,
    JudgeId,
    JudgeVote,
    MajorityDecision,
    MajorityOutcome,
    RunManifest,
    UnknownCandidate,
    majority_outcome,
    stable_id,
)


class SFT2BPipelineError(RuntimeError):
    """Raised when a smoke cannot produce a coherent durable terminal."""


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    pair_id: str
    existing_recipe_path: Path
    judges_config_path: Path
    helper_path: Path
    staging_root: Path
    mathlib_project_path: Path
    lean_timeout_seconds: float
    owner_session: str


@dataclass(frozen=True, slots=True)
class PassCounts:
    lean_requests: int = 0
    judge_calls: int = 0


@dataclass(frozen=True, slots=True)
class PassResult:
    compilation: CompilationEvidence
    votes: tuple[JudgeVote, ...]
    majority: MajorityOutcome | None
    counts: PassCounts


@dataclass(frozen=True, slots=True)
class SmokeResult:
    manifest: RunManifest
    output_root: Path
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
        raise SFT2BPipelineError("SFT2B direct-Expr route must use one persistent request")

    def close(self) -> None:
        self.backend.close()


def load_smoke_config(repo_root: Path, path: Path) -> SmokeConfig:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "sft2b_existing_smoke_v1":
        raise SFT2BPipelineError("unsupported existing smoke config")
    return SmokeConfig(
        pair_id=str(value["pair_id"]),
        existing_recipe_path=repo_root / str(value["existing_recipe_path"]),
        judges_config_path=repo_root / str(value["judges_config_path"]),
        helper_path=repo_root / str(value["helper_path"]),
        staging_root=Path(str(value["staging_root"])),
        mathlib_project_path=Path(str(value["mathlib_project_path"])),
        lean_timeout_seconds=float(value["lean_timeout_seconds"]),
        owner_session=str(value["owner_session"]),
    )


def _git_revision(path: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise SFT2BPipelineError(f"cannot inspect project revision: {path}")
    return completed.stdout.strip()


def _select(rows: tuple[ExistingCandidate, ...], pair_id: str) -> ExistingCandidate:
    matches = [row for row in rows if row.candidate.legacy_pair_id == pair_id]
    if len(matches) != 1:
        raise SFT2BPipelineError(f"smoke pair selection produced {len(matches)} rows")
    return matches[0]


def _compile_context(
    item: ExistingCandidate, config: SmokeConfig, pins: RuntimePins
) -> CompileContext:
    path = Path(item.source.compile_context.source_context_path)
    context, _ = compile_context_from_source(
        source_context_path=path, helper_path=config.helper_path, pins=pins
    )
    if context.compile_context_id != item.source.compile_context.render_compile_context_id:
        raise SFT2BPipelineError("recovered render compile context drifted")
    return context


def _endpoints(item: ExistingCandidate) -> tuple[PropositionEndpoint, PropositionEndpoint]:
    return (
        PropositionEndpoint(
            endpoint_id="reference",
            endpoint_role="reference",
            proposition=item.source.reference_proposition,
            source_id=item.source.source_id,
        ),
        PropositionEndpoint(
            endpoint_id="candidate",
            endpoint_role="candidate",
            proposition=item.candidate.raw_proof_free_signature,
            source_id=item.source.source_id,
            candidate_id=item.candidate.candidate_id,
        ),
    )


def _endpoint_record(
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
        raise SFT2BPipelineError("REPR output contains a forbidden model-facing placeholder")
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


def _render_cache_paths(
    root: Path, keys: tuple[str, str], batch_key: str
) -> tuple[Path, Path, Path]:
    return (
        root / "cache/endpoints" / f"{keys[0]}.json",
        root / "cache/endpoints" / f"{keys[1]}.json",
        root / "cache/render_batches" / f"{batch_key}.json",
    )


def _ensure_render(
    *,
    repo_root: Path,
    root: Path,
    item: ExistingCandidate,
    config: SmokeConfig,
    pins: RuntimePins,
    journal: AppendOnlyJournal,
    pass_name: str,
) -> tuple[CompilationEvidence, int]:
    context = _compile_context(item, config, pins)
    endpoints = _endpoints(item)
    keys = tuple(
        endpoint_cache_key(
            endpoint,
            source_context_id=item.candidate.source_context_id,
            compile_context=context,
            pins=pins,
        )
        for endpoint in endpoints
    )
    typed_keys = cast(tuple[str, str], keys)
    batch_key = hash_canonical(
        {
            "schema_version": "sft2b_render_batch_key_v1",
            "endpoint_cache_keys": typed_keys,
            "render_scope": item.candidate.candidate_id,
        }
    )
    reference_path, candidate_path, batch_path = _render_cache_paths(root, typed_keys, batch_key)
    endpoint_presence = (reference_path.exists(), candidate_path.exists())
    if batch_path.exists():
        evidence = read_model(batch_path, CompilationEvidence)
        if evidence.status == CompileStatus.VALID:
            if evidence.reference is None or evidence.candidate is None:
                raise SFT2BPipelineError("valid render terminal lacks cached endpoints")
            # The batch record is the atomic commit point. Reconstituting a
            # missing task-owned endpoint file from that immutable terminal is
            # safe and does not invoke Lean again after an interrupted write.
            write_model(reference_path, evidence.reference)
            write_model(candidate_path, evidence.candidate)
        elif any(endpoint_presence):
            raise SFT2BPipelineError("failed render terminal has unexpected endpoint caches")
        journal.append(
            stage="render_cache_hit",
            terminal_key=f"{pass_name}:render:{batch_key}",
            artifact_path=batch_path,
            candidate_id=item.candidate.candidate_id,
        )
        return evidence, 0
    if any(endpoint_presence):
        raise SFT2BPipelineError("endpoint cache exists without its atomic batch terminal")

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
        rendered = render_propositions(
            capturing,
            endpoints=endpoints,
            compile_context=context,
            render_scope_id=f"scope:{item.candidate.candidate_id}",
            request_id=f"sft2b-existing-smoke:{item.candidate.candidate_id}",
            timeout_seconds=config.lean_timeout_seconds,
        )
    finally:
        capturing.close()
        released = release_resources(task="SFT2B")
        if released != reservation:
            raise SFT2BPipelineError("released resource claim differs from acquired claim")
    if len(capturing.requests) != 1 or len(capturing.results) != 1:
        raise SFT2BPipelineError("existing smoke must issue exactly one Lean request")
    lean_result = capturing.results[0]
    raw_path = Path(rendered.raw_response_path) if rendered.raw_response_path else None
    raw_hash = hash_file(raw_path) if raw_path is not None and raw_path.is_file() else None
    if rendered.failures:
        status = (
            CompileStatus.INVALID
            if lean_result.status in {LeanStatus.INVALID, LeanStatus.UNSUPPORTED}
            else CompileStatus.INFRASTRUCTURE_FAILURE
        )
        detail = "; ".join(
            f"{failure.endpoint_id}: {failure.detail}" for failure in rendered.failures
        )
        evidence_id = stable_id(
            "sft2b_compile",
            {"batch_key": batch_key, "request_hash": rendered.request_hash, "status": status},
        )
        evidence = CompilationEvidence(
            evidence_id=evidence_id,
            source_id=item.source.source_id,
            candidate_id=item.candidate.candidate_id,
            reference_cache_key=typed_keys[0],
            candidate_cache_key=typed_keys[1],
            request_hash=rendered.request_hash,
            status=status,
            elapsed_ms=rendered.elapsed_ms,
            raw_response_path=str(raw_path) if raw_path is not None else None,
            raw_response_sha256=raw_hash,
            backend_method_version=METHOD_VERSION,
            cache_hit=False,
            failure_class=(
                "lean_or_repr_invalid" if status == CompileStatus.INVALID else "lean_infrastructure"
            ),
            failure_detail=detail,
        )
        write_model(batch_path, evidence)
    else:
        if len(rendered.sidecars) != 2:
            raise SFT2BPipelineError("valid render did not return exactly two atomic sidecars")
        by_id = {sidecar.record.endpoint_id: sidecar for sidecar in rendered.sidecars}
        reference = _endpoint_record(
            endpoint=endpoints[0],
            cache_key=typed_keys[0],
            source_context_id=item.candidate.source_context_id,
            context=context,
            pins=pins,
            sidecar=by_id["reference"],
        )
        candidate = _endpoint_record(
            endpoint=endpoints[1],
            cache_key=typed_keys[1],
            source_context_id=item.candidate.source_context_id,
            context=context,
            pins=pins,
            sidecar=by_id["candidate"],
        )
        evidence_id = stable_id(
            "sft2b_compile",
            {
                "reference_cache_key": reference.endpoint_cache_key,
                "candidate_cache_key": candidate.endpoint_cache_key,
                "request_hash": rendered.request_hash,
            },
        )
        evidence = CompilationEvidence(
            evidence_id=evidence_id,
            source_id=item.source.source_id,
            candidate_id=item.candidate.candidate_id,
            reference_cache_key=reference.endpoint_cache_key,
            candidate_cache_key=candidate.endpoint_cache_key,
            request_hash=rendered.request_hash,
            status=CompileStatus.VALID,
            elapsed_ms=rendered.elapsed_ms,
            raw_response_path=str(raw_path) if raw_path is not None else None,
            raw_response_sha256=raw_hash,
            backend_method_version=METHOD_VERSION,
            cache_hit=False,
            reference=reference,
            candidate=candidate,
        )
        # The nested batch is the atomic commit point. A restart can restore
        # either individual cache record from it without another Lean call.
        write_model(batch_path, evidence)
        write_model(reference_path, reference)
        write_model(candidate_path, candidate)
    journal.append(
        stage="render_completed",
        terminal_key=f"{pass_name}:render:{batch_key}",
        artifact_path=batch_path,
        candidate_id=item.candidate.candidate_id,
    )
    return evidence, 1


def _vote_path(root: Path, cache_key: str) -> Path:
    return root / "cache/votes" / cache_key / "vote.json"


def _ensure_votes(
    *,
    root: Path,
    item: ExistingCandidate,
    compilation: CompilationEvidence,
    loaded: LoadedJudges,
    journal: AppendOnlyJournal,
    pass_name: str,
) -> tuple[tuple[JudgeVote, ...], int]:
    if compilation.status != CompileStatus.VALID:
        return (), 0
    if compilation.reference is None or compilation.candidate is None:
        raise SFT2BPipelineError("valid compilation lacks judge-facing endpoints")
    reference = cast(str, compilation.reference.goal_v1)
    candidate = cast(str, compilation.candidate.goal_v1)
    input_hash = judge_input_hash(
        nl_statement=item.source.nl_statement, reference=reference, candidate=candidate
    )
    votes: list[JudgeVote] = []
    calls = 0
    for judge in JudgeId:
        provider = loaded.provider(judge)
        cache_key = vote_cache_key(
            loaded,
            provider,
            candidate_id=item.candidate.candidate_id,
            input_sha256=input_hash,
        )
        path = _vote_path(root, cache_key)
        if path.exists():
            vote = read_model(path, JudgeVote)
            journal.append(
                stage="vote_cache_hit",
                terminal_key=f"{pass_name}:vote:{judge.value}:{cache_key}",
                artifact_path=path,
                candidate_id=item.candidate.candidate_id,
            )
        else:
            result: JudgeCallResult = run_judge(
                loaded,
                judge=judge,
                candidate_id=item.candidate.candidate_id,
                nl_statement=item.source.nl_statement,
                reference=reference,
                candidate=candidate,
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
                    "candidate_id": item.candidate.candidate_id,
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
                candidate_id=item.candidate.candidate_id,
            )
            calls += 1
        if vote.judge != judge or vote.candidate_id != item.candidate.candidate_id:
            raise SFT2BPipelineError("vote cache identity mismatch")
        votes.append(vote)
    return tuple(votes), calls


def _run_pass(
    *,
    repo_root: Path,
    root: Path,
    item: ExistingCandidate,
    config: SmokeConfig,
    pins: RuntimePins,
    loaded_judges: LoadedJudges,
    journal: AppendOnlyJournal,
    pass_name: str,
) -> PassResult:
    compilation, lean_requests = _ensure_render(
        repo_root=repo_root,
        root=root,
        item=item,
        config=config,
        pins=pins,
        journal=journal,
        pass_name=pass_name,
    )
    votes, judge_calls = _ensure_votes(
        root=root,
        item=item,
        compilation=compilation,
        loaded=loaded_judges,
        journal=journal,
        pass_name=pass_name,
    )
    majority = None
    if compilation.status == CompileStatus.VALID:
        if len(votes) != 3:
            raise SFT2BPipelineError("valid pair did not receive three votes")
        majority = majority_outcome(
            item.candidate.candidate_id,
            votes,
        )
    return PassResult(
        compilation=compilation,
        votes=votes,
        majority=majority,
        counts=PassCounts(lean_requests=lean_requests, judge_calls=judge_calls),
    )


def _compact(
    *,
    root: Path,
    item: ExistingCandidate,
    receipt_hash: str,
    pins: RuntimePins,
    loaded_judges: LoadedJudges,
    first: PassResult,
    restart: PassResult,
    journal: AppendOnlyJournal,
    run_id: str,
    source_before: dict[str, object],
    source_after: dict[str, object],
) -> RunManifest:
    outputs = root / "outputs"
    write_jsonl(outputs / "sources.jsonl", [item.source.model_dump(mode="json")])
    write_jsonl(outputs / "candidates.jsonl", [item.candidate.model_dump(mode="json")])
    write_jsonl(outputs / "compilation_evidence.jsonl", [first.compilation.model_dump(mode="json")])
    write_jsonl(outputs / "votes.jsonl", [vote.model_dump(mode="json") for vote in first.votes])
    core_rows: list[object] = []
    invalid_rows: list[object] = []
    unknown_rows: list[object] = []
    majority_rows: list[object] = []
    if first.compilation.status != CompileStatus.VALID:
        invalid = InvalidAttempt(
            candidate_id=item.candidate.candidate_id,
            source_id=item.source.source_id,
            compilation_evidence_id=first.compilation.evidence_id,
            validity_label=False,
            failure_class=cast(str, first.compilation.failure_class),
            failure_detail=cast(str, first.compilation.failure_detail),
        )
        invalid_rows.append(invalid.model_dump(mode="json"))
    elif first.majority is None:
        raise SFT2BPipelineError("valid pair lacks majority record")
    else:
        majority_rows.append(first.majority.model_dump(mode="json"))
        if first.majority.decision == MajorityDecision.UNKNOWN:
            unknown = UnknownCandidate(
                candidate_id=item.candidate.candidate_id,
                source_id=item.source.source_id,
                compilation_evidence_id=first.compilation.evidence_id,
                majority_outcome_id=first.majority.outcome_id,
                vote_ids=first.majority.vote_ids,
                reason="three-voter policy produced no two-vote semantic majority",
            )
            unknown_rows.append(unknown.model_dump(mode="json"))
        else:
            if first.compilation.reference is None or first.compilation.candidate is None:
                raise SFT2BPipelineError("core row lacks valid rendered endpoints")
            core = CoreRow(
                reference=cast(str, first.compilation.reference.goal_v1),
                candidate=cast(str, first.compilation.candidate.goal_v1),
                label=cast(bool, first.majority.label),
            )
            core_rows.append(core.model_dump(mode="json"))
    write_jsonl(outputs / "majority.jsonl", majority_rows)
    write_jsonl(outputs / "core.jsonl", core_rows)
    write_jsonl(outputs / "invalid_attempts.jsonl", invalid_rows)
    write_jsonl(outputs / "unknowns.jsonl", unknown_rows)
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
        terminal_key="compaction:outputs-v1",
        artifact_path=outputs / "majority.jsonl",
        candidate_id=item.candidate.candidate_id,
    )
    output_files = [
        outputs / name
        for name in (
            "sources.jsonl",
            "candidates.jsonl",
            "compilation_evidence.jsonl",
            "votes.jsonl",
            "majority.jsonl",
            "core.jsonl",
            "invalid_attempts.jsonl",
            "unknowns.jsonl",
            "source_immutability.json",
        )
    ]
    output_hashes = {path.name: hash_file(path) for path in output_files}
    prompt_hashes = {item.judge: item.prompt_sha256 for item in loaded_judges.config.providers}
    manifest = RunManifest(
        run_id=run_id,
        run_kind="existing_301_smoke",
        source_ids=(item.source.source_id,),
        candidate_ids=(item.candidate.candidate_id,),
        repr_freeze_commit=pins.repr_freeze_commit,
        repr_spec_sha256=pins.repr_spec_hash,
        repr_implementation_set_sha256=pins.repr_implementation_set_hash,
        repr_api_sha256=pins.repr_api_hash,
        helper_sha256=pins.sft2b_helper_hash,
        prompt_hashes=prompt_hashes,
        input_receipt_sha256=receipt_hash,
        journal_sha256=hash_file(journal.path),
        output_hashes=output_hashes,
        counts={
            "sources": 1,
            "candidates": 1,
            "valid": int(first.compilation.status == CompileStatus.VALID),
            "core": len(core_rows),
            "invalid": len(invalid_rows),
            "unknown": len(unknown_rows),
            "votes": len(first.votes),
        },
        lean_request_count=first.counts.lean_requests,
        judge_call_count=first.counts.judge_calls,
        restart_lean_request_count=restart.counts.lean_requests,
        restart_judge_call_count=restart.counts.judge_calls,
        publication_performed=False,
        training_performed=False,
    )
    write_model(root / "manifest.json", manifest)
    return manifest


def _source_snapshot(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": hash_file(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def run_existing_smoke(repo_root: Path, config_path: Path) -> SmokeResult:
    """Run one existing candidate, then immediately prove zero-call restart."""

    config = load_smoke_config(repo_root, config_path)
    # Process-start gate: no staging file, Lean request, or judge call precedes
    # complete REPR/helper and provider pin verification.
    pins = verify_runtime_pins(repo_root, helper_path=config.helper_path)
    loaded_judges = load_judges(repo_root, config.judges_config_path)
    rows, receipt = load_existing_301(
        repo_root,
        recipe_path=config.existing_recipe_path,
        helper_path=config.helper_path,
        pins=pins,
    )
    item = _select(rows, config.pair_id)
    if _git_revision(config.mathlib_project_path) != item.source.compile_context.project_revision:
        raise SFT2BPipelineError("Mathlib checkout revision differs from recovered source context")
    run_id = stable_id(
        "sft2b_run",
        {
            "run_kind": "existing_301_smoke",
            "candidate_id": item.candidate.candidate_id,
            "consumed_bundle_sha256": receipt.consumed_bundle_sha256,
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
                raise SFT2BPipelineError(f"existing manifest output drift: {name}")
        return SmokeResult(manifest=manifest, output_root=root, resumed_existing_manifest=True)
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = root / "inputs/existing_301_receipt.json"
    immutable_write(receipt_path, receipt_bytes(receipt))
    write_json(root / "inputs/runtime_pins.json", pins.to_dict())
    write_model(root / "inputs/source.json", item.source)
    write_model(root / "inputs/candidate.json", item.candidate)
    pair_path = repo_root / item.pair_path
    source_before = _source_snapshot(pair_path)
    journal = AppendOnlyJournal(
        root / "journal/events.jsonl", run_id=run_id, source_id=item.source.source_id
    )
    journal.append(
        stage="source_recovered",
        terminal_key="source:recovered",
        artifact_path=root / "inputs/source.json",
    )
    journal.append(
        stage="candidate_recovered",
        terminal_key="candidate:recovered",
        artifact_path=root / "inputs/candidate.json",
        candidate_id=item.candidate.candidate_id,
    )
    first = _run_pass(
        repo_root=repo_root,
        root=root,
        item=item,
        config=config,
        pins=pins,
        loaded_judges=loaded_judges,
        journal=journal,
        pass_name="first",
    )
    restart = _run_pass(
        repo_root=repo_root,
        root=root,
        item=item,
        config=config,
        pins=pins,
        loaded_judges=loaded_judges,
        journal=journal,
        pass_name="restart",
    )
    if restart.counts != PassCounts():
        raise SFT2BPipelineError("restart performed a duplicate Lean or judge call")
    if first.compilation != restart.compilation:
        raise SFT2BPipelineError("restart compilation terminal differs")
    if first.votes != restart.votes or first.majority != restart.majority:
        raise SFT2BPipelineError("restart semantic terminal differs")
    source_after = _source_snapshot(pair_path)
    if source_before != source_after:
        raise SFT2BPipelineError("legacy source changed during smoke")
    manifest = _compact(
        root=root,
        item=item,
        receipt_hash=hash_file(receipt_path),
        pins=pins,
        loaded_judges=loaded_judges,
        first=first,
        restart=restart,
        journal=journal,
        run_id=run_id,
        source_before=source_before,
        source_after=source_after,
    )
    return SmokeResult(manifest=manifest, output_root=root, resumed_existing_manifest=False)
