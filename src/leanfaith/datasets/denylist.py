"""Benchmark denylist freeze (PLAN.md §19.4, §19.7, LF-013).

Writes ``data/benchmarks/frozen_ids.json`` BEFORE any Phase-4 generation:
exact source IDs, normalized-NL hashes, and raw-text hashes for every
protected benchmark, so contaminated items can be excluded from the problem
pool and no benchmark row leaks into training/calibration/prompts.
Representation-based near-duplicate signatures are appended (never rewritten)
at the end of Phase 3 (§19.4); this module only writes the pre-generation
identity + text signatures.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from pydantic import Field, field_validator, model_validator

from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.manifest import require_utc

FREEZE_SCHEMA_VERSION: Literal[1] = 1
REPRESENTATION_SIGNATURE_MANIFEST_SCHEMA_VERSION = 1
REPRESENTATION_SIGNATURE_MANIFEST_KIND = "benchmark_representation_signatures"
REPRESENTATION_SIGNATURE_MANIFEST_PATH = Path(
    "data/benchmarks/manifests/representation_signatures_v1.json"
)
LF016_AUTHORIZATION_PATH = Path("reports/gates/lf_016_authorization.json")

_WS = re.compile(r"\s+")
_HEX64 = r"^[0-9a-f]{64}$"
_REPRESENTATION_HASH_FIELDS = (
    "headless_hash",
    "signature_pp_hash",
    "signature_explicit_hash",
    "alpha_identity_fingerprint",
)


def normalize_nl(text: str) -> str:
    """Canonical NL form for near-duplicate matching: lowercase, collapse all
    whitespace to single spaces, strip. Deliberately aggressive so trivially
    reformatted problem statements still match."""
    return _WS.sub(" ", text.lower()).strip()


def normalize_lean(text: str) -> str:
    """Canonical Lean-text form: collapse whitespace, strip. Case-preserving
    (identifiers are case-sensitive)."""
    return _WS.sub(" ", text).strip()


def text_hash(normalized: str) -> str:
    return sha256_hex(normalized.encode("utf-8"))


def nl_hash(text: str) -> str:
    return text_hash(normalize_nl(text))


def lean_hash(text: str) -> str:
    return text_hash(normalize_lean(text))


class FrozenBenchmark(StrictModel):
    """One protected benchmark's frozen identity + text signatures."""

    registry_key: str
    source_id: str | None = None
    revision: str | None = None
    role: str = "evaluation_only"
    resolved: bool
    splits: dict[str, int] = Field(default_factory=dict)
    row_ids: tuple[str, ...] = ()
    nl_hashes: tuple[str, ...] = ()
    text_hashes: tuple[str, ...] = ()
    representation_hashes: tuple[str, ...] = ()
    resolution_plan: str = ""

    @model_validator(mode="after")
    def _frozen_values_are_canonical(self) -> FrozenBenchmark:
        if not self.registry_key:
            raise ValueError("registry_key must not be empty")
        if list(self.row_ids) != sorted(self.row_ids):
            raise ValueError("row_ids must be sorted")
        for field_name in ("nl_hashes", "text_hashes", "representation_hashes"):
            values = getattr(self, field_name)
            if list(values) != sorted(set(values)):
                raise ValueError(f"{field_name} must be sorted and unique")
            if any(re.fullmatch(_HEX64, value) is None for value in values):
                raise ValueError(f"{field_name} contains an invalid SHA-256 digest")
        return self

    def all_text_hashes(self) -> frozenset[str]:
        return (
            frozenset(self.nl_hashes)
            | frozenset(self.text_hashes)
            | frozenset(self.representation_hashes)
        )


class FrozenRegistry(StrictModel):
    """The machine-readable denylist written before generation (§19.4)."""

    schema_version: Literal[1] = FREEZE_SCHEMA_VERSION
    frozen_at: datetime.datetime
    policy_version: str = "benchmark_denylist_v1"
    benchmarks: tuple[FrozenBenchmark, ...]
    representation_signatures_appended: bool = False

    _utc = field_validator("frozen_at")(require_utc)

    @model_validator(mode="after")
    def _benchmark_keys_are_canonical(self) -> FrozenRegistry:
        keys = [benchmark.registry_key for benchmark in self.benchmarks]
        if len(keys) != len(set(keys)):
            raise ValueError("benchmarks must have unique registry_key values")
        return self


class RegistryArtifactReference(StrictModel):
    """A repository-relative or absolute frozen-registry artifact."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class UriArtifactReference(StrictModel):
    """A local artifact addressed by a relative, absolute, or ``file:`` URI."""

    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class DetailedIndexReference(UriArtifactReference):
    required_for_preflight: Literal[True]


class InputManifestReference(UriArtifactReference):
    statement_count: int = Field(gt=0)


class CodeBundleReference(UriArtifactReference):
    code_tree_hash: str = Field(pattern=_HEX64)


class AuthorizedBenchmarkRepresentationFreeze(StrictModel):
    manifest_path: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=_HEX64)


class ActiveBenchmarkAuthorization(StrictModel):
    decision: Literal["pass"]
    lf_016_authorized: Literal[True]
    benchmark_representation_freeze: AuthorizedBenchmarkRepresentationFreeze


class ResolvedBenchmarkSignatureSummary(StrictModel):
    statement_count: int = Field(gt=0)
    representation_hash_count: int = Field(gt=0)


class RepresentationSignatureAccounting(StrictModel):
    attempted: int = Field(gt=0)
    elaborated: int = Field(ge=0)
    all_views_ok: int = Field(ge=0)
    records_with_failures: int = Field(ge=0)
    failures: int = Field(ge=0)
    by_benchmark: dict[str, int]
    view_success: dict[str, int]
    failure_counts: dict[str, int]

    @model_validator(mode="after")
    def _counts_reconcile(self) -> RepresentationSignatureAccounting:
        if not self.by_benchmark:
            raise ValueError("accounting.by_benchmark must not be empty")
        if any(not key or count <= 0 for key, count in self.by_benchmark.items()):
            raise ValueError("accounting.by_benchmark keys and counts must be positive")
        if sum(self.by_benchmark.values()) != self.attempted:
            raise ValueError("accounting.by_benchmark does not sum to attempted")
        if self.elaborated > self.attempted:
            raise ValueError("accounting.elaborated exceeds attempted")
        if self.all_views_ok > self.elaborated:
            raise ValueError("accounting.all_views_ok exceeds elaborated")
        if self.records_with_failures > self.attempted:
            raise ValueError("accounting.records_with_failures exceeds attempted")
        if self.records_with_failures > self.failures:
            raise ValueError("records_with_failures exceeds failures")
        if not self.view_success:
            raise ValueError("accounting.view_success must not be empty")
        if any(
            not key or count < 0 or count > self.attempted
            for key, count in self.view_success.items()
        ):
            raise ValueError("accounting.view_success contains an invalid key or count")
        if any(not key or count <= 0 for key, count in self.failure_counts.items()):
            raise ValueError("accounting.failure_counts keys and counts must be positive")
        if sum(self.failure_counts.values()) != self.failures:
            raise ValueError("accounting.failure_counts does not sum to failures")
        return self


class RepresentationSignatureManifest(StrictModel):
    """Fail-closed pointer manifest for the active benchmark denylist."""

    schema_version: Literal[1]
    artifact_kind: Literal["benchmark_representation_signatures"]
    selection_version: str = Field(min_length=1)
    normalization_version: str = Field(min_length=1)
    generated_at: datetime.datetime
    completed_at: datetime.datetime
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    base_registry: RegistryArtifactReference
    active_registry: RegistryArtifactReference
    detailed_index: DetailedIndexReference
    input_manifest: InputManifestReference
    code_bundle: CodeBundleReference
    accounting: RepresentationSignatureAccounting
    resolved_benchmarks: dict[str, ResolvedBenchmarkSignatureSummary]
    unresolved_benchmark_policy: str = Field(min_length=1)
    missing_representation_policy: Literal["protected_unknown_never_non_overlap"]

    @field_validator("generated_at", "completed_at")
    @classmethod
    def _timestamps_are_utc(cls, value: datetime.datetime) -> datetime.datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def _manifest_reconciles(self) -> RepresentationSignatureManifest:
        if self.completed_at < self.generated_at:
            raise ValueError("completed_at precedes generated_at")
        if not self.resolved_benchmarks:
            raise ValueError("resolved_benchmarks must not be empty")
        if any(not key for key in self.resolved_benchmarks):
            raise ValueError("resolved_benchmarks contains an empty registry key")
        expected = {
            key: summary.statement_count for key, summary in self.resolved_benchmarks.items()
        }
        if self.accounting.by_benchmark != expected:
            raise ValueError("resolved_benchmarks counts do not match accounting.by_benchmark")
        if self.input_manifest.statement_count != self.accounting.attempted:
            raise ValueError("input_manifest.statement_count does not match attempted")
        return self


def build_proofnetverif(
    rows_by_split: dict[str, list[dict[str, object]]],
    *,
    source_id: str,
    revision: str,
) -> FrozenBenchmark:
    """Freeze ProofNetVerif: NL statements + reference and candidate Lean.

    §9.3 columns: id, nl_statement, lean4_formalization (reference),
    lean4_prediction (candidate). All three carry contaminating content, so
    NL and both Lean sides are hashed.
    """
    row_ids: list[str] = []
    nl_hashes: set[str] = set()
    text_hashes: set[str] = set()
    splits: dict[str, int] = {}
    for split, rows in sorted(rows_by_split.items()):
        splits[split] = len(rows)
        for row in rows:
            row_ids.append(f"{split}:{row['id']}")
            nl_hashes.add(nl_hash(str(row["nl_statement"])))
            text_hashes.add(lean_hash(str(row["lean4_formalization"])))
            text_hashes.add(lean_hash(str(row["lean4_prediction"])))
    return FrozenBenchmark(
        registry_key="proofnetverif",
        source_id=source_id,
        revision=revision,
        resolved=True,
        splits=splits,
        row_ids=tuple(sorted(row_ids)),
        nl_hashes=tuple(sorted(nl_hashes)),
        text_hashes=tuple(sorted(text_hashes)),
    )


def build_formalrx_test(
    rows: list[dict[str, object]],
    *,
    source_id: str,
    revision: str,
) -> FrozenBenchmark:
    """Freeze the released FormalRx-Test *inputs*.

    The pinned 2026-07-14 artifact withholds diagnoses.  Identity, NL,
    headers, and candidate statements are still sufficient for fail-closed
    contamination exclusion; no label counts are inferred from them.
    """

    row_ids: list[str] = []
    nl_hashes: set[str] = set()
    text_hashes: set[str] = set()
    for row in rows:
        row_ids.append(str(row["idx"]))
        nl_hashes.add(nl_hash(str(row["informal_statement"])))
        text_hashes.add(lean_hash(str(row["header"])))
        text_hashes.add(lean_hash(str(row["formal_statement"])))
    return FrozenBenchmark(
        registry_key="formalrx_test",
        source_id=source_id,
        revision=revision,
        resolved=True,
        splits={"test": len(rows)},
        row_ids=tuple(sorted(row_ids)),
        nl_hashes=tuple(sorted(nl_hashes)),
        text_hashes=tuple(sorted(text_hashes)),
        resolution_plan="gold diagnoses withheld in pinned artifact; inputs frozen only",
    )


def unresolved_benchmark(registry_key: str, resolution_plan: str) -> FrozenBenchmark:
    """A benchmark denylisted by name whose exact identity is resolved before
    it is used (§19.7 / J.6: recorded, never substituted)."""
    return FrozenBenchmark(
        registry_key=registry_key, resolved=False, resolution_plan=resolution_plan
    )


def append_representation_signatures(
    registry: FrozenRegistry,
    registry_key: str,
    representation_hashes: tuple[str, ...],
) -> FrozenRegistry:
    """Additively attach representation-based near-duplicate signatures to one
    benchmark and flip ``representation_signatures_appended`` (§19.4: additive
    and versioned, never a rewrite of the identity/text signatures)."""
    updated = []
    found = False
    for benchmark in registry.benchmarks:
        if benchmark.registry_key == registry_key:
            found = True
            merged = tuple(
                sorted(set(benchmark.representation_hashes) | set(representation_hashes))
            )
            updated.append(benchmark.model_copy(update={"representation_hashes": merged}))
        else:
            updated.append(benchmark)
    if not found:
        raise KeyError(f"benchmark {registry_key!r} not in the frozen registry")
    return registry.model_copy(
        update={
            "benchmarks": tuple(updated),
            "representation_signatures_appended": True,
        }
    )


class DenylistIndex:
    """O(1) membership over frozen NL, Lean, and representation hashes."""

    def __init__(self, registry: FrozenRegistry) -> None:
        self._registry_content_hash = hash_canonical(registry.model_dump(mode="json"))
        self._nl: set[str] = set()
        self._text: set[str] = set()
        self._repr: set[str] = set()
        self._ids: set[str] = set()
        self._ids_by_registry: dict[str, set[str]] = {}
        self._registry_keys: set[str] = set()
        for benchmark in registry.benchmarks:
            self._nl.update(benchmark.nl_hashes)
            self._text.update(benchmark.text_hashes)
            self._repr.update(benchmark.representation_hashes)
            self._ids.update(benchmark.row_ids)
            self._ids_by_registry[benchmark.registry_key] = set(benchmark.row_ids)
            self._registry_keys.add(benchmark.registry_key)

    def contains_row_id(self, row_id: str, *, registry_key: str | None = None) -> bool:
        if registry_key is None:
            return row_id in self._ids
        return row_id in self._ids_by_registry.get(registry_key, set())

    @property
    def registry_content_hash(self) -> str:
        """Canonical hash of the exact frozen registry used to build the index."""

        return self._registry_content_hash

    def protects_registry_key(self, registry_key: str) -> bool:
        """Return true even for unresolved benchmarks protected only by name."""

        return registry_key in self._registry_keys

    def contains_nl(self, text: str) -> bool:
        return nl_hash(text) in self._nl

    def contains_lean(self, text: str) -> bool:
        return lean_hash(text) in self._text

    def contains_representation(self, signature_hash: str) -> bool:
        return signature_hash in self._repr

    def contains_any(self, *, nl: str | None = None, lean: str | None = None) -> bool:
        return (nl is not None and self.contains_nl(nl)) or (
            lean is not None and self.contains_lean(lean)
        )

    def __len__(self) -> int:
        return len(self._nl) + len(self._text) + len(self._repr)


def frozen_ids_path(data_dir: Path) -> Path:
    return data_dir / "benchmarks" / "frozen_ids.json"


def write_frozen_registry(registry: FrozenRegistry, path: Path) -> str:
    """Write the frozen registry as canonical JSON; return its file sha256."""
    from leanfaith.config.hashing import canonical_json_bytes, hash_file

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(registry.model_dump(mode="json")) + b"\n"
    path.write_bytes(payload)
    return hash_file(path)


def load_frozen_registry(path: Path) -> FrozenRegistry:
    data = json.loads(path.read_text(encoding="utf-8"))
    return FrozenRegistry.model_validate(data)


class BenchmarkRegistryPreflightError(RuntimeError):
    """The active benchmark registry is missing, stale, or inconsistent."""


@dataclass(frozen=True)
class ActiveBenchmarkRegistry:
    """Validated active registry and denylist index for LF-016 consumers."""

    manifest_path: Path
    manifest: RepresentationSignatureManifest
    base_registry_path: Path
    active_registry_path: Path
    detailed_index_path: Path
    input_manifest_path: Path
    code_bundle_path: Path
    base_registry: FrozenRegistry
    active_registry: FrozenRegistry
    index: DenylistIndex


def _preflight_path(repo_root: Path, value: str, *, field: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme:
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise BenchmarkRegistryPreflightError(
                f"{field} must be a local path or file URI, got {value!r}"
            )
        path = Path(unquote(parsed.path))
    else:
        path = Path(value)
    return path if path.is_absolute() else repo_root / path


def _load_preflight_json(path: Path, *, field: str) -> object:
    if not path.is_file():
        raise BenchmarkRegistryPreflightError(f"{field} is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkRegistryPreflightError(f"cannot read {field}: {path}: {exc}") from exc


def _verify_preflight_hash(path: Path, expected: str, *, field: str) -> None:
    if not path.is_file():
        raise BenchmarkRegistryPreflightError(f"{field} is missing: {path}")
    actual = hash_file(path)
    if actual != expected:
        raise BenchmarkRegistryPreflightError(
            f"{field} SHA-256 mismatch: expected {expected}, got {actual}: {path}"
        )


def _registry_identity_payload(registry: FrozenRegistry) -> dict[str, object]:
    payload = registry.model_dump(mode="json")
    payload.pop("representation_signatures_appended")
    for benchmark in payload["benchmarks"]:
        benchmark.pop("representation_hashes")
    return payload


def _benchmarks_by_key(registry: FrozenRegistry, *, field: str) -> dict[str, FrozenBenchmark]:
    by_key = {benchmark.registry_key: benchmark for benchmark in registry.benchmarks}
    if len(by_key) != len(registry.benchmarks):
        raise BenchmarkRegistryPreflightError(f"{field} has duplicate benchmark keys")
    return by_key


def _validate_signature_artifacts(
    input_payload: object,
    detailed_payload: object,
    *,
    manifest: RepresentationSignatureManifest,
    base_registry_sha256: str,
    active_by_key: dict[str, FrozenBenchmark],
) -> None:
    # Lazy import is required: benchmark_signatures imports the frozen-registry
    # models from this module. At preflight runtime both modules are initialized.
    from leanfaith.datasets.benchmark_signatures import (
        BenchmarkSignatureArtifact,
        BenchmarkSignatureWorkManifest,
    )

    try:
        input_manifest = BenchmarkSignatureWorkManifest.model_validate(input_payload)
    except Exception as exc:
        raise BenchmarkRegistryPreflightError(
            f"input_manifest is not a canonical BenchmarkSignatureWorkManifest: {exc}"
        ) from exc
    try:
        artifact = BenchmarkSignatureArtifact.model_validate(detailed_payload)
    except Exception as exc:
        raise BenchmarkRegistryPreflightError(
            f"detailed_index is not a canonical BenchmarkSignatureArtifact: {exc}"
        ) from exc

    for field_name, expected in (
        ("selection_version", manifest.selection_version),
        ("normalization_version", manifest.normalization_version),
        ("context_id", manifest.context_id),
        ("identity_registry_sha256", base_registry_sha256),
        ("generated_at", manifest.generated_at),
    ):
        if getattr(input_manifest, field_name) != expected:
            raise BenchmarkRegistryPreflightError(
                f"input_manifest {field_name} does not match the preflight manifest"
            )
        if getattr(artifact, field_name) != expected:
            raise BenchmarkRegistryPreflightError(
                f"detailed_index {field_name} does not match the preflight manifest"
            )

    if len(input_manifest.ordered_inputs) != manifest.input_manifest.statement_count:
        raise BenchmarkRegistryPreflightError(
            "input_manifest ordered_inputs count does not match statement_count"
        )
    artifact_inputs = tuple(
        (record.statement_id, record.input_content_hash) for record in artifact.records
    )
    if input_manifest.ordered_inputs != artifact_inputs:
        raise BenchmarkRegistryPreflightError(
            "input_manifest ordered_inputs do not exactly match detailed_index records"
        )
    if len(artifact.records) != manifest.accounting.attempted:
        raise BenchmarkRegistryPreflightError(
            "detailed_index record count does not match manifest accounting"
        )
    if len(artifact.failures) != manifest.accounting.failures:
        raise BenchmarkRegistryPreflightError(
            "detailed_index failure count does not match manifest accounting"
        )

    expected_accounting = manifest.accounting.model_dump(exclude={"failures"})
    if artifact.accounting.model_dump(mode="json") != expected_accounting:
        raise BenchmarkRegistryPreflightError(
            "detailed_index accounting does not match the preflight manifest"
        )

    statements_by_key: dict[str, int] = {}
    hashes_by_key: dict[str, set[str]] = {}
    for record in artifact.records:
        registry_key = record.registry_key
        if registry_key not in manifest.resolved_benchmarks:
            raise BenchmarkRegistryPreflightError(
                f"detailed_index record has unexpected benchmark key {registry_key!r}"
            )
        statements_by_key[registry_key] = statements_by_key.get(registry_key, 0) + 1
        hashes_by_key.setdefault(registry_key, set()).update(record.representation_hashes())

    expected_statements = {
        key: summary.statement_count for key, summary in manifest.resolved_benchmarks.items()
    }
    if statements_by_key != expected_statements:
        raise BenchmarkRegistryPreflightError(
            "detailed_index benchmark statement counts do not match the manifest"
        )
    for key, summary in manifest.resolved_benchmarks.items():
        digests = hashes_by_key.get(key, set())
        if len(digests) != summary.representation_hash_count:
            raise BenchmarkRegistryPreflightError(
                f"detailed_index representation count mismatch for {key!r}"
            )
        if digests != set(active_by_key[key].representation_hashes):
            raise BenchmarkRegistryPreflightError(
                f"active registry representation hashes do not match detailed_index for {key!r}"
            )


def _load_authorized_manifest_binding(
    repo_root: Path,
    effective_manifest_path: Path,
    authorization_path: Path | None,
) -> str:
    auth_path = authorization_path or LF016_AUTHORIZATION_PATH
    if not auth_path.is_absolute():
        auth_path = repo_root / auth_path
    raw = _load_preflight_json(auth_path, field="LF-016 authorization")
    if not isinstance(raw, dict):
        raise BenchmarkRegistryPreflightError("LF-016 authorization root must be an object")
    prerequisites = raw.get("prerequisites")
    if not isinstance(prerequisites, dict):
        raise BenchmarkRegistryPreflightError(
            "LF-016 authorization prerequisites must be an object"
        )
    freeze = prerequisites.get("benchmark_representation_freeze")
    if not isinstance(freeze, dict):
        raise BenchmarkRegistryPreflightError(
            "LF-016 authorization benchmark_representation_freeze must be an object"
        )
    try:
        binding = ActiveBenchmarkAuthorization.model_validate(
            {
                "decision": raw.get("decision"),
                "lf_016_authorized": raw.get("lf_016_authorized"),
                "benchmark_representation_freeze": {
                    "manifest_path": freeze.get("manifest_path"),
                    "manifest_sha256": freeze.get("manifest_sha256"),
                },
            }
        )
    except Exception as exc:
        raise BenchmarkRegistryPreflightError(
            f"invalid LF-016 benchmark authorization binding: {exc}"
        ) from exc
    authorized_path = _preflight_path(
        repo_root,
        binding.benchmark_representation_freeze.manifest_path,
        field="LF-016 authorization manifest_path",
    )
    if authorized_path.resolve() != effective_manifest_path.resolve():
        raise BenchmarkRegistryPreflightError(
            "LF-016 authorization manifest_path does not match the requested manifest"
        )
    return binding.benchmark_representation_freeze.manifest_sha256


def load_active_benchmark_registry(
    manifest_path: Path | None = None,
    *,
    repo_root: Path | None = None,
    expected_manifest_sha256: str | None = None,
    authorization_path: Path | None = None,
) -> ActiveBenchmarkRegistry:
    """Load the active representation-aware benchmark denylist, fail closed.

    This is the LF-016 preflight boundary.  A consumer receives an index only
    after the pointer manifest, immutable file hashes, additive-only registry
    identity, resolved-benchmark accounting, and required detailed index all
    reconcile.
    """

    root = find_repo_root(repo_root)
    effective_manifest_path = manifest_path or REPRESENTATION_SIGNATURE_MANIFEST_PATH
    if not effective_manifest_path.is_absolute():
        effective_manifest_path = root / effective_manifest_path
    if expected_manifest_sha256 is None:
        expected_manifest_sha256 = _load_authorized_manifest_binding(
            root, effective_manifest_path, authorization_path
        )
    elif re.fullmatch(_HEX64, expected_manifest_sha256) is None:
        raise BenchmarkRegistryPreflightError(
            "expected_manifest_sha256 must be a lowercase SHA-256 digest"
        )
    _verify_preflight_hash(
        effective_manifest_path,
        expected_manifest_sha256,
        field="representation-signature manifest",
    )
    try:
        raw_manifest = _load_preflight_json(
            effective_manifest_path, field="representation-signature manifest"
        )
        manifest = RepresentationSignatureManifest.model_validate(raw_manifest)
    except BenchmarkRegistryPreflightError:
        raise
    except Exception as exc:
        raise BenchmarkRegistryPreflightError(
            f"invalid representation-signature manifest: {effective_manifest_path}: {exc}"
        ) from exc

    base_path = _preflight_path(root, manifest.base_registry.path, field="base_registry.path")
    active_path = _preflight_path(root, manifest.active_registry.path, field="active_registry.path")
    _verify_preflight_hash(base_path, manifest.base_registry.sha256, field="base_registry")
    _verify_preflight_hash(active_path, manifest.active_registry.sha256, field="active_registry")
    input_manifest_path = _preflight_path(
        root, manifest.input_manifest.uri, field="input_manifest.uri"
    )
    _verify_preflight_hash(
        input_manifest_path, manifest.input_manifest.sha256, field="input_manifest"
    )
    input_manifest_payload = _load_preflight_json(input_manifest_path, field="input_manifest")
    code_bundle_path = _preflight_path(root, manifest.code_bundle.uri, field="code_bundle.uri")
    _verify_preflight_hash(code_bundle_path, manifest.code_bundle.sha256, field="code_bundle")
    try:
        validate_code_bundle(code_bundle_path, manifest.code_bundle.code_tree_hash)
    except Exception as exc:
        raise BenchmarkRegistryPreflightError(
            f"code_bundle content validation failed: {exc}"
        ) from exc

    try:
        base_registry = load_frozen_registry(base_path)
        active_registry = load_frozen_registry(active_path)
    except Exception as exc:
        raise BenchmarkRegistryPreflightError(f"cannot validate frozen registries: {exc}") from exc

    if not active_registry.representation_signatures_appended:
        raise BenchmarkRegistryPreflightError(
            "active registry has representation_signatures_appended=false"
        )
    if _registry_identity_payload(base_registry) != _registry_identity_payload(active_registry):
        raise BenchmarkRegistryPreflightError(
            "active registry changed identity/text fields from the base registry"
        )

    base_by_key = _benchmarks_by_key(base_registry, field="base_registry")
    active_by_key = _benchmarks_by_key(active_registry, field="active_registry")
    if set(base_by_key) != set(active_by_key):
        raise BenchmarkRegistryPreflightError(
            "active registry benchmark keys differ from the base registry"
        )
    resolved_keys = {key for key, item in base_by_key.items() if item.resolved}
    if resolved_keys != set(manifest.resolved_benchmarks):
        raise BenchmarkRegistryPreflightError(
            "manifest resolved benchmark keys differ from the base registry"
        )
    for key in sorted(resolved_keys):
        base_benchmark = base_by_key[key]
        active_benchmark = active_by_key[key]
        expected_rows = sum(base_benchmark.splits.values())
        if expected_rows <= 0 or expected_rows != len(base_benchmark.row_ids):
            raise BenchmarkRegistryPreflightError(
                f"base registry row accounting is invalid for {key!r}"
            )
        hashes = active_benchmark.representation_hashes
        if not hashes:
            raise BenchmarkRegistryPreflightError(
                f"active registry has no representation hashes for {key!r}"
            )
        if len(set(hashes)) != len(hashes) or any(
            re.fullmatch(_HEX64, digest) is None for digest in hashes
        ):
            raise BenchmarkRegistryPreflightError(
                f"active registry has invalid representation hashes for {key!r}"
            )
        expected_hash_count = manifest.resolved_benchmarks[key].representation_hash_count
        if len(hashes) != expected_hash_count:
            raise BenchmarkRegistryPreflightError(
                f"active registry representation count mismatch for {key!r}"
            )

    detailed_index_path = _preflight_path(
        root, manifest.detailed_index.uri, field="detailed_index.uri"
    )
    _verify_preflight_hash(
        detailed_index_path,
        manifest.detailed_index.sha256,
        field="detailed_index",
    )
    detailed_payload = _load_preflight_json(detailed_index_path, field="detailed_index")
    _validate_signature_artifacts(
        input_manifest_payload,
        detailed_payload,
        manifest=manifest,
        base_registry_sha256=manifest.base_registry.sha256,
        active_by_key=active_by_key,
    )

    return ActiveBenchmarkRegistry(
        manifest_path=effective_manifest_path,
        manifest=manifest,
        base_registry_path=base_path,
        active_registry_path=active_path,
        detailed_index_path=detailed_index_path,
        input_manifest_path=input_manifest_path,
        code_bundle_path=code_bundle_path,
        base_registry=base_registry,
        active_registry=active_registry,
        index=DenylistIndex(active_registry),
    )
