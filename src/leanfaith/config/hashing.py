"""Canonical JSON serialization and content hashing.

Canonical form (PLAN.md §11.12): UTF-8 JSON with sorted keys and compact
separators, restricted to JSON-native values. Non-finite floats, non-string
mapping keys, and non-JSON objects (datetimes, paths, sets, ...) are rejected
rather than coerced, so a hash can never silently absorb a timestamp or a
machine-local path object.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path

_CHUNK_SIZE = 1 << 20

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented in canonical JSON."""


def to_canonical(value: object, *, _path: str = "$") -> JsonValue:
    """Validate and convert ``value`` into a canonical JSON-native structure."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"non-finite float at {_path}: {value!r}")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string mapping key at {_path}: {key!r}")
            result[key] = to_canonical(item, _path=f"{_path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [to_canonical(item, _path=f"{_path}[{i}]") for i, item in enumerate(value)]
    raise CanonicalizationError(f"non-JSON value at {_path}: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Serialize ``value`` as canonical JSON bytes (sorted keys, compact, UTF-8)."""
    canonical = to_canonical(value)
    text = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_canonical(value: object) -> str:
    """SHA-256 hex digest of the canonical JSON serialization of ``value``."""
    return sha256_hex(canonical_json_bytes(value))


def hash_file(path: Path) -> str:
    """SHA-256 hex digest of a file's exact bytes, streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()
