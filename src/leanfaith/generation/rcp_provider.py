"""Small OpenAI-compatible RCP transport used by LF-022 production execution.

The transport is injectable and contains no retry loop.  The executor owns
attempt accounting and must persist the exact wire response before invoking
the parsers in this module.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.lf022_execution import LF022RCPRetryPolicy


class RCPProviderError(RuntimeError):
    """Base class for production RCP transport and response failures."""


class RCPResponseError(RCPProviderError):
    """An HTTP or response-shape failure with an explicit retry decision."""

    def __init__(
        self,
        *,
        code: str,
        retryable: bool,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


class RCPTransportUnknownError(RCPProviderError):
    """The request may have reached the provider but no response was recorded."""


class RCPWireResponse(StrictModel):
    """Exact in-memory HTTP response returned by an injectable transport."""

    status_code: int = Field(ge=100, le=599, strict=True)
    body: bytes
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _canonical_headers(self) -> Self:
        if list(self.headers) != sorted(self.headers):
            raise ValueError("response headers must be sorted")
        if any(key != key.casefold() for key in self.headers):
            raise ValueError("response header names must be lowercase")
        return self


class RCPHTTPTransport(Protocol):
    """Injectable POST-only boundary for public LF-022 calls."""

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPWireResponse: ...


class RCPWireDecodingContract(Protocol):
    """Minimal exact-decoding interface shared by proposer and judge routes."""

    def wire_fields(self) -> dict[str, object]: ...


def _headers(headers: object) -> dict[str, str]:
    items = getattr(headers, "items", None)
    if not callable(items):
        return {}
    return dict(
        sorted(
            (str(key).casefold(), str(value))
            for key, value in cast(list[tuple[object, object]], list(items()))
        )
    )


class UrllibOpenAICompatibleRCPTransport:
    """Stdlib HTTPS transport with runtime-only bearer credentials."""

    def post_json(
        self,
        *,
        url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_seconds: int,
    ) -> RCPWireResponse:
        request = urllib.request.Request(
            url,
            data=canonical_json_bytes(payload),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "LeanFaith-LF022-Public-Provisional/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return RCPWireResponse(
                    status_code=int(response.status),
                    body=response.read(),
                    headers=_headers(response.headers),
                )
        except urllib.error.HTTPError as exc:
            return RCPWireResponse(
                status_code=int(exc.code),
                body=exc.read(),
                headers=_headers(exc.headers),
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            # Once urlopen begins, the client cannot prove the provider did not
            # receive the request.  Automatic retry could create a paid
            # duplicate, so the executor quarantines this state.
            raise RCPTransportUnknownError(
                "RCP request ended without a response; delivery status is unknown"
            ) from exc


def prompt_messages(rendered_prompt: str) -> list[dict[str, str]]:
    """Recover the frozen system/user boundary emitted by the proposer renderer."""

    prefix = "SYSTEM\n"
    marker = "\n\nPROMPT_TEMPLATE_SHA256\n"
    if not rendered_prompt.startswith(prefix) or marker not in rendered_prompt:
        raise RCPProviderError("rendered prompt lacks the frozen system/user boundary")
    system, user = rendered_prompt[len(prefix) :].split(marker, 1)
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": "PROMPT_TEMPLATE_SHA256\n" + user},
    ]


def make_chat_completion_payload(
    *,
    model_id: str,
    rendered_prompt: str,
    decoding: RCPWireDecodingContract,
) -> dict[str, object]:
    """Build the exact OpenAI-compatible request body."""

    return {
        "model": model_id,
        "messages": prompt_messages(rendered_prompt),
        **decoding.wire_fields(),
    }


class RCPCompletion(StrictModel):
    """Task-visible completion content plus non-semantic usage metadata."""

    content: str = Field(min_length=1)
    returned_model: str = Field(min_length=1)
    provider_request_id: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str | None = None
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _strict_object(body: bytes) -> dict[str, object]:
    def duplicate_keys(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value!r}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RCPResponseError(code="invalid_json", retryable=False) from exc
    if not isinstance(document, dict):
        raise RCPResponseError(code="invalid_response_shape", retryable=False)
    return cast(dict[str, object], document)


def parse_chat_completion(
    body: bytes,
    *,
    expected_model: str,
) -> RCPCompletion:
    """Parse only ``choices[0].message.content`` from an already-persisted body."""

    document = _strict_object(body)
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RCPResponseError(code="invalid_response_shape", retryable=False)
    choice = cast(dict[str, object], choices[0])
    returned_model = document.get("model")
    if not isinstance(returned_model, str) or returned_model != expected_model:
        raise RCPResponseError(code="returned_model_mismatch", retryable=False)
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise RCPResponseError(code="invalid_response_shape", retryable=False)
    if finish_reason == "length":
        # A reasoning model can consume its entire completion budget without
        # emitting final content.  This is not an empty transport response and
        # retrying the identical payload would repeat a paid deterministic call.
        raise RCPResponseError(code="output_budget_exhausted", retryable=False)
    message = choice.get("message")
    if not isinstance(message, dict):
        raise RCPResponseError(code="invalid_response_shape", retryable=False)
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RCPResponseError(code="empty_response", retryable=False)
    usage_raw = document.get("usage")
    usage = (
        {
            str(key): value
            for key, value in cast(dict[object, object], usage_raw).items()
            if isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        if isinstance(usage_raw, dict)
        else {}
    )
    request_id = document.get("id")
    return RCPCompletion(
        content=content,
        returned_model=returned_model,
        provider_request_id=request_id if isinstance(request_id, str) else None,
        usage=dict(sorted(usage.items())),
        finish_reason=finish_reason,
        body_sha256=sha256_hex(body),
    )


def retry_after_seconds(
    value: str | None,
    *,
    now: datetime.datetime,
) -> float | None:
    """Parse either delta-seconds or an RFC 7231 HTTP date."""

    if value is None or not value.strip():
        return None
    stripped = value.strip()
    try:
        seconds = float(stripped)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.UTC)
        seconds = (parsed.astimezone(datetime.UTC) - now.astimezone(datetime.UTC)).total_seconds()
    if seconds < 0:
        return 0.0
    return seconds


def classify_http_response(
    response: RCPWireResponse,
    *,
    policy: LF022RCPRetryPolicy,
    now: datetime.datetime,
) -> RCPResponseError | None:
    """Return a frozen retry decision for a non-200 HTTP response."""

    if response.status_code == 200:
        return None
    retryable = response.status_code in policy.retryable_http_statuses
    retry_after = (
        retry_after_seconds(response.headers.get("retry-after"), now=now)
        if retryable and policy.honor_retry_after
        else None
    )
    return RCPResponseError(
        code=f"http_{response.status_code}",
        retryable=retryable,
        http_status=response.status_code,
        retry_after_seconds=retry_after,
    )


def retry_delay_seconds(
    *,
    policy: LF022RCPRetryPolicy,
    attempt_index: int,
    retry_after: float | None,
) -> float:
    """Compute bounded exponential delay, honoring a larger Retry-After value."""

    exponential = min(
        policy.maximum_delay_seconds,
        policy.base_delay_seconds * (2**attempt_index),
    )
    requested = retry_after if retry_after is not None else 0.0
    return float(min(policy.maximum_delay_seconds, max(exponential, requested)))


__all__ = [
    "RCPCompletion",
    "RCPHTTPTransport",
    "RCPProviderError",
    "RCPResponseError",
    "RCPTransportUnknownError",
    "RCPWireDecodingContract",
    "RCPWireResponse",
    "UrllibOpenAICompatibleRCPTransport",
    "classify_http_response",
    "make_chat_completion_payload",
    "parse_chat_completion",
    "prompt_messages",
    "retry_after_seconds",
    "retry_delay_seconds",
]
