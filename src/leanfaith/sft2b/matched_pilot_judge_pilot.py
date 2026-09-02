"""Concurrent, restart-safe three-judge pilot over the matched Lean audit."""

from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import Field

from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.sft2b.durable import (
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
from leanfaith.sft2b.matched_pilot_lean_audit import _load_inputs
from leanfaith.sft2b.matched_pilot_lean_audit import (
    load_config as load_audit_config,
)
from leanfaith.sft2b.schemas import (
    CandidateRecord,
    JudgeId,
    JudgeVote,
    MajorityDecision,
    NonEmpty,
    Sha256,
    SourceRecord,
    StableId,
    majority_outcome,
    stable_id,
)

SCHEMA_VERSION = "sft2b_matched_pilot_judge_pilot_v1"
SELECTION_VERSION = "valid_unique_hash_rank_v1"
MANIFEST_VERSION = "sft2b_matched_pilot_judge_manifest_v1"


class MatchedPilotJudgeError(RuntimeError):
    """The judge pilot input, cache, output, or gate contract failed."""


class PilotThresholds(StrictModel):
    maximum_unknown_fraction: Annotated[float, Field(ge=0.0, le=1.0)]
    minimum_pairwise_agreement: Annotated[float, Field(ge=0.0, le=1.0)]


class MatchedPilotJudgeConfig(StrictModel):
    schema_version: Literal["sft2b_matched_pilot_judge_pilot_v1"]
    audit_config_path: NonEmpty
    audit_run_id: StableId
    audit_manifest_sha256: Sha256
    source_compilation_sha256: Sha256
    candidate_compilation_sha256: Sha256
    judges_config_path: NonEmpty
    judges_config_sha256: Sha256
    labeling_policy_path: NonEmpty
    labeling_policy_sha256: Sha256
    module_path: NonEmpty
    module_sha256: Sha256
    selection_count: Annotated[int, Field(gt=0)]
    maximum_provider_calls: Annotated[int, Field(gt=0)]
    maximum_concurrency: Annotated[int, Field(gt=0, le=64)]
    output_parent: Path
    thresholds: PilotThresholds


class SelectedCandidate(StrictModel):
    schema_version: Literal["sft2b_matched_judge_selection_v1"] = "sft2b_matched_judge_selection_v1"
    selection_rank: Annotated[int, Field(ge=0)]
    selection_key: Sha256
    source_id: StableId
    candidate_id: StableId
    source_class: NonEmpty
    signature_sha256: Sha256
    nl_statement: NonEmpty
    nl_statement_sha256: Sha256
    reference_goal_v1: NonEmpty
    reference_goal_v1_sha256: Sha256
    candidate_goal_v1: NonEmpty
    candidate_goal_v1_sha256: Sha256


class VoteTerminal(StrictModel):
    schema_version: Literal["sft2b_matched_judge_vote_terminal_v1"] = (
        "sft2b_matched_judge_vote_terminal_v1"
    )
    run_id: StableId
    source_id: StableId
    candidate_id: StableId
    judge: JudgeId
    input_sha256: Sha256
    cache_key: Sha256
    vote_path: NonEmpty
    vote_sha256: Sha256


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MatchedPilotJudgeError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _jsonl_objects(path: Path) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise MatchedPilotJudgeError(f"non-object row at {path}:{number}")
            rows.append(cast(dict[str, Any], value))
    return tuple(rows)


def load_config(repo_root: Path, config_path: Path) -> MatchedPilotJudgeConfig:
    config = MatchedPilotJudgeConfig.model_validate(_json_object(config_path))
    module_path = repo_root / config.module_path
    if (
        module_path.resolve() != Path(__file__).resolve()
        or hash_file(module_path) != config.module_sha256
    ):
        raise MatchedPilotJudgeError("judge-pilot module pin mismatch")
    for relative, expected, label in (
        (config.judges_config_path, config.judges_config_sha256, "judge config"),
        (config.labeling_policy_path, config.labeling_policy_sha256, "labeling policy"),
    ):
        if hash_file(repo_root / relative) != expected:
            raise MatchedPilotJudgeError(f"{label} pin mismatch")
    if config.maximum_provider_calls < config.selection_count * len(JudgeId):
        raise MatchedPilotJudgeError("provider-call ceiling cannot cover the frozen selection")
    return config


def _verify_active_defaults(
    repo_root: Path, config: MatchedPilotJudgeConfig, loaded: LoadedJudges
) -> None:
    value = yaml.safe_load((repo_root / config.labeling_policy_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("status") != "active_default":
        raise MatchedPilotJudgeError("labeling policy is not the active default")
    applies = value.get("applies_to")
    if not isinstance(applies, list) or "SFT2B" not in applies:
        raise MatchedPilotJudgeError("labeling policy does not apply to SFT2B")
    providers = value.get("providers")
    if not isinstance(providers, dict):
        raise MatchedPilotJudgeError("labeling policy lacks provider defaults")
    for judge in JudgeId:
        policy = providers.get(judge.value)
        provider = loaded.provider(judge)
        if not isinstance(policy, dict):
            raise MatchedPilotJudgeError(f"labeling policy lacks {judge.value}")
        if provider.model_id != policy.get("model") or provider.effort != policy.get("effort"):
            raise MatchedPilotJudgeError(f"{judge.value} judge differs from active defaults")


def _audit_root(config: MatchedPilotJudgeConfig, audit_output_parent: Path) -> Path:
    return audit_output_parent / config.audit_run_id.replace(":", "_")


def select_candidates(
    *,
    sources: Sequence[SourceRecord],
    candidates: Sequence[CandidateRecord],
    source_rows: Sequence[dict[str, Any]],
    candidate_rows: Sequence[dict[str, Any]],
    count: int,
) -> tuple[SelectedCandidate, ...]:
    """Select a canonical hash-ranked subset with unique formal signatures."""

    if len(source_rows) != len(sources) or len(candidate_rows) != len(candidates):
        raise MatchedPilotJudgeError("audit compaction count differs from frozen input count")
    source_by_id = {item.source_id: item for item in sources}
    source_audit = {cast(str, item.get("source_id")): item for item in source_rows}
    candidates_by_id = {item.candidate_id: item for item in candidates}
    if len(source_by_id) != len(sources) or len(candidates_by_id) != len(candidates):
        raise MatchedPilotJudgeError("frozen audit inputs contain duplicate stable IDs")

    eligible: list[tuple[str, CandidateRecord, dict[str, Any], dict[str, Any]]] = []
    for row, candidate in zip(candidate_rows, candidates, strict=True):
        if row.get("candidate_id") != candidate.candidate_id:
            raise MatchedPilotJudgeError("candidate audit order/identity drifted")
        source_row = source_audit.get(candidate.source_id)
        reference = source_row.get("reference") if source_row is not None else None
        if (
            row.get("elaboration_status") != "valid"
            or row.get("status") != "valid"
            or not isinstance(source_row, dict)
            or source_row.get("reference_elaborated") is not True
            or not isinstance(reference, dict)
            or reference.get("status") != "valid"
        ):
            continue
        key = hash_canonical(
            {
                "schema_version": SELECTION_VERSION,
                "candidate_id": candidate.candidate_id,
                "signature_sha256": candidate.signature_sha256,
            }
        )
        eligible.append((key, candidate, row, source_row))
    eligible.sort(key=lambda item: (item[0], item[1].candidate_id))

    selected: list[SelectedCandidate] = []
    signatures: set[str] = set()
    for key, candidate, row, source_row in eligible:
        if candidate.signature_sha256 in signatures:
            continue
        source = source_by_id[candidate.source_id]
        reference = cast(dict[str, Any], source_row["reference"])
        reference_goal = reference.get("goal_v1")
        candidate_goal = row.get("goal_v1")
        source_class = row.get("source_class")
        if not all(
            isinstance(item, str) and item
            for item in (reference_goal, candidate_goal, source_class)
        ):
            raise MatchedPilotJudgeError("eligible audit row lacks judge-facing text")
        selected.append(
            SelectedCandidate(
                selection_rank=len(selected),
                selection_key=key,
                source_id=source.source_id,
                candidate_id=candidate.candidate_id,
                source_class=cast(str, source_class),
                signature_sha256=candidate.signature_sha256,
                nl_statement=source.nl_statement,
                nl_statement_sha256=sha256_hex(source.nl_statement.encode("utf-8")),
                reference_goal_v1=cast(str, reference_goal),
                reference_goal_v1_sha256=sha256_hex(cast(str, reference_goal).encode("utf-8")),
                candidate_goal_v1=cast(str, candidate_goal),
                candidate_goal_v1_sha256=sha256_hex(cast(str, candidate_goal).encode("utf-8")),
            )
        )
        signatures.add(candidate.signature_sha256)
        if len(selected) == count:
            break
    if len(selected) != count:
        raise MatchedPilotJudgeError(
            f"only {len(selected)} representation-valid unique candidates are available"
        )
    return tuple(selected)


def prepare(
    *, repo_root: Path, config_path: Path, limit: int | None = None
) -> tuple[MatchedPilotJudgeConfig, LoadedJudges, tuple[SelectedCandidate, ...], str, Path]:
    config = load_config(repo_root, config_path)
    audit_config_path = repo_root / config.audit_config_path
    audit_config, _ = load_audit_config(audit_config_path)
    run_root = _audit_root(config, audit_config.output_parent)
    manifest_path = run_root / "manifest.json"
    manifest = _json_object(manifest_path)
    if (
        manifest.get("run_id") != config.audit_run_id
        or manifest.get("gate_passed") is not True
        or hash_file(manifest_path) != config.audit_manifest_sha256
    ):
        raise MatchedPilotJudgeError("matched Lean audit identity or gate drifted")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or (
        artifacts.get("source_compilation.jsonl") != config.source_compilation_sha256
        or artifacts.get("candidate_compilation.jsonl") != config.candidate_compilation_sha256
    ):
        raise MatchedPilotJudgeError("matched Lean audit artifact binding drifted")
    if (
        hash_file(run_root / "source_compilation.jsonl") != config.source_compilation_sha256
        or hash_file(run_root / "candidate_compilation.jsonl")
        != config.candidate_compilation_sha256
    ):
        raise MatchedPilotJudgeError("matched Lean audit bytes drifted")
    sources, candidates, _, _ = _load_inputs(audit_config)
    selected = select_candidates(
        sources=sources,
        candidates=candidates,
        source_rows=_jsonl_objects(run_root / "source_compilation.jsonl"),
        candidate_rows=_jsonl_objects(run_root / "candidate_compilation.jsonl"),
        count=config.selection_count,
    )
    if limit is not None:
        if limit < 1 or limit > len(selected):
            raise MatchedPilotJudgeError("limit is outside the frozen selection")
        selected = selected[:limit]
    loaded = load_judges(repo_root, repo_root / config.judges_config_path)
    _verify_active_defaults(repo_root, config, loaded)
    run_id = stable_id(
        "sft2b_judge_pilot",
        {
            "schema_version": SCHEMA_VERSION,
            "audit_run_id": config.audit_run_id,
            "selected_candidate_ids": tuple(item.candidate_id for item in selected),
            "judges_config_sha256": config.judges_config_sha256,
            "labeling_policy_sha256": config.labeling_policy_sha256,
            "module_sha256": config.module_sha256,
        },
    )
    pilot_root = config.output_parent / run_id.replace(":", "_")
    write_jsonl(
        pilot_root / "selected_candidates.jsonl",
        [item.model_dump(mode="json") for item in selected],
    )
    return config, loaded, selected, run_id, pilot_root


def _vote_paths(output_parent: Path, cache_key: str) -> tuple[Path, Path]:
    cell = output_parent / "cache" / "votes" / cache_key
    return cell, cell / "vote.json"


def _validate_cached_vote(
    vote: JudgeVote,
    *,
    item: SelectedCandidate,
    judge: JudgeId,
    input_sha256: str,
) -> None:
    if (
        vote.candidate_id != item.candidate_id
        or vote.judge != judge
        or vote.judge_input_sha256 != input_sha256
    ):
        raise MatchedPilotJudgeError("vote cache identity drifted")


def _call_or_load_vote(
    *,
    config: MatchedPilotJudgeConfig,
    loaded: LoadedJudges,
    item: SelectedCandidate,
    judge: JudgeId,
) -> tuple[JudgeVote, str, bool]:
    provider = loaded.provider(judge)
    input_sha = judge_input_hash(
        nl_statement=item.nl_statement,
        reference=item.reference_goal_v1,
        candidate=item.candidate_goal_v1,
    )
    cache_key = vote_cache_key(
        loaded, provider, candidate_id=item.candidate_id, input_sha256=input_sha
    )
    cell, vote_path = _vote_paths(config.output_parent, cache_key)
    if vote_path.is_file():
        vote = read_model(vote_path, JudgeVote)
        _validate_cached_vote(vote, item=item, judge=judge, input_sha256=input_sha)
        return vote, cache_key, False
    if cell.exists() and any(cell.iterdir()):
        raise MatchedPilotJudgeError(f"ambiguous incomplete judge cell: {cell}")
    write_json(
        cell / "started.json",
        {
            "schema_version": "sft2b_matched_judge_call_started_v1",
            "candidate_id": item.candidate_id,
            "judge": judge.value,
            "cache_key": cache_key,
            "input_sha256": input_sha,
        },
    )
    result: JudgeCallResult = run_judge(
        loaded,
        judge=judge,
        candidate_id=item.candidate_id,
        nl_statement=item.nl_statement,
        reference=item.reference_goal_v1,
        candidate=item.candidate_goal_v1,
        working_dir=config.output_parent / "work",
    )
    _validate_cached_vote(result.vote, item=item, judge=judge, input_sha256=input_sha)
    immutable_write(cell / "stdout.bin", result.stdout)
    immutable_write(cell / "stderr.bin", result.stderr)
    immutable_write(cell / "provider_payload.json", result.provider_payload)
    write_json(
        cell / "receipt.json",
        {
            "schema_version": "sft2b_matched_judge_call_receipt_v1",
            "candidate_id": item.candidate_id,
            "judge": judge.value,
            "cache_key": cache_key,
            "elapsed_seconds": result.elapsed_seconds,
            "stdout_sha256": hash_file(cell / "stdout.bin"),
            "stderr_sha256": hash_file(cell / "stderr.bin"),
            "provider_payload_sha256": hash_file(cell / "provider_payload.json"),
        },
    )
    write_model(vote_path, result.vote)
    return result.vote, cache_key, True


def pairwise_agreement(votes: Sequence[tuple[JudgeVote, JudgeVote, JudgeVote]]) -> float:
    pairs = [
        first.decision == second.decision
        for triple in votes
        for first, second in itertools.combinations(triple, 2)
    ]
    return sum(pairs) / len(pairs) if pairs else 0.0


def _terminal_path(pilot_root: Path, candidate_id: str, judge: JudgeId) -> Path:
    return pilot_root / "vote_terminals" / f"{candidate_id.split(':', 1)[1]}.{judge.value}.json"


def _write_vote_terminal(
    *,
    config: MatchedPilotJudgeConfig,
    loaded: LoadedJudges,
    item: SelectedCandidate,
    judge: JudgeId,
    vote: JudgeVote,
    cache_key: str,
    run_id: str,
    pilot_root: Path,
) -> None:
    input_sha = judge_input_hash(
        nl_statement=item.nl_statement,
        reference=item.reference_goal_v1,
        candidate=item.candidate_goal_v1,
    )
    _, vote_path = _vote_paths(config.output_parent, cache_key)
    expected = vote_cache_key(
        loaded,
        loaded.provider(judge),
        candidate_id=item.candidate_id,
        input_sha256=input_sha,
    )
    if expected != cache_key:
        raise MatchedPilotJudgeError("vote terminal cache key does not replay")
    terminal = VoteTerminal(
        run_id=run_id,
        source_id=item.source_id,
        candidate_id=item.candidate_id,
        judge=judge,
        input_sha256=input_sha,
        cache_key=cache_key,
        vote_path=str(vote_path),
        vote_sha256=hash_file(vote_path),
    )
    write_model(_terminal_path(pilot_root, item.candidate_id, judge), terminal)


def _compact(
    *,
    repo_root: Path,
    config_path: Path,
    config: MatchedPilotJudgeConfig,
    loaded: LoadedJudges,
    selected: Sequence[SelectedCandidate],
    run_id: str,
    pilot_root: Path,
) -> dict[str, Any]:
    votes_by_candidate: list[tuple[JudgeVote, JudgeVote, JudgeVote]] = []
    all_votes: list[JudgeVote] = []
    for item in selected:
        current: list[JudgeVote] = []
        for judge in JudgeId:
            terminal = read_model(
                _terminal_path(pilot_root, item.candidate_id, judge), VoteTerminal
            )
            vote = read_model(Path(terminal.vote_path), JudgeVote)
            _validate_cached_vote(vote, item=item, judge=judge, input_sha256=terminal.input_sha256)
            if hash_file(Path(terminal.vote_path)) != terminal.vote_sha256:
                raise MatchedPilotJudgeError("vote terminal artifact hash drifted")
            current.append(vote)
        triple = cast(tuple[JudgeVote, JudgeVote, JudgeVote], tuple(current))
        votes_by_candidate.append(triple)
        all_votes.extend(triple)
    outcomes = [
        majority_outcome(item.candidate_id, votes)
        for item, votes in zip(selected, votes_by_candidate, strict=True)
    ]
    unknown = [outcome for outcome in outcomes if outcome.decision == MajorityDecision.UNKNOWN]
    labeled: list[object] = [
        {
            "reference": item.reference_goal_v1,
            "candidate": item.candidate_goal_v1,
            "label": outcome.label,
        }
        for item, outcome in zip(selected, outcomes, strict=True)
        if outcome.label is not None
    ]
    unknown_rows: list[object] = [
        {
            "source_id": item.source_id,
            "candidate_id": item.candidate_id,
            "outcome_id": outcome.outcome_id,
            "vote_ids": outcome.vote_ids,
        }
        for item, outcome in zip(selected, outcomes, strict=True)
        if outcome.label is None
    ]
    vote_output = pilot_root / "votes.jsonl"
    outcome_output = pilot_root / "majority_outcomes.jsonl"
    core_output = pilot_root / "core.jsonl"
    unknown_output = pilot_root / "unknown.jsonl"
    write_jsonl(vote_output, [item.model_dump(mode="json") for item in all_votes])
    write_jsonl(outcome_output, [item.model_dump(mode="json") for item in outcomes])
    write_jsonl(core_output, labeled)
    write_jsonl(unknown_output, unknown_rows)
    unknown_fraction = len(unknown) / len(outcomes)
    agreement = pairwise_agreement(votes_by_candidate)
    gate_checks = {
        "maximum_unknown_fraction": unknown_fraction <= config.thresholds.maximum_unknown_fraction,
        "minimum_pairwise_agreement": agreement >= config.thresholds.minimum_pairwise_agreement,
        "exact_vote_coverage": len(all_votes) == len(selected) * len(JudgeId),
        "majority_routing_complete": len(labeled) + len(unknown_rows) == len(selected),
    }
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "run_id": run_id,
        "config_sha256": hash_file(config_path),
        "module_sha256": config.module_sha256,
        "audit_run_id": config.audit_run_id,
        "audit_manifest_sha256": config.audit_manifest_sha256,
        "judges_config_sha256": config.judges_config_sha256,
        "labeling_policy_sha256": config.labeling_policy_sha256,
        "selected_candidate_ids": [item.candidate_id for item in selected],
        "counts": {
            "selected_candidates": len(selected),
            "votes": len(all_votes),
            "labeled": len(labeled),
            "unknown": len(unknown_rows),
            "equivalent": sum(item.decision == MajorityDecision.EQUIVALENT for item in outcomes),
            "non_equivalent": sum(
                item.decision == MajorityDecision.NON_EQUIVALENT for item in outcomes
            ),
        },
        "vote_decision_histogram": dict(
            sorted(Counter(item.decision.value for item in all_votes).items())
        ),
        "metrics": {"unknown_fraction": unknown_fraction, "pairwise_agreement": agreement},
        "gate_checks": gate_checks,
        "gate_passed": all(gate_checks.values()),
        "artifacts": {
            "selected_candidates.jsonl": hash_file(pilot_root / "selected_candidates.jsonl"),
            "votes.jsonl": hash_file(vote_output),
            "majority_outcomes.jsonl": hash_file(outcome_output),
            "core.jsonl": hash_file(core_output),
            "unknown.jsonl": hash_file(unknown_output),
            "vote_terminal_ledger_sha256": hash_canonical(
                {
                    path.name: hash_file(path)
                    for path in sorted((pilot_root / "vote_terminals").glob("*.json"))
                }
            ),
        },
    }
    write_json(pilot_root / "manifest.json", manifest)
    return manifest


def run_pilot(*, repo_root: Path, config_path: Path, limit: int | None = None) -> dict[str, Any]:
    config, loaded, selected, run_id, pilot_root = prepare(
        repo_root=repo_root, config_path=config_path, limit=limit
    )
    jobs = [(item, judge) for item in selected for judge in JudgeId]
    missing = 0
    for item, judge in jobs:
        provider = loaded.provider(judge)
        input_sha = judge_input_hash(
            nl_statement=item.nl_statement,
            reference=item.reference_goal_v1,
            candidate=item.candidate_goal_v1,
        )
        cache_key = vote_cache_key(
            loaded, provider, candidate_id=item.candidate_id, input_sha256=input_sha
        )
        _, vote_path = _vote_paths(config.output_parent, cache_key)
        missing += not vote_path.is_file()
    if missing > config.maximum_provider_calls:
        raise MatchedPilotJudgeError("missing cells exceed the provider-call ceiling")

    calls = 0
    cache_hits = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.maximum_concurrency) as executor:
        futures = {
            executor.submit(
                _call_or_load_vote,
                config=config,
                loaded=loaded,
                item=item,
                judge=judge,
            ): (item, judge)
            for item, judge in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            item, judge = futures[future]
            vote, cache_key, called = future.result()
            calls += called
            cache_hits += not called
            _write_vote_terminal(
                config=config,
                loaded=loaded,
                item=item,
                judge=judge,
                vote=vote,
                cache_key=cache_key,
                run_id=run_id,
                pilot_root=pilot_root,
            )
    manifest = _compact(
        repo_root=repo_root,
        config_path=config_path,
        config=config,
        loaded=loaded,
        selected=selected,
        run_id=run_id,
        pilot_root=pilot_root,
    )
    return {
        "run_id": run_id,
        "run_root": str(pilot_root),
        "provider_calls_this_run": calls,
        "vote_cache_hits_this_run": cache_hits,
        "manifest": manifest,
    }


def verify_pilot(*, repo_root: Path, config_path: Path, limit: int | None = None) -> dict[str, Any]:
    result = run_pilot(repo_root=repo_root, config_path=config_path, limit=limit)
    if result["provider_calls_this_run"] != 0:
        raise MatchedPilotJudgeError("judge-pilot replay made a new provider call")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("command", choices=("preflight", "run", "verify"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    if args.command == "preflight":
        config, _, selected, run_id, root = prepare(
            repo_root=repo_root, config_path=config_path, limit=args.limit
        )
        result: dict[str, Any] = {
            "run_id": run_id,
            "run_root": str(root),
            "selected_candidates": len(selected),
            "maximum_provider_calls": config.maximum_provider_calls,
            "provider_calls_started": False,
        }
    elif args.command == "run":
        result = run_pilot(repo_root=repo_root, config_path=config_path, limit=args.limit)
    else:
        result = verify_pilot(repo_root=repo_root, config_path=config_path, limit=args.limit)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
