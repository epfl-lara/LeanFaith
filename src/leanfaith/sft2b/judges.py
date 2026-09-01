"""Frozen, blinded Codex/Lemex/Claude judge clients for SFT2B."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import Field

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.sft2b.schemas import (
    JudgeDecision,
    JudgeId,
    JudgeVote,
    NonEmpty,
    Sha256,
    stable_id,
)


class JudgeExecutionError(RuntimeError):
    """Raised when a frozen judge endpoint does not yield a strict vote."""


class ProviderConfig(StrictModel):
    judge: JudgeId
    provider: NonEmpty
    binary_path: NonEmpty
    binary_sha256: Sha256
    cli_version: NonEmpty
    model_id: NonEmpty
    effort: NonEmpty
    prompt_path: NonEmpty
    prompt_sha256: Sha256
    timeout_seconds: Annotated[int, Field(gt=0, le=1800)]


class JudgesConfig(StrictModel):
    schema_version: Literal["sft2b_judges_v1"]
    rubric_version: Literal["intended_claim_consistency_v1"]
    output_schema_path: NonEmpty
    output_schema_sha256: Sha256
    blinded_to_expected_label: Literal[True]
    blinded_to_other_votes: Literal[True]
    providers: tuple[ProviderConfig, ProviderConfig, ProviderConfig]


class JudgeResponse(StrictModel):
    decision: JudgeDecision
    probability_equivalent: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: Annotated[str, Field(min_length=1, max_length=800)]
    relation_class: Annotated[str, Field(min_length=1, max_length=120)]


@dataclass(frozen=True, slots=True)
class LoadedJudges:
    config: JudgesConfig
    repo_root: Path
    output_schema_path: Path
    output_schema_sha256: str

    def provider(self, judge: JudgeId) -> ProviderConfig:
        matches = [item for item in self.config.providers if item.judge == judge]
        if len(matches) != 1:
            raise JudgeExecutionError(f"judge config lacks exactly one {judge.value} provider")
        return matches[0]


@dataclass(frozen=True, slots=True)
class JudgeCallResult:
    vote: JudgeVote
    elapsed_seconds: float
    stdout: bytes
    stderr: bytes
    provider_payload: bytes


def _version(binary: Path) -> str:
    completed = subprocess.run(
        [str(binary), "--version"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise JudgeExecutionError(f"cannot verify {binary.name} version")
    return (completed.stdout or completed.stderr).decode("utf-8", errors="replace").strip()


def load_judges(repo_root: Path, config_path: Path) -> LoadedJudges:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    config = JudgesConfig.model_validate(raw)
    if {item.judge for item in config.providers} != set(JudgeId):
        raise JudgeExecutionError("judge config must contain Codex, Lemex, and Claude exactly once")
    schema_path = repo_root / config.output_schema_path
    if hash_file(schema_path) != config.output_schema_sha256:
        raise JudgeExecutionError("judge output schema hash mismatch")
    schema_value = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema_value, dict) or schema_value.get("additionalProperties") is not False:
        raise JudgeExecutionError("judge output schema must fail closed on extra fields")
    for provider in config.providers:
        binary = Path(provider.binary_path)
        if not binary.is_file() or binary.is_symlink():
            raise JudgeExecutionError(f"frozen judge binary is missing or symlinked: {binary}")
        if hash_file(binary) != provider.binary_sha256:
            raise JudgeExecutionError(f"{provider.judge.value} binary hash mismatch")
        if _version(binary) != provider.cli_version:
            raise JudgeExecutionError(f"{provider.judge.value} CLI version mismatch")
        prompt_path = repo_root / provider.prompt_path
        if hash_file(prompt_path) != provider.prompt_sha256:
            raise JudgeExecutionError(f"{provider.judge.value} prompt hash mismatch")
        prompt = prompt_path.read_text(encoding="utf-8")
        for marker in ("{{NL}}", "{{REFERENCE}}", "{{CANDIDATE}}"):
            if prompt.count(marker) != 1:
                raise JudgeExecutionError(
                    f"{provider.judge.value} prompt must contain {marker} exactly once"
                )
        forbidden = ("{{LABEL}}", "expected label:", "other judge output:", "majority:")
        if any(token in prompt.lower() for token in forbidden):
            raise JudgeExecutionError(f"{provider.judge.value} prompt leaks forbidden supervision")
    return LoadedJudges(
        config=config,
        repo_root=repo_root,
        output_schema_path=schema_path,
        output_schema_sha256=config.output_schema_sha256,
    )


def render_prompt(
    loaded: LoadedJudges,
    provider: ProviderConfig,
    *,
    nl_statement: str,
    reference: str,
    candidate: str,
) -> str:
    if not nl_statement.strip() or not reference.strip() or not candidate.strip():
        raise ValueError("judge inputs must be nonempty")
    template = (loaded.repo_root / provider.prompt_path).read_text(encoding="utf-8")
    return (
        template.replace("{{NL}}", nl_statement)
        .replace("{{REFERENCE}}", reference)
        .replace("{{CANDIDATE}}", candidate)
    )


def judge_input_hash(*, nl_statement: str, reference: str, candidate: str) -> str:
    return hash_canonical(
        {
            "schema_version": "sft2b_judge_input_v1",
            "nl_statement": nl_statement,
            "reference": reference,
            "candidate": candidate,
        }
    )


def vote_cache_key(
    loaded: LoadedJudges,
    provider: ProviderConfig,
    *,
    candidate_id: str,
    input_sha256: str,
) -> str:
    return hash_canonical(
        {
            "schema_version": "sft2b_vote_cache_key_v1",
            "candidate_id": candidate_id,
            "judge": provider.judge,
            "provider": provider.provider,
            "model_id": provider.model_id,
            "cli_version": provider.cli_version,
            "effort": provider.effort,
            "prompt_sha256": provider.prompt_sha256,
            "output_schema_sha256": loaded.output_schema_sha256,
            "judge_input_sha256": input_sha256,
        }
    )


def _command(
    provider: ProviderConfig,
    *,
    schema_path: Path,
    output_path: Path,
    prompt: str,
) -> list[str]:
    binary = provider.binary_path
    if provider.judge in {JudgeId.CODEX, JudgeId.LEMEX}:
        return [
            binary,
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            provider.model_id,
            "--config",
            f'model_reasoning_effort="{provider.effort}"',
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            prompt,
        ]
    return [
        binary,
        "--print",
        "--no-session-persistence",
        "--safe-mode",
        "--model",
        provider.model_id,
        "--effort",
        provider.effort,
        "--tools",
        "",
        "--output-format",
        "json",
        "--json-schema",
        schema_path.read_text(encoding="utf-8"),
        prompt,
    ]


def _parse_payload(judge: JudgeId, stdout: bytes, output_path: Path) -> tuple[JudgeResponse, bytes]:
    if judge in {JudgeId.CODEX, JudgeId.LEMEX}:
        payload_bytes = output_path.read_bytes()
        value = json.loads(payload_bytes)
    else:
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict):
            raise JudgeExecutionError("Claude response envelope must be an object")
        value = envelope.get("structured_output")
        if value is None and isinstance(envelope.get("result"), str):
            value = json.loads(cast(str, envelope["result"]))
        payload_bytes = canonical_json_bytes(value)
    try:
        response = JudgeResponse.model_validate(value)
    except Exception as exc:
        raise JudgeExecutionError(f"invalid {judge.value} structured vote: {exc}") from exc
    return response, payload_bytes


def run_judge(
    loaded: LoadedJudges,
    *,
    judge: JudgeId,
    candidate_id: str,
    nl_statement: str,
    reference: str,
    candidate: str,
    working_dir: Path,
) -> JudgeCallResult:
    """Invoke one blinded provider and return a strict vote plus raw captures."""

    provider = loaded.provider(judge)
    prompt = render_prompt(
        loaded,
        provider,
        nl_statement=nl_statement,
        reference=reference,
        candidate=candidate,
    )
    input_hash = judge_input_hash(
        nl_statement=nl_statement, reference=reference, candidate=candidate
    )
    working_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"sft2b-{judge.value}-", dir=working_dir) as temporary:
        output_path = Path(temporary) / "last_message.json"
        command = _command(
            provider,
            schema_path=loaded.output_schema_path,
            output_path=output_path,
            prompt=prompt,
        )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=temporary,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=provider.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise JudgeExecutionError(f"{judge.value} timed out") from exc
        elapsed = time.monotonic() - started
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-2000:]
            raise JudgeExecutionError(f"{judge.value} exited with {completed.returncode}: {detail}")
        response, payload_bytes = _parse_payload(judge, completed.stdout, output_path)
    response_hash = sha256_hex(canonical_json_bytes(response.model_dump(mode="json")))
    vote_id = stable_id(
        "sft2b_vote",
        {
            "candidate_id": candidate_id,
            "judge": judge,
            "model_id": provider.model_id,
            "prompt_sha256": provider.prompt_sha256,
            "judge_input_sha256": input_hash,
        },
    )
    vote = JudgeVote(
        vote_id=vote_id,
        candidate_id=candidate_id,
        judge=judge,
        provider=provider.provider,
        model_id=provider.model_id,
        cli_version=provider.cli_version,
        prompt_sha256=provider.prompt_sha256,
        judge_input_sha256=input_hash,
        response_sha256=response_hash,
        decision=response.decision,
        probability_equivalent=response.probability_equivalent,
        rationale=response.rationale,
        relation_class=response.relation_class,
        saw_expected_label=False,
        saw_other_votes=False,
    )
    return JudgeCallResult(
        vote=vote,
        elapsed_seconds=elapsed,
        stdout=completed.stdout,
        stderr=completed.stderr,
        provider_payload=payload_bytes,
    )
