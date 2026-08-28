"""Offline-first provider boundary for LF-021 real-output collection.

This module deliberately contains no network adapter.  It defines the strict
request/response protocol, a deterministic fixture writer, an immutable replay
reader, and a fail-closed placeholder for disabled external providers.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
    to_canonical,
)
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import (
    LLMAttemptStatus,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
)
from leanfaith.schemas.llm import (
    LLMAttemptRecord,
    LLMCallRecord,
    LLMExecutionMode,
    make_llm_attempt_id,
    make_llm_call_id,
)
from leanfaith.schemas.nl_lean import ProblemPoolRecord

_HEX64 = r"^[0-9a-f]{64}$"
_REQUEST_SCHEMA = "provider_request_v1"
_ATTEMPT_SCHEMA = "provider_attempt_v1"

DecodingScalar = str | int | float | bool | None
DecodingValue = DecodingScalar | tuple[DecodingScalar, ...]


class ProviderError(RuntimeError):
    """Base class for provider-boundary failures."""


class ProviderIdentityMismatchError(ProviderError):
    """A request was submitted to a differently pinned provider."""


class ProviderDisabledError(ProviderError):
    """An external provider slot is intentionally disabled."""


class PrivateContentTransmissionError(ProviderError):
    """Private-source content reached an external-provider path."""


class FixtureResponseMissingError(ProviderError):
    """No deterministic fixture was registered for a request."""


class ReplayArtifactError(ProviderError):
    """A persisted raw response is missing, malformed, or inconsistent."""


class RawResponseConflictError(ProviderError):
    """An immutable attempt path already contains different bytes."""


class ProviderIdentity(StrictModel):
    """Exact provider/model/revision pin plus its executable transport."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    transport: Literal["fixture", "replay", "local", "external_disabled"]

    @property
    def external(self) -> bool:
        return self.transport == "external_disabled"


def _request_hash_payload(
    *,
    identity: ProviderIdentity,
    prompt_template_hash: str,
    prompt_render_hash: str,
    decoding_hash: str,
    input_ids: tuple[str, ...],
    private_source_content: bool,
) -> dict[str, object]:
    return {
        "schema": _REQUEST_SCHEMA,
        "provider": identity.provider,
        "model": identity.model,
        "revision": identity.revision,
        "prompt_template_hash": prompt_template_hash,
        "prompt_render_hash": prompt_render_hash,
        "decoding_hash": decoding_hash,
        "input_ids": input_ids,
        "private_source_content": private_source_content,
    }


def _attempt_id(request_hash: str, attempt_index: int) -> str:
    return "provider-attempt:" + hash_canonical(
        {
            "schema": _ATTEMPT_SCHEMA,
            "request_hash": request_hash,
            "attempt_index": attempt_index,
        }
    )


class ProviderRequest(StrictModel):
    """One fully pinned prompt attempt.

    ``request_hash`` identifies semantic provider input and intentionally
    excludes retry position. ``attempt_id`` binds that input to an explicit
    zero-based attempt index.
    """

    schema_version: Literal[1] = 1
    request_hash: str = Field(pattern=_HEX64)
    attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    attempt_index: int = Field(ge=0)
    is_retry: bool
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    prompt_template_hash: str = Field(pattern=_HEX64)
    rendered_prompt: str
    prompt_render_hash: str = Field(pattern=_HEX64)
    decoding: dict[str, DecodingValue]
    decoding_hash: str = Field(pattern=_HEX64)
    input_ids: tuple[str, ...] = ()
    private_source_content: bool

    @classmethod
    def create(
        cls,
        *,
        identity: ProviderIdentity,
        prompt_template_hash: str,
        rendered_prompt: str,
        decoding: Mapping[str, DecodingValue],
        input_ids: tuple[str, ...] = (),
        private_source_content: bool = False,
        attempt_index: int = 0,
    ) -> Self:
        prompt_render_hash = sha256_hex(rendered_prompt.encode("utf-8"))
        decoding_dict = dict(decoding)
        decoding_hash = hash_canonical(decoding_dict)
        request_hash = hash_canonical(
            _request_hash_payload(
                identity=identity,
                prompt_template_hash=prompt_template_hash,
                prompt_render_hash=prompt_render_hash,
                decoding_hash=decoding_hash,
                input_ids=input_ids,
                private_source_content=private_source_content,
            )
        )
        return cls(
            request_hash=request_hash,
            attempt_id=_attempt_id(request_hash, attempt_index),
            attempt_index=attempt_index,
            is_retry=attempt_index > 0,
            provider=identity.provider,
            model=identity.model,
            revision=identity.revision,
            prompt_template_hash=prompt_template_hash,
            rendered_prompt=rendered_prompt,
            prompt_render_hash=prompt_render_hash,
            decoding=decoding_dict,
            decoding_hash=decoding_hash,
            input_ids=input_ids,
            private_source_content=private_source_content,
        )

    @model_validator(mode="after")
    def _hashes_match(self) -> ProviderRequest:
        if self.prompt_render_hash != sha256_hex(self.rendered_prompt.encode("utf-8")):
            raise ValueError("prompt_render_hash does not match rendered_prompt")
        if self.decoding_hash != hash_canonical(self.decoding):
            raise ValueError("decoding_hash does not match decoding parameters")
        identity = ProviderIdentity(
            provider=self.provider,
            model=self.model,
            revision=self.revision,
            transport="fixture",
        )
        expected_request_hash = hash_canonical(
            _request_hash_payload(
                identity=identity,
                prompt_template_hash=self.prompt_template_hash,
                prompt_render_hash=self.prompt_render_hash,
                decoding_hash=self.decoding_hash,
                input_ids=self.input_ids,
                private_source_content=self.private_source_content,
            )
        )
        if self.request_hash != expected_request_hash:
            raise ValueError("request_hash does not match provider/prompt/decoding binding")
        if self.attempt_id != _attempt_id(self.request_hash, self.attempt_index):
            raise ValueError("attempt_id does not match request_hash and attempt_index")
        if self.is_retry != (self.attempt_index > 0):
            raise ValueError("is_retry must equal attempt_index > 0")
        return self


class ProviderRawResponse(StrictModel):
    """Canonical raw response persisted before downstream parsing."""

    schema_version: Literal[1] = 1
    request_hash: str = Field(pattern=_HEX64)
    attempt_id: str = Field(pattern=r"^provider-attempt:[0-9a-f]{64}$")
    attempt_index: int = Field(ge=0)
    is_retry: bool
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    prompt_template_hash: str = Field(pattern=_HEX64)
    prompt_render_hash: str = Field(pattern=_HEX64)
    decoding_hash: str = Field(pattern=_HEX64)
    status: Literal["success", "error"]
    output_text: str | None = None
    output_hash: str | None = Field(default=None, pattern=_HEX64)
    error_type: str | None = None
    error_detail: str | None = None

    @classmethod
    def success(cls, request: ProviderRequest, output_text: str) -> ProviderRawResponse:
        return cls(
            request_hash=request.request_hash,
            attempt_id=request.attempt_id,
            attempt_index=request.attempt_index,
            is_retry=request.is_retry,
            provider=request.provider,
            model=request.model,
            revision=request.revision,
            prompt_template_hash=request.prompt_template_hash,
            prompt_render_hash=request.prompt_render_hash,
            decoding_hash=request.decoding_hash,
            status="success",
            output_text=output_text,
            output_hash=sha256_hex(output_text.encode("utf-8")),
        )

    @classmethod
    def error(
        cls,
        request: ProviderRequest,
        *,
        error_type: str,
        error_detail: str | None = None,
    ) -> ProviderRawResponse:
        """Create a terminal provider error bound to the attempted request."""

        return cls(
            request_hash=request.request_hash,
            attempt_id=request.attempt_id,
            attempt_index=request.attempt_index,
            is_retry=request.is_retry,
            provider=request.provider,
            model=request.model,
            revision=request.revision,
            prompt_template_hash=request.prompt_template_hash,
            prompt_render_hash=request.prompt_render_hash,
            decoding_hash=request.decoding_hash,
            status="error",
            error_type=error_type,
            error_detail=error_detail,
        )

    @model_validator(mode="after")
    def _status_payload_matches(self) -> ProviderRawResponse:
        if self.is_retry != (self.attempt_index > 0):
            raise ValueError("raw-response is_retry must equal attempt_index > 0")
        if self.attempt_id != _attempt_id(self.request_hash, self.attempt_index):
            raise ValueError("raw-response attempt_id is inconsistent")
        if self.status == "success":
            if self.output_text is None or self.output_hash is None:
                raise ValueError("successful raw response requires output text and hash")
            if self.output_hash != sha256_hex(self.output_text.encode("utf-8")):
                raise ValueError("output_hash does not match output_text")
            if self.error_type is not None or self.error_detail is not None:
                raise ValueError("successful raw response cannot carry an error")
        else:
            if self.output_text is not None or self.output_hash is not None:
                raise ValueError("error raw response cannot carry output")
            if not self.error_type:
                raise ValueError("error raw response requires error_type")
        return self


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Validated provider result plus its immutable raw artifact."""

    response: ProviderRawResponse
    raw_response_path: Path
    raw_response_sha256: str
    replayed: bool


@runtime_checkable
class GenerationProvider(Protocol):
    """Minimal provider protocol used by the LF-021 collection runtime."""

    identity: ProviderIdentity

    def generate(self, request: ProviderRequest) -> ProviderResult: ...


def _validate_identity(identity: ProviderIdentity, request: ProviderRequest) -> None:
    observed = (request.provider, request.model, request.revision)
    expected = (identity.provider, identity.model, identity.revision)
    if observed != expected:
        raise ProviderIdentityMismatchError(
            "request provider/model/revision does not match provider pin: "
            f"{observed!r} != {expected!r}"
        )


def _raw_response_path(
    root: Path,
    *,
    request_hash: str,
    attempt_id: str,
    attempt_index: int,
) -> Path:
    return (
        root
        / "v1"
        / request_hash[:2]
        / request_hash
        / f"{attempt_index:04d}-{attempt_id.removeprefix('provider-attempt:')}.json"
    )


def _canonical_raw_bytes(response: ProviderRawResponse) -> bytes:
    return canonical_json_bytes(response.model_dump(mode="json")) + b"\n"


def _canonical_request_bytes(request: ProviderRequest) -> bytes:
    return canonical_json_bytes(request.model_dump(mode="json")) + b"\n"


def _persist_immutable_bytes(path: Path, payload: bytes, *, artifact: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RawResponseConflictError(f"{artifact} path is not a regular file: {path}")
        if path.read_bytes() != payload:
            raise RawResponseConflictError(
                f"immutable {artifact} path contains different bytes: {path}"
            )
        return hash_file(path)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise RawResponseConflictError(
                    f"concurrent immutable {artifact} conflict at {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def persist_provider_request(request: ProviderRequest, path: Path) -> str:
    """Persist one canonical provider request without allowing replacement."""

    return _persist_immutable_bytes(
        path,
        _canonical_request_bytes(request),
        artifact="provider request",
    )


def _persist_immutable_response(
    root: Path,
    response: ProviderRawResponse,
) -> tuple[Path, str]:
    path = _raw_response_path(
        root,
        request_hash=response.request_hash,
        attempt_id=response.attempt_id,
        attempt_index=response.attempt_index,
    )
    payload = _canonical_raw_bytes(response)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RawResponseConflictError(f"raw response path is not a regular file: {path}")
        if path.read_bytes() != payload:
            raise RawResponseConflictError(
                f"immutable raw response path contains different bytes: {path}"
            )
        return path, hash_file(path)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{response.attempt_index:04d}.",
        suffix=".partial",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise RawResponseConflictError(
                    f"concurrent raw response conflict at {path}"
                ) from None
        return path, hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def persist_provider_raw_response(
    root: Path,
    response: ProviderRawResponse,
    *,
    replayed: bool = False,
) -> ProviderResult:
    """Persist one canonical response and return its hash-bound result.

    Local runtimes use this public boundary after generation and before any
    parsing.  Keeping persistence here ensures fixture, replay, and local
    responses share identical immutable bytes and replay validation.
    """

    path, digest = _persist_immutable_response(root, response)
    return ProviderResult(
        response=response,
        raw_response_path=path,
        raw_response_sha256=digest,
        replayed=replayed,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reject_nonfinite(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _load_raw_response(path: Path) -> ProviderRawResponse:
    if path.is_symlink() or not path.is_file():
        raise ReplayArtifactError(f"raw response is missing or not a regular file: {path}")
    try:
        raw = path.read_bytes()
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        response = ProviderRawResponse.model_validate(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReplayArtifactError(f"invalid raw response {path}: {exc}") from exc
    if raw != _canonical_raw_bytes(response):
        raise ReplayArtifactError(f"raw response is not canonical JSON: {path}")
    return response


def load_provider_request(path: Path) -> ProviderRequest:
    """Reload and canonical-byte validate one persisted provider request."""

    if path.is_symlink() or not path.is_file():
        raise ReplayArtifactError(f"provider request is missing or not a regular file: {path}")
    try:
        raw = path.read_bytes()
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        request = ProviderRequest.model_validate(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReplayArtifactError(f"invalid provider request {path}: {exc}") from exc
    if raw != _canonical_request_bytes(request):
        raise ReplayArtifactError(f"provider request is not canonical JSON: {path}")
    return request


def load_provider_raw_response(
    path: Path,
    *,
    request: ProviderRequest | None = None,
) -> ProviderRawResponse:
    """Reload one canonical raw response and optionally verify its request binding.

    This is the public, network-free replay primitive for higher-level batch
    orchestrators.  Keeping the canonical-byte and request-binding checks in
    the provider module prevents callers from implementing weaker ad-hoc JSON
    readers.
    """

    response = _load_raw_response(path)
    if request is not None:
        _validate_response_binding(response, request)
    return response


def provider_raw_response_path(root: Path, request: ProviderRequest) -> Path:
    """Return the canonical raw-first artifact path for one provider request."""

    return _raw_response_path(
        root,
        request_hash=request.request_hash,
        attempt_id=request.attempt_id,
        attempt_index=request.attempt_index,
    )


def _validate_response_binding(
    response: ProviderRawResponse,
    request: ProviderRequest,
) -> None:
    expected = (
        request.request_hash,
        request.attempt_id,
        request.attempt_index,
        request.is_retry,
        request.provider,
        request.model,
        request.revision,
        request.prompt_template_hash,
        request.prompt_render_hash,
        request.decoding_hash,
    )
    observed = (
        response.request_hash,
        response.attempt_id,
        response.attempt_index,
        response.is_retry,
        response.provider,
        response.model,
        response.revision,
        response.prompt_template_hash,
        response.prompt_render_hash,
        response.decoding_hash,
    )
    if observed != expected:
        raise ReplayArtifactError("raw response does not match the requested attempt binding")


@dataclass(frozen=True, slots=True)
class ProviderLLMLineage:
    """Canonical bridge from provider-protocol artifacts to schema-v2 lineage."""

    attempt: LLMAttemptRecord
    call: LLMCallRecord


def create_provider_request_for_problem(
    *,
    identity: ProviderIdentity,
    problem: ProblemPoolRecord,
    prompt_template_hash: str,
    rendered_prompt: str,
    decoding: Mapping[str, DecodingValue],
    attempt_index: int = 0,
) -> ProviderRequest:
    """Create a request from a policy-screened problem, before provider I/O.

    Callers must not copy a privacy flag into a provider request themselves:
    the authoritative value is the ``ProblemPoolRecord``.  External transports
    additionally require the problem to have been explicitly admitted for
    external transmission.  This check intentionally happens before a provider
    object can receive the request.
    """

    if problem.eligibility != "eligible":
        raise ProviderError("provider requests require an eligible problem-pool record")
    if not problem.denylist_checked or problem.denylist_hits:
        raise ProviderError("provider requests require a denylist-cleared problem")
    if identity.external and (
        problem.private_source_content or not problem.external_provider_eligible
    ):
        raise PrivateContentTransmissionError(
            "external provider request requires a public, external-provider-eligible problem"
        )
    return ProviderRequest.create(
        identity=identity,
        prompt_template_hash=prompt_template_hash,
        rendered_prompt=rendered_prompt,
        decoding=decoding,
        input_ids=(problem.problem_record_id,),
        private_source_content=problem.private_source_content,
        attempt_index=attempt_index,
    )


def _repository_artifact(path: Path, *, artifact_root: Path, field: str) -> str:
    try:
        return str(path.resolve().relative_to(artifact_root.resolve()))
    except ValueError as exc:
        raise ProviderError(f"{field} must stay inside artifact_root") from exc


def _resolve_repository_artifact(
    artifact: str,
    *,
    artifact_root: Path,
    field: str,
) -> Path:
    path = Path(artifact)
    if path.is_absolute() or ".." in path.parts or not artifact.strip():
        raise ProviderError(f"{field} must be a nonempty repository-relative path")
    resolved = (artifact_root / path).resolve()
    try:
        resolved.relative_to(artifact_root.resolve())
    except ValueError as exc:
        raise ProviderError(f"{field} escapes artifact_root") from exc
    return resolved


def verify_llm_call_artifacts(
    *,
    call: LLMCallRecord,
    problem: ProblemPoolRecord,
    artifact_root: Path,
) -> ProviderRawResponse:
    """Reload and verify the exact provider artifacts behind one completed call.

    This is the final trust boundary before Lean materialization.  It prevents
    a caller from pairing a valid ``LLMCallRecord`` with different in-memory
    text, a different problem, or tampered request/response bytes.
    """

    if (
        call.schema_version != 2
        or call.role is not LLMRole.AUTOFORMALIZER
        or call.terminal_status is not LLMCallStatus.COMPLETED
    ):
        raise ProviderError(
            "artifact verification requires a completed schema-v2 autoformalizer call"
        )
    required = {
        "request_artifact": call.request_artifact,
        "raw_output_artifact": call.raw_output_artifact,
        "provider_request_hash": call.provider_request_hash,
        "request_artifact_sha256": call.request_artifact_sha256,
        "raw_response_sha256": call.raw_response_sha256,
        "model_revision": call.model_revision,
    }
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise ProviderError("completed call lacks artifact bindings: " + ", ".join(missing))
    if (
        call.problem_record_id != problem.problem_record_id
        or call.problem_id != problem.problem_id
        or call.problem_group != problem.problem_group
    ):
        raise ProviderError("call and problem identity/provenance differ")
    if (
        call.private_source_content != problem.private_source_content
        or call.denylist_checked != problem.denylist_checked
        or call.denylist_hits != problem.denylist_hits
    ):
        raise ProviderError("call and problem privacy/denylist provenance differ")

    assert call.request_artifact is not None
    assert call.raw_output_artifact is not None
    request_path = _resolve_repository_artifact(
        call.request_artifact,
        artifact_root=artifact_root,
        field="request_artifact",
    )
    raw_path = _resolve_repository_artifact(
        call.raw_output_artifact,
        artifact_root=artifact_root,
        field="raw_output_artifact",
    )
    request = load_provider_request(request_path)
    response = _load_raw_response(raw_path)
    _validate_response_binding(response, request)
    if hash_file(request_path) != call.request_artifact_sha256:
        raise ReplayArtifactError("request artifact SHA-256 differs from LLMCallRecord")
    if hash_file(raw_path) != call.raw_response_sha256:
        raise ReplayArtifactError("raw response SHA-256 differs from LLMCallRecord")
    if request.request_hash != call.provider_request_hash:
        raise ReplayArtifactError("provider request hash differs from LLMCallRecord")
    expected_request_values = (
        call.provider,
        call.model,
        call.model_revision,
        call.prompt_template_hash,
        call.prompt_render_hash,
        call.decoding,
        call.input_ids,
        call.private_source_content,
    )
    observed_request_values = (
        request.provider,
        request.model,
        request.revision,
        request.prompt_template_hash,
        request.prompt_render_hash,
        request.decoding,
        request.input_ids,
        request.private_source_content,
    )
    if observed_request_values != expected_request_values:
        raise ReplayArtifactError("persisted provider request differs from call payload")
    if request.input_ids != (problem.problem_record_id,):
        raise ReplayArtifactError(
            "provider request must bind exactly one authoritative problem_record_id"
        )
    if response.status != "success" or not response.output_text:
        raise ReplayArtifactError("completed autoformalizer call lacks a nonempty response")
    return response


def bridge_provider_result_to_llm_lineage(
    *,
    request: ProviderRequest,
    result: ProviderResult,
    request_artifact_path: Path,
    artifact_root: Path,
    problem: ProblemPoolRecord,
    provider_slot: str,
    model_family: str,
    prompt_template_id: str,
    prompt_template_version: str,
    execution_mode: LLMExecutionMode,
    parse_status: ParseStatus,
    parsed_statement: str | None,
    started_at: datetime.datetime,
    completed_at: datetime.datetime,
    supervision_eligible: bool = False,
    heldout_generator: bool = False,
    metadata: Mapping[str, DecodingValue] | None = None,
) -> ProviderLLMLineage:
    """Build fully hash-bound schema-v2 records for one terminal attempt.

    The bridge reloads canonical request/raw bytes before creating lineage.
    It is intentionally single-attempt for the LF-021 offline smoke; retry
    orchestration must call a future multi-attempt bridge with the complete
    ordered attempt sequence.
    """

    if request.attempt_index != 0:
        raise ProviderError("single-attempt lineage bridge requires attempt_index=0")
    persisted_request = load_provider_request(request_artifact_path)
    if persisted_request != request:
        raise ReplayArtifactError("persisted provider request differs from in-memory request")
    request_artifact_sha256 = hash_file(request_artifact_path)

    persisted_response = _load_raw_response(result.raw_response_path)
    _validate_response_binding(persisted_response, request)
    if persisted_response != result.response:
        raise ReplayArtifactError("persisted raw response differs from ProviderResult")
    if hash_file(result.raw_response_path) != result.raw_response_sha256:
        raise ReplayArtifactError("ProviderResult raw_response_sha256 does not match artifact")

    if problem.private_source_content != request.private_source_content:
        raise PrivateContentTransmissionError(
            "problem.private_source_content must exactly match ProviderRequest"
        )
    if execution_mode == "external" and (
        problem.private_source_content or not problem.external_provider_eligible
    ):
        raise PrivateContentTransmissionError(
            "external execution requires a public, external-provider-eligible problem"
        )

    if result.response.status == "success":
        output_text = result.response.output_text or ""
        attempt_status = (
            LLMAttemptStatus.RESPONSE_RECEIVED if output_text else LLMAttemptStatus.EMPTY_RESPONSE
        )
        terminal_status = (
            LLMCallStatus.COMPLETED
            if attempt_status is LLMAttemptStatus.RESPONSE_RECEIVED
            else LLMCallStatus.EXHAUSTED
        )
        error_code = None
        error_detail = None
        retryable = attempt_status is LLMAttemptStatus.EMPTY_RESPONSE
        if parse_status is ParseStatus.PARSED and parsed_statement is None:
            raise ProviderError("parse_status=parsed requires parsed_statement")
        if parse_status is not ParseStatus.PARSED and parsed_statement is not None:
            raise ProviderError("non-parsed provider result cannot carry parsed_statement")
    else:
        attempt_status = LLMAttemptStatus.PROVIDER_ERROR
        terminal_status = LLMCallStatus.EXHAUSTED
        error_code = result.response.error_type or "provider_error"
        error_detail = result.response.error_detail
        retryable = False
        if parse_status is not ParseStatus.EMPTY or parsed_statement is not None:
            raise ProviderError("provider-error result requires empty parse state")

    request_artifact = _repository_artifact(
        request_artifact_path,
        artifact_root=artifact_root,
        field="request_artifact_path",
    )
    raw_response_artifact = _repository_artifact(
        result.raw_response_path,
        artifact_root=artifact_root,
        field="raw_response_path",
    )
    metadata_dict = dict(metadata or {})
    metadata_dict.update(
        {
            "provider_protocol": "provider_v1",
            "provider_request_hash": request.request_hash,
            "provider_attempt_id": request.attempt_id,
            "request_artifact_sha256": request_artifact_sha256,
            "raw_response_sha256": result.raw_response_sha256,
        }
    )
    call_id = make_llm_call_id(
        provider=request.provider,
        provider_slot=provider_slot,
        model=request.model,
        model_family=model_family,
        model_revision=request.revision,
        role=LLMRole.AUTOFORMALIZER,
        problem_record_id=problem.problem_record_id,
        prompt_template_hash=request.prompt_template_hash,
        prompt_render_hash=request.prompt_render_hash,
        input_ids=request.input_ids,
        decoding=request.decoding,
    )
    attempt_id = make_llm_attempt_id(call_id, request.attempt_index)
    latency_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
    attempt = LLMAttemptRecord(
        attempt_id=attempt_id,
        call_id=call_id,
        attempt_index=request.attempt_index,
        execution_mode=execution_mode,
        started_at=started_at,
        completed_at=completed_at,
        request_artifact=request_artifact,
        raw_response_artifact=raw_response_artifact,
        status=attempt_status,
        error_code=error_code,
        error_detail=error_detail,
        retryable=retryable,
        latency_ms=latency_ms,
        provider_request_hash=request.request_hash,
        provider_attempt_id=request.attempt_id,
        request_artifact_sha256=request_artifact_sha256,
        raw_response_sha256=result.raw_response_sha256,
        metadata=metadata_dict,
    )
    call = LLMCallRecord(
        schema_version=2,
        call_id=call_id,
        provider=request.provider,
        provider_slot=provider_slot,
        model=request.model,
        model_family=model_family,
        role=LLMRole.AUTOFORMALIZER,
        model_revision=request.revision,
        request_date=started_at,
        started_at=started_at,
        completed_at=completed_at,
        execution_mode=execution_mode,
        prompt_template_id=prompt_template_id,
        prompt_template_version=prompt_template_version,
        prompt_template_hash=request.prompt_template_hash,
        prompt_render_hash=request.prompt_render_hash,
        request_artifact=request_artifact,
        input_ids=request.input_ids,
        decoding=request.decoding,
        raw_output_artifact=raw_response_artifact,
        parsed_output=(
            {"lean_statement": parsed_statement}
            if parse_status is ParseStatus.PARSED and parsed_statement is not None
            else None
        ),
        parse_status=parse_status,
        retry_count=0,
        supervision_eligible=supervision_eligible,
        private_source_content=problem.private_source_content,
        denylist_checked=problem.denylist_checked,
        denylist_hits=problem.denylist_hits,
        problem_record_id=problem.problem_record_id,
        problem_id=problem.problem_id,
        problem_group=problem.problem_group,
        terminal_status=terminal_status,
        attempt_ids=(attempt_id,),
        latency_ms=latency_ms,
        heldout_generator=heldout_generator,
        provider_request_hash=request.request_hash,
        request_artifact_sha256=request_artifact_sha256,
        raw_response_sha256=result.raw_response_sha256,
        metadata=metadata_dict,
    )
    return ProviderLLMLineage(attempt=attempt, call=call)


def verify_generic_llm_call_artifacts(
    *,
    call: LLMCallRecord,
    expected_role: LLMRole,
    expected_input_ids: tuple[str, ...],
    private_source_content: bool,
    denylist_checked: bool,
    denylist_hits: tuple[str, ...],
    artifact_root: Path,
) -> ProviderRawResponse:
    """Verify a completed proposer/judge call against immutable artifacts.

    Autoformalizer calls retain the stronger ``ProblemPoolRecord``-bound
    verifier above.  This role-generic verifier exists for LF-022 theorem and
    pair tasks whose semantic input IDs are not problem-pool IDs.
    """

    if expected_role is LLMRole.AUTOFORMALIZER:
        raise ProviderError("autoformalizer calls require verify_llm_call_artifacts")
    if (
        call.schema_version != 2
        or call.role is not expected_role
        or call.terminal_status is not LLMCallStatus.COMPLETED
    ):
        raise ProviderError(
            "generic artifact verification requires a completed schema-v2 call "
            "with the expected non-autoformalizer role"
        )
    required = {
        "request_artifact": call.request_artifact,
        "raw_output_artifact": call.raw_output_artifact,
        "provider_request_hash": call.provider_request_hash,
        "request_artifact_sha256": call.request_artifact_sha256,
        "raw_response_sha256": call.raw_response_sha256,
        "model_revision": call.model_revision,
    }
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise ProviderError("completed call lacks artifact bindings: " + ", ".join(missing))
    if call.input_ids != expected_input_ids:
        raise ProviderError("call input IDs differ from the registered LF-022 task")
    if (
        call.private_source_content != private_source_content
        or call.denylist_checked != denylist_checked
        or call.denylist_hits != denylist_hits
    ):
        raise ProviderError("call privacy/denylist provenance differs")

    assert call.request_artifact is not None
    assert call.raw_output_artifact is not None
    request_path = _resolve_repository_artifact(
        call.request_artifact,
        artifact_root=artifact_root,
        field="request_artifact",
    )
    raw_path = _resolve_repository_artifact(
        call.raw_output_artifact,
        artifact_root=artifact_root,
        field="raw_output_artifact",
    )
    request = load_provider_request(request_path)
    response = _load_raw_response(raw_path)
    _validate_response_binding(response, request)
    if hash_file(request_path) != call.request_artifact_sha256:
        raise ReplayArtifactError("request artifact SHA-256 differs from LLMCallRecord")
    if hash_file(raw_path) != call.raw_response_sha256:
        raise ReplayArtifactError("raw response SHA-256 differs from LLMCallRecord")
    if request.request_hash != call.provider_request_hash:
        raise ReplayArtifactError("provider request hash differs from LLMCallRecord")
    expected_request_values = (
        call.provider,
        call.model,
        call.model_revision,
        call.prompt_template_hash,
        call.prompt_render_hash,
        call.decoding,
        call.input_ids,
        call.private_source_content,
    )
    observed_request_values = (
        request.provider,
        request.model,
        request.revision,
        request.prompt_template_hash,
        request.prompt_render_hash,
        request.decoding,
        request.input_ids,
        request.private_source_content,
    )
    if observed_request_values != expected_request_values:
        raise ReplayArtifactError("persisted provider request differs from call payload")
    if response.status != "success" or not response.output_text:
        raise ReplayArtifactError("completed LF-022 call lacks a nonempty response")
    return response


def bridge_provider_result_to_generic_llm_lineage(
    *,
    request: ProviderRequest,
    result: ProviderResult,
    request_artifact_path: Path,
    artifact_root: Path,
    role: LLMRole,
    provider_slot: str,
    model_family: str,
    prompt_template_id: str,
    prompt_template_version: str,
    execution_mode: LLMExecutionMode,
    parse_status: ParseStatus,
    parsed_output: Mapping[str, object] | None,
    private_source_content: bool,
    denylist_checked: bool,
    denylist_hits: tuple[str, ...],
    started_at: datetime.datetime,
    completed_at: datetime.datetime,
    supervision_eligible: bool,
    metadata: Mapping[str, DecodingValue] | None = None,
) -> ProviderLLMLineage:
    """Create hash-bound schema-v2 lineage for one LF-022 proposer/judge call."""

    if role is LLMRole.AUTOFORMALIZER:
        raise ProviderError("autoformalizer calls require bridge_provider_result_to_llm_lineage")
    if request.attempt_index != 0:
        raise ProviderError("single-attempt generic lineage bridge requires attempt_index=0")
    if request.private_source_content != private_source_content:
        raise PrivateContentTransmissionError(
            "registered private_source_content must match ProviderRequest"
        )
    if execution_mode == "external" and private_source_content:
        raise PrivateContentTransmissionError(
            "external LF-022 execution cannot contain private-source content"
        )
    if not denylist_checked or denylist_hits:
        raise ProviderError("LF-022 calls require a completed denylist check with zero hits")
    if role is LLMRole.PRIMARY_EVAL_JUDGE and supervision_eligible:
        raise ProviderError("primary evaluation judge cannot be supervision eligible")

    if parsed_output is not None:
        canonical = to_canonical(parsed_output)
        if not isinstance(canonical, dict):
            raise ProviderError("parsed_output must be a canonical JSON object")
        parsed_output_dict: dict[str, object] | None = dict(canonical)
    else:
        parsed_output_dict = None

    persisted_request = load_provider_request(request_artifact_path)
    if persisted_request != request:
        raise ReplayArtifactError("persisted provider request differs from in-memory request")
    request_artifact_sha256 = hash_file(request_artifact_path)
    persisted_response = _load_raw_response(result.raw_response_path)
    _validate_response_binding(persisted_response, request)
    if persisted_response != result.response:
        raise ReplayArtifactError("persisted raw response differs from ProviderResult")
    if hash_file(result.raw_response_path) != result.raw_response_sha256:
        raise ReplayArtifactError("ProviderResult raw_response_sha256 does not match artifact")

    if result.response.status == "success":
        output_text = result.response.output_text or ""
        attempt_status = (
            LLMAttemptStatus.RESPONSE_RECEIVED if output_text else LLMAttemptStatus.EMPTY_RESPONSE
        )
        terminal_status = (
            LLMCallStatus.COMPLETED
            if attempt_status is LLMAttemptStatus.RESPONSE_RECEIVED
            else LLMCallStatus.EXHAUSTED
        )
        error_code = None
        error_detail = None
        retryable = attempt_status is LLMAttemptStatus.EMPTY_RESPONSE
        if parse_status is ParseStatus.PARSED and parsed_output_dict is None:
            raise ProviderError("parse_status=parsed requires parsed_output")
        if parse_status is not ParseStatus.PARSED and parsed_output_dict is not None:
            raise ProviderError("non-parsed provider result cannot carry parsed_output")
        if not output_text and parse_status is not ParseStatus.EMPTY:
            raise ProviderError("empty provider response requires parse_status=empty")
    else:
        attempt_status = LLMAttemptStatus.PROVIDER_ERROR
        terminal_status = LLMCallStatus.EXHAUSTED
        error_code = result.response.error_type or "provider_error"
        error_detail = result.response.error_detail
        retryable = False
        if parse_status is not ParseStatus.EMPTY or parsed_output_dict is not None:
            raise ProviderError("provider-error result requires empty parse state")

    request_artifact = _repository_artifact(
        request_artifact_path,
        artifact_root=artifact_root,
        field="request_artifact_path",
    )
    raw_response_artifact = _repository_artifact(
        result.raw_response_path,
        artifact_root=artifact_root,
        field="raw_response_path",
    )
    metadata_dict = dict(metadata or {})
    metadata_dict.update(
        {
            "provider_protocol": "provider_v1",
            "provider_request_hash": request.request_hash,
            "provider_attempt_id": request.attempt_id,
            "request_artifact_sha256": request_artifact_sha256,
            "raw_response_sha256": result.raw_response_sha256,
        }
    )
    call_id = make_llm_call_id(
        provider=request.provider,
        provider_slot=provider_slot,
        model=request.model,
        model_family=model_family,
        model_revision=request.revision,
        role=role,
        problem_record_id=None,
        prompt_template_hash=request.prompt_template_hash,
        prompt_render_hash=request.prompt_render_hash,
        input_ids=request.input_ids,
        decoding=request.decoding,
    )
    attempt_id = make_llm_attempt_id(call_id, request.attempt_index)
    latency_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
    attempt = LLMAttemptRecord(
        attempt_id=attempt_id,
        call_id=call_id,
        attempt_index=request.attempt_index,
        execution_mode=execution_mode,
        started_at=started_at,
        completed_at=completed_at,
        request_artifact=request_artifact,
        raw_response_artifact=raw_response_artifact,
        status=attempt_status,
        error_code=error_code,
        error_detail=error_detail,
        retryable=retryable,
        latency_ms=latency_ms,
        provider_request_hash=request.request_hash,
        provider_attempt_id=request.attempt_id,
        request_artifact_sha256=request_artifact_sha256,
        raw_response_sha256=result.raw_response_sha256,
        metadata=metadata_dict,
    )
    call = LLMCallRecord(
        schema_version=2,
        call_id=call_id,
        provider=request.provider,
        provider_slot=provider_slot,
        model=request.model,
        model_family=model_family,
        role=role,
        model_revision=request.revision,
        request_date=started_at,
        started_at=started_at,
        completed_at=completed_at,
        execution_mode=execution_mode,
        prompt_template_id=prompt_template_id,
        prompt_template_version=prompt_template_version,
        prompt_template_hash=request.prompt_template_hash,
        prompt_render_hash=request.prompt_render_hash,
        request_artifact=request_artifact,
        input_ids=request.input_ids,
        decoding=request.decoding,
        raw_output_artifact=raw_response_artifact,
        parsed_output=parsed_output_dict,
        parse_status=parse_status,
        retry_count=0,
        supervision_eligible=supervision_eligible,
        private_source_content=private_source_content,
        denylist_checked=denylist_checked,
        denylist_hits=denylist_hits,
        terminal_status=terminal_status,
        attempt_ids=(attempt_id,),
        latency_ms=latency_ms,
        provider_request_hash=request.request_hash,
        request_artifact_sha256=request_artifact_sha256,
        raw_response_sha256=result.raw_response_sha256,
        metadata=metadata_dict,
    )
    return ProviderLLMLineage(attempt=attempt, call=call)


class DeterministicFixtureProvider:
    """Local deterministic provider that persists exact raw responses."""

    def __init__(
        self,
        *,
        identity: ProviderIdentity,
        raw_response_root: Path,
        responses: Mapping[str, str],
    ) -> None:
        if identity.transport != "fixture":
            raise ValueError("DeterministicFixtureProvider requires transport='fixture'")
        self.identity = identity
        self.raw_response_root = raw_response_root
        self.responses = dict(responses)

    def generate(self, request: ProviderRequest) -> ProviderResult:
        _validate_identity(self.identity, request)
        try:
            output = self.responses[request.request_hash]
        except KeyError as exc:
            raise FixtureResponseMissingError(
                f"no fixture response for request {request.request_hash}"
            ) from exc
        response = ProviderRawResponse.success(request, output)
        path, digest = _persist_immutable_response(self.raw_response_root, response)
        return ProviderResult(
            response=response,
            raw_response_path=path,
            raw_response_sha256=digest,
            replayed=False,
        )


class ReplayProvider:
    """Network-free provider that replays an immutable fixture response."""

    def __init__(self, *, identity: ProviderIdentity, raw_response_root: Path) -> None:
        if identity.transport != "replay":
            raise ValueError("ReplayProvider requires transport='replay'")
        self.identity = identity
        self.raw_response_root = raw_response_root

    def generate(self, request: ProviderRequest) -> ProviderResult:
        _validate_identity(self.identity, request)
        path = _raw_response_path(
            self.raw_response_root,
            request_hash=request.request_hash,
            attempt_id=request.attempt_id,
            attempt_index=request.attempt_index,
        )
        response = _load_raw_response(path)
        _validate_response_binding(response, request)
        return ProviderResult(
            response=response,
            raw_response_path=path,
            raw_response_sha256=hash_file(path),
            replayed=True,
        )


class DisabledExternalProvider:
    """Fail-closed external slot placeholder; it performs no I/O."""

    def __init__(self, identity: ProviderIdentity) -> None:
        if identity.transport != "external_disabled":
            raise ValueError("DisabledExternalProvider requires transport='external_disabled'")
        self.identity = identity

    def generate(self, request: ProviderRequest) -> ProviderResult:
        if request.private_source_content:
            raise PrivateContentTransmissionError(
                "private-source content is prohibited on every external-provider path"
            )
        _validate_identity(self.identity, request)
        raise ProviderDisabledError(
            f"external provider {self.identity.provider!r} is disabled until the Phase-5 ADR"
        )


__all__ = [
    "DeterministicFixtureProvider",
    "DisabledExternalProvider",
    "FixtureResponseMissingError",
    "GenerationProvider",
    "PrivateContentTransmissionError",
    "ProviderDisabledError",
    "ProviderError",
    "ProviderIdentity",
    "ProviderIdentityMismatchError",
    "ProviderLLMLineage",
    "ProviderRawResponse",
    "ProviderRequest",
    "ProviderResult",
    "RawResponseConflictError",
    "ReplayArtifactError",
    "ReplayProvider",
    "bridge_provider_result_to_generic_llm_lineage",
    "bridge_provider_result_to_llm_lineage",
    "create_provider_request_for_problem",
    "load_provider_raw_response",
    "load_provider_request",
    "persist_provider_raw_response",
    "persist_provider_request",
    "provider_raw_response_path",
    "verify_generic_llm_call_artifacts",
    "verify_llm_call_artifacts",
]
