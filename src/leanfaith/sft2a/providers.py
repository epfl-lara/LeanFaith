"""Strict, cached CLI provider boundary for SFT2A proposer and blinded judges."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.generation.capture_redaction import redact_captured_streams
from leanfaith.generation.claude_fable_judge_v1 import SubprocessClaudeCliExecutor
from leanfaith.generation.lf022_codex_proposer import SubprocessCodexProposerExecutor
from leanfaith.sft2a.config import LoadedSFT2AConfig, verify_provider_binary
from leanfaith.sft2a.models import ArtifactBinding, JudgeOutput, ProposerOutput, ProviderPin

_MAX_CAPTURE_BYTES = 16 * 1024 * 1024
_MAX_INFRASTRUCTURE_ATTEMPTS = 3
_SYSTEM_PROMPT = (
    "Follow the supplied LeanFaith task exactly. Do not use tools. Return only the JSON object "
    "required by the supplied schema."
)


class StructuredProviderError(RuntimeError):
    """A provider process, capture, schema, or immutable replay failed."""


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    call_key: str
    provider_id: str
    structured: dict[str, object]
    usage: dict[str, object]
    cost_usd: float | None
    elapsed_seconds: float
    cache_hit: bool
    terminal_path: Path


def _safe_env(source: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CODEX_HOME",
        "LEMEX_HOME",
    }
    return {key: value for key, value in source.items() if key in allowed}


def _atomic(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise StructuredProviderError(f"immutable provider artifact conflict: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _strict_object(raw: bytes, *, label: str) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise StructuredProviderError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise StructuredProviderError(f"{label} must be a JSON object")
    return value


def _codex_usage(stdout: bytes) -> dict[str, object]:
    usage: dict[str, object] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        candidate = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(candidate, dict):
            usage = candidate
    return usage


def _model_validate(
    structured: dict[str, object],
    *,
    response_kind: Literal["proposer", "judge"],
) -> dict[str, object]:
    model = ProposerOutput if response_kind == "proposer" else JudgeOutput
    try:
        return model.model_validate(structured).model_dump(mode="json")
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise StructuredProviderError(f"provider output schema violation: {details}") from exc


class CliStructuredProvider:
    """One frozen provider/model/schema with immutable content-addressed calls."""

    def __init__(
        self,
        loaded: LoadedSFT2AConfig,
        *,
        pin: ProviderPin,
        schema: ArtifactBinding,
        response_kind: Literal["proposer", "judge"],
    ) -> None:
        self.loaded = loaded
        self.pin = pin
        self.schema = schema
        self.response_kind = response_kind
        self.schema_path = loaded.repo_root / schema.path
        self.schema_document = _strict_object(
            self.schema_path.read_bytes(), label=f"{pin.provider_id} output schema"
        )
        self.output_root = Path(loaded.config.staging_root) / "provider_calls" / pin.provider_id

    def call(self, *, prompt: str, input_ids: Sequence[str]) -> ProviderCallResult:
        verify_provider_binary(self.pin)
        request_payload = {
            "version": "leanfaith_sft2a_provider_call_v1",
            "provider": self.pin.model_dump(mode="json"),
            "response_kind": self.response_kind,
            "prompt_sha256": sha256_hex(prompt.encode("utf-8")),
            "schema_path": self.schema.path,
            "schema_sha256": self.schema.sha256,
            "input_ids": list(input_ids),
            "private_source_content": False,
            "external_transmission_authorized": True,
        }
        call_key = hash_canonical(request_payload)
        call_dir = self.output_root / call_key[:2] / call_key
        terminal_path = call_dir / "terminal.json"
        if terminal_path.exists():
            terminal = _strict_object(terminal_path.read_bytes(), label="provider terminal")
            if terminal.get("call_key") != call_key or terminal.get("request") != request_payload:
                raise StructuredProviderError("cached provider terminal lineage differs")
            structured = terminal.get("structured")
            usage = terminal.get("usage")
            if not isinstance(structured, dict) or not isinstance(usage, dict):
                raise StructuredProviderError("cached provider terminal payload is malformed")
            structured = _model_validate(structured, response_kind=self.response_kind)
            cost = terminal.get("cost_usd")
            if cost is not None and not isinstance(cost, int | float):
                raise StructuredProviderError("cached provider cost is not numeric")
            elapsed_seconds = terminal.get("elapsed_seconds")
            if not isinstance(elapsed_seconds, int | float):
                raise StructuredProviderError("cached provider elapsed_seconds is not numeric")
            return ProviderCallResult(
                call_key=call_key,
                provider_id=self.pin.provider_id,
                structured=structured,
                usage=usage,
                cost_usd=None if cost is None else float(cost),
                elapsed_seconds=float(elapsed_seconds),
                cache_hit=True,
                terminal_path=terminal_path,
            )

        call_dir.mkdir(parents=True, exist_ok=True)
        _atomic(call_dir / "request.json", canonical_json_bytes(request_payload) + b"\n")
        _atomic(call_dir / "prompt.txt", prompt.encode("utf-8"))
        _atomic(call_dir / "output_schema.json", self.schema_path.read_bytes())

        attempts_root = call_dir / "attempts"
        attempts_root.mkdir(exist_ok=True)
        prior_attempts = sorted(path for path in attempts_root.iterdir() if path.is_dir())
        if any(
            not path.name.isdigit() or len(path.name) != 3 or not (path / "failure.json").is_file()
            for path in prior_attempts
        ):
            raise StructuredProviderError(
                f"provider call has an incomplete infrastructure attempt: {call_key}"
            )
        if len(prior_attempts) >= _MAX_INFRASTRUCTURE_ATTEMPTS:
            raise StructuredProviderError(
                f"provider call exhausted {_MAX_INFRASTRUCTURE_ATTEMPTS} infrastructure attempts"
            )
        attempt_number = len(prior_attempts) + 1
        attempt_dir = attempts_root / f"{attempt_number:03d}"
        attempt_dir.mkdir()
        _atomic(attempt_dir / "CLAIM", b"")
        workspace = attempt_dir / "workspace"
        workspace.mkdir()
        child_env = _safe_env(os.environ)
        process_failure: str | None = None
        elapsed = 0.0
        raw_streams: dict[str, bytes]
        transport_schema_sha256 = self.schema.sha256
        transport_schema_transform = "identity"

        if self.pin.cli in {"codex", "lemex"}:
            final_path = attempt_dir / "final_message.json"
            argv: tuple[str, ...] = (
                self.pin.binary_path,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
                "--disable",
                "shell_tool",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--json",
                "--model",
                self.pin.model,
                "-c",
                f'model_reasoning_effort="{self.pin.effort}"',
                "-c",
                "web_search=disabled",
                "-c",
                "shell_environment_policy.inherit=none",
                "--output-schema",
                str(self.schema_path),
                "-o",
                str(final_path),
                "-",
            )
            codex_capture = SubprocessCodexProposerExecutor().execute(
                argv=argv,
                prompt=prompt.encode("utf-8"),
                cwd=workspace,
                final_message_path=final_path,
                timeout_seconds=self.pin.timeout_seconds,
                termination_grace_seconds=self.pin.termination_grace_seconds,
            )
            raw_streams = {
                "stdout": codex_capture.stdout,
                "stderr": codex_capture.stderr,
                "final_message": codex_capture.final_message or b"",
            }
            if codex_capture.status != "completed" or codex_capture.exit_code != 0:
                process_failure = (
                    f"{self.pin.cli} process failed: {codex_capture.status}, "
                    f"exit={codex_capture.exit_code}"
                )
            elif codex_capture.final_message is None:
                process_failure = f"{self.pin.cli} did not create a final message"
            elapsed = (codex_capture.completed_at - codex_capture.started_at).total_seconds()
        else:
            claude_schema = dict(self.schema_document)
            metaschema_uri = claude_schema.pop("$schema", None)
            if metaschema_uri != "https://json-schema.org/draft/2020-12/schema":
                raise StructuredProviderError(
                    "frozen judge schema lacks the expected draft-2020-12 annotation"
                )
            schema_bytes = canonical_json_bytes(claude_schema)
            schema_text = schema_bytes.decode("utf-8")
            transport_schema_sha256 = sha256_hex(schema_bytes)
            transport_schema_transform = "omit_nonvalidating_draft_2020_12_annotation_for_claude"
            argv = (
                self.pin.binary_path,
                "--print",
                "--safe-mode",
                "--no-session-persistence",
                "--disable-slash-commands",
                "--no-chrome",
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
                "--model",
                self.pin.model,
                "--effort",
                self.pin.effort,
                "--system-prompt",
                _SYSTEM_PROMPT,
                "--json-schema",
                schema_text,
                "--output-format",
                "json",
            )
            claude_capture = SubprocessClaudeCliExecutor().execute(
                argv=argv,
                prompt=prompt.encode("utf-8"),
                cwd=workspace,
                env=child_env,
                timeout_seconds=self.pin.timeout_seconds,
                termination_grace_seconds=self.pin.termination_grace_seconds,
            )
            raw_streams = {"stdout": claude_capture.stdout, "stderr": claude_capture.stderr}
            if claude_capture.status != "completed" or claude_capture.exit_code != 0:
                process_failure = (
                    f"claude process failed: {claude_capture.status}, "
                    f"exit={claude_capture.exit_code}"
                )

        if any(len(value) > _MAX_CAPTURE_BYTES for value in raw_streams.values()):
            failure = {
                "version": "leanfaith_sft2a_provider_failure_v1",
                "call_key": call_key,
                "attempt_number": attempt_number,
                "detail": "provider capture exceeds the frozen 16 MiB limit",
                "capture_bytes": {name: len(value) for name, value in raw_streams.items()},
            }
            _atomic(attempt_dir / "failure.json", canonical_json_bytes(failure) + b"\n")
            raise StructuredProviderError(str(failure["detail"]))
        redacted = redact_captured_streams(raw_streams, environment=os.environ)
        for name, value in redacted.streams.items():
            suffix = "jsonl" if name == "stdout" and self.pin.cli != "claude" else "txt"
            _atomic(attempt_dir / f"{name}.{suffix}", value)
        _atomic(attempt_dir / "redaction.json", redacted.report_bytes)
        capture_hashes = {
            name: sha256_hex(value) for name, value in sorted(redacted.streams.items())
        }

        def fail(detail: str) -> NoReturn:
            failure = {
                "version": "leanfaith_sft2a_provider_failure_v1",
                "call_key": call_key,
                "attempt_number": attempt_number,
                "detail": detail,
                "transport_schema_sha256": transport_schema_sha256,
                "transport_schema_transform": transport_schema_transform,
                "redaction_report_sha256": redacted.report_sha256,
                "capture_hashes": capture_hashes,
                "semantic_judgment_created": False,
            }
            _atomic(attempt_dir / "failure.json", canonical_json_bytes(failure) + b"\n")
            raise StructuredProviderError(detail)

        if redacted.replacement_count:
            fail("provider capture required secret redaction; call rejected")
        if process_failure is not None:
            fail(process_failure)

        try:
            if self.pin.cli in {"codex", "lemex"}:
                final_message = raw_streams["final_message"]
                structured = _strict_object(final_message, label=f"{self.pin.cli} final message")
                usage = _codex_usage(raw_streams["stdout"])
                cost_usd = None
            else:
                wrapper = _strict_object(raw_streams["stdout"], label="Claude result wrapper")
                if (
                    wrapper.get("type") != "result"
                    or wrapper.get("subtype") != "success"
                    or wrapper.get("is_error") is not False
                ):
                    raise StructuredProviderError(
                        "Claude result wrapper is not a successful result"
                    )
                structured_value = wrapper.get("structured_output")
                if not isinstance(structured_value, dict):
                    raise StructuredProviderError("Claude result wrapper lacks structured_output")
                structured = structured_value
                usage_value = wrapper.get("usage")
                usage = usage_value if isinstance(usage_value, dict) else {}
                cost_value = wrapper.get("total_cost_usd")
                cost_usd = float(cost_value) if isinstance(cost_value, int | float) else None
                duration_ms = wrapper.get("duration_ms")
                elapsed = (
                    float(duration_ms) / 1000 if isinstance(duration_ms, int | float) else elapsed
                )
            structured = _model_validate(structured, response_kind=self.response_kind)
        except StructuredProviderError as exc:
            fail(str(exc))

        terminal = {
            "version": "leanfaith_sft2a_provider_terminal_v1",
            "call_key": call_key,
            "attempt_number": attempt_number,
            "request": request_payload,
            "transport_schema_sha256": transport_schema_sha256,
            "transport_schema_transform": transport_schema_transform,
            "structured": structured,
            "usage": usage,
            "cost_usd": cost_usd,
            "elapsed_seconds": elapsed,
            "redaction_report_sha256": redacted.report_sha256,
            "capture_hashes": capture_hashes,
        }
        _atomic(terminal_path, canonical_json_bytes(terminal) + b"\n")
        return ProviderCallResult(
            call_key=call_key,
            provider_id=self.pin.provider_id,
            structured=structured,
            usage=usage,
            cost_usd=cost_usd,
            elapsed_seconds=elapsed,
            cache_hit=False,
            terminal_path=terminal_path,
        )


def proposer_provider(loaded: LoadedSFT2AConfig) -> CliStructuredProvider:
    return CliStructuredProvider(
        loaded,
        pin=loaded.config.proposer,
        schema=loaded.config.schemas.codex_proposer_output,
        response_kind="proposer",
    )


def claude_judge_provider(loaded: LoadedSFT2AConfig) -> CliStructuredProvider:
    return CliStructuredProvider(
        loaded,
        pin=loaded.config.claude_judge,
        schema=loaded.config.schemas.blinded_judge_output,
        response_kind="judge",
    )


def lemex_audit_provider(loaded: LoadedSFT2AConfig) -> CliStructuredProvider:
    return CliStructuredProvider(
        loaded,
        pin=loaded.config.lemex_auditor,
        schema=loaded.config.schemas.blinded_judge_output,
        response_kind="judge",
    )


__all__ = [
    "CliStructuredProvider",
    "ProviderCallResult",
    "StructuredProviderError",
    "claude_judge_provider",
    "lemex_audit_provider",
    "proposer_provider",
]
