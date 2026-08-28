"""Reproducible public-mathlib yield probe for Meta-engine slice 2.

The production entry point is deliberately narrow: it selects 500 public
declarations from one content-bound extraction, writes a names file and a
standalone Mathlib driver beneath a fresh ``/storage/milikic`` run root, and
runs Lean with the project's 24 GiB memory-hard-limit semantics.  No row is
sent to an external service.

The verifier is independent of the Lean-side hashing implementation.  It
recomputes every source/candidate pretty-text SHA-256 in Python and reconciles
all candidate lines against exactly one terminal line per requested
declaration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import statistics
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_file

METHOD_VERSION = "meta_engine_slice2_yield_probe_v1"
SELECTION_DOMAIN = "leanfaith_meta_slice2_yield_probe_v1"
SELECTION_PREFIX = b"leanfaith_meta_slice2_yield_probe_v1\0"

PRODUCTION_SAMPLE_SIZE = 500
PRODUCTION_TIMEOUT_SECONDS = 7_200
PRODUCTION_ADDRESS_SPACE_BYTES = 25_769_803_776
PRODUCTION_LEAN_MEMORY_MB = 24_576
PRODUCTION_SOURCE = "mathlib"
PRODUCTION_SOURCE_REVISION = "d568c8c09630de097a046763c17b9ea99f95f950"

PRODUCTION_THEOREM_STORE = Path(
    "/storage/milikic/leanfaith/immutable/extractions/"
    "mathlib_d568c8c_manifest_b1831204/theorems/mathlib.jsonl"
)
PRODUCTION_THEOREM_STORE_SHA256 = "7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7"
PRODUCTION_EXTRACTION_MANIFEST = Path(
    "/storage/milikic/leanfaith/immutable/extractions/"
    "mathlib_d568c8c_manifest_b1831204/manifests/mathlib.json"
)
PRODUCTION_EXTRACTION_MANIFEST_SHA256 = (
    "b183120468eb8f88f832d4336c206c14fb5f2a4fd3b9d968165228a6185bad06"
)
PRODUCTION_MATHLIB_PROJECT = Path("/storage/milikic/leanfaith/mathlib4")
PRODUCTION_STORAGE_ROOT = Path("/storage/milikic")

NAMES_FILENAME = "declaration_names.txt"
DRIVER_FILENAME = "MetaSlice2YieldProbe.lean"
STDOUT_FILENAME = "lean.stdout.jsonl"
STDERR_FILENAME = "lean.stderr.txt"
LOG_FILENAME = "lean.log"
SUMMARY_FILENAME = "summary.json"
MANIFEST_FILENAME = "manifest.json"
AUDIT_DRIVER_FILENAME = "MetaSlice2IndependentAudit.lean"
AUDIT_STDOUT_FILENAME = "audit.stdout.jsonl"
AUDIT_STDERR_FILENAME = "audit.stderr.txt"
AUDIT_LOG_FILENAME = "audit.log"

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL_STATUSES = frozenset({"complete", "notfound", "notProp", "error"})
_FAMILY_EVIDENCE_CLASS = {
    "P20": "P-DEF",
    "P21": "P-DEF",
    "P23": "P-SCHEMA",
    "P24": "P-SCHEMA",
}
_P21_OPERATIONS = frozenset({"betaIntroduce", "zetaIntroduce", "betaEliminate", "zetaEliminate"})


class MetaSlice2Error(RuntimeError):
    """A frozen input, execution, or output invariant failed closed."""


@dataclass(frozen=True, slots=True)
class MetaSlice2Config:
    """All inputs and execution semantics for one fresh yield-probe run."""

    output_root: Path
    theorem_store_path: Path
    theorem_store_sha256: str
    extraction_manifest_path: Path
    extraction_manifest_sha256: str
    transform_engine_path: Path
    mathlib_project_path: Path
    expected_source: str = PRODUCTION_SOURCE
    expected_source_revision: str = PRODUCTION_SOURCE_REVISION
    sample_size: int = PRODUCTION_SAMPLE_SIZE
    timeout_seconds: int = PRODUCTION_TIMEOUT_SECONDS
    address_space_bytes: int = PRODUCTION_ADDRESS_SPACE_BYTES
    lean_memory_mb: int = PRODUCTION_LEAN_MEMORY_MB
    enforce_production_bindings: bool = True
    enforce_storage_root: bool = True
    verify_mathlib_revision: bool = True


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Deterministic projection of the extraction onto unique public names."""

    names: tuple[str, ...]
    theorem_rows: int
    eligible_rows: int
    eligible_unique_names: int
    duplicate_eligible_names: int
    excluded_transform_ineligible: int
    excluded_private: int

    def manifest_payload(self) -> dict[str, object]:
        return {
            "selection_domain": SELECTION_DOMAIN,
            "requested_count": len(self.names),
            "theorem_rows": self.theorem_rows,
            "eligible_rows": self.eligible_rows,
            "eligible_unique_names": self.eligible_unique_names,
            "duplicate_eligible_names": self.duplicate_eligible_names,
            "excluded_transform_ineligible": self.excluded_transform_ineligible,
            "excluded_private": self.excluded_private,
            "selected_names_sha256": hashlib.sha256(_names_bytes(self.names)).hexdigest(),
        }


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome returned by a real or fixture Lean executor."""

    returncode: int | None
    timed_out: bool
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class CandidateCertificate:
    """Minimal immutable input to Lean's independent site reconstruction."""

    declaration: str
    family: str
    operation: str
    site_path: str
    candidate_type_hash: str

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.declaration,
            self.family,
            self.operation,
            self.site_path,
            self.candidate_type_hash,
        )


@dataclass(frozen=True, slots=True)
class ParsedProbeOutput:
    """Independently checked primary output plus audit inputs."""

    summary: dict[str, object]
    certificates: tuple[CandidateCertificate, ...]


class LeanExecutor(Protocol):
    """Injectable execution boundary used by the focused CPU tests."""

    def run(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
    ) -> ExecutionResult: ...


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    def group_exists() -> bool:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    if not group_exists():
        if process.poll() is None:
            process.wait()
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while group_exists() and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.05)
    if group_exists():
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.wait()


class SubprocessLeanExecutor:
    """Run Lean locally with closed stdin and file-backed output streams."""

    def run(
        self,
        *,
        command: tuple[str, ...],
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
    ) -> ExecutionResult:
        started = time.monotonic()
        with stdout_path.open("xb") as stdout_handle, stderr_path.open("xb") as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _terminate_process_group(process)
                return ExecutionResult(
                    returncode=None,
                    timed_out=True,
                    elapsed_seconds=time.monotonic() - started,
                )
            except BaseException:
                _terminate_process_group(process)
                raise
        return ExecutionResult(
            returncode=returncode,
            timed_out=False,
            elapsed_seconds=time.monotonic() - started,
        )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def production_config(output_root: Path) -> MetaSlice2Config:
    """Construct the only configuration exposed by the production CLI."""
    return MetaSlice2Config(
        output_root=output_root,
        theorem_store_path=PRODUCTION_THEOREM_STORE,
        theorem_store_sha256=PRODUCTION_THEOREM_STORE_SHA256,
        extraction_manifest_path=PRODUCTION_EXTRACTION_MANIFEST,
        extraction_manifest_sha256=PRODUCTION_EXTRACTION_MANIFEST_SHA256,
        transform_engine_path=_repo_root() / "LeanFaith" / "Meta" / "TransformEngine.lean",
        mathlib_project_path=PRODUCTION_MATHLIB_PROJECT,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _selection_rank(name: str) -> tuple[str, str]:
    return hashlib.sha256(SELECTION_PREFIX + name.encode("utf-8")).hexdigest(), name


def _parse_json_object(text: str, *, context: str) -> dict[str, object]:
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MetaSlice2Error(f"{context} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MetaSlice2Error(f"{context} must be a JSON object with string keys")
    return cast(dict[str, object], value)


def _mapping_field(row: Mapping[str, object], key: str, *, context: str) -> dict[str, object]:
    value = row.get(key)
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value):
        raise MetaSlice2Error(f"{context}.{key} must be an object")
    return cast(dict[str, object], value)


def _list_field(row: Mapping[str, object], key: str, *, context: str) -> list[object]:
    value = row.get(key)
    if not isinstance(value, list):
        raise MetaSlice2Error(f"{context}.{key} must be an array")
    return cast(list[object], value)


def _string_field(row: Mapping[str, object], key: str, *, context: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise MetaSlice2Error(f"{context}.{key} must be a non-empty string")
    return value


def _bool_field(row: Mapping[str, object], key: str, *, context: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise MetaSlice2Error(f"{context}.{key} must be a boolean")
    return value


def _nonnegative_int_field(row: Mapping[str, object], key: str, *, context: str) -> int:
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise MetaSlice2Error(f"{context}.{key} must be a non-negative integer")
    return value


def _hash_field(row: Mapping[str, object], key: str, *, context: str) -> str:
    value = _string_field(row, key, context=context)
    if _HEX64.fullmatch(value) is None:
        raise MetaSlice2Error(f"{context}.{key} must be a lowercase SHA-256")
    return value


def _validate_hash_literal(value: str, *, label: str) -> None:
    if _HEX64.fullmatch(value) is None:
        raise MetaSlice2Error(f"{label} must be a lowercase SHA-256")


def _resolve(path: Path) -> Path:
    return path.resolve(strict=False)


def _require_regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise MetaSlice2Error(f"{label} must be a regular non-symlink file: {path}")


def _validate_config(config: MetaSlice2Config) -> None:
    _validate_hash_literal(config.theorem_store_sha256, label="theorem_store_sha256")
    _validate_hash_literal(
        config.extraction_manifest_sha256,
        label="extraction_manifest_sha256",
    )
    if config.sample_size <= 0:
        raise MetaSlice2Error("sample_size must be positive")
    if config.timeout_seconds <= 0:
        raise MetaSlice2Error("timeout_seconds must be positive")
    if config.address_space_bytes <= 0 or config.lean_memory_mb <= 0:
        raise MetaSlice2Error("Lean memory limits must be positive")
    if not config.expected_source or not config.expected_source_revision:
        raise MetaSlice2Error("source and source revision bindings must be non-empty")
    if config.output_root == config.output_root.parent:
        raise MetaSlice2Error("output root cannot be a filesystem root")
    if config.enforce_storage_root and not _resolve(config.output_root).is_relative_to(
        PRODUCTION_STORAGE_ROOT
    ):
        raise MetaSlice2Error("all yield-probe artifacts must be under /storage/milikic")
    if config.enforce_production_bindings:
        exact_paths = {
            "theorem store": (config.theorem_store_path, PRODUCTION_THEOREM_STORE),
            "extraction manifest": (
                config.extraction_manifest_path,
                PRODUCTION_EXTRACTION_MANIFEST,
            ),
            "mathlib project": (config.mathlib_project_path, PRODUCTION_MATHLIB_PROJECT),
            "transform engine": (
                config.transform_engine_path,
                _repo_root() / "LeanFaith" / "Meta" / "TransformEngine.lean",
            ),
        }
        for label, (actual_path, expected_path) in exact_paths.items():
            if _resolve(actual_path) != _resolve(expected_path):
                raise MetaSlice2Error(f"production {label} binding differs from {expected_path}")
        exact_values: tuple[tuple[str, object, object], ...] = (
            ("theorem store SHA-256", config.theorem_store_sha256, PRODUCTION_THEOREM_STORE_SHA256),
            (
                "extraction manifest SHA-256",
                config.extraction_manifest_sha256,
                PRODUCTION_EXTRACTION_MANIFEST_SHA256,
            ),
            ("source", config.expected_source, PRODUCTION_SOURCE),
            ("source revision", config.expected_source_revision, PRODUCTION_SOURCE_REVISION),
            ("sample size", config.sample_size, PRODUCTION_SAMPLE_SIZE),
            ("timeout", config.timeout_seconds, PRODUCTION_TIMEOUT_SECONDS),
            (
                "address-space limit",
                config.address_space_bytes,
                PRODUCTION_ADDRESS_SPACE_BYTES,
            ),
            ("Lean memory limit", config.lean_memory_mb, PRODUCTION_LEAN_MEMORY_MB),
        )
        for label, actual_value, expected_value in exact_values:
            if actual_value != expected_value:
                raise MetaSlice2Error(f"production {label} binding differs from {expected_value}")
        if not config.enforce_storage_root or not config.verify_mathlib_revision:
            raise MetaSlice2Error("production run cannot disable storage or revision checks")


def _verify_file_hash(path: Path, expected: str, *, label: str) -> None:
    _require_regular_file(path, label=label)
    actual = hash_file(path)
    if actual != expected:
        raise MetaSlice2Error(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")


def _git_revision(path: Path) -> str:
    if path.is_symlink() or not path.is_dir():
        raise MetaSlice2Error(f"git checkout must be a non-symlink directory: {path}")
    completed = subprocess.run(
        ("git", "-C", str(path), "rev-parse", "HEAD"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
        text=True,
    )
    revision = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        error = completed.stderr.strip() or f"exit {completed.returncode}"
        raise MetaSlice2Error(f"cannot read git revision for {path}: {error}")
    return revision


def _git_contains_commit(path: Path, revision: str) -> bool:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        return False
    completed = subprocess.run(
        ("git", "-C", str(path), "cat-file", "-e", f"{revision}^{{commit}}"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.returncode == 0


def _validate_extraction_manifest(config: MetaSlice2Config) -> dict[str, object]:
    _verify_file_hash(
        config.extraction_manifest_path,
        config.extraction_manifest_sha256,
        label="extraction manifest",
    )
    manifest = _parse_json_object(
        config.extraction_manifest_path.read_text(encoding="utf-8"),
        context="extraction manifest",
    )
    if manifest.get("source") != config.expected_source:
        raise MetaSlice2Error("extraction manifest source binding mismatch")
    if manifest.get("source_revision") != config.expected_source_revision:
        raise MetaSlice2Error("extraction manifest source revision mismatch")
    if manifest.get("stage") != "elaborated":
        raise MetaSlice2Error("extraction manifest is not an elaborated artifact")
    if manifest.get("artifact_class") != "production":
        raise MetaSlice2Error("extraction manifest is not a production artifact")
    checksums = _mapping_field(manifest, "output_partition_checksums", context="manifest")
    matching = [
        value
        for key, value in checksums.items()
        if key.endswith("/theorems/mathlib.jsonl") and isinstance(value, str)
    ]
    if matching != [config.theorem_store_sha256]:
        raise MetaSlice2Error("manifest does not bind the exact theorem-store checksum")
    return manifest


def select_declarations(config: MetaSlice2Config) -> SelectionResult:
    """Validate the frozen extraction and select exact names by salted SHA-256."""
    _validate_config(config)
    _verify_file_hash(
        config.theorem_store_path,
        config.theorem_store_sha256,
        label="theorem store",
    )
    manifest = _validate_extraction_manifest(config)

    theorem_rows = 0
    eligible_rows = 0
    excluded_transform_ineligible = 0
    excluded_private = 0
    names: set[str] = set()
    with config.theorem_store_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            theorem_rows += 1
            outer = _parse_json_object(raw_line, context=f"theorem row {line_number}")
            theorem = _mapping_field(outer, "theorem", context=f"theorem row {line_number}")
            context = f"theorem row {line_number}"
            if theorem.get("source") != config.expected_source:
                raise MetaSlice2Error(f"{context} is not public {config.expected_source}")
            if theorem.get("source_revision") != config.expected_source_revision:
                raise MetaSlice2Error(f"{context} has an unexpected source revision")
            declaration = _string_field(theorem, "declaration_full_name", context=context)
            metadata = _mapping_field(theorem, "metadata", context=context)
            if "_private." in declaration:
                excluded_private += 1
                continue
            eligible = (
                theorem.get("is_proposition") is True
                and theorem.get("elaboration_status") == "elaborates"
                and metadata.get("transform_source_eligible") is True
            )
            if not eligible:
                excluded_transform_ineligible += 1
                continue
            if any(character in declaration for character in ("\0", "\n", "\r")):
                raise MetaSlice2Error(f"{context} has a control character in its name")
            eligible_rows += 1
            names.add(declaration)

    row_count = manifest.get("row_count")
    if type(row_count) is not int or row_count != theorem_rows:
        raise MetaSlice2Error("extraction manifest row_count does not match the theorem-store rows")
    ordered = tuple(sorted(names, key=_selection_rank))
    if len(ordered) < config.sample_size:
        raise MetaSlice2Error(
            f"only {len(ordered)} unique eligible public names for sample size {config.sample_size}"
        )
    selected = ordered[: config.sample_size]
    if len(selected) != config.sample_size or len(set(selected)) != config.sample_size:
        raise MetaSlice2Error("selector did not produce the exact unique requested count")
    return SelectionResult(
        names=selected,
        theorem_rows=theorem_rows,
        eligible_rows=eligible_rows,
        eligible_unique_names=len(ordered),
        duplicate_eligible_names=eligible_rows - len(ordered),
        excluded_transform_ineligible=excluded_transform_ineligible,
        excluded_private=excluded_private,
    )


def _config_payload(config: MetaSlice2Config) -> dict[str, object]:
    return {
        "method_version": METHOD_VERSION,
        "selection_domain": SELECTION_DOMAIN,
        "sample_size": config.sample_size,
        "timeout_seconds": config.timeout_seconds,
        "address_space_bytes": config.address_space_bytes,
        "lean_memory_mb": config.lean_memory_mb,
        "expected_source": config.expected_source,
        "expected_source_revision": config.expected_source_revision,
        "theorem_store": {
            "path": str(_resolve(config.theorem_store_path)),
            "sha256": config.theorem_store_sha256,
        },
        "extraction_manifest": {
            "path": str(_resolve(config.extraction_manifest_path)),
            "sha256": config.extraction_manifest_sha256,
        },
        "transform_engine_path": str(_resolve(config.transform_engine_path)),
        "mathlib_project_path": str(_resolve(config.mathlib_project_path)),
        "public_only": True,
        "external_transmission": False,
        "private_source_content": False,
    }


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: object) -> None:
    _write_atomic(path, canonical_json_bytes(value) + b"\n")


def _names_bytes(names: Sequence[str]) -> bytes:
    return ("".join(f"{name}\n" for name in names)).encode("utf-8")


def _engine_helper_body(engine_path: Path) -> str:
    _require_regular_file(engine_path, label="TransformEngine source")
    text = engine_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    import_rows = [index for index, line in enumerate(lines) if line.strip().startswith("import ")]
    imports = [lines[index].strip() for index in import_rows]
    if "import Lean" not in imports or any(
        not imported.startswith("import Lean") for imported in imports
    ):
        raise MetaSlice2Error("TransformEngine imports must be Lean modules supplied by Mathlib")
    body = "\n".join(
        line for index, line in enumerate(lines) if index not in set(import_rows)
    ).strip()
    required_markers = (
        "namespace LeanFaith.Meta.TransformEngineHelper",
        "lfTransformBatch",
        "lfAuditTransform",
        "end LeanFaith.Meta.TransformEngineHelper",
    )
    if not all(marker in body for marker in required_markers):
        raise MetaSlice2Error("TransformEngine helper body lacks the batch-driver contract")
    return body + "\n"


def _driver_bytes(config: MetaSlice2Config, names_path: Path) -> bytes:
    helper_body = _engine_helper_body(config.transform_engine_path)
    names_literal = _lean_string(str(_resolve(names_path)))
    driver = f"import Mathlib\n\n{helper_body}\nlfTransformBatch {names_literal}\n"
    return driver.encode("utf-8")


def _lean_string(value: str) -> str:
    if any(character in value for character in ("\0", "\n", "\r")):
        raise MetaSlice2Error("Lean audit literal contains a forbidden control character")
    return json.dumps(value, ensure_ascii=False)


def _audit_driver_bytes(
    config: MetaSlice2Config,
    certificates: Sequence[CandidateCertificate],
) -> bytes:
    helper_body = _engine_helper_body(config.transform_engine_path)
    commands = []
    for certificate in certificates:
        arguments = " ".join(
            _lean_string(value)
            for value in (
                certificate.declaration,
                certificate.family,
                certificate.operation,
                certificate.site_path,
                certificate.candidate_type_hash,
            )
        )
        commands.append(f"lfAuditTransform {arguments}")
    command_body = "\n".join(commands)
    driver = f"import Mathlib\n\n{helper_body}\n{command_body}\n"
    return driver.encode("utf-8")


def _command(config: MetaSlice2Config, driver_path: Path) -> tuple[str, ...]:
    return (
        "/usr/bin/prlimit",
        f"--as={config.address_space_bytes}",
        "--",
        "lake",
        "env",
        "lean",
        f"-M{config.lean_memory_mb}",
        "-j1",
        str(_resolve(driver_path)),
    )


def _artifact(path: Path) -> dict[str, object]:
    _require_regular_file(path, label="output artifact")
    return {
        "path": str(_resolve(path)),
        "sha256": hash_file(path),
        "size_bytes": path.stat().st_size,
    }


def _artifacts_present(output_root: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in (
        NAMES_FILENAME,
        DRIVER_FILENAME,
        STDOUT_FILENAME,
        STDERR_FILENAME,
        LOG_FILENAME,
        SUMMARY_FILENAME,
        AUDIT_DRIVER_FILENAME,
        AUDIT_STDOUT_FILENAME,
        AUDIT_STDERR_FILENAME,
        AUDIT_LOG_FILENAME,
    ):
        path = output_root / name
        if path.exists() and path.is_file() and not path.is_symlink():
            result[name] = _artifact(path)
    return result


def _log_bytes(command: Sequence[str], stdout_path: Path, stderr_path: Path) -> bytes:
    stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
    stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
    command_line = json.dumps(list(command), ensure_ascii=False, separators=(",", ":"))
    return (
        f"command={command_line}\n--- stdout ---\n".encode()
        + stdout
        + b"\n--- stderr ---\n"
        + stderr
    )


def _candidate_row(
    row: Mapping[str, object],
    *,
    context: str,
    selected: frozenset[str],
) -> dict[str, object]:
    if row.get("schemaVersion") != 2:
        raise MetaSlice2Error(f"{context}.schemaVersion must be 2")
    declaration = _string_field(row, "declaration", context=context)
    if declaration not in selected:
        raise MetaSlice2Error(f"{context} names an unrequested declaration")
    if row.get("recordKind") != "candidate":
        raise MetaSlice2Error(f"{context}.recordKind must be candidate")
    if row.get("status") != "ok":
        raise MetaSlice2Error(f"{context}.status must be ok")
    family = _string_field(row, "family", context=context)
    evidence_class = _string_field(row, "evidenceClass", context=context)
    expected_class = _FAMILY_EVIDENCE_CLASS.get(family)
    if expected_class is None:
        raise MetaSlice2Error(f"{context} has unsupported family {family}")
    if evidence_class != expected_class:
        raise MetaSlice2Error(f"{context} evidence class {evidence_class} does not match {family}")
    operation = _string_field(row, "operation", context=context)
    operation_kind = _string_field(row, "operationKind", context=context)
    if family == "P20":
        operation_valid = operation.startswith("unfold:") and len(operation) > len("unfold:")
        expected_operation_kind = "unfold"
    elif family == "P21":
        operation_valid = operation in _P21_OPERATIONS
        expected_operation_kind = operation
    elif family == "P23":
        operation_valid = re.fullmatch(r"(?:curry|uncurry):[0-9]+", operation) is not None
        expected_operation_kind = operation.split(":", maxsplit=1)[0]
    else:
        operation_valid = re.fullmatch(r"swapAdjacent:[0-9]+", operation) is not None
        expected_operation_kind = "swapAdjacent"
    if not operation_valid or operation_kind != expected_operation_kind:
        raise MetaSlice2Error(f"{context} has a family/operationKind mismatch")
    site_path = _string_field(row, "sitePath", context=context)
    if not site_path.startswith("/"):
        raise MetaSlice2Error(f"{context}.sitePath must be a stable absolute coordinate")
    binder_depth = _nonnegative_int_field(row, "binderDepth", context=context)
    nested_site = _bool_field(row, "nestedSite", context=context)
    if nested_site != (site_path != "/" or binder_depth != 0):
        raise MetaSlice2Error(f"{context}.nestedSite does not match path/depth")
    source = _string_field(row, "source", context=context)
    candidate = _string_field(row, "candidate", context=context)
    if source == candidate:
        raise MetaSlice2Error(f"{context} does not change the pretty-printed type")
    if _string_field(row, "sourcePretty", context=context) != source:
        raise MetaSlice2Error(f"{context}.sourcePretty alias differs from source")
    if _string_field(row, "candidatePretty", context=context) != candidate:
        raise MetaSlice2Error(f"{context}.candidatePretty alias differs from candidate")
    source_hash = _hash_field(row, "sourceTypeHash", context=context)
    candidate_hash = _hash_field(row, "candidateTypeHash", context=context)
    if source_hash != _sha256_text(source):
        raise MetaSlice2Error(f"{context}.sourceTypeHash failed independent SHA-256 audit")
    if candidate_hash != _sha256_text(candidate):
        raise MetaSlice2Error(f"{context}.candidateTypeHash failed independent SHA-256 audit")
    if source_hash == candidate_hash:
        raise MetaSlice2Error(f"{context} source/candidate hashes are identical")
    source_site = _string_field(row, "sourceSite", context=context)
    candidate_site = _string_field(row, "candidateSite", context=context)
    source_site_hash = _hash_field(row, "sourceSiteHash", context=context)
    candidate_site_hash = _hash_field(row, "candidateSiteHash", context=context)
    if source_site_hash != _sha256_text(source_site):
        raise MetaSlice2Error(f"{context}.sourceSiteHash failed independent SHA-256 audit")
    if candidate_site_hash != _sha256_text(candidate_site):
        raise MetaSlice2Error(f"{context}.candidateSiteHash failed independent SHA-256 audit")
    if not _bool_field(row, "candidateElaborates", context=context):
        raise MetaSlice2Error(f"{context} candidate did not elaborate")
    whole_type_defeq = _bool_field(row, "wholeTypeDefEq", context=context)
    if evidence_class == "P-DEF" and not whole_type_defeq:
        raise MetaSlice2Error(f"{context} P-DEF candidate is not whole-type defeq")
    evidence = _mapping_field(row, "evidence", context=context)
    witness = _mapping_field(row, "witness", context=context)
    if witness.get("sourceSiteHash") != source_site_hash:
        raise MetaSlice2Error(f"{context} witness source-site binding differs")
    if witness.get("candidateSiteHash") != candidate_site_hash:
        raise MetaSlice2Error(f"{context} witness candidate-site binding differs")
    if evidence_class == "P-DEF":
        if evidence.get("relation") != "definitionalEquality" or row.get("axioms") != "none":
            raise MetaSlice2Error(f"{context} has malformed P-DEF evidence")
        if evidence.get("wholeTypeDefEqRequired") is not True:
            raise MetaSlice2Error(f"{context} P-DEF evidence does not require whole-type defeq")
    elif row.get("axioms") != "constructive":
        raise MetaSlice2Error(f"{context} has malformed P-SCHEMA axiom evidence")
    if family == "P20":
        constant = _string_field(witness, "constant", context=f"{context}.witness")
        arguments = _list_field(witness, "arguments", context=f"{context}.witness")
        binder_info = _list_field(
            witness,
            "argumentBinderInfo",
            context=f"{context}.witness",
        )
        universe_arguments = _list_field(
            witness,
            "universeArguments",
            context=f"{context}.witness",
        )
        argument_count = _nonnegative_int_field(
            witness,
            "argumentCount",
            context=f"{context}.witness",
        )
        delta_steps = _nonnegative_int_field(
            evidence,
            "deltaSteps",
            context=f"{context}.evidence",
        )
        if (
            operation != f"unfold:{constant}"
            or delta_steps != 1
            or evidence.get("safeDefinition") is not True
            or evidence.get("transparentDefinition") is not True
            or evidence.get("typedSubterm") is not True
            or evidence.get("contextReconstructed") is not True
            or evidence.get("inverseFoldCertified") is not True
            or witness.get("definitionSafety") != "safe"
            or witness.get("reducibility") not in {"reducible", "semireducible"}
            or witness.get("inverseOperation") != "fold"
            or witness.get("inverseUsesPreservedApplication") is not True
            or witness.get("foldSearch") is not False
            or witness.get("unfoldResidualStructuralMatch") is not True
            or witness.get("residualHash") != candidate_site_hash
            or argument_count != len(arguments)
            or argument_count != len(binder_info)
            or not all(isinstance(value, str) and value for value in arguments)
            or not all(
                isinstance(value, str)
                and value
                in {"default", "implicit", "strictImplicit", "instImplicit", "overapplied"}
                for value in binder_info
            )
            or not all(isinstance(value, str) and value for value in universe_arguments)
        ):
            raise MetaSlice2Error(f"{context} lacks the exact no-search inverse-fold certificate")
    elif family == "P21":
        redex_kind = "beta" if operation.startswith("beta") else "zeta"
        direction = "introduce" if operation.endswith("Introduce") else "eliminate"
        expected_residual = source_site_hash if direction == "introduce" else candidate_site_hash
        redex_count = _nonnegative_int_field(
            evidence,
            "redexCount",
            context=f"{context}.evidence",
        )
        if (
            evidence.get("redexKind") != redex_kind
            or redex_count != 1
            or evidence.get("contextReconstructed") is not True
            or witness.get("direction") != direction
            or witness.get("residualRule") != "instantiate1"
            or witness.get("captureFreeByKernelSubstitution") is not True
            or witness.get("residualHash") != expected_residual
        ):
            raise MetaSlice2Error(f"{context} has a malformed beta/zeta residual certificate")
    return {
        "declaration": declaration,
        "family": family,
        "operation": operation,
        "operation_kind": operation_kind,
        "site_path": site_path,
        "binder_depth": binder_depth,
        "nested_site": nested_site,
        "source": source,
        "candidate_hash": candidate_hash,
        "evidence_class": evidence_class,
        "whole_type_defeq": whole_type_defeq,
    }


def _terminal_row(
    row: Mapping[str, object],
    *,
    context: str,
    selected: frozenset[str],
) -> dict[str, object]:
    if row.get("schemaVersion") != 2:
        raise MetaSlice2Error(f"{context}.schemaVersion must be 2")
    declaration = _string_field(row, "declaration", context=context)
    if declaration not in selected:
        raise MetaSlice2Error(f"{context} names an unrequested declaration")
    if row.get("recordKind") != "status":
        raise MetaSlice2Error(f"{context}.recordKind must be status")
    status = _string_field(row, "status", context=context)
    if status not in _TERMINAL_STATUSES:
        raise MetaSlice2Error(f"{context} has unsupported terminal status {status}")
    candidate_count = _nonnegative_int_field(row, "candidateCount", context=context)
    emitted_count = _nonnegative_int_field(row, "emittedCount", context=context)
    duplicate_count = _nonnegative_int_field(row, "duplicateCount", context=context)
    rejected_count = _nonnegative_int_field(row, "rejectedCount", context=context)
    if candidate_count != emitted_count + duplicate_count:
        raise MetaSlice2Error(f"{context} candidate/emitted/duplicate counts do not reconcile")
    if status != "complete" and (
        candidate_count != 0 or emitted_count != 0 or duplicate_count != 0 or rejected_count != 0
    ):
        raise MetaSlice2Error(f"{context} rejected declaration reports candidates")
    error = row.get("error")
    if error is not None and (not isinstance(error, str) or not error):
        raise MetaSlice2Error(f"{context}.error must be null or a non-empty string")
    if status == "error" and not isinstance(error, str):
        raise MetaSlice2Error(f"{context} error terminal lacks an error message")
    if status == "complete":
        source = _string_field(row, "source", context=context)
        source_hash = _hash_field(row, "sourceTypeHash", context=context)
        if source_hash != _sha256_text(source):
            raise MetaSlice2Error(f"{context}.sourceTypeHash failed independent SHA-256 audit")
        discovered_count = _nonnegative_int_field(row, "discoveredCount", context=context)
        _nonnegative_int_field(row, "pathCount", context=context)
        if discovered_count != candidate_count + rejected_count:
            raise MetaSlice2Error(
                f"{context} discovered/candidate/rejected counts do not reconcile"
            )
    else:
        source = None
        source_hash = None
        discovered_count = 0
    return {
        "declaration": declaration,
        "status": status,
        "candidate_count": candidate_count,
        "emitted_count": emitted_count,
        "duplicate_count": duplicate_count,
        "rejected_count": rejected_count,
        "discovered_count": discovered_count,
        "source": source,
        "source_hash": source_hash,
    }


def _nearest_rank_p95(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _parse_probe_output(
    stdout_path: Path,
    *,
    selection: SelectionResult,
    names_path: Path,
) -> ParsedProbeOutput:
    """Fail-closed audit of line-delimited Lean output and its yield summary."""
    _require_regular_file(stdout_path, label="Lean stdout")
    selected = frozenset(selection.names)
    candidates: list[dict[str, object]] = []
    terminals: dict[str, dict[str, object]] = {}
    seen_candidate_keys: set[tuple[str, str, str, str, str]] = set()
    seen_candidate_hashes: set[tuple[str, str]] = set()
    source_by_declaration: dict[str, str] = {}
    source_hash_by_declaration: dict[str, str] = {}
    batch: dict[str, object] | None = None

    with stdout_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            context = f"Lean stdout line {line_number}"
            row = _parse_json_object(raw_line, context=context)
            kind = row.get("kind")
            if kind == "candidate":
                if batch is not None:
                    raise MetaSlice2Error(f"{context} occurs after the batch terminal")
                parsed = _candidate_row(row, context=context, selected=selected)
                declaration = cast(str, parsed["declaration"])
                if declaration in terminals:
                    raise MetaSlice2Error(
                        f"{context} emits a candidate after its declaration terminal"
                    )
                family = cast(str, parsed["family"])
                operation = cast(str, parsed["operation"])
                site_path = cast(str, parsed["site_path"])
                candidate_hash = cast(str, parsed["candidate_hash"])
                key = (declaration, family, operation, site_path, candidate_hash)
                if key in seen_candidate_keys:
                    raise MetaSlice2Error(f"{context} duplicates an emitted candidate")
                seen_candidate_keys.add(key)
                declaration_hash = (declaration, candidate_hash)
                if declaration_hash in seen_candidate_hashes:
                    raise MetaSlice2Error(
                        f"{context} repeats a candidate hash that Lean should deduplicate"
                    )
                seen_candidate_hashes.add(declaration_hash)
                source = cast(str, parsed["source"])
                source_hash = _hash_field(row, "sourceTypeHash", context=context)
                previous_source = source_by_declaration.setdefault(declaration, source)
                previous_hash = source_hash_by_declaration.setdefault(declaration, source_hash)
                if previous_source != source or previous_hash != source_hash:
                    raise MetaSlice2Error(
                        f"{context} changes the source type within one declaration"
                    )
                candidates.append(parsed)
            elif kind == "terminal":
                if batch is not None:
                    raise MetaSlice2Error(f"{context} occurs after the batch terminal")
                parsed_terminal = _terminal_row(row, context=context, selected=selected)
                declaration = cast(str, parsed_terminal["declaration"])
                if declaration in terminals:
                    raise MetaSlice2Error(f"{context} duplicates a terminal declaration")
                terminals[declaration] = parsed_terminal
            elif kind == "batch":
                if batch is not None:
                    raise MetaSlice2Error(f"{context} duplicates the batch terminal")
                if row.get("recordKind") != "batch":
                    raise MetaSlice2Error(f"{context}.recordKind must be batch")
                if row.get("schemaVersion") != 2:
                    raise MetaSlice2Error(f"{context}.schemaVersion must be 2")
                status = _string_field(row, "status", context=context)
                if status != "complete":
                    raise MetaSlice2Error(
                        f"{context} reports non-complete batch status {status}: {row.get('error')}"
                    )
                if row.get("namesFile") != str(_resolve(names_path)):
                    raise MetaSlice2Error(f"{context} namesFile differs from the bound input")
                declaration_count = _nonnegative_int_field(row, "declarationCount", context=context)
                completed_count = _nonnegative_int_field(row, "completedCount", context=context)
                failed_count = _nonnegative_int_field(row, "failedCount", context=context)
                if (
                    declaration_count != len(selection.names)
                    or completed_count != len(selection.names)
                    or failed_count != 0
                ):
                    raise MetaSlice2Error(f"{context} batch counts do not reconcile")
                batch = {
                    "status": status,
                    "declaration_count": declaration_count,
                    "completed_count": completed_count,
                    "failed_count": failed_count,
                }
            else:
                raise MetaSlice2Error(f"{context} has unknown kind {kind!r}")

    if batch is None:
        raise MetaSlice2Error("Lean stdout lacks the batch terminal")
    if set(terminals) != selected:
        missing = sorted(selected.difference(terminals))
        extra = sorted(set(terminals).difference(selected))
        raise MetaSlice2Error(
            f"terminal declarations do not reconcile: missing={missing[:5]}, extra={extra[:5]}"
        )
    derived_completed = sum(row["status"] == "complete" for row in terminals.values())
    derived_failed = len(terminals) - derived_completed
    if batch["completed_count"] != derived_completed or batch["failed_count"] != derived_failed:
        raise MetaSlice2Error("batch counts contradict per-declaration terminal statuses")
    emitted_by_declaration = Counter(cast(str, row["declaration"]) for row in candidates)
    for declaration, terminal in terminals.items():
        emitted_count = cast(int, terminal["emitted_count"])
        if emitted_by_declaration[declaration] != emitted_count:
            raise MetaSlice2Error(
                f"terminal emittedCount differs from candidate lines for {declaration}"
            )
        if emitted_count > 0 and terminal["status"] != "complete":
            raise MetaSlice2Error(f"candidates belong to rejected declaration {declaration}")
        candidate_source = source_by_declaration.get(declaration)
        candidate_source_hash = source_hash_by_declaration.get(declaration)
        if candidate_source is not None and (
            terminal["status"] != "complete"
            or terminal.get("source") != candidate_source
            or terminal.get("source_hash") != candidate_source_hash
        ):
            raise MetaSlice2Error(f"candidate/terminal source binding differs for {declaration}")

    family_counts = Counter(cast(str, row["family"]) for row in candidates)
    operation_counts = Counter(cast(str, row["operation_kind"]) for row in candidates)
    family_operation_counts = Counter(
        f"{row['family']}:{row['operation_kind']}" for row in candidates
    )
    evidence_counts = Counter(cast(str, row["evidence_class"]) for row in candidates)
    terminal_status_counts = Counter(cast(str, row["status"]) for row in terminals.values())
    nested_count = sum(cast(bool, row["nested_site"]) for row in candidates)
    duplicate_count = sum(cast(int, row["duplicate_count"]) for row in terminals.values())
    rejected_count = sum(cast(int, row["rejected_count"]) for row in terminals.values())
    discovered_count = sum(cast(int, row["discovered_count"]) for row in terminals.values())
    emitted_counts = [
        cast(int, terminals[declaration]["emitted_count"]) for declaration in selection.names
    ]
    covered = sum(count > 0 for count in emitted_counts)
    total = len(candidates)
    summary: dict[str, object] = {
        "method_version": METHOD_VERSION,
        "selected_declaration_count": len(selection.names),
        "terminal_declaration_count": len(terminals),
        "total_candidate_count": total,
        "validated_candidate_count": total + duplicate_count,
        "discovered_candidate_count": discovered_count,
        "duplicate_rejection_count": duplicate_count,
        "validation_rejection_count": rejected_count,
        "per_family_counts": dict(sorted(family_counts.items())),
        "per_operation_counts": dict(sorted(operation_counts.items())),
        "per_family_operation_counts": dict(sorted(family_operation_counts.items())),
        "evidence_class_counts": dict(sorted(evidence_counts.items())),
        "terminal_status_counts": dict(sorted(terminal_status_counts.items())),
        "rejection_counts": {
            "duplicate_candidate": duplicate_count,
            "candidate_validation": rejected_count,
            "terminal_error": terminal_status_counts["error"],
            "terminal_notProp": terminal_status_counts["notProp"],
            "terminal_notfound": terminal_status_counts["notfound"],
        },
        "declaration_coverage": {
            "with_candidate": covered,
            "without_candidate": len(selection.names) - covered,
            "share": covered / len(selection.names),
        },
        "nested_candidates": {
            "count": nested_count,
            "share": nested_count / total if total else 0.0,
        },
        "candidate_count_distribution": {
            "mean": statistics.fmean(emitted_counts),
            "median": float(statistics.median(emitted_counts)),
            "p95": _nearest_rank_p95(emitted_counts),
            "max": max(emitted_counts, default=0),
        },
        "batch": batch,
        "selection": selection.manifest_payload(),
    }
    certificates = tuple(
        CandidateCertificate(
            declaration=cast(str, row["declaration"]),
            family=cast(str, row["family"]),
            operation=cast(str, row["operation"]),
            site_path=cast(str, row["site_path"]),
            candidate_type_hash=cast(str, row["candidate_hash"]),
        )
        for row in candidates
    )
    return ParsedProbeOutput(summary=summary, certificates=certificates)


def summarize_lean_output(
    stdout_path: Path,
    *,
    selection: SelectionResult,
    names_path: Path,
) -> dict[str, object]:
    """Fail-closed public summary of the primary line-delimited Lean output."""
    return _parse_probe_output(
        stdout_path,
        selection=selection,
        names_path=names_path,
    ).summary


def verify_audit_output(
    stdout_path: Path,
    *,
    certificates: Sequence[CandidateCertificate],
) -> dict[str, object]:
    """Require exact successful reconstruction for every emitted certificate."""
    _require_regular_file(stdout_path, label="Lean audit stdout")
    expected = {certificate.key for certificate in certificates}
    if len(expected) != len(certificates):
        raise MetaSlice2Error("primary candidates do not have unique audit keys")
    observed: set[tuple[str, str, str, str, str]] = set()
    with stdout_path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            context = f"Lean audit stdout line {line_number}"
            row = _parse_json_object(raw_line, context=context)
            if row.get("schemaVersion") != 2:
                raise MetaSlice2Error(f"{context}.schemaVersion must be 2")
            if row.get("kind") != "audit" or row.get("recordKind") != "audit":
                raise MetaSlice2Error(f"{context} is not an audit record")
            declaration = _string_field(row, "declaration", context=context)
            family = _string_field(row, "family", context=context)
            operation = _string_field(row, "operation", context=context)
            site_path = _string_field(row, "sitePath", context=context)
            expected_hash = _hash_field(row, "expectedCandidateTypeHash", context=context)
            actual_hash = _hash_field(row, "actualCandidateTypeHash", context=context)
            key = (declaration, family, operation, site_path, expected_hash)
            if key not in expected:
                raise MetaSlice2Error(f"{context} does not match an emitted candidate")
            if key in observed:
                raise MetaSlice2Error(f"{context} duplicates an independent audit key")
            observed.add(key)
            if actual_hash != expected_hash:
                raise MetaSlice2Error(f"{context} reconstructed a different candidate hash")
            if not _bool_field(row, "verified", context=context):
                raise MetaSlice2Error(f"{context} did not independently verify")
            inverse_fold_verified = _bool_field(
                row,
                "inverseFoldVerified",
                context=context,
            )
            if inverse_fold_verified != (family == "P20"):
                raise MetaSlice2Error(f"{context} has an invalid inverse-fold audit result")
            if row.get("status") != "verified" or row.get("reason") != "verified":
                raise MetaSlice2Error(f"{context} has a non-verified terminal status")
            if row.get("auditMode") != "independent-site-reconstruction":
                raise MetaSlice2Error(f"{context} used an unexpected audit mode")
    if observed != expected:
        missing = sorted(expected.difference(observed))
        raise MetaSlice2Error(f"independent audit is missing {len(missing)} candidate records")
    return {
        "mode": "independent-site-reconstruction",
        "requested_count": len(certificates),
        "verified_count": len(observed),
        "failed_count": 0,
        "coverage": 1.0,
    }


def _manifest_base(config: MetaSlice2Config, *, started_at: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "status": "running",
        "started_at": started_at,
        "config": _config_payload(config),
        "privacy": {
            "public_only": True,
            "private_source_content": False,
            "external_transmission": False,
        },
    }


def _create_fresh_output_root(config: MetaSlice2Config) -> None:
    parent = config.output_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    if config.output_root.exists() or config.output_root.is_symlink():
        raise MetaSlice2Error(f"output root must be fresh: {config.output_root}")
    config.output_root.mkdir(mode=0o700)


def run_meta_slice2(
    config: MetaSlice2Config,
    *,
    executor: LeanExecutor | None = None,
) -> dict[str, object]:
    """Create and execute one fresh, atomic-manifest Meta slice-2 yield probe."""
    _validate_config(config)
    _create_fresh_output_root(config)
    manifest_path = config.output_root / MANIFEST_FILENAME
    manifest = _manifest_base(config, started_at=_utc_now())
    _write_json_atomic(manifest_path, manifest)

    try:
        selection = select_declarations(config)
        _require_regular_file(config.transform_engine_path, label="TransformEngine source")
        if config.mathlib_project_path.is_symlink() or not config.mathlib_project_path.is_dir():
            raise MetaSlice2Error("mathlib project must be a non-symlink directory")
        mathlib_revision = (
            _git_revision(config.mathlib_project_path)
            if config.verify_mathlib_revision
            else config.expected_source_revision
        )
        if mathlib_revision != config.expected_source_revision:
            raise MetaSlice2Error(
                "mathlib checkout revision differs from the extraction source revision"
            )
        repository_revision = _git_revision(_repo_root())

        names_path = config.output_root / NAMES_FILENAME
        driver_path = config.output_root / DRIVER_FILENAME
        stdout_path = config.output_root / STDOUT_FILENAME
        stderr_path = config.output_root / STDERR_FILENAME
        log_path = config.output_root / LOG_FILENAME
        audit_driver_path = config.output_root / AUDIT_DRIVER_FILENAME
        audit_stdout_path = config.output_root / AUDIT_STDOUT_FILENAME
        audit_stderr_path = config.output_root / AUDIT_STDERR_FILENAME
        audit_log_path = config.output_root / AUDIT_LOG_FILENAME
        summary_path = config.output_root / SUMMARY_FILENAME
        _write_atomic(names_path, _names_bytes(selection.names))
        _write_atomic(driver_path, _driver_bytes(config, names_path))
        command = _command(config, driver_path)
        manifest.update(
            {
                "selection": selection.manifest_payload(),
                "source_state": {
                    "repository_git_revision": repository_revision,
                    "mathlib_git_revision": mathlib_revision,
                    "runner_sha256": hash_file(Path(__file__)),
                    "transform_engine_sha256": hash_file(config.transform_engine_path),
                },
                "execution": {
                    "primary": {
                        "command": list(command),
                        "cwd": str(_resolve(config.mathlib_project_path)),
                        "timeout_seconds": config.timeout_seconds,
                        "stdin": "closed",
                    }
                },
                "outputs": _artifacts_present(config.output_root),
            }
        )
        _write_json_atomic(manifest_path, manifest)

        active_executor = executor or SubprocessLeanExecutor()
        result = active_executor.run(
            command=command,
            cwd=config.mathlib_project_path,
            timeout_seconds=config.timeout_seconds,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        if not stdout_path.is_file():
            _write_atomic(stdout_path, b"")
        if not stderr_path.is_file():
            _write_atomic(stderr_path, b"")
        _write_atomic(log_path, _log_bytes(command, stdout_path, stderr_path))
        primary_execution: dict[str, object] = {
            "command": list(command),
            "cwd": str(_resolve(config.mathlib_project_path)),
            "timeout_seconds": config.timeout_seconds,
            "stdin": "closed",
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "elapsed_seconds": result.elapsed_seconds,
        }
        manifest["execution"] = {"primary": primary_execution}
        if result.timed_out:
            raise MetaSlice2Error(f"Lean yield probe timed out after {config.timeout_seconds}s")
        if result.returncode != 0:
            raise MetaSlice2Error(f"Lean yield probe exited with status {result.returncode}")

        parsed = _parse_probe_output(
            stdout_path,
            selection=selection,
            names_path=names_path,
        )
        _write_atomic(
            audit_driver_path,
            _audit_driver_bytes(config, parsed.certificates),
        )
        audit_command = _command(config, audit_driver_path)
        manifest["execution"] = {
            "primary": primary_execution,
            "audit": {
                "command": list(audit_command),
                "cwd": str(_resolve(config.mathlib_project_path)),
                "timeout_seconds": config.timeout_seconds,
                "stdin": "closed",
            },
        }
        manifest["outputs"] = _artifacts_present(config.output_root)
        _write_json_atomic(manifest_path, manifest)
        audit_result = active_executor.run(
            command=audit_command,
            cwd=config.mathlib_project_path,
            timeout_seconds=config.timeout_seconds,
            stdout_path=audit_stdout_path,
            stderr_path=audit_stderr_path,
        )
        if not audit_stdout_path.is_file():
            _write_atomic(audit_stdout_path, b"")
        if not audit_stderr_path.is_file():
            _write_atomic(audit_stderr_path, b"")
        _write_atomic(
            audit_log_path,
            _log_bytes(audit_command, audit_stdout_path, audit_stderr_path),
        )
        audit_execution: dict[str, object] = {
            "command": list(audit_command),
            "cwd": str(_resolve(config.mathlib_project_path)),
            "timeout_seconds": config.timeout_seconds,
            "stdin": "closed",
            "returncode": audit_result.returncode,
            "timed_out": audit_result.timed_out,
            "elapsed_seconds": audit_result.elapsed_seconds,
        }
        manifest["execution"] = {
            "primary": primary_execution,
            "audit": audit_execution,
        }
        if audit_result.timed_out:
            raise MetaSlice2Error(
                f"Lean independent audit timed out after {config.timeout_seconds}s"
            )
        if audit_result.returncode != 0:
            raise MetaSlice2Error(
                f"Lean independent audit exited with status {audit_result.returncode}"
            )
        audit_summary = verify_audit_output(
            audit_stdout_path,
            certificates=parsed.certificates,
        )
        summary = dict(parsed.summary)
        summary["independent_audit"] = audit_summary
        _write_json_atomic(summary_path, summary)
        manifest.update(
            {
                "status": "completed",
                "completed_at": _utc_now(),
                "summary": summary,
                "outputs": _artifacts_present(config.output_root),
            }
        )
        _write_json_atomic(manifest_path, manifest)
        verify_meta_slice2(config)
        return manifest
    except BaseException as exc:
        manifest.update(
            {
                "status": "failure",
                "failed_at": _utc_now(),
                "failure": {"type": type(exc).__name__, "message": str(exc)},
                "outputs": _artifacts_present(config.output_root),
            }
        )
        _write_json_atomic(manifest_path, manifest)
        raise


def _load_completed_manifest(config: MetaSlice2Config) -> dict[str, object]:
    path = config.output_root / MANIFEST_FILENAME
    _require_regular_file(path, label="yield-probe manifest")
    manifest = _parse_json_object(path.read_text(encoding="utf-8"), context="yield-probe manifest")
    if manifest.get("schema_version") != 1 or manifest.get("method_version") != METHOD_VERSION:
        raise MetaSlice2Error("yield-probe manifest schema/method mismatch")
    if manifest.get("status") != "completed":
        raise MetaSlice2Error("yield-probe manifest is not completed")
    if manifest.get("config") != _config_payload(config):
        raise MetaSlice2Error("yield-probe manifest config differs from the verifier config")
    if manifest.get("privacy") != {
        "public_only": True,
        "private_source_content": False,
        "external_transmission": False,
    }:
        raise MetaSlice2Error("yield-probe privacy boundary is not explicit")
    return manifest


def _verify_artifact_inventory(config: MetaSlice2Config, manifest: Mapping[str, object]) -> None:
    recorded = _mapping_field(manifest, "outputs", context="yield-probe manifest")
    expected_names = {
        NAMES_FILENAME,
        DRIVER_FILENAME,
        STDOUT_FILENAME,
        STDERR_FILENAME,
        LOG_FILENAME,
        SUMMARY_FILENAME,
        AUDIT_DRIVER_FILENAME,
        AUDIT_STDOUT_FILENAME,
        AUDIT_STDERR_FILENAME,
        AUDIT_LOG_FILENAME,
    }
    if set(recorded) != expected_names:
        raise MetaSlice2Error("completed manifest does not bind the exact output inventory")
    for name in sorted(expected_names):
        entry = _mapping_field(recorded, name, context="yield-probe outputs")
        path = config.output_root / name
        expected_path = str(_resolve(path))
        if entry.get("path") != expected_path:
            raise MetaSlice2Error(f"output path binding mismatch for {name}")
        expected_hash = _hash_field(entry, "sha256", context=f"output {name}")
        expected_size = _nonnegative_int_field(entry, "size_bytes", context=f"output {name}")
        _require_regular_file(path, label=f"output {name}")
        if path.stat().st_size != expected_size or hash_file(path) != expected_hash:
            raise MetaSlice2Error(f"output artifact drift for {name}")


def verify_meta_slice2(config: MetaSlice2Config) -> dict[str, object]:
    """Replay selection and independently verify one completed output root."""
    _validate_config(config)
    if config.output_root.is_symlink() or not config.output_root.is_dir():
        raise MetaSlice2Error("yield-probe output root must be a non-symlink directory")
    manifest = _load_completed_manifest(config)
    selection = select_declarations(config)
    if manifest.get("selection") != selection.manifest_payload():
        raise MetaSlice2Error("completed manifest selection statistics drifted")
    source_state = _mapping_field(manifest, "source_state", context="yield-probe manifest")
    repository_revision = _string_field(
        source_state,
        "repository_git_revision",
        context="yield-probe source_state",
    )
    if not _git_contains_commit(_repo_root(), repository_revision):
        raise MetaSlice2Error("recorded repository revision is not an available commit")
    if source_state.get("mathlib_git_revision") != config.expected_source_revision:
        raise MetaSlice2Error("recorded mathlib revision differs from the extraction")
    if (
        config.verify_mathlib_revision
        and _git_revision(config.mathlib_project_path) != config.expected_source_revision
    ):
        raise MetaSlice2Error("live mathlib revision differs from the extraction")
    if _hash_field(
        source_state,
        "runner_sha256",
        context="yield-probe source_state",
    ) != hash_file(Path(__file__)):
        raise MetaSlice2Error("current verifier differs from the recorded runner source")
    if _hash_field(
        source_state,
        "transform_engine_sha256",
        context="yield-probe source_state",
    ) != hash_file(config.transform_engine_path):
        raise MetaSlice2Error("current TransformEngine differs from the recorded source")

    names_path = config.output_root / NAMES_FILENAME
    driver_path = config.output_root / DRIVER_FILENAME
    audit_driver_path = config.output_root / AUDIT_DRIVER_FILENAME
    _verify_artifact_inventory(config, manifest)
    if names_path.read_bytes() != _names_bytes(selection.names):
        raise MetaSlice2Error("declaration names differ from deterministic selection")
    if driver_path.read_bytes() != _driver_bytes(config, names_path):
        raise MetaSlice2Error("Lean driver differs from the bound TransformEngine helper body")

    parsed = _parse_probe_output(
        config.output_root / STDOUT_FILENAME,
        selection=selection,
        names_path=names_path,
    )
    if audit_driver_path.read_bytes() != _audit_driver_bytes(config, parsed.certificates):
        raise MetaSlice2Error("audit driver differs from the emitted candidate certificates")

    execution = _mapping_field(manifest, "execution", context="yield-probe manifest")
    for stage, stage_driver in (("primary", driver_path), ("audit", audit_driver_path)):
        stage_execution = _mapping_field(execution, stage, context="yield-probe execution")
        if stage_execution.get("command") != list(_command(config, stage_driver)):
            raise MetaSlice2Error(f"recorded {stage} Lean command differs from the frozen command")
        if stage_execution.get("cwd") != str(_resolve(config.mathlib_project_path)):
            raise MetaSlice2Error(f"recorded {stage} cwd differs from the pinned checkout")
        if stage_execution.get("timeout_seconds") != config.timeout_seconds:
            raise MetaSlice2Error(f"recorded {stage} timeout differs from the frozen timeout")
        if stage_execution.get("stdin") != "closed":
            raise MetaSlice2Error(f"recorded {stage} subprocess did not have closed stdin")
        if stage_execution.get("returncode") != 0 or stage_execution.get("timed_out") is not False:
            raise MetaSlice2Error(f"completed manifest records unsuccessful {stage} Lean")

    audit_summary = verify_audit_output(
        config.output_root / AUDIT_STDOUT_FILENAME,
        certificates=parsed.certificates,
    )
    summary = dict(parsed.summary)
    summary["independent_audit"] = audit_summary
    summary_path = config.output_root / SUMMARY_FILENAME
    recorded_summary = _parse_json_object(
        summary_path.read_text(encoding="utf-8"), context="yield-probe summary"
    )
    if recorded_summary != summary or manifest.get("summary") != summary:
        raise MetaSlice2Error("yield summary differs from independently replayed output")
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    output_root = cast(Path, args.output_root)
    config = production_config(output_root)
    if args.command == "run":
        manifest = run_meta_slice2(config)
        print(json.dumps(manifest["summary"], sort_keys=True))
        return 0
    if args.command == "verify":
        summary = verify_meta_slice2(config)
        print(json.dumps(summary, sort_keys=True))
        return 0
    raise AssertionError(f"unreachable command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
