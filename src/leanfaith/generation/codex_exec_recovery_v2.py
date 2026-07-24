"""Offline recovery for the v1 Codex usage-field drift.

The single live v1 call returned a valid response but codex-cli 0.144.1 added
``reasoning_output_tokens`` to the usage object.  V1 correctly failed closed.
This module never calls a provider and never changes v1 artifacts; it produces
one hash-bound operational recovery record from their exact bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation import codex_exec_provider_v1 as v1

_HEX64 = r"^[0-9a-f]{64}$"


class CodexExecRecoveryV2Error(RuntimeError):
    """Immutable v1 bytes cannot be recovered under the narrow v2 rule."""


class CodexUsageV2(StrictModel):
    input_tokens: int = Field(ge=0, strict=True)
    cached_input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    reasoning_output_tokens: int = Field(ge=0, strict=True)


class CodexExecRecoveryV2(StrictModel):
    schema_version: Literal[2] = 2
    recovery_id: str = Field(pattern=r"^codex_exec_recovery_v2:[0-9a-f]{64}$")
    recovery_kind: Literal["offline_usage_shape_recovery"]
    source_request_id: str
    source_attempt_id: str
    source_terminal_id: str
    source_terminal_status: Literal["usage_missing_or_invalid"]
    source_terminal_sha256: str = Field(pattern=_HEX64)
    source_stdout_sha256: str = Field(pattern=_HEX64)
    source_stderr_sha256: str = Field(pattern=_HEX64)
    source_final_message_sha256: str = Field(pattern=_HEX64)
    recovery_module_artifact: str
    recovery_module_sha256: str = Field(pattern=_HEX64)
    recovered_status: Literal["operationally_parsed"]
    parsed_output: dict[str, object]
    parsed_output_hash: str = Field(pattern=_HEX64)
    usage: CodexUsageV2
    event_count: Literal[4] = 4
    provider_calls_performed: Literal[0] = 0
    reference_transmission_performed: Literal[False] = False
    private_source_transmission_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_faithfulness_assessed: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False
    heldout_or_unseen_claimed: Literal[False] = False
    contamination_status: Literal["unknown_no_public_training_cutoff_or_immutable_revision"]

    def id_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if key != "recovery_id"
        }

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.parsed_output_hash != hash_canonical(self.parsed_output):
            raise ValueError("parsed output hash differs")
        expected = "codex_exec_recovery_v2:" + hash_canonical(
            {"schema": "codex_exec_recovery_v2", **self.id_payload()}
        )
        if self.recovery_id != expected:
            raise ValueError("recovery ID differs")
        return self


def _load_object(path: Path) -> dict[str, object]:
    value = v1._json_load_strict(path.read_bytes(), what=str(path))
    if not isinstance(value, dict):
        raise CodexExecRecoveryV2Error(f"{path} is not a JSON object")
    return value


def recover_codex_exec_v2(
    *,
    attempt_directory: Path,
    recovery_module_path: Path,
    output_path: Path,
) -> tuple[CodexExecRecoveryV2, str]:
    """Recover exactly one immutable v1 attempt without provider I/O."""

    attempt = attempt_directory.resolve()
    terminal_path = attempt / "terminal.json"
    stdout_path = attempt / "stdout.jsonl"
    stderr_path = attempt / "stderr.txt"
    final_path = attempt / "final_message.json"
    for path in (terminal_path, stdout_path, stderr_path, final_path, recovery_module_path):
        if path.is_symlink() or not path.is_file():
            raise CodexExecRecoveryV2Error(f"unsafe or missing artifact: {path}")

    terminal = v1.CodexExecTerminalV1.model_validate(_load_object(terminal_path))
    if terminal.status != "usage_missing_or_invalid":
        raise CodexExecRecoveryV2Error("recovery accepts only the observed v1 usage-shape failure")
    if hash_file(stdout_path) != terminal.stdout_sha256:
        raise CodexExecRecoveryV2Error("stdout hash differs from v1 terminal")
    if hash_file(stderr_path) != terminal.stderr_sha256 or stderr_path.read_bytes():
        raise CodexExecRecoveryV2Error("stderr differs or is nonempty")
    if (
        terminal.final_message_sha256 is None
        or hash_file(final_path) != terminal.final_message_sha256
    ):
        raise CodexExecRecoveryV2Error("final-message hash differs")

    lines = stdout_path.read_bytes().splitlines()
    if len(lines) != 4:
        raise CodexExecRecoveryV2Error("recovery requires the exact four-event success shape")
    events = [v1._json_load_strict(line, what="Codex JSONL event") for line in lines]
    if not all(isinstance(event, dict) for event in events):
        raise CodexExecRecoveryV2Error("event is not an object")
    typed = [event for event in events if isinstance(event, dict)]
    if [event.get("type") for event in typed] != [
        "thread.started",
        "turn.started",
        "item.completed",
        "turn.completed",
    ]:
        raise CodexExecRecoveryV2Error("event sequence differs from narrow recovery shape")
    item = typed[2].get("item")
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        raise CodexExecRecoveryV2Error("terminal item is not an agent message")
    text = item.get("text")
    if not isinstance(text, str) or text.encode("utf-8") != final_path.read_bytes():
        raise CodexExecRecoveryV2Error("agent message and fresh final file differ")
    usage = CodexUsageV2.model_validate(typed[3].get("usage"))
    output = _load_object(final_path)

    run_dir = attempt.parents[1]
    request = v1.CodexExecRequestV1.model_validate(_load_object(run_dir / "request.json"))
    schema_path = run_dir / "inputs" / "output_schema.json"
    if hash_file(schema_path) != request.output_schema_sha256:
        raise CodexExecRecoveryV2Error("bound output schema hash differs")
    schema = _load_object(schema_path)
    try:
        v1._validate_schema(output, schema)
    except ValueError as exc:
        raise CodexExecRecoveryV2Error(f"output schema violation: {exc}") from exc

    values: dict[str, object] = {
        "schema_version": 2,
        "recovery_kind": "offline_usage_shape_recovery",
        "source_request_id": terminal.request_id,
        "source_attempt_id": terminal.attempt_id,
        "source_terminal_id": terminal.terminal_id,
        "source_terminal_status": "usage_missing_or_invalid",
        "source_terminal_sha256": hash_file(terminal_path),
        "source_stdout_sha256": hash_file(stdout_path),
        "source_stderr_sha256": hash_file(stderr_path),
        "source_final_message_sha256": hash_file(final_path),
        "recovery_module_artifact": str(recovery_module_path),
        "recovery_module_sha256": hash_file(recovery_module_path),
        "recovered_status": "operationally_parsed",
        "parsed_output": output,
        "parsed_output_hash": hash_canonical(output),
        "usage": usage.model_dump(mode="json"),
        "event_count": 4,
        "provider_calls_performed": 0,
        "reference_transmission_performed": False,
        "private_source_transmission_performed": False,
        "semantic_labels_created": False,
        "semantic_faithfulness_assessed": False,
        "supervision_eligible": False,
        "gate_credit_claimed": False,
        "heldout_or_unseen_claimed": False,
        "contamination_status": "unknown_no_public_training_cutoff_or_immutable_revision",
    }
    recovery_id = "codex_exec_recovery_v2:" + hash_canonical(
        {"schema": "codex_exec_recovery_v2", **values}
    )
    record = CodexExecRecoveryV2.model_validate({"recovery_id": recovery_id, **values})
    digest = v1._immutable(
        output_path,
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n",
    )
    return record, digest
