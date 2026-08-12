"""Deterministic secret redaction for durable provider-process captures.

Provider CLIs occasionally echo authentication or proxy diagnostics.  The
original bytes therefore remain process-local: only the deterministically
redacted streams and this non-secret provenance record may be persisted.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex

CAPTURE_REDACTION_METHOD_VERSION = "provider_capture_redaction_v1"
_SECRET_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CREDENTIAL|AUTH)",
    flags=re.IGNORECASE,
)
_PROXY_NAME = re.compile(r"(?:^|_)PROXY$", flags=re.IGNORECASE)
_GENERIC_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "bearer_token",
        re.compile(rb"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/=-]{8,}"),
    ),
    (
        "provider_token",
        re.compile(rb"(?i)\b(?:sk|key|token|secret|api)[-_][A-Za-z0-9._~+/=-]{8,}"),
    ),
    (
        "proxy_url_credentials",
        re.compile(rb"(?i)\b(?:https?|socks5?)://[^\s/@:]+:[^\s/@]+@"),
    ),
)


@dataclass(frozen=True, slots=True)
class RedactedCapture:
    streams: dict[str, bytes]
    report_bytes: bytes
    report_sha256: str
    replacement_count: int


def _environment_secrets(environment: Mapping[str, str]) -> tuple[tuple[str, bytes], ...]:
    candidates: set[tuple[str, bytes]] = set()
    for name, value in environment.items():
        if not value:
            continue
        encoded = value.encode("utf-8", errors="surrogateescape")
        if _SECRET_NAME.search(name):
            candidates.add(("environment_secret", encoded))
        if _PROXY_NAME.search(name):
            try:
                parsed = urlsplit(value)
            except ValueError:
                parsed = None
            if parsed is not None:
                for credential in (parsed.username, parsed.password):
                    if credential:
                        candidates.add(("proxy_credential", credential.encode("utf-8")))
                if parsed.username is not None and parsed.password is not None:
                    userinfo = f"{parsed.username}:{parsed.password}".encode()
                    candidates.add(("proxy_credential", userinfo))
    return tuple(sorted(candidates, key=lambda item: (-len(item[1]), item[0], item[1])))


def redact_captured_streams(
    streams: Mapping[str, bytes],
    *,
    environment: Mapping[str, str],
) -> RedactedCapture:
    """Return bounded captures safe for persistence plus replayable provenance."""

    exact = _environment_secrets(environment)
    redacted: dict[str, bytes] = {}
    per_kind: Counter[str] = Counter()
    per_stream: dict[str, dict[str, object]] = {}
    for stream_name in sorted(streams):
        original = streams[stream_name]
        value = original
        stream_counts: Counter[str] = Counter()
        for kind, secret in exact:
            count = value.count(secret)
            if count:
                value = value.replace(secret, f"[REDACTED:{kind}]".encode("ascii"))
                stream_counts[kind] += count
        for kind, pattern in _GENERIC_PATTERNS:
            value, count = pattern.subn(f"[REDACTED:{kind}]".encode("ascii"), value)
            stream_counts[kind] += count
        redacted[stream_name] = value
        per_kind.update(stream_counts)
        per_stream[stream_name] = {
            "original_sha256": sha256_hex(original),
            "redacted_sha256": sha256_hex(value),
            "replacement_count": sum(stream_counts.values()),
            "replacement_kinds": dict(sorted(stream_counts.items())),
        }
    report = {
        "schema_version": 1,
        "method_version": CAPTURE_REDACTION_METHOD_VERSION,
        "replacement_count": sum(per_kind.values()),
        "replacement_kinds": dict(sorted(per_kind.items())),
        "streams": per_stream,
    }
    report_bytes = canonical_json_bytes(report) + b"\n"
    return RedactedCapture(
        streams=redacted,
        report_bytes=report_bytes,
        report_sha256=sha256_hex(report_bytes),
        replacement_count=sum(per_kind.values()),
    )


__all__ = [
    "CAPTURE_REDACTION_METHOD_VERSION",
    "RedactedCapture",
    "redact_captured_streams",
]
