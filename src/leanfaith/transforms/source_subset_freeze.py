"""Freeze one source from aligned Gate-3 theorem and representation streams.

The freezer is deliberately narrow and fail closed.  It validates the complete
aligned input pair before selecting a source, writes a canonical source-only
subset, and treats an existing output directory as an immutable replay target.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas import RepresentationRecord, TheoremRecord

SOURCE_SUBSET_FREEZER_CODE_VERSION = "transform_source_subset_freezer_v1"
_HEX64 = r"^[0-9a-f]{64}$"
_THEOREM_OUTPUT = "theorems.jsonl"
_REPRESENTATION_OUTPUT = "representations.jsonl"
_MANIFEST_OUTPUT = "manifest.json"
_EXPECTED_OUTPUTS = frozenset({_THEOREM_OUTPUT, _REPRESENTATION_OUTPUT, _MANIFEST_OUTPUT})


class SourceSubsetFreezeError(ValueError):
    """Raised when input validation or immutable replay verification fails."""


class SourceSubsetFreezeManifest(StrictModel):
    """Content-addressed binding for a frozen source subset."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["transform_source_subset_freeze_manifest"] = (
        "transform_source_subset_freeze_manifest"
    )
    code_version: Literal["transform_source_subset_freezer_v1"] = (
        "transform_source_subset_freezer_v1"
    )
    source: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    input_theorem_path: str = Field(min_length=1)
    input_theorem_sha256: str = Field(pattern=_HEX64)
    input_representation_path: str = Field(min_length=1)
    input_representation_sha256: str = Field(pattern=_HEX64)
    input_record_count: int = Field(ge=1)
    record_count: int = Field(ge=1)
    theorem_schema_versions: tuple[int, ...]
    representation_schema_versions: tuple[int, ...]
    ordered_theorem_ids_sha256: str = Field(pattern=_HEX64)
    theorem_output: Literal["theorems.jsonl"] = "theorems.jsonl"
    theorem_output_sha256: str = Field(pattern=_HEX64)
    representation_output: Literal["representations.jsonl"] = "representations.jsonl"
    representation_output_sha256: str = Field(pattern=_HEX64)


@dataclass(frozen=True)
class SourceSubsetFreezeArtifacts:
    """Paths and summary for a completed freeze or verified replay."""

    theorem_path: Path
    representation_path: Path
    manifest_path: Path
    source: str
    context_id: str
    record_count: int
    replayed: bool


@dataclass(frozen=True)
class _TheoremInput:
    theorem: TheoremRecord
    output_row: dict[str, object]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SourceSubsetFreezeError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_jsonl_objects(path: Path) -> list[dict[str, object]]:
    if not path.is_file() or path.is_symlink():
        raise SourceSubsetFreezeError(f"input is not a regular file: {path}")
    payload = path.read_bytes()
    if not payload:
        raise SourceSubsetFreezeError(f"input JSONL is empty: {path}")
    if not payload.endswith(b"\n"):
        raise SourceSubsetFreezeError(f"input JSONL must end with a newline: {path}")

    rows: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        if not raw_line.strip():
            raise SourceSubsetFreezeError(f"blank JSONL row at {path}:{line_number}")
        try:
            value = json.loads(raw_line, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceSubsetFreezeError(f"invalid JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise SourceSubsetFreezeError(f"JSONL row must be an object at {path}:{line_number}")
        rows.append(value)
    return rows


def _load_theorems(path: Path) -> list[_TheoremInput]:
    loaded: list[_TheoremInput] = []
    seen: set[str] = set()
    for line_number, row in enumerate(_read_jsonl_objects(path), start=1):
        if "theorem" in row:
            if set(row) != {"theorem", "representation"}:
                raise SourceSubsetFreezeError(
                    f"wrapped theorem row at {path}:{line_number} must contain exactly "
                    "'theorem' and 'representation'"
                )
            theorem_value = row["theorem"]
            representation_value = row["representation"]
            if not isinstance(theorem_value, dict) or not isinstance(representation_value, dict):
                raise SourceSubsetFreezeError(
                    f"wrapped theorem row at {path}:{line_number} has non-object fields"
                )
            theorem_payload = theorem_value
            output_row = row
        else:
            theorem_payload = row
            output_row = row
        try:
            theorem = TheoremRecord.model_validate(theorem_payload)
        except ValueError as exc:
            raise SourceSubsetFreezeError(
                f"invalid TheoremRecord at {path}:{line_number}: {exc}"
            ) from exc
        if theorem.theorem_id in seen:
            raise SourceSubsetFreezeError(f"duplicate theorem_id: {theorem.theorem_id}")
        seen.add(theorem.theorem_id)
        loaded.append(_TheoremInput(theorem=theorem, output_row=output_row))
    return loaded


def _load_representations(path: Path) -> list[RepresentationRecord]:
    loaded: list[RepresentationRecord] = []
    representation_ids: set[str] = set()
    theorem_ids: set[str] = set()
    for line_number, row in enumerate(_read_jsonl_objects(path), start=1):
        try:
            representation = RepresentationRecord.model_validate(row)
        except ValueError as exc:
            raise SourceSubsetFreezeError(
                f"invalid RepresentationRecord at {path}:{line_number}: {exc}"
            ) from exc
        if representation.representation_id in representation_ids:
            raise SourceSubsetFreezeError(
                f"duplicate representation_id: {representation.representation_id}"
            )
        if representation.theorem_id in theorem_ids:
            raise SourceSubsetFreezeError(
                f"multiple representations for theorem_id: {representation.theorem_id}"
            )
        representation_ids.add(representation.representation_id)
        theorem_ids.add(representation.theorem_id)
        loaded.append(representation)
    return loaded


def _canonical_jsonl(rows: list[dict[str, object]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def _canonical_model_jsonl(rows: Sequence[StrictModel]) -> bytes:
    return b"".join(canonical_json_bytes(row.model_dump(mode="json")) + b"\n" for row in rows)


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _verify_replay(
    output_dir: Path,
    *,
    expected_payloads: dict[str, bytes],
) -> None:
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise SourceSubsetFreezeError(
            f"pre-existing output is not a regular directory: {output_dir}"
        )
    actual_names = {item.name for item in output_dir.iterdir()}
    if actual_names != _EXPECTED_OUTPUTS:
        raise SourceSubsetFreezeError(
            "pre-existing output directory is not an exact replay; "
            f"expected files {sorted(_EXPECTED_OUTPUTS)}, found {sorted(actual_names)}"
        )
    for name, expected in expected_payloads.items():
        path = output_dir / name
        if not path.is_file() or path.is_symlink():
            raise SourceSubsetFreezeError(f"replay artifact is not a regular file: {path}")
        if path.read_bytes() != expected:
            raise SourceSubsetFreezeError(
                f"pre-existing output differs from the requested freeze: {path}"
            )


def freeze_transform_source_subset(
    *,
    theorem_path: Path,
    representation_path: Path,
    source: str,
    output_dir: Path,
) -> SourceSubsetFreezeArtifacts:
    """Validate aligned inputs and immutably freeze all rows for ``source``.

    An existing output directory is accepted only when every expected artifact
    is byte-identical to the requested result.  In that case nothing is
    rewritten and the returned result has ``replayed=True``.
    """
    if not source or source != source.strip():
        raise SourceSubsetFreezeError("source must be a nonempty, trimmed string")

    theorem_path = theorem_path.resolve(strict=True)
    representation_path = representation_path.resolve(strict=True)
    output_dir = output_dir.resolve(strict=False)
    if theorem_path == representation_path:
        raise SourceSubsetFreezeError("theorem and representation inputs must differ")
    for output_name in _EXPECTED_OUTPUTS:
        output_path = output_dir / output_name
        if output_path in (theorem_path, representation_path):
            raise SourceSubsetFreezeError("an output artifact aliases an input artifact")

    theorems = _load_theorems(theorem_path)
    representations = _load_representations(representation_path)
    if len(theorems) != len(representations):
        raise SourceSubsetFreezeError(
            "theorem/representation record counts differ: "
            f"{len(theorems)} != {len(representations)}"
        )

    theorem_ids = [item.theorem.theorem_id for item in theorems]
    representation_theorem_ids = [item.theorem_id for item in representations]
    if theorem_ids != representation_theorem_ids:
        raise SourceSubsetFreezeError(
            "theorem and representation streams are not positionally one-to-one aligned"
        )

    context_ids = {item.theorem.context_id for item in theorems}
    context_ids.update(item.context_id for item in representations)
    if len(context_ids) != 1:
        raise SourceSubsetFreezeError(
            f"inputs must have exactly one context_id; found {sorted(context_ids)}"
        )
    context_id = next(iter(context_ids))

    selected_pairs = [
        (theorem, representation)
        for theorem, representation in zip(theorems, representations, strict=True)
        if theorem.theorem.source == source
    ]
    if not selected_pairs:
        raise SourceSubsetFreezeError(f"no theorem records match source {source!r}")
    for theorem, representation in selected_pairs:
        if theorem.theorem.source != source:
            raise SourceSubsetFreezeError("source selection invariant failed")
        if theorem.theorem.context_id != representation.context_id:
            raise SourceSubsetFreezeError(
                f"context mismatch for theorem {theorem.theorem.theorem_id}"
            )
        wrapper_representation = theorem.output_row.get("representation")
        if isinstance(wrapper_representation, dict):
            wrapped_theorem_id = wrapper_representation.get("theorem_id")
            if wrapped_theorem_id is not None and wrapped_theorem_id != theorem.theorem.theorem_id:
                raise SourceSubsetFreezeError(
                    f"wrapped representation theorem_id mismatch for {theorem.theorem.theorem_id}"
                )
            wrapped_representation_id = wrapper_representation.get("representation_id")
            if (
                wrapped_representation_id is not None
                and wrapped_representation_id != representation.representation_id
            ):
                raise SourceSubsetFreezeError(
                    f"wrapped representation_id mismatch for {theorem.theorem.theorem_id}"
                )

    selected_pairs.sort(key=lambda pair: pair[0].theorem.theorem_id)
    selected_theorems = [pair[0] for pair in selected_pairs]
    selected_representations = [pair[1] for pair in selected_pairs]

    theorem_payload = _canonical_jsonl([item.output_row for item in selected_theorems])
    representation_payload = _canonical_model_jsonl(selected_representations)
    ordered_theorem_ids = [item.theorem.theorem_id for item in selected_theorems]
    manifest = SourceSubsetFreezeManifest(
        source=source,
        context_id=context_id,
        input_theorem_path=str(theorem_path),
        input_theorem_sha256=hash_file(theorem_path),
        input_representation_path=str(representation_path),
        input_representation_sha256=hash_file(representation_path),
        input_record_count=len(theorems),
        record_count=len(selected_pairs),
        theorem_schema_versions=tuple(
            sorted({item.theorem.schema_version for item in selected_theorems})
        ),
        representation_schema_versions=tuple(
            sorted({item.schema_version for item in selected_representations})
        ),
        ordered_theorem_ids_sha256=sha256_hex(canonical_json_bytes(ordered_theorem_ids)),
        theorem_output_sha256=sha256_hex(theorem_payload),
        representation_output_sha256=sha256_hex(representation_payload),
    )
    manifest_payload = canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
    expected_payloads = {
        _THEOREM_OUTPUT: theorem_payload,
        _REPRESENTATION_OUTPUT: representation_payload,
        _MANIFEST_OUTPUT: manifest_payload,
    }

    if output_dir.exists():
        _verify_replay(output_dir, expected_payloads=expected_payloads)
        replayed = True
    else:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
        try:
            _write_exclusive(temporary / _THEOREM_OUTPUT, theorem_payload)
            _write_exclusive(temporary / _REPRESENTATION_OUTPUT, representation_payload)
            _write_exclusive(temporary / _MANIFEST_OUTPUT, manifest_payload)
            temporary.rename(output_dir)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        replayed = False

    return SourceSubsetFreezeArtifacts(
        theorem_path=output_dir / _THEOREM_OUTPUT,
        representation_path=output_dir / _REPRESENTATION_OUTPUT,
        manifest_path=output_dir / _MANIFEST_OUTPUT,
        source=source,
        context_id=context_id,
        record_count=len(selected_pairs),
        replayed=replayed,
    )
