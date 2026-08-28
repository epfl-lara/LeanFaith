"""Plain-manifest batch pipeline for Track D-2 autoformalizer candidates."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from leanfaith.collect2.invoke import (
    AutoformalizationTask,
    InvocationError,
    InvocationResult,
    InvocationSession,
    ProviderSpec,
    RenderedAutoformalizationTask,
    parse_cli_json_tail,
    render_task,
    resolve_local_profile,
)
from leanfaith.collect2.postprocess import (
    CandidateRejected,
    GoldenBlocklist,
    postprocess_candidate,
)
from leanfaith.representations.views import signature_near_dup_hash

DEFAULT_BLOCKLIST = Path("data/benchmarks/golden_blocklist_v1.json")
InvokeOne = Callable[[RenderedAutoformalizationTask, ProviderSpec], InvocationResult]


@dataclass(frozen=True, slots=True)
class BatchTask:
    problem_id: str
    nl_statement: str
    header: str
    reference_headless: str | None = None

    def __post_init__(self) -> None:
        if not self.problem_id.strip() or not self.nl_statement.strip():
            raise ValueError("problem_id and nl_statement must be nonempty")
        if any("\x00" in value for value in (self.problem_id, self.nl_statement, self.header)):
            raise ValueError("batch task text contains a NUL byte")
        if self.reference_headless is not None and "\x00" in self.reference_headless:
            raise ValueError("reference_headless contains a NUL byte")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BatchTask:
        problem_id = value.get("problem_id")
        nl_statement = value.get("nl_statement")
        header = value.get("header")
        reference = value.get("reference_headless")
        if not isinstance(problem_id, str) or not isinstance(nl_statement, str):
            raise ValueError("batch mappings require string problem_id and nl_statement")
        if not isinstance(header, str):
            raise ValueError("batch mappings require a string header")
        if reference is not None and not isinstance(reference, str):
            raise ValueError("reference_headless must be a string or null")
        return cls(
            problem_id=problem_id,
            nl_statement=nl_statement,
            header=header,
            reference_headless=reference,
        )

    def invocation_task(self) -> AutoformalizationTask:
        return AutoformalizationTask.named_from_problem(
            problem_id=self.problem_id,
            nl_statement=self.nl_statement,
            header=self.header,
        )


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """The intentionally small ``records.jsonl`` contract."""

    problem_id: str
    provider: str
    model: str
    candidate_lean: str
    candidate_headless: str
    generator_prompt_sha256: str
    raw_output_path: str
    blocklist_screened: Literal[True] = True

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RejectionRecord:
    problem_id: str
    provider: str
    model: str
    stage: str
    reason: str
    generator_prompt_sha256: str
    raw_output_path: str | None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BatchRunResult:
    records_path: Path
    manifest_path: Path
    rejections_path: Path
    accepted: int
    rejected: int
    resumed: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_revision(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _model_label(provider: ProviderSpec) -> str:
    if provider.kind == "local_hf":
        return resolve_local_profile(provider.model).repo_id
    return provider.model


def _json_line(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _load_existing_records(
    path: Path,
    *,
    provider: str,
    model: str,
) -> tuple[set[str], set[str]]:
    completed: set[str] = set()
    seen_hashes: set[str] = set()
    if not path.exists():
        return completed, seen_hashes
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid existing records JSON at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"existing record {line_number} is not an object")
            row = cast(dict[str, object], value)
            if row.get("provider") != provider or row.get("model") != model:
                continue
            problem_id = row.get("problem_id")
            headless = row.get("candidate_headless")
            if not isinstance(problem_id, str) or not isinstance(headless, str):
                raise ValueError(f"existing record {line_number} has an invalid schema")
            completed.add(problem_id)
            seen_hashes.add(signature_near_dup_hash(headless))
    return completed, seen_hashes


def _safe_filename(problem_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", problem_id).strip("_.")[:60] or "problem"


def _task_input_sha256(tasks: Sequence[BatchTask]) -> str:
    payload = json.dumps(
        [asdict(task) for task in tasks],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _decoding_manifest(provider: ProviderSpec) -> dict[str, object] | None:
    if provider.kind != "local_hf":
        return None
    profile = resolve_local_profile(provider.model)
    return asdict(provider.decoding or profile.decoding)


def _raw_result_from_resume(
    raw_path: Path,
    *,
    rendered: RenderedAutoformalizationTask,
    provider: ProviderSpec,
) -> InvocationResult:
    raw = raw_path.read_text(encoding="utf-8")
    candidate = raw if provider.kind == "local_hf" else parse_cli_json_tail(raw)
    return InvocationResult(
        provider=provider.provider_label,
        model=_model_label(provider),
        prompt=rendered.prompt,
        raw_output=raw,
        candidate_output=candidate,
    )


def _execute_batch(
    *,
    tasks: Sequence[BatchTask],
    provider: ProviderSpec,
    output_dir: Path,
    blocklist: GoldenBlocklist,
    run_one: InvokeOne,
    completed_problem_ids: set[str],
    seen_hashes: set[str],
) -> tuple[list[CandidateRecord], list[RejectionRecord], int]:
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    accepted: list[CandidateRecord] = []
    rejected: list[RejectionRecord] = []
    resumed = 0
    model = _model_label(provider)
    for index, batch_task in enumerate(tasks):
        if batch_task.problem_id in completed_problem_ids:
            resumed += 1
            continue
        try:
            rendered = render_task(batch_task.invocation_task(), provider)
        except ValueError as exc:
            # A data-shaped rendering failure (e.g. a template guard tripping
            # on statement content) rejects one task, never the whole batch.
            rejected.append(
                RejectionRecord(
                    problem_id=batch_task.problem_id,
                    provider=provider.provider_label,
                    model=model,
                    stage="render",
                    reason=str(exc)[:500],
                    generator_prompt_sha256="",
                    raw_output_path=None,
                )
            )
            continue
        raw_relative = Path("raw") / (
            f"{index:05d}-{_safe_filename(batch_task.problem_id)}-{rendered.prompt_sha256[:12]}.txt"
        )
        raw_path = output_dir / raw_relative
        try:
            result = (
                _raw_result_from_resume(raw_path, rendered=rendered, provider=provider)
                if raw_path.exists()
                else run_one(rendered, provider)
            )
            if not raw_path.exists():
                raw_path.write_text(result.raw_output, encoding="utf-8")
        except (InvocationError, UnicodeDecodeError, OSError) as exc:
            rejected.append(
                RejectionRecord(
                    problem_id=batch_task.problem_id,
                    provider=provider.provider_label,
                    model=model,
                    stage="invoke",
                    reason=str(exc),
                    generator_prompt_sha256=rendered.prompt_sha256,
                    raw_output_path=str(raw_relative) if raw_path.exists() else None,
                )
            )
            continue
        try:
            processed = postprocess_candidate(
                result.candidate_output,
                problem_id=batch_task.problem_id,
                registered_header=batch_task.header,
                blocklist=blocklist,
                family=rendered.family,
                expected_declaration_name=rendered.task.theorem_name,
                seen_hashes=seen_hashes,
            )
        except CandidateRejected as exc:
            rejected.append(
                RejectionRecord(
                    problem_id=batch_task.problem_id,
                    provider=result.provider,
                    model=result.model,
                    stage="postprocess",
                    reason=str(exc),
                    generator_prompt_sha256=result.prompt_sha256,
                    raw_output_path=str(raw_relative),
                )
            )
            continue
        accepted.append(
            CandidateRecord(
                problem_id=batch_task.problem_id,
                provider=result.provider,
                model=result.model,
                candidate_lean=processed.candidate_lean,
                candidate_headless=processed.candidate_headless,
                generator_prompt_sha256=result.prompt_sha256,
                raw_output_path=str(raw_relative),
            )
        )
    return accepted, rejected, resumed


def run_batch(
    tasks: Sequence[BatchTask | Mapping[str, object]],
    *,
    provider: ProviderSpec,
    output_dir: Path,
    blocklist_path: Path = DEFAULT_BLOCKLIST,
    repo_root: Path | None = None,
    invoke_one: InvokeOne | None = None,
) -> BatchRunResult:
    """Run or resume one provider batch and write JSONL plus one plain manifest."""

    resolved_repo_root = Path.cwd() if repo_root is None else repo_root
    normalized = [
        task if isinstance(task, BatchTask) else BatchTask.from_mapping(task) for task in tasks
    ]
    problem_ids = [task.problem_id for task in normalized]
    if len(problem_ids) != len(set(problem_ids)):
        raise ValueError("a collect2 batch requires unique problem_id values")
    output_dir.mkdir(parents=True, exist_ok=True)
    records_path = output_dir / "records.jsonl"
    rejections_path = output_dir / "rejections.jsonl"
    manifest_path = output_dir / "run_manifest.json"
    blocklist = GoldenBlocklist.load(blocklist_path)
    model = _model_label(provider)
    completed, seen_hashes = _load_existing_records(
        records_path,
        provider=provider.provider_label,
        model=model,
    )

    if invoke_one is None:
        with InvocationSession(provider) as session:
            accepted, rejected, resumed = _execute_batch(
                tasks=normalized,
                provider=provider,
                output_dir=output_dir,
                blocklist=blocklist,
                run_one=lambda rendered, _provider: session.run(rendered),
                completed_problem_ids=completed,
                seen_hashes=seen_hashes,
            )
    else:
        accepted, rejected, resumed = _execute_batch(
            tasks=normalized,
            provider=provider,
            output_dir=output_dir,
            blocklist=blocklist,
            run_one=invoke_one,
            completed_problem_ids=completed,
            seen_hashes=seen_hashes,
        )

    with records_path.open("a", encoding="utf-8") as handle:
        for record in accepted:
            handle.write(_json_line(record.to_json()))
    with rejections_path.open("a", encoding="utf-8") as handle:
        for rejection in rejected:
            handle.write(_json_line(rejection.to_json()))

    rejection_counts = Counter(rejection.reason.split(":", 1)[0] for rejection in rejected)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "track": "D-2 autoformalizer collection",
        "regime": "plain run manifest; no attestation or gates",
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "git_rev": _git_revision(resolved_repo_root),
        "provider": {
            "kind": provider.kind,
            "provider": provider.provider_label,
            "model": model,
            "revision": provider.resolved_revision,
            "device": provider.device if provider.kind == "local_hf" else None,
            "cli": provider.cli,
            "timeout_seconds": provider.timeout_seconds,
            "reasoning_effort": (provider.reasoning_effort if provider.kind == "cli" else None),
            "decoding": _decoding_manifest(provider),
        },
        "input": {
            "count": len(normalized),
            "sha256": _task_input_sha256(normalized),
        },
        "golden_blocklist": {
            "path": str(blocklist_path),
            "sha256": _sha256_file(blocklist_path),
            "near_dup_hash_count": len(blocklist.near_dup_hashes),
            "group_key_count": len(blocklist.group_keys),
        },
        "output": {
            "records_path": str(records_path),
            "records_sha256": _sha256_file(records_path),
            "rejections_path": str(rejections_path),
            "rejections_sha256": _sha256_file(rejections_path),
            "raw_directory": str(output_dir / "raw"),
        },
        "counts": {
            "accepted_this_run": len(accepted),
            "rejected_this_run": len(rejected),
            "resumed_existing": resumed,
            "rejections_by_code": dict(sorted(rejection_counts.items())),
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return BatchRunResult(
        records_path=records_path,
        manifest_path=manifest_path,
        rejections_path=rejections_path,
        accepted=len(accepted),
        rejected=len(rejected),
        resumed=resumed,
    )


__all__ = [
    "DEFAULT_BLOCKLIST",
    "BatchRunResult",
    "BatchTask",
    "CandidateRecord",
    "RejectionRecord",
    "run_batch",
]
