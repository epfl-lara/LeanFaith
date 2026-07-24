"""Write-once accounting for local qualification launcher failures.

An invocation failure is deliberately *not* a provider request, LLM attempt,
semantic example, or model-training artifact.  It records failures in the
launcher before the normal local-qualification terminal/bundle exists, so an
allocated run directory can never be mistaken for an unaccounted model run.
"""

from __future__ import annotations

import datetime
import os
import re
import stat
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
_FAILURE_ID = r"^local_qualification_invocation_failure:[0-9a-f]{64}$"
_SECRET_ENV_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CREDENTIAL|AUTH)",
    flags=re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"\bhf_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(token|api[_-]?key|password|secret)=([^&\s]+)"),
)
_REDACTED = "[REDACTED]"
_MAX_EXCEPTION_MESSAGE = 8_192


class InvocationFailurePersistenceError(RuntimeError):
    """The write-once invocation-failure artifact could not be preserved."""


class LocalQualificationInvocationStage(StrEnum):
    """Launcher stages before or around normal qualification persistence."""

    PREFLIGHT = "preflight"
    CHECKPOINT_VERIFICATION = "checkpoint_verification"
    CODE_BUNDLE_FREEZE = "code_bundle_freeze"
    RUNTIME_INITIALIZATION = "runtime_initialization"
    QUALIFICATION_PRE_PROVIDER = "qualification_pre_provider"
    MODEL_EXECUTION = "model_execution"
    BACKEND_CLOSE = "backend_close"
    BUNDLE_PERSISTENCE = "bundle_persistence"
    REPLAY_VERIFICATION = "replay_verification"


class InvocationCheckpointBinding(StrictModel):
    """Hash-level binding to a successfully verified local checkpoint."""

    verification_hash: str = Field(pattern=_HEX64)
    model_repo_id: str = Field(min_length=1)
    model_revision: str = Field(pattern=_HEX40)
    snapshot_reference: str = Field(min_length=1)
    checkpoint_bytes: int = Field(ge=1, strict=True)


class InvocationCodeBundleBinding(StrictModel):
    """Hash-level binding to an executable source snapshot already frozen."""

    source_artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    code_tree_hash: str = Field(pattern=_HEX64)


class LocalQualificationInvocationFailure(StrictModel):
    """Canonical diagnostic record for a launcher exception.

    The hard-false fields are intentional.  Even when
    ``model_execution_started`` is true, this record itself is not a
    ProviderRequest, LLM attempt, semantic record, or scientific artifact.
    """

    schema_version: Literal[1] = 1
    failure_id: str = Field(pattern=_FAILURE_ID)
    record_kind: Literal["local_qualification_invocation_failure"] = (
        "local_qualification_invocation_failure"
    )
    artifact_class: Literal["diagnostic"] = "diagnostic"
    stage: LocalQualificationInvocationStage
    exception_type: str = Field(min_length=1, max_length=512)
    exception_message: str = Field(min_length=1, max_length=_MAX_EXCEPTION_MESSAGE)
    invoked_at: datetime.datetime
    failed_at: datetime.datetime
    qualification_config_id: str = Field(min_length=1)
    qualification_config_artifact: str = Field(min_length=1)
    qualification_config_file_sha256: str = Field(pattern=_HEX64)
    qualification_config_hash: str = Field(pattern=_HEX64)
    model_family: str = Field(min_length=1)
    model_repo_id: str = Field(min_length=1)
    model_revision: str = Field(pattern=_HEX40)
    provider_slot: str = Field(min_length=1)
    checkpoint_binding: InvocationCheckpointBinding | None = None
    code_bundle_binding: InvocationCodeBundleBinding | None = None
    model_execution_started: bool
    counts_as_provider_request: Literal[False] = False
    counts_as_llm_attempt: Literal[False] = False
    counts_as_semantic_or_model_attempt: Literal[False] = False
    qualifies_for_gate5g: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    semantic_pool_eligible: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    release_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    scientific_evaluation_eligible: Literal[False] = False
    scientific_table_eligible: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "failure_id"
        }

    @model_validator(mode="after")
    def _coherent_failure(self) -> Self:
        require_utc(self.invoked_at)
        require_utc(self.failed_at)
        if self.failed_at < self.invoked_at:
            raise ValueError("failed_at cannot precede invoked_at")
        expected = "local_qualification_invocation_failure:" + hash_canonical(
            {
                "schema": "lf021_local_qualification_invocation_failure_v1",
                **self.id_payload(),
            }
        )
        if self.failure_id != expected:
            raise ValueError("failure_id does not match invocation-failure payload")
        return self

    @classmethod
    def create(
        cls,
        *,
        stage: LocalQualificationInvocationStage,
        exception: BaseException,
        invoked_at: datetime.datetime,
        failed_at: datetime.datetime,
        qualification_config_id: str,
        qualification_config_artifact: str,
        qualification_config_file_sha256: str,
        qualification_config_hash: str,
        model_family: str,
        model_repo_id: str,
        model_revision: str,
        provider_slot: str,
        checkpoint_binding: InvocationCheckpointBinding | None,
        code_bundle_binding: InvocationCodeBundleBinding | None,
        model_execution_started: bool,
    ) -> Self:
        exception_type = f"{type(exception).__module__}.{type(exception).__qualname__}"
        message = redact_exception_message(str(exception))
        if not message:
            message = "(exception carried no message)"
        payload: dict[str, object] = {
            "schema_version": 1,
            "record_kind": "local_qualification_invocation_failure",
            "artifact_class": "diagnostic",
            "stage": stage.value,
            "exception_type": exception_type,
            "exception_message": message,
            "invoked_at": _utc_text(invoked_at),
            "failed_at": _utc_text(failed_at),
            "qualification_config_id": qualification_config_id,
            "qualification_config_artifact": qualification_config_artifact,
            "qualification_config_file_sha256": qualification_config_file_sha256,
            "qualification_config_hash": qualification_config_hash,
            "model_family": model_family,
            "model_repo_id": model_repo_id,
            "model_revision": model_revision,
            "provider_slot": provider_slot,
            "checkpoint_binding": (
                None if checkpoint_binding is None else checkpoint_binding.model_dump(mode="json")
            ),
            "code_bundle_binding": (
                None if code_bundle_binding is None else code_bundle_binding.model_dump(mode="json")
            ),
            "model_execution_started": model_execution_started,
            "counts_as_provider_request": False,
            "counts_as_llm_attempt": False,
            "counts_as_semantic_or_model_attempt": False,
            "qualifies_for_gate5g": False,
            "semantic_labels_created": False,
            "semantic_pool_eligible": False,
            "supervision_eligible": False,
            "training_eligible": False,
            "release_eligible": False,
            "calibration_eligible": False,
            "model_selection_eligible": False,
            "scientific_evaluation_eligible": False,
            "scientific_table_eligible": False,
        }
        failure_id = "local_qualification_invocation_failure:" + hash_canonical(
            {
                "schema": "lf021_local_qualification_invocation_failure_v1",
                **payload,
            }
        )
        return cls.model_validate({"failure_id": failure_id, **payload})


def redact_exception_message(message: str) -> str:
    """Remove process secrets from a persisted exception message."""

    redacted = message
    secret_values = sorted(
        {
            value
            for name, value in os.environ.items()
            if value and len(value) >= 4 and _SECRET_ENV_NAME.search(name)
        },
        key=len,
        reverse=True,
    )
    for secret in secret_values:
        redacted = redacted.replace(secret, _REDACTED)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            redacted = pattern.sub(lambda match: f"{match.group(1)}={_REDACTED}", redacted)
        else:
            redacted = pattern.sub(_REDACTED, redacted)
    # Keep one bounded, textual field. NUL is never useful in a diagnostic JSON
    # record and can make downstream terminal tooling misleading.
    redacted = redacted.replace("\x00", "\\0")
    return redacted[:_MAX_EXCEPTION_MESSAGE]


def persist_invocation_failure(
    record: LocalQualificationInvocationFailure,
    *,
    run_directory: Path,
    artifact_root: Path,
) -> tuple[Path, str]:
    """Persist ``invocation_failure.json`` exactly once as canonical JSON."""

    root = Path(os.path.abspath(artifact_root))
    directory = Path(os.path.abspath(run_directory))
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise InvocationFailurePersistenceError(
            "invocation-failure run directory escapes artifact root"
        ) from exc
    current = root
    if current.is_symlink():
        raise InvocationFailurePersistenceError("artifact root must not be a symlink")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise InvocationFailurePersistenceError(
                f"invocation-failure path traverses a symlink: {current}"
            )
        with suppress(FileExistsError):
            current.mkdir()
        if not current.is_dir():
            raise InvocationFailurePersistenceError(
                f"invocation-failure path component is not a directory: {current}"
            )

    path = directory / "invocation_failure.json"
    payload = canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        try:
            mode = os.stat(path, follow_symlinks=False).st_mode
            existing = path.read_bytes()
        except OSError as exc:
            raise InvocationFailurePersistenceError(
                "cannot read existing invocation-failure record"
            ) from exc
        if not stat.S_ISREG(mode) or existing != payload:
            raise InvocationFailurePersistenceError(
                "immutable invocation-failure record conflict"
            ) from None
    else:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with suppress(OSError):
                path.unlink()
            raise
    return path, sha256_hex(payload)


def _utc_text(value: datetime.datetime) -> str:
    require_utc(value)
    return value.isoformat().replace("+00:00", "Z")
