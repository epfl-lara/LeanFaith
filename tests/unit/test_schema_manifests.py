"""LF-003: run/output manifests and migration maps (§28.2, §10, §11.12)."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from leanfaith.config.paths import RepoPaths
from leanfaith.schemas import (
    ArtifactClass,
    CodeState,
    DataStage,
    ManifestError,
    MigrationMap,
    OutputManifest,
    RunManifest,
    collect_code_state,
    manifest_hash,
    new_run_id,
    read_manifest,
    run_manifest_path,
    write_manifest,
)

_UTC_NOW = datetime.datetime(2026, 7, 10, 12, 0, 0, tzinfo=datetime.UTC)
_CODE = CodeState(git_revision="a" * 40, git_dirty=False)


def _run_manifest(**overrides: object) -> RunManifest:
    payload: dict[str, object] = {
        "run_id": new_run_id(_UTC_NOW, nonce="deadbeef"),
        "artifact_class": ArtifactClass.SMOKE,
        "command": "leanfaith doctor",
        "argv": ("leanfaith", "doctor"),
        "code": _CODE,
        "created_at": _UTC_NOW,
    }
    payload.update(overrides)
    return RunManifest.model_validate(payload)


def test_new_run_id_format_and_determinism() -> None:
    run_id = new_run_id(_UTC_NOW, nonce="deadbeef")
    assert run_id == "run_20260710T120000Z_deadbeef"
    assert new_run_id(_UTC_NOW, nonce="deadbeef") == run_id


def test_new_run_id_requires_utc() -> None:
    naive = datetime.datetime(2026, 7, 10, 12, 0, 0)
    with pytest.raises(ValueError, match="UTC"):
        new_run_id(naive)


def test_new_run_id_rejects_bad_nonce() -> None:
    with pytest.raises(ValueError, match="nonce"):
        new_run_id(_UTC_NOW, nonce="xyz")


def test_run_manifest_round_trip(tmp_path: Path) -> None:
    manifest = _run_manifest(
        config_hashes={"doctor": "b" * 64},
        seeds={"global": 7},
        measurements={"elapsed_ms": 12.5, "lean_requests": 3},
        status_counts={"valid": 2, "invalid": 1},
    )
    path = tmp_path / "manifest.json"
    digest = write_manifest(manifest, path)
    assert manifest_hash(path) == digest
    loaded = read_manifest(path, RunManifest)
    assert loaded == manifest


def test_write_is_canonical_and_stable(tmp_path: Path) -> None:
    manifest = _run_manifest()
    first = write_manifest(manifest, tmp_path / "a.json")
    second = write_manifest(manifest, tmp_path / "b.json")
    assert first == second
    body = (tmp_path / "a.json").read_bytes()
    assert body.endswith(b"\n")
    assert b"  " not in body  # compact separators


def test_run_manifest_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="UTC"):
        _run_manifest(created_at=datetime.datetime(2026, 7, 10))


def test_run_manifest_rejects_unknown_key(tmp_path: Path) -> None:
    manifest = _run_manifest()
    path = tmp_path / "manifest.json"
    write_manifest(manifest, path)
    corrupted = path.read_text().replace('"notes":""', '"notes":"","extra":1')
    path.write_text(corrupted)
    with pytest.raises(ManifestError, match="extra"):
        read_manifest(path, RunManifest)


def test_read_manifest_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="cannot read"):
        read_manifest(tmp_path / "absent.json", RunManifest)


def test_read_manifest_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(ManifestError, match="invalid JSON"):
        read_manifest(path, RunManifest)


def test_run_id_pattern_enforced() -> None:
    with pytest.raises(ValueError, match="pattern"):
        _run_manifest(run_id="run-oops")


def test_output_manifest_round_trip(tmp_path: Path) -> None:
    manifest = OutputManifest(
        stage=DataStage.RAW,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id=new_run_id(_UTC_NOW, nonce="deadbeef"),
        source="proofnetverif",
        source_revision="main@abc123",
        config_hash="c" * 64,
        record_schema_version=1,
        row_count=3752,
        shard_ids=("shard-000",),
        file_checksums={"shard-000.jsonl.zst": "d" * 64},
        input_manifest_hashes={"probe": "e" * 64},
        code=_CODE,
        created_at=_UTC_NOW,
    )
    path = tmp_path / "out.json"
    write_manifest(manifest, path)
    assert read_manifest(path, OutputManifest) == manifest


def test_output_manifest_rejects_bad_checksum() -> None:
    with pytest.raises(ValueError, match="sha256"):
        OutputManifest(
            stage=DataStage.RAW,
            artifact_class=ArtifactClass.PRODUCTION,
            run_id=new_run_id(_UTC_NOW, nonce="deadbeef"),
            source="s",
            source_revision="r",
            config_hash="c" * 64,
            record_schema_version=1,
            row_count=0,
            file_checksums={"x": "nothex"},
            code=_CODE,
            created_at=_UTC_NOW,
        )


def test_output_manifest_rejects_negative_rows() -> None:
    with pytest.raises(ValueError, match="greater than or equal"):
        OutputManifest(
            stage=DataStage.RAW,
            artifact_class=ArtifactClass.PRODUCTION,
            run_id=new_run_id(_UTC_NOW, nonce="deadbeef"),
            source="s",
            source_revision="r",
            config_hash="c" * 64,
            record_schema_version=1,
            row_count=-1,
            code=_CODE,
            created_at=_UTC_NOW,
        )


def test_migration_map_round_trip(tmp_path: Path) -> None:
    migration = MigrationMap(
        migration_id="theorem_v1_to_v2",
        record_kind="TheoremRecord",
        from_schema_version=1,
        to_schema_version=2,
        id_mapping={"thm:" + "0" * 64: "thm:" + "1" * 64},
        created_at=_UTC_NOW,
    )
    path = tmp_path / "migration.json"
    write_manifest(migration, path)
    assert read_manifest(path, MigrationMap) == migration


def test_run_manifest_path_shape() -> None:
    paths = RepoPaths.discover(Path(__file__).parent)
    run_id = new_run_id(_UTC_NOW, nonce="deadbeef")
    assert run_manifest_path(paths, run_id) == paths.runs / run_id / "manifest.json"


def test_collect_code_state_in_this_repo() -> None:
    paths = RepoPaths.discover(Path(__file__).parent)
    state = collect_code_state(paths.root)
    assert len(state.git_revision) == 40
    assert isinstance(state.git_dirty, bool)


def test_collect_code_state_outside_git(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="code state"):
        collect_code_state(tmp_path)
