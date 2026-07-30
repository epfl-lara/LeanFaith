from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.generation import lf022_extraction_reuse as reuse
from leanfaith.generation.lf022_extraction_reuse import (
    LF022ExtractionReuseArtifactBinding,
    LF022ExtractionReuseAttestationV1,
    LF022ExtractionReuseError,
    LF022ExtractionReusePolicyV1,
    freeze_lf022_extraction_reuse_attestation,
    load_reviewed_lf022_extraction_reuse_policy,
    verify_lf022_extraction_reuse_attestation,
)
from leanfaith.schemas.enums import ArtifactClass, DataStage
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.manifest import CodeState, OutputManifest

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.datetime(2026, 7, 30, tzinfo=datetime.UTC)


def _write_model(
    root: Path,
    relative: str,
    value: object,
) -> LF022ExtractionReuseArtifactBinding:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    return LF022ExtractionReuseArtifactBinding(
        path=relative,
        sha256=hash_file(path),
    )


def _manifest(
    *,
    stage: DataStage,
    revision: str,
    tree: str,
    environment: str,
    context: str,
    input_checksums: dict[str, str],
) -> OutputManifest:
    code = CodeState(
        git_revision=revision,
        git_dirty=False,
        base_git_commit=revision,
        code_tree_hash=tree,
    )
    return OutputManifest(
        stage=stage,
        artifact_class=ArtifactClass.PRODUCTION,
        run_id="run_20260730T000000Z_12345678",
        source="mathlib",
        source_revision=("from_theorem_partition" if stage is DataStage.REPRESENTED else "a" * 40),
        config_hash="1" * 64,
        record_schema_version=1,
        row_count=1,
        attempted_row_count=1,
        input_partition_checksums=input_checksums,
        environment_hash=environment,
        context_hash=context,
        code_tree_hash=tree,
        code=code,
        created_at=NOW,
    )


def _verification_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    LF022ExtractionReuseAttestationV1,
    LF022ExtractionReuseArtifactBinding,
    OutputManifest,
    LF022ExtractionReuseArtifactBinding,
    LF022ExtractionReuseArtifactBinding,
    OutputManifest,
    LF022ExtractionReuseArtifactBinding,
    LF022ExtractionReuseArtifactBinding,
]:
    reviewed_policy, _ = load_reviewed_lf022_extraction_reuse_policy(ROOT)
    old_tree = reviewed_policy.old_code_tree_hash
    new_tree = reviewed_policy.reviewed_representation_code_tree_hash
    old_revision = reviewed_policy.old_git_revision
    new_revision = reviewed_policy.reviewed_representation_git_revision
    theorem_path = tmp_path / "data/theorems.jsonl"
    theorem_path.parent.mkdir(parents=True, exist_ok=True)
    theorem_path.write_bytes(b"{}\n" * reviewed_policy.old_theorem_record_count)
    theorem_binding = LF022ExtractionReuseArtifactBinding(
        path="data/theorems.jsonl",
        sha256=hash_file(theorem_path),
    )
    context_binding = _write_model(tmp_path, "data/contexts.json", {"context": "fixture"})
    frame_binding = _write_model(tmp_path, "data/frame.json", {"frame": "fixture"})
    representation_binding = _write_model(
        tmp_path,
        "data/representations.jsonl",
        {"representation": "fixture"},
    )
    extraction_manifest = _manifest(
        stage=DataStage.ELABORATED,
        revision=old_revision,
        tree=old_tree,
        environment=reviewed_policy.environment_hash,
        context=reviewed_policy.context_hash,
        input_checksums={},
    ).model_copy(
        update={
            "source_revision": reviewed_policy.mathlib_revision,
            "row_count": reviewed_policy.old_theorem_record_count,
            "attempted_row_count": reviewed_policy.old_theorem_record_count,
            "output_partition_checksums": {theorem_binding.path: theorem_binding.sha256},
            "file_checksums": {theorem_binding.path: theorem_binding.sha256},
            "input_partition_checksums": {frame_binding.path: frame_binding.sha256},
        }
    )
    representation_input_path = "/immutable/extraction/theorems.jsonl"
    representation_manifest = _manifest(
        stage=DataStage.REPRESENTED,
        revision=new_revision,
        tree=new_tree,
        environment=reviewed_policy.environment_hash,
        context=reviewed_policy.context_hash,
        input_checksums={representation_input_path: theorem_binding.sha256},
    ).model_copy(
        update={
            "attempted_row_count": reviewed_policy.old_theorem_record_count,
            "output_partition_checksums": {
                "/immutable/representation/records.jsonl": representation_binding.sha256
            },
            "file_checksums": {
                "/immutable/representation/records.jsonl": representation_binding.sha256
            },
        }
    )
    extraction_manifest_binding = _write_model(
        tmp_path,
        "data/extraction-manifest.json",
        extraction_manifest.model_dump(mode="json"),
    )
    representation_manifest_binding = _write_model(
        tmp_path,
        "data/representation-manifest.json",
        representation_manifest.model_dump(mode="json"),
    )
    fixture_policy = reviewed_policy.model_copy(
        update={
            "old_extraction_manifest": extraction_manifest_binding,
            "old_theorem_records": theorem_binding,
            "context_records": context_binding,
            "mathlib_source_frame": frame_binding,
        }
    )
    policy_binding = _write_model(
        tmp_path,
        "configs/reviewed-policy.json",
        fixture_policy.model_dump(mode="json"),
    )
    monkeypatch.setattr(
        reuse,
        "load_reviewed_lf022_extraction_reuse_policy",
        lambda _root: (fixture_policy, policy_binding),
    )
    attesting_revision = "b" * 40
    attesting_tree = "c" * 64
    monkeypatch.setattr(
        reuse,
        "_verify_current_reviewed_paths",
        lambda *_args, **_kwargs: (attesting_revision, attesting_tree),
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "policy": policy_binding.model_dump(mode="json"),
        "decision": reviewed_policy.decision,
        "old_extraction_manifest": extraction_manifest_binding.model_dump(mode="json"),
        "old_theorem_records": theorem_binding.model_dump(mode="json"),
        "context_records": context_binding.model_dump(mode="json"),
        "mathlib_source_frame": frame_binding.model_dump(mode="json"),
        "new_representation_manifest": representation_manifest_binding.model_dump(mode="json"),
        "new_representation_records": representation_binding.model_dump(mode="json"),
        "representation_input_theorem_locator_hash": hash_canonical(
            {
                "schema": "lf022_representation_input_locator_v1",
                "path": representation_input_path,
            }
        ),
        "old_git_revision": old_revision,
        "old_code_tree_hash": old_tree,
        "new_git_revision": new_revision,
        "new_code_tree_hash": new_tree,
        "attesting_git_revision": attesting_revision,
        "attesting_code_tree_hash": attesting_tree,
        "audited_diff_sha256": reviewed_policy.audited_diff_sha256,
        "source": "mathlib",
        "mathlib_revision": reviewed_policy.mathlib_revision,
        "context_hash": reviewed_policy.context_hash,
        "environment_hash": reviewed_policy.environment_hash,
        "public_source_only": True,
        "representation_refresh_only": True,
        "network_execution_authorized": False,
        "semantic_labels_created": False,
        "gate_credit_authorized": False,
    }
    attestation = LF022ExtractionReuseAttestationV1.model_validate(
        {
            **payload,
            "attestation_id": make_id(
                "lf022_extraction_reuse_attestation_v1",
                payload,
            ),
        }
    )
    attestation_binding = _write_model(
        tmp_path,
        "data/attestation.json",
        attestation.model_dump(mode="json"),
    )
    return (
        attestation,
        attestation_binding,
        extraction_manifest,
        extraction_manifest_binding,
        theorem_binding,
        representation_manifest,
        representation_manifest_binding,
        representation_binding,
    )


def test_checked_in_policy_is_digest_pinned_and_git_replayable() -> None:
    policy, binding = load_reviewed_lf022_extraction_reuse_policy(ROOT)

    assert binding.sha256 == reuse.LF022_EXTRACTION_REUSE_POLICY_SHA256
    assert policy.old_extraction_manifest.sha256 == (
        "b183120468eb8f88f832d4336c206c14fb5f2a4fd3b9d968165228a6185bad06"
    )
    assert policy.old_theorem_records.sha256 == (
        "7f1a157bfb818b49d082dcc58de221bdddb67f6e8309554395baeb29850838d7"
    )
    assert policy.old_theorem_record_count == 27_786


def test_policy_tampering_and_critical_diff_tampering_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reuse, "LF022_EXTRACTION_REUSE_POLICY_SHA256", "0" * 64)
    with pytest.raises(LF022ExtractionReuseError, match="code-pinned digest"):
        load_reviewed_lf022_extraction_reuse_policy(ROOT)

    document = json.loads(
        (ROOT / reuse.LF022_EXTRACTION_REUSE_POLICY_PATH).read_text(encoding="utf-8")
    )
    document["audited_diff_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="audited_diff_sha256"):
        LF022ExtractionReusePolicyV1.model_validate(document)


def test_attestation_rejects_wrong_theorem_binding_and_wrong_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verification_fixture(tmp_path, monkeypatch)
    (
        attestation,
        attestation_binding,
        extraction_manifest,
        extraction_manifest_binding,
        theorem_binding,
        representation_manifest,
        representation_manifest_binding,
        representation_binding,
    ) = fixture
    wrong_theorem = theorem_binding.model_copy(update={"sha256": "f" * 64})
    with pytest.raises(LF022ExtractionReuseError, match="artifact bindings differ"):
        verify_lf022_extraction_reuse_attestation(
            repo_root=tmp_path,
            attestation=attestation,
            attestation_binding=attestation_binding,
            extraction_manifest=extraction_manifest,
            extraction_manifest_binding=extraction_manifest_binding,
            theorem_records_binding=wrong_theorem,
            representation_manifest=representation_manifest,
            representation_manifest_binding=representation_manifest_binding,
            representation_records_binding=representation_binding,
        )

    bad_representation_manifest = representation_manifest.model_copy(
        update={"code_tree_hash": "d" * 64}
    )
    with pytest.raises(LF022ExtractionReuseError, match="supplied manifest differs"):
        verify_lf022_extraction_reuse_attestation(
            repo_root=tmp_path,
            attestation=attestation,
            attestation_binding=attestation_binding,
            extraction_manifest=extraction_manifest,
            extraction_manifest_binding=extraction_manifest_binding,
            theorem_records_binding=theorem_binding,
            representation_manifest=bad_representation_manifest,
            representation_manifest_binding=representation_manifest_binding,
            representation_records_binding=representation_binding,
        )


def test_attestation_rejects_wrong_current_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verification_fixture(tmp_path, monkeypatch)
    (
        attestation,
        attestation_binding,
        extraction_manifest,
        extraction_manifest_binding,
        theorem_binding,
        representation_manifest,
        representation_manifest_binding,
        representation_binding,
    ) = fixture
    monkeypatch.setattr(
        reuse,
        "_verify_current_reviewed_paths",
        lambda *_args, **_kwargs: (attestation.attesting_git_revision, "e" * 64),
    )

    with pytest.raises(LF022ExtractionReuseError, match="current attesting code tree"):
        verify_lf022_extraction_reuse_attestation(
            repo_root=tmp_path,
            attestation=attestation,
            attestation_binding=attestation_binding,
            extraction_manifest=extraction_manifest,
            extraction_manifest_binding=extraction_manifest_binding,
            theorem_records_binding=theorem_binding,
            representation_manifest=representation_manifest,
            representation_manifest_binding=representation_manifest_binding,
            representation_records_binding=representation_binding,
        )


def test_attestation_rejects_recomputed_id_for_non_policy_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verification_fixture(tmp_path, monkeypatch)
    (
        attestation,
        _attestation_binding,
        extraction_manifest,
        extraction_manifest_binding,
        theorem_binding,
        representation_manifest,
        representation_manifest_binding,
        representation_binding,
    ) = fixture
    payload = attestation.model_dump(mode="json", exclude={"attestation_id"})
    payload["context_records"] = representation_binding.model_dump(mode="json")
    forged = LF022ExtractionReuseAttestationV1.model_validate(
        {
            **payload,
            "attestation_id": make_id(
                "lf022_extraction_reuse_attestation_v1",
                payload,
            ),
        }
    )
    forged_binding = _write_model(
        tmp_path,
        "data/forged-attestation.json",
        forged.model_dump(mode="json"),
    )

    with pytest.raises(LF022ExtractionReuseError, match="differs from reviewed policy"):
        verify_lf022_extraction_reuse_attestation(
            repo_root=tmp_path,
            attestation=forged,
            attestation_binding=forged_binding,
            extraction_manifest=extraction_manifest,
            extraction_manifest_binding=extraction_manifest_binding,
            theorem_records_binding=theorem_binding,
            representation_manifest=representation_manifest,
            representation_manifest_binding=representation_manifest_binding,
            representation_records_binding=representation_binding,
        )


def test_attestation_rejects_recomputed_manifest_with_nested_code_tree_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verification_fixture(tmp_path, monkeypatch)
    (
        attestation,
        _attestation_binding,
        extraction_manifest,
        extraction_manifest_binding,
        theorem_binding,
        representation_manifest,
        _representation_manifest_binding,
        representation_binding,
    ) = fixture
    bad_code = representation_manifest.code.model_copy(update={"code_tree_hash": "d" * 64})
    bad_manifest = representation_manifest.model_copy(update={"code": bad_code})
    bad_manifest_binding = _write_model(
        tmp_path,
        "data/representation-manifest-bad-nested-tree.json",
        bad_manifest.model_dump(mode="json"),
    )
    payload = attestation.model_dump(mode="json", exclude={"attestation_id"})
    payload["new_representation_manifest"] = bad_manifest_binding.model_dump(mode="json")
    forged = LF022ExtractionReuseAttestationV1.model_validate(
        {
            **payload,
            "attestation_id": make_id(
                "lf022_extraction_reuse_attestation_v1",
                payload,
            ),
        }
    )
    forged_binding = _write_model(
        tmp_path,
        "data/attestation-bad-nested-tree.json",
        forged.model_dump(mode="json"),
    )

    with pytest.raises(LF022ExtractionReuseError, match="manifest provenance differs"):
        verify_lf022_extraction_reuse_attestation(
            repo_root=tmp_path,
            attestation=forged,
            attestation_binding=forged_binding,
            extraction_manifest=extraction_manifest,
            extraction_manifest_binding=extraction_manifest_binding,
            theorem_records_binding=theorem_binding,
            representation_manifest=bad_manifest,
            representation_manifest_binding=bad_manifest_binding,
            representation_records_binding=representation_binding,
        )


def test_freezer_binds_exact_reviewed_pair_and_rejects_theorem_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed_policy, _ = load_reviewed_lf022_extraction_reuse_policy(ROOT)
    theorem_path = tmp_path / "data/theorems.jsonl"
    theorem_path.parent.mkdir(parents=True)
    theorem_path.write_bytes(b"{}\n" * reviewed_policy.old_theorem_record_count)
    theorem_binding = LF022ExtractionReuseArtifactBinding(
        path="data/theorems.jsonl",
        sha256=hash_file(theorem_path),
    )
    context_binding = _write_model(tmp_path, "data/contexts.json", {"context": "fixture"})
    frame_binding = _write_model(tmp_path, "data/frame.json", {"frame": "fixture"})
    representation_binding = _write_model(
        tmp_path,
        "data/representations.jsonl",
        {"representation": "fixture"},
    )
    extraction_manifest = _manifest(
        stage=DataStage.ELABORATED,
        revision=reviewed_policy.old_git_revision,
        tree=reviewed_policy.old_code_tree_hash,
        environment=reviewed_policy.environment_hash,
        context=reviewed_policy.context_hash,
        input_checksums={frame_binding.path: frame_binding.sha256},
    ).model_copy(
        update={
            "source_revision": reviewed_policy.mathlib_revision,
            "row_count": reviewed_policy.old_theorem_record_count,
            "attempted_row_count": reviewed_policy.old_theorem_record_count,
            "output_partition_checksums": {theorem_binding.path: theorem_binding.sha256},
            "file_checksums": {theorem_binding.path: theorem_binding.sha256},
        }
    )
    extraction_binding = _write_model(
        tmp_path,
        "data/extraction-manifest.json",
        extraction_manifest.model_dump(mode="json"),
    )
    representation_manifest = _manifest(
        stage=DataStage.REPRESENTED,
        revision=reviewed_policy.reviewed_representation_git_revision,
        tree=reviewed_policy.reviewed_representation_code_tree_hash,
        environment=reviewed_policy.environment_hash,
        context=reviewed_policy.context_hash,
        input_checksums={"/immutable/extraction/theorems.jsonl": theorem_binding.sha256},
    ).model_copy(
        update={
            "attempted_row_count": reviewed_policy.old_theorem_record_count,
            "output_partition_checksums": {
                representation_binding.path: representation_binding.sha256
            },
            "file_checksums": {representation_binding.path: representation_binding.sha256},
        }
    )
    representation_manifest_binding = _write_model(
        tmp_path,
        "data/representation-manifest.json",
        representation_manifest.model_dump(mode="json"),
    )
    fixture_policy = reviewed_policy.model_copy(
        update={
            "old_extraction_manifest": extraction_binding,
            "old_theorem_records": theorem_binding,
            "context_records": context_binding,
            "mathlib_source_frame": frame_binding,
        }
    )
    fixture_policy_binding = _write_model(
        tmp_path,
        "configs/reviewed-policy.json",
        fixture_policy.model_dump(mode="json"),
    )
    monkeypatch.setattr(
        reuse,
        "load_reviewed_lf022_extraction_reuse_policy",
        lambda _root: (fixture_policy, fixture_policy_binding),
    )
    monkeypatch.setattr(
        reuse,
        "_verify_current_reviewed_paths",
        lambda *_args, **_kwargs: ("b" * 40, "c" * 64),
    )

    attestation = freeze_lf022_extraction_reuse_attestation(
        repo_root=tmp_path,
        extraction_manifest_path=tmp_path / extraction_binding.path,
        theorem_records_path=theorem_path,
        context_records_path=tmp_path / context_binding.path,
        mathlib_source_frame_path=tmp_path / frame_binding.path,
        representation_manifest_path=tmp_path / representation_manifest_binding.path,
        representation_records_path=tmp_path / representation_binding.path,
        output_path=Path("data/reuse-attestation.json"),
    )
    assert attestation.old_theorem_records == theorem_binding
    assert attestation.new_representation_records == representation_binding
    assert not attestation.network_execution_authorized
    assert not attestation.semantic_labels_created

    missing_file_map = representation_manifest.model_copy(update={"file_checksums": {}})
    missing_file_map_binding = _write_model(
        tmp_path,
        "data/representation-manifest-missing-file-map.json",
        missing_file_map.model_dump(mode="json"),
    )
    with pytest.raises(
        LF022ExtractionReuseError,
        match="representation output file must have exactly one checksum binding",
    ):
        freeze_lf022_extraction_reuse_attestation(
            repo_root=tmp_path,
            extraction_manifest_path=tmp_path / extraction_binding.path,
            theorem_records_path=theorem_path,
            context_records_path=tmp_path / context_binding.path,
            mathlib_source_frame_path=tmp_path / frame_binding.path,
            representation_manifest_path=tmp_path / missing_file_map_binding.path,
            representation_records_path=tmp_path / representation_binding.path,
            output_path=Path("data/reuse-attestation-missing-file-map.json"),
        )

    contradictory_file_map = representation_manifest.model_copy(
        update={
            "file_checksums": {
                "/immutable/representation/different-records.jsonl": (representation_binding.sha256)
            }
        }
    )
    contradictory_file_map_binding = _write_model(
        tmp_path,
        "data/representation-manifest-contradictory-file-map.json",
        contradictory_file_map.model_dump(mode="json"),
    )
    with pytest.raises(
        LF022ExtractionReuseError,
        match="representation output checksum maps bind different paths",
    ):
        freeze_lf022_extraction_reuse_attestation(
            repo_root=tmp_path,
            extraction_manifest_path=tmp_path / extraction_binding.path,
            theorem_records_path=theorem_path,
            context_records_path=tmp_path / context_binding.path,
            mathlib_source_frame_path=tmp_path / frame_binding.path,
            representation_manifest_path=tmp_path / contradictory_file_map_binding.path,
            representation_records_path=tmp_path / representation_binding.path,
            output_path=Path("data/reuse-attestation-contradictory-file-map.json"),
        )

    theorem_path.write_bytes(theorem_path.read_bytes() + b"{}\n")
    with pytest.raises(LF022ExtractionReuseError, match="old_theorem_records"):
        freeze_lf022_extraction_reuse_attestation(
            repo_root=tmp_path,
            extraction_manifest_path=tmp_path / extraction_binding.path,
            theorem_records_path=theorem_path,
            context_records_path=tmp_path / context_binding.path,
            mathlib_source_frame_path=tmp_path / frame_binding.path,
            representation_manifest_path=tmp_path / representation_manifest_binding.path,
            representation_records_path=tmp_path / representation_binding.path,
            output_path=Path("data/reuse-attestation-2.json"),
        )


def test_attestation_cli_and_materializer_opt_in_are_explicit() -> None:
    freeze = CliRunner().invoke(
        app,
        ["freeze-lf022-extraction-reuse-attestation", "--help"],
        terminal_width=220,
    )
    materialize = CliRunner().invoke(
        app,
        ["materialize-lf022-public-pool", "--help"],
        terminal_width=220,
    )

    assert freeze.exit_code == 0
    assert "--representation-manifest" in freeze.output
    assert "--theorems" in freeze.output
    assert "--output" in freeze.output
    assert materialize.exit_code == 0
    assert "--extraction-reuse-a" in materialize.output
