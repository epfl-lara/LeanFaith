"""Fail-closed active benchmark-registry preflight for LF-016."""

from __future__ import annotations

import datetime
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file
from leanfaith.datasets import (
    BenchmarkRegistryPreflightError,
    DenylistIndex,
    FrozenBenchmark,
    FrozenRegistry,
    load_active_benchmark_registry,
    write_frozen_registry,
)
from leanfaith.datasets.benchmark_signatures import (
    BENCHMARK_SIGNATURE_SCHEMA_VERSION,
    BENCHMARK_SIGNATURE_SELECTION_VERSION,
    BenchmarkSide,
    BenchmarkSignatureRecord,
    BenchmarkSignatureWorkManifest,
    BenchmarkViewStatus,
    build_benchmark_signature_artifact,
)
from leanfaith.lean.protocol import LeanStatus

_UTC = datetime.datetime(2026, 7, 18, 12, tzinfo=datetime.UTC)
_CONTEXT_ID = "ctx:" + "c" * 64
_STATEMENT_ID = "a" * 64
_INPUT_CONTENT_HASH = "6" * 64
_REPRESENTATION_HASH = "d" * 64


def _write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return hash_file(path)


def _write_code_bundle(path: Path, code_tree_hash: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    source = b"# frozen fixture\n"
    payload = json.dumps(
        {
            "schema_version": 1,
            "code_state": {
                "git_revision": "1" * 40,
                "git_dirty": False,
                "code_tree_hash": code_tree_hash,
            },
            "files": [
                {
                    "path": "fixture.py",
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "mode": 0o644,
                }
            ],
        }
    ).encode("utf-8")
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        manifest_info = tarfile.TarInfo("CODE_BUNDLE_MANIFEST.json")
        manifest_info.size = len(payload)
        manifest_info.mtime = 0
        manifest_info.mode = 0o644
        archive.addfile(manifest_info, io.BytesIO(payload))
        source_info = tarfile.TarInfo("fixture.py")
        source_info.size = len(source)
        source_info.mtime = 0
        source_info.mode = 0o644
        archive.addfile(source_info, io.BytesIO(source))
    return hash_file(path)


def _registry(*, active: bool, row_id: str = "test:r1", with_hash: bool = True) -> FrozenRegistry:
    benchmark = FrozenBenchmark(
        registry_key="proofnetverif",
        source_id="PAug/ProofNetVerif",
        revision="b" * 40,
        resolved=True,
        splits={"test": 1},
        row_ids=(row_id,),
        nl_hashes=("1" * 64,),
        text_hashes=("2" * 64,),
        representation_hashes=(_REPRESENTATION_HASH,) if with_hash else (),
    )
    return FrozenRegistry(
        frozen_at=_UTC,
        benchmarks=(benchmark,),
        representation_signatures_appended=active,
    )


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, object], Path, Path, Path]:
    (tmp_path / "PLAN.md").write_text("# fixture\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    base_path = tmp_path / "data" / "base.json"
    active_path = tmp_path / "data" / "active.json"
    base_hash = write_frozen_registry(_registry(active=False, with_hash=False), base_path)
    active_hash = write_frozen_registry(_registry(active=True), active_path)

    record = BenchmarkSignatureRecord(
        schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
        statement_id=_STATEMENT_ID,
        input_content_hash=_INPUT_CONTENT_HASH,
        registry_key="proofnetverif",
        source_id="PAug/ProofNetVerif",
        revision="b" * 40,
        split="test",
        row_id="test:r1",
        side=BenchmarkSide.CANDIDATE,
        context_id=_CONTEXT_ID,
        elaboration_status=LeanStatus.VALID.value,
        headless_hash=_REPRESENTATION_HASH,
        signature_pp_hash=_REPRESENTATION_HASH,
        signature_explicit_hash=_REPRESENTATION_HASH,
        alpha_identity_fingerprint=_REPRESENTATION_HASH,
        view_status={
            "headless": BenchmarkViewStatus.OK,
            "signature_pp": BenchmarkViewStatus.OK,
            "signature_explicit": BenchmarkViewStatus.OK,
            "alpha_identity_fingerprint": BenchmarkViewStatus.OK,
        },
    )
    artifact = build_benchmark_signature_artifact(
        identity_registry_sha256=base_hash,
        context_id=_CONTEXT_ID,
        generated_at=_UTC,
        input_checksums={"input": "f" * 64},
        records=(record,),
        failures=(),
    )
    detailed_path = tmp_path / "external" / "index.json"
    detailed_hash = _write_json(detailed_path, artifact.model_dump(mode="json"))

    input_manifest = BenchmarkSignatureWorkManifest(
        schema_version=BENCHMARK_SIGNATURE_SCHEMA_VERSION,
        selection_version=BENCHMARK_SIGNATURE_SELECTION_VERSION,
        identity_registry_sha256=base_hash,
        context_id=_CONTEXT_ID,
        generated_at=_UTC,
        ordered_inputs=((_STATEMENT_ID, _INPUT_CONTENT_HASH),),
    )
    input_path = tmp_path / "artifacts" / "input.json"
    input_hash = _write_json(input_path, input_manifest.model_dump(mode="json"))
    code_path = tmp_path / "artifacts" / "code.tar.gz"
    code_hash = _write_code_bundle(code_path, "5" * 64)
    accounting = artifact.accounting.model_dump(mode="json")
    manifest: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "benchmark_representation_signatures",
        "selection_version": artifact.selection_version,
        "normalization_version": artifact.normalization_version,
        "generated_at": _UTC.isoformat().replace("+00:00", "Z"),
        "completed_at": (_UTC + datetime.timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "context_id": _CONTEXT_ID,
        "base_registry": {"path": "data/base.json", "sha256": base_hash},
        "active_registry": {"path": "data/active.json", "sha256": active_hash},
        "detailed_index": {
            "uri": str(detailed_path),
            "sha256": detailed_hash,
            "required_for_preflight": True,
        },
        "input_manifest": {
            "uri": str(input_path.relative_to(tmp_path)),
            "sha256": input_hash,
            "statement_count": 1,
        },
        "code_bundle": {
            "uri": str(code_path.relative_to(tmp_path)),
            "sha256": code_hash,
            "code_tree_hash": "5" * 64,
        },
        "accounting": {**accounting, "failures": 0},
        "resolved_benchmarks": {
            "proofnetverif": {"statement_count": 1, "representation_hash_count": 1}
        },
        "unresolved_benchmark_policy": "protected by name",
        "missing_representation_policy": "protected_unknown_never_non_overlap",
    }
    manifest_path = tmp_path / "data" / "benchmarks" / "manifests" / "manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path, manifest, base_path, active_path, detailed_path


def _load_fixture(manifest_path: Path, tmp_path: Path):
    return load_active_benchmark_registry(
        manifest_path,
        repo_root=tmp_path,
        expected_manifest_sha256=hash_file(manifest_path),
    )


def _rewrite_bound_json(
    manifest_path: Path, manifest: dict[str, object], field: str, payload: object
) -> None:
    reference = manifest[field]
    assert isinstance(reference, dict)
    artifact_path = Path(str(reference["uri"]))
    if not artifact_path.is_absolute():
        artifact_path = manifest_path.parents[3] / artifact_path
    reference["sha256"] = _write_json(artifact_path, payload)
    _write_json(manifest_path, manifest)


def test_preflight_loads_verified_active_index(tmp_path: Path) -> None:
    manifest_path, _, base_path, active_path, detailed_path = _fixture(tmp_path)

    loaded = _load_fixture(manifest_path, tmp_path)

    assert loaded.base_registry_path == base_path
    assert loaded.active_registry_path == active_path
    assert loaded.detailed_index_path == detailed_path
    assert loaded.active_registry.representation_signatures_appended
    assert loaded.index.contains_representation(_REPRESENTATION_HASH)
    assert loaded.index.contains_row_id("test:r1", registry_key="proofnetverif")
    assert loaded.index.protects_registry_key("proofnetverif")


def test_repository_manifest_passes_fail_closed_preflight() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    loaded = load_active_benchmark_registry(repo_root=repo_root)

    assert loaded.manifest.accounting.attempted == 14_534
    assert set(loaded.manifest.resolved_benchmarks) == {"formalrx_test", "proofnetverif"}


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        ("detailed_index", "detailed_index is missing"),
        ("input_manifest", "input_manifest is missing"),
        ("code_bundle", "code_bundle is missing"),
    ],
)
def test_preflight_rejects_missing_bound_artifact(
    tmp_path: Path, artifact: str, message: str
) -> None:
    manifest_path, manifest, _, _, _ = _fixture(tmp_path)
    reference = manifest[artifact]
    assert isinstance(reference, dict)
    artifact_path = Path(str(reference["uri"]))
    if not artifact_path.is_absolute():
        artifact_path = tmp_path / artifact_path
    artifact_path.unlink()

    with pytest.raises(BenchmarkRegistryPreflightError, match=message):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_registry_hash_mismatch(tmp_path: Path) -> None:
    manifest_path, manifest, _, _, _ = _fixture(tmp_path)
    active = manifest["active_registry"]
    assert isinstance(active, dict)
    active["sha256"] = "f" * 64
    _write_json(manifest_path, manifest)

    with pytest.raises(BenchmarkRegistryPreflightError, match="active_registry SHA-256 mismatch"):
        _load_fixture(manifest_path, tmp_path)


@pytest.mark.parametrize(
    ("active", "message"),
    [
        (_registry(active=False), "representation_signatures_appended=false"),
        (_registry(active=True, row_id="test:changed"), "changed identity/text fields"),
        (_registry(active=True, with_hash=False), "no representation hashes"),
    ],
)
def test_preflight_rejects_non_additive_or_unsigned_registry(
    tmp_path: Path, active: FrozenRegistry, message: str
) -> None:
    manifest_path, manifest, _, active_path, _ = _fixture(tmp_path)
    active_ref = manifest["active_registry"]
    assert isinstance(active_ref, dict)
    active_ref["sha256"] = write_frozen_registry(active, active_path)
    _write_json(manifest_path, manifest)

    with pytest.raises(BenchmarkRegistryPreflightError, match=message):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_manifest_representation_count_mismatch(tmp_path: Path) -> None:
    manifest_path, manifest, _, _, _ = _fixture(tmp_path)
    resolved = manifest["resolved_benchmarks"]
    assert isinstance(resolved, dict)
    summary = resolved["proofnetverif"]
    assert isinstance(summary, dict)
    summary["representation_hash_count"] = 2
    _write_json(manifest_path, manifest)

    with pytest.raises(BenchmarkRegistryPreflightError, match="representation count mismatch"):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_required_detailed_index_hash_mismatch(tmp_path: Path) -> None:
    manifest_path, _, _, _, detailed_path = _fixture(tmp_path)
    detailed_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(BenchmarkRegistryPreflightError, match="detailed_index SHA-256 mismatch"):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_optional_detailed_index_bypass(tmp_path: Path) -> None:
    manifest_path, manifest, _, _, _ = _fixture(tmp_path)
    detailed = manifest["detailed_index"]
    assert isinstance(detailed, dict)
    detailed["required_for_preflight"] = False
    _write_json(manifest_path, manifest)

    with pytest.raises(BenchmarkRegistryPreflightError, match="required_for_preflight"):
        _load_fixture(manifest_path, tmp_path)


@pytest.mark.parametrize("field", ["schema_version", "selection_version", "context_id"])
def test_preflight_rejects_incomplete_input_manifest(tmp_path: Path, field: str) -> None:
    manifest_path, manifest, _, _, _ = _fixture(tmp_path)
    reference = manifest["input_manifest"]
    assert isinstance(reference, dict)
    input_path = tmp_path / str(reference["uri"])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    payload.pop(field)
    _rewrite_bound_json(manifest_path, manifest, "input_manifest", payload)

    with pytest.raises(BenchmarkRegistryPreflightError, match="canonical"):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_unsupported_input_schema(tmp_path: Path) -> None:
    manifest_path, manifest, _, _, _ = _fixture(tmp_path)
    reference = manifest["input_manifest"]
    assert isinstance(reference, dict)
    input_path = tmp_path / str(reference["uri"])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    _rewrite_bound_json(manifest_path, manifest, "input_manifest", payload)

    with pytest.raises(BenchmarkRegistryPreflightError, match="canonical"):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_input_record_hash_mismatch(tmp_path: Path) -> None:
    manifest_path, manifest, _, _, _ = _fixture(tmp_path)
    reference = manifest["input_manifest"]
    assert isinstance(reference, dict)
    input_path = tmp_path / str(reference["uri"])
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    payload["ordered_inputs"][0][1] = "9" * 64
    _rewrite_bound_json(manifest_path, manifest, "input_manifest", payload)

    with pytest.raises(BenchmarkRegistryPreflightError, match="do not exactly match"):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_noncanonical_detailed_artifact(tmp_path: Path) -> None:
    manifest_path, manifest, _, _, detailed_path = _fixture(tmp_path)
    payload = json.loads(detailed_path.read_text(encoding="utf-8"))
    payload.pop("retrieval_indexes")
    _rewrite_bound_json(manifest_path, manifest, "detailed_index", payload)

    with pytest.raises(BenchmarkRegistryPreflightError, match="canonical"):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_invalid_input_checksum_digest(tmp_path: Path) -> None:
    manifest_path, manifest, _, _, detailed_path = _fixture(tmp_path)
    payload = json.loads(detailed_path.read_text(encoding="utf-8"))
    payload["input_checksums"]["input"] = "not-a-digest"
    _rewrite_bound_json(manifest_path, manifest, "detailed_index", payload)

    with pytest.raises(BenchmarkRegistryPreflightError, match="input_checksums"):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_failure_object_mismatch(tmp_path: Path) -> None:
    manifest_path, manifest, _, _, detailed_path = _fixture(tmp_path)
    payload = json.loads(detailed_path.read_text(encoding="utf-8"))
    payload["records"][0]["failure_codes"] = ["source_invalid"]
    payload["accounting"]["records_with_failures"] = 1
    payload["accounting"]["failure_counts"] = {"source_invalid": 1}
    _rewrite_bound_json(manifest_path, manifest, "detailed_index", payload)

    with pytest.raises(BenchmarkRegistryPreflightError, match="failure objects"):
        _load_fixture(manifest_path, tmp_path)


def test_preflight_rejects_authorization_manifest_hash_mismatch(tmp_path: Path) -> None:
    manifest_path, _, _, _, _ = _fixture(tmp_path)
    auth_path = tmp_path / "reports" / "gates" / "lf_016_authorization.json"
    _write_json(
        auth_path,
        {
            "decision": "pass",
            "lf_016_authorized": True,
            "prerequisites": {
                "benchmark_representation_freeze": {
                    "manifest_path": str(manifest_path.relative_to(tmp_path)),
                    "manifest_sha256": hash_file(manifest_path),
                }
            },
        },
    )
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(
        BenchmarkRegistryPreflightError, match="representation-signature manifest SHA-256 mismatch"
    ):
        load_active_benchmark_registry(
            manifest_path, repo_root=tmp_path, authorization_path=auth_path
        )


def test_frozen_registry_rejects_unsupported_schema_and_bad_hashes() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        FrozenRegistry(
            schema_version=2,
            frozen_at=_UTC,
            benchmarks=(_registry(active=False).benchmarks[0],),
        )
    with pytest.raises(ValidationError, match="nl_hashes"):
        FrozenBenchmark(
            registry_key="bad",
            resolved=False,
            nl_hashes=("not-a-digest",),
        )


def test_frozen_registry_requires_unique_sorted_keys_but_preserves_duplicate_row_ids() -> None:
    first = FrozenBenchmark(registry_key="a", resolved=False, row_ids=("same", "same"))
    second = FrozenBenchmark(registry_key="b", resolved=False)
    registry = FrozenRegistry(frozen_at=_UTC, benchmarks=(first, second))
    assert registry.benchmarks[0].row_ids == ("same", "same")
    with pytest.raises(ValidationError, match="unique registry_key"):
        FrozenRegistry(frozen_at=_UTC, benchmarks=(first, first))


def test_unresolved_benchmark_is_protected_by_name() -> None:
    unresolved = FrozenBenchmark(registry_key="driftbench", resolved=False)
    index = DenylistIndex(FrozenRegistry(frozen_at=_UTC, benchmarks=(unresolved,)))

    assert index.protects_registry_key("driftbench")
    assert not index.protects_registry_key("not_registered")
