"""Bounded persistent Lean declaration-range oracle for CPT2 splitters."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanfaith.cpt2.source import SourceRow
from leanfaith.cpt2.splitters import (
    DECLARATION_AWARE_METHOD,
    MASKED_REVERSE_METHOD,
    RAW_REVERSE_METHOD,
    declaration_delimiters,
    split_source,
)
from leanfaith.lean.protocol import LeanBackend, LeanRequest

ORACLE_VERSION = "cpt2_declaration_range_oracle_v2"
MAX_ORACLE_ROWS = 500


@dataclass(frozen=True, slots=True)
class OracleObservation:
    source_id: str
    source_sha256: str
    cache_key: str
    boundary: int | None
    status: str
    failure: str | None
    request_hash: str | None
    elapsed_ms: int
    cache_hit: bool


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(source):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _position_to_offset(source: str, position: object) -> int:
    if not isinstance(position, dict):
        raise ValueError("missing Lean source position")
    line = position.get("line")
    column = position.get("column")
    if not isinstance(line, int) or not isinstance(column, int):
        raise ValueError("invalid Lean source position")
    starts = _line_starts(source)
    if line < 1 or line > len(starts):
        raise ValueError("Lean source line is out of range")
    offset = starts[line - 1] + column
    if offset < 0 or offset > len(source):
        raise ValueError("Lean source offset is out of range")
    return offset


def boundary_from_declarations(source: str, declarations: Sequence[dict[str, Any]]) -> int:
    """Anchor at Lean's last theorem/lemma signature, then find its proof delimiter."""

    candidates: list[tuple[int, dict[str, Any]]] = []
    for declaration in declarations:
        if str(declaration.get("kind") or "") not in {"theorem", "lemma"}:
            continue
        decl_range = declaration.get("range")
        signature = declaration.get("signature")
        if not isinstance(decl_range, dict) or not isinstance(signature, dict):
            continue
        sig_range = signature.get("range")
        if not isinstance(sig_range, dict):
            continue
        try:
            start = _position_to_offset(source, decl_range.get("start"))
        except ValueError:
            continue
        candidates.append((start, declaration))
    if not candidates:
        raise ValueError("Lean returned no ranged theorem/lemma declaration")
    declaration = max(candidates, key=lambda item: item[0])[1]
    signature = declaration["signature"]
    if not isinstance(signature, dict):
        raise AssertionError("candidate filtering lost signature mapping")
    sig_range = signature["range"]
    if not isinstance(sig_range, dict):
        raise AssertionError("candidate filtering lost signature range")
    signature_finish = _position_to_offset(source, sig_range.get("finish"))
    delimiters = declaration_delimiters(source, start=signature_finish)
    if not delimiters:
        raise ValueError("no declaration delimiter follows Lean signature range")
    return delimiters[0]


def _probe_source(source: str) -> str:
    """Replace only the final proof with ``sorry``; never elaborate the corpus proof."""

    for method in (
        DECLARATION_AWARE_METHOD,
        MASKED_REVERSE_METHOD,
        RAW_REVERSE_METHOD,
    ):
        split = split_source(source, method)
        if split is not None:
            return split.theorem + "by\n  sorry\n"
    raise ValueError("no cheap splitter can form an oracle probe")


def oracle_cache_key(
    source: str,
    *,
    context_fingerprint: str,
) -> str:
    payload = {
        "oracle_version": ORACLE_VERSION,
        "source_sha256": _sha256_text(source),
        "context_fingerprint": context_fingerprint,
        "allow_sorry": True,
        "parallel_elaboration": False,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_cache(path: Path) -> dict[str, OracleObservation]:
    cached: dict[str, OracleObservation] = {}
    if not path.exists():
        return cached
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            observation = OracleObservation(
                source_id=str(payload["source_id"]),
                source_sha256=str(payload["source_sha256"]),
                cache_key=str(payload["cache_key"]),
                boundary=(int(payload["boundary"]) if payload["boundary"] is not None else None),
                status=str(payload["status"]),
                failure=(str(payload["failure"]) if payload["failure"] is not None else None),
                request_hash=(
                    str(payload["request_hash"]) if payload["request_hash"] is not None else None
                ),
                elapsed_ms=int(payload["elapsed_ms"]),
                cache_hit=False,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid CPT2 oracle cache line {line_number}: {exc}") from exc
        prior = cached.get(observation.cache_key)
        if prior is not None and prior != observation:
            raise ValueError(f"conflicting CPT2 oracle cache key {observation.cache_key}")
        cached[observation.cache_key] = observation
    return cached


def load_oracle_observations(
    path: Path,
    *,
    cache_hit: bool = True,
) -> tuple[OracleObservation, ...]:
    """Load a verified append-only oracle journal in first-seen order."""

    return tuple(
        OracleObservation(
            source_id=observation.source_id,
            source_sha256=observation.source_sha256,
            cache_key=observation.cache_key,
            boundary=observation.boundary,
            status=observation.status,
            failure=observation.failure,
            request_hash=observation.request_hash,
            elapsed_ms=observation.elapsed_ms,
            cache_hit=cache_hit,
        )
        for observation in _load_cache(path).values()
    )


def _append_cache(path: Path, observation: OracleObservation) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_id": observation.source_id,
        "source_sha256": observation.source_sha256,
        "cache_key": observation.cache_key,
        "boundary": observation.boundary,
        "status": observation.status,
        "failure": observation.failure,
        "request_hash": observation.request_hash,
        "elapsed_ms": observation.elapsed_ms,
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _request_for_row(
    row: SourceRow,
    *,
    context_id: str,
    timeout_seconds: float,
) -> LeanRequest:
    return LeanRequest(
        request_id=f"cpt2-oracle-{row.source_id[:24]}",
        context_id=context_id,
        code=_probe_source(row.source_code),
        declarations=True,
        allow_sorry=True,
        timeout_seconds=timeout_seconds,
        metadata={"task": "CPT2", "oracle_version": ORACLE_VERSION},
    )


def _chunks(rows: Sequence[SourceRow], size: int) -> Iterable[Sequence[SourceRow]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def run_oracle(
    backend: LeanBackend,
    rows: Sequence[SourceRow],
    *,
    context_id: str,
    context_fingerprint: str,
    cache_path: Path,
    timeout_seconds: float = 60.0,
    batch_size: int = 16,
) -> tuple[OracleObservation, ...]:
    """Run at most 500 proof-stripped requests through one persistent backend."""

    if len(rows) > MAX_ORACLE_ROWS:
        raise ValueError(f"CPT2 oracle is capped at {MAX_ORACLE_ROWS} rows")
    if batch_size <= 0:
        raise ValueError("oracle batch_size must be positive")
    cached = _load_cache(cache_path)
    observations: dict[str, OracleObservation] = {}
    misses: list[SourceRow] = []
    for row in rows:
        key = oracle_cache_key(row.source_code, context_fingerprint=context_fingerprint)
        if key in cached:
            prior = cached[key]
            observations[row.source_id] = OracleObservation(
                source_id=row.source_id,
                source_sha256=prior.source_sha256,
                cache_key=prior.cache_key,
                boundary=prior.boundary,
                status=prior.status,
                failure=prior.failure,
                request_hash=prior.request_hash,
                elapsed_ms=prior.elapsed_ms,
                cache_hit=True,
            )
        else:
            misses.append(row)

    for chunk in _chunks(misses, batch_size):
        requests = [
            _request_for_row(row, context_id=context_id, timeout_seconds=timeout_seconds)
            for row in chunk
        ]
        results = backend.run_batch(requests)
        if len(results) != len(chunk):
            raise ValueError("Lean backend returned a different oracle batch size")
        for row, result in zip(chunk, results, strict=True):
            boundary: int | None = None
            failure: str | None = None
            try:
                boundary = boundary_from_declarations(row.source_code, result.declarations)
            except ValueError as exc:
                failure = str(exc)
            observation = OracleObservation(
                source_id=row.source_id,
                source_sha256=_sha256_text(row.source_code),
                cache_key=oracle_cache_key(
                    row.source_code,
                    context_fingerprint=context_fingerprint,
                ),
                boundary=boundary,
                status=result.status.value,
                failure=failure,
                request_hash=result.request_hash,
                elapsed_ms=result.elapsed_ms,
                cache_hit=False,
            )
            _append_cache(cache_path, observation)
            observations[row.source_id] = observation
    return tuple(observations[row.source_id] for row in rows)
