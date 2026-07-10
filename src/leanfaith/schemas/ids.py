"""Deterministic semantic IDs (PLAN.md §11.12).

An ID is ``<prefix>:<sha256 hex>`` over the normalized UTF-8 canonical JSON of
its payload with sorted keys. Timestamps, machine-local absolute paths, and
mutable state never enter semantic IDs: datetimes and other non-JSON objects
are rejected by canonicalization, and strings pointing into machine-local
roots (home directory, current working directory, ``/tmp``) are rejected by an
explicit guard so payloads must use repo-relative paths.
"""

from __future__ import annotations

import re
from pathlib import Path

from leanfaith.config.hashing import CanonicalizationError, hash_canonical, to_canonical

_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_ID_PATTERN = re.compile(r"^(?P<prefix>[a-z][a-z0-9_]*):(?P<digest>[0-9a-f]{64})$")

HEX64_PATTERN = r"^[0-9a-f]{64}$"

#: Canonical ID prefixes by record kind. §8.11 fixes ``ctx``; §12.6 fixes
#: ``anc``; §20.6 fixes ``pair``/``nllean``; the rest are fixed here once.
CONTEXT_PREFIX = "ctx"
THEOREM_PREFIX = "thm"
ANCESTRY_PREFIX = "anc"
REPRESENTATION_PREFIX = "repr"
VARIANT_PREFIX = "var"
PAIR_PREFIX = "pair"
EVIDENCE_PREFIX = "ev"
LABEL_PREFIX = "lbl"
NL_LEAN_PREFIX = "nllean"
DRAFT_PREFIX = "draft"
AUDIT_PREFIX = "audit"
LLM_CALL_PREFIX = "call"
ANNOTATION_PREFIX = "ann"


def id_pattern(prefix: str) -> str:
    """Regex (for ``Field(pattern=...)``) matching IDs with the given prefix."""
    if not _PREFIX_PATTERN.match(prefix):
        raise InvalidIdError(f"invalid ID prefix {prefix!r}")
    return rf"^{prefix}:[0-9a-f]{{64}}$"


class InvalidIdError(ValueError):
    """Raised for malformed IDs or forbidden ID payloads."""


def _machine_local_roots() -> tuple[str, ...]:
    roots = ["/tmp/"]
    for candidate in (Path.home(), Path.cwd()):
        try:
            roots.append(str(candidate.resolve()) + "/")
        except OSError:  # pragma: no cover - resolution failure is environment-specific
            continue
    return tuple(roots)


def _forbid_machine_local_strings(value: object, roots: tuple[str, ...], path: str) -> None:
    if isinstance(value, str):
        for root in roots:
            if value.startswith(root) or value.startswith(root.rstrip("/") + "\n"):
                raise InvalidIdError(
                    f"machine-local absolute path in ID payload at {path}: {value!r}; "
                    "use repo-relative paths in semantic IDs"
                )
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _forbid_machine_local_strings(item, roots, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _forbid_machine_local_strings(item, roots, f"{path}[{index}]")


def make_id(prefix: str, payload: object) -> str:
    """Build ``prefix:sha256(canonical_json(payload))``.

    Raises ``InvalidIdError`` for a malformed prefix, a payload that is not
    canonical-JSON representable (datetimes, paths, NaN, ...), or a payload
    containing machine-local absolute path strings.
    """
    if not _PREFIX_PATTERN.match(prefix):
        raise InvalidIdError(f"invalid ID prefix {prefix!r}: must match {_PREFIX_PATTERN.pattern}")
    try:
        canonical = to_canonical(payload)
    except CanonicalizationError as exc:
        raise InvalidIdError(f"ID payload is not canonical-JSON representable: {exc}") from exc
    _forbid_machine_local_strings(canonical, _machine_local_roots(), "$")
    return f"{prefix}:{hash_canonical(canonical)}"


def parse_id(identifier: str) -> tuple[str, str]:
    """Split a valid ID into ``(prefix, digest)``; raise ``InvalidIdError`` otherwise."""
    match = _ID_PATTERN.match(identifier)
    if match is None:
        raise InvalidIdError(f"malformed ID: {identifier!r}")
    return match.group("prefix"), match.group("digest")


def is_valid_id(identifier: str, *, prefix: str | None = None) -> bool:
    match = _ID_PATTERN.match(identifier)
    if match is None:
        return False
    return prefix is None or match.group("prefix") == prefix


def id_prefix(identifier: str) -> str:
    return parse_id(identifier)[0]
