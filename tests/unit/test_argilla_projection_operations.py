from __future__ import annotations

import datetime
import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.annotation_support.argilla_backend import (
    ArgillaDirectFetchReceiptContentV1,
    ArgillaDirectFetchReceiptV1,
    ArgillaDirectFetchRun,
    ArgillaExpectedResponseV1,
    write_argilla_private_immutable,
)
from leanfaith.annotation_support.argilla_projection import (
    ArgillaProjectionBindingContentV1,
    ArgillaProjectionBindingManifestV1,
    make_argilla_projection_binding_id,
)
from leanfaith.annotation_support.attestation import (
    HumanAnnotationAssignmentContentV1,
    authenticate_human_assignment,
    authentication_key_id,
)
from leanfaith.annotation_support.export import ArtifactBinding, BlindedBundleManifestV1
from leanfaith.cli import argilla_operations
from leanfaith.cli.app import app
from leanfaith.cli.argilla_operations import (
    ArgillaExpectedResponseManifestV1,
    write_argilla_backend_pin,
)
from leanfaith.cli.argilla_projection_operations import (
    ArgillaProjectionOperationError,
    load_argilla_projection_binding,
    load_persisted_argilla_projection,
    project_and_persist_argilla_capture,
    write_argilla_projection_binding,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex

UTC = datetime.UTC
KEY = b"lf023-argilla-projection-operations-key!" * 2
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
DATASET_ID = "22222222-2222-4222-8222-222222222222"
ANNOTATOR_ID = "33333333-3333-4333-8333-333333333333"
ROOT = Path(__file__).resolve().parents[2]
PUBLIC_BUNDLE_MANIFEST = (
    ROOT / "annotation/exports/lf021_prevalence_v1/annotator_manifests/"
    "acffa0f85555b50776b1d1964d96671edf63f1c619ddbca913f1b5ad3a2a7168.json"
)


def _record_id(index: int) -> str:
    return f"44444444-4444-4444-8444-{index:012x}"


def _response_id(index: int) -> str:
    return f"55555555-5555-4555-8555-{index:012x}"


def _public_bundle() -> tuple[BlindedBundleManifestV1, tuple[str, ...]]:
    manifest = BlindedBundleManifestV1.model_validate_json(PUBLIC_BUNDLE_MANIFEST.read_bytes())
    bundle_path = ROOT / manifest.bundle.artifact
    tokens = tuple(
        sorted(
            str(json.loads(line)["opaque_item_token"])
            for line in bundle_path.read_bytes().splitlines()
        )
    )
    assert len(tokens) == len(set(tokens)) == manifest.item_count
    return manifest, tokens


def _write_private_json(path: Path, value: object) -> Path:
    write_argilla_private_immutable(path, canonical_json_bytes(value) + b"\n")
    return path


def _assignment_path(tmp_path: Path) -> tuple[Path, object]:
    public_manifest, _ = _public_bundle()
    content = HumanAnnotationAssignmentContentV1(
        campaign_id="lf021_prevalence_v1",
        round_id="prevalence_round_1",
        annotator_slot="independent_annotator_1",
        annotator_id="expert_1",
        annotator_principal_hash="a" * 64,
        assignment_mode="operator_attested_human",
        backend_id="argilla",
        assigned_at=datetime.datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        public_bundle_manifest=ArtifactBinding(
            artifact=PUBLIC_BUNDLE_MANIFEST.relative_to(ROOT).as_posix(),
            sha256=hash_file(PUBLIC_BUNDLE_MANIFEST),
        ),
        private_linkage_manifest=ArtifactBinding(
            artifact="private-linkage-manifest.json",
            sha256="2" * 64,
        ),
        bundle_manifest_id=public_manifest.manifest_id,
        guideline=ArtifactBinding(
            artifact="annotation/guidelines_v1.md",
            sha256="3" * 64,
        ),
        authentication_key_id=authentication_key_id(KEY),
    )
    assignment = authenticate_human_assignment(content, key=KEY)
    path = _write_private_json(
        tmp_path / "inputs" / "assignment.json",
        assignment.model_dump(mode="json"),
    )
    return path, assignment


def _pin(tmp_path: Path):
    return write_argilla_backend_pin(
        endpoint="https://argilla.internal.example",
        workspace_id=WORKSPACE_ID,
        dataset_id=DATASET_ID,
        annotator_id=ANNOTATOR_ID,
        api_key_env="LF_ARGILLA_PROJECTION_TEST_KEY",
        output_dir=tmp_path / "inputs" / "pins",
    )


def _mapping_path(tmp_path: Path, *, assignment_id: str, pin_id: str) -> Path:
    _, tokens = _public_bundle()
    payload = {
        "schema_version": 1,
        "mapping_kind": "lf023_argilla_record_item_mapping_v1",
        "assignment_id": assignment_id,
        "backend_pin_id": pin_id,
        "item_bindings": [
            {
                "schema_version": 1,
                "opaque_item_token": token,
                "backend_record_id": _record_id(index),
            }
            for index, token in enumerate(tokens, start=1)
        ],
        "record_allocation_only": True,
        "response_values_included": False,
        "semantic_labels_included": False,
        "human_gold_eligible": False,
        "training_eligible": False,
    }
    return _write_private_json(tmp_path / "inputs" / "record-mapping.json", payload)


def _raw_dataset() -> bytes:
    return canonical_json_bytes({"id": DATASET_ID, "workspace_id": WORKSPACE_ID})


def _raw_record(index: int) -> bytes:
    return canonical_json_bytes(
        {
            "id": _record_id(index),
            "dataset_id": DATASET_ID,
            "responses": [
                {
                    "id": _response_id(index),
                    "record_id": _record_id(index),
                    "user_id": ANNOTATOR_ID,
                    "status": "submitted",
                    "inserted_at": "2026-07-28T11:00:00Z",
                    "updated_at": "2026-07-28T11:30:00Z",
                    "values": {
                        "same_claim": {"value": "same_claim"},
                        "relation": {"value": "equivalent"},
                        "confidence": {"value": 5},
                        "rationale": {"value": ""},
                        "reference_issue": {"value": "none"},
                    },
                }
            ],
        }
    )


def _capture(
    tmp_path: Path,
    *,
    pin_result: object,
) -> tuple[Path, Path]:
    pin = pin_result.pin
    capture_root = tmp_path / "capture"
    raw_dataset = _raw_dataset()
    raw_record = _raw_record(1)
    dataset_relative = Path("raw/datasets/dataset.json")
    record_relative = Path("raw/records/record.json")
    dataset_path = capture_root / dataset_relative
    record_path = capture_root / record_relative
    write_argilla_private_immutable(dataset_path, raw_dataset)
    write_argilla_private_immutable(record_path, raw_record)
    receipt_content = ArgillaDirectFetchReceiptContentV1(
        evidence_kind="argilla_backend_origin_submitted_snapshot_v1",
        artifact_class="backend_origin",
        backend_pin_id=pin.pin_id,
        backend_id="argilla",
        endpoint=pin.endpoint,
        workspace_id=pin.workspace_id,
        dataset_id=pin.dataset_id,
        annotator_id=pin.annotator_id,
        backend_record_id=_record_id(1),
        backend_response_id=_response_id(1),
        backend_submission_id=_response_id(1),
        transport_id="argilla_v2_8_rest_get_record_v1",
        fetched_at=datetime.datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        raw_dataset_payload=ArtifactBinding(
            artifact=dataset_relative.as_posix(),
            sha256=sha256_hex(raw_dataset),
        ),
        raw_dataset_payload_size=len(raw_dataset),
        raw_record_payload=ArtifactBinding(
            artifact=record_relative.as_posix(),
            sha256=sha256_hex(raw_record),
        ),
        raw_record_payload_size=len(raw_record),
        backend_origin_transport_verified=True,
        fixture_only=False,
    )
    receipt_id = "lf023_argilla_direct_fetch_receipt_v1:" + hash_canonical(
        {
            "schema": "lf023_argilla_direct_fetch_receipt_v1",
            **receipt_content.model_dump(mode="json"),
        }
    )
    receipt = ArgillaDirectFetchReceiptV1(
        receipt_id=receipt_id,
        **receipt_content.model_dump(mode="python"),
    )
    receipt_path = capture_root / "receipts" / f"{receipt_id.rsplit(':', 1)[-1]}.json"
    _write_private_json(receipt_path, receipt.model_dump(mode="json"))
    expected = ArgillaExpectedResponseV1(
        backend_record_id=_record_id(1),
        backend_response_id=_response_id(1),
        backend_submission_id=_response_id(1),
    )
    expected_manifest = ArgillaExpectedResponseManifestV1(
        manifest_kind="lf023_argilla_expected_responses_v1",
        backend_pin_id=pin.pin_id,
        expected_responses=(expected,),
    )
    expected_raw = canonical_json_bytes(expected_manifest.model_dump(mode="json")) + b"\n"
    run = ArgillaDirectFetchRun(
        receipts=(receipt,),
        receipt_paths=(receipt_path,),
        raw_dataset_payload_paths=(dataset_path,),
        raw_record_payload_paths=(record_path,),
    )
    manifest, manifest_path = argilla_operations._persist_capture_manifest(
        pin=pin,
        pin_raw=pin_result.path.read_bytes(),
        expected_manifest=expected_manifest,
        expected_manifest_raw=expected_raw,
        run=run,
        output_root=capture_root,
    )
    assert manifest.entry_count == 1
    return capture_root, manifest_path


def _binding_setup(tmp_path: Path):
    assignment_path, assignment = _assignment_path(tmp_path)
    pin_result = _pin(tmp_path)
    mapping_path = _mapping_path(
        tmp_path,
        assignment_id=assignment.assignment_id,
        pin_id=pin_result.pin.pin_id,
    )
    result = write_argilla_projection_binding(
        repo_root=ROOT,
        assignment_path=assignment_path,
        public_bundle_manifest_path=PUBLIC_BUNDLE_MANIFEST,
        pin_path=pin_result.path,
        mapping_path=mapping_path,
        output_root=tmp_path / "projection-binding",
    )
    return assignment_path, pin_result, result


def test_pre_response_binding_is_private_content_addressed_and_reloads(
    tmp_path: Path,
) -> None:
    _, _, result = _binding_setup(tmp_path)

    assert result.path.name == f"{result.manifest.manifest_id.rsplit(':', 1)[-1]}.json"
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert result.manifest.record_allocation_only is True
    assert result.manifest.response_values_included is False
    assert result.manifest.human_gold_eligible is False
    assert result.manifest.training_eligible is False
    assert result.manifest.public_bundle_manifest.artifact == (
        PUBLIC_BUNDLE_MANIFEST.relative_to(ROOT).as_posix()
    )
    _, raw, restored = load_argilla_projection_binding(result.path)
    assert restored == result.manifest
    assert sha256_hex(raw) == result.sha256


@pytest.mark.parametrize("duplicate_field", ["opaque_item_token", "backend_record_id"])
def test_binding_rejects_duplicate_mapping_and_pin_mismatch(
    tmp_path: Path,
    duplicate_field: str,
) -> None:
    assignment_path, assignment = _assignment_path(tmp_path)
    pin_result = _pin(tmp_path)
    mapping_path = _mapping_path(
        tmp_path,
        assignment_id=assignment.assignment_id,
        pin_id=pin_result.pin.pin_id,
    )
    payload = json.loads(mapping_path.read_bytes())
    payload["item_bindings"][1][duplicate_field] = payload["item_bindings"][0][duplicate_field]
    duplicate = _write_private_json(tmp_path / "inputs" / "duplicate.json", payload)
    with pytest.raises(ArgillaProjectionOperationError, match="strict schema validation"):
        write_argilla_projection_binding(
            repo_root=ROOT,
            assignment_path=assignment_path,
            public_bundle_manifest_path=PUBLIC_BUNDLE_MANIFEST,
            pin_path=pin_result.path,
            mapping_path=duplicate,
            output_root=tmp_path / "must-not-exist",
        )
    assert not (tmp_path / "must-not-exist").exists()

    payload = json.loads(mapping_path.read_bytes())
    payload["backend_pin_id"] = "lf023_argilla_backend_pin_v1:" + "f" * 64
    wrong_pin = _write_private_json(tmp_path / "inputs" / "wrong-pin.json", payload)
    with pytest.raises(ArgillaProjectionOperationError, match="different backend pin"):
        write_argilla_projection_binding(
            repo_root=ROOT,
            assignment_path=assignment_path,
            public_bundle_manifest_path=PUBLIC_BUNDLE_MANIFEST,
            pin_path=pin_result.path,
            mapping_path=wrong_pin,
            output_root=tmp_path / "must-not-exist",
        )


def test_binding_requires_exact_public_bundle_and_token_membership(tmp_path: Path) -> None:
    assignment_path, assignment = _assignment_path(tmp_path)
    pin_result = _pin(tmp_path)
    mapping_path = _mapping_path(
        tmp_path,
        assignment_id=assignment.assignment_id,
        pin_id=pin_result.pin.pin_id,
    )
    output_root = tmp_path / "must-not-exist"

    with pytest.raises(
        ArgillaProjectionOperationError,
        match="path differs from the human assignment binding",
    ):
        write_argilla_projection_binding(
            repo_root=ROOT,
            assignment_path=assignment_path,
            public_bundle_manifest_path=mapping_path,
            pin_path=pin_result.path,
            mapping_path=mapping_path,
            output_root=output_root,
        )

    payload = json.loads(mapping_path.read_bytes())
    bundle_tokens = {item["opaque_item_token"] for item in payload["item_bindings"]}
    unknown_token = "lf023_blind_item_v1:" + "f" * 64
    assert unknown_token not in bundle_tokens
    payload["item_bindings"][-1]["opaque_item_token"] = unknown_token
    payload["item_bindings"].sort(key=lambda item: item["opaque_item_token"])
    wrong_membership = _write_private_json(
        tmp_path / "inputs" / "wrong-membership.json",
        payload,
    )
    with pytest.raises(
        ArgillaProjectionOperationError,
        match="token set differs from the exact public blinded bundle",
    ):
        write_argilla_projection_binding(
            repo_root=ROOT,
            assignment_path=assignment_path,
            public_bundle_manifest_path=PUBLIC_BUNDLE_MANIFEST,
            pin_path=pin_result.path,
            mapping_path=wrong_membership,
            output_root=output_root,
        )
    assert not output_root.exists()


def test_capture_projection_persists_only_private_raw_non_gold_outputs(
    tmp_path: Path,
) -> None:
    assignment_path, pin_result, binding = _binding_setup(tmp_path)
    capture_root, capture_manifest_path = _capture(tmp_path, pin_result=pin_result)
    output_root = tmp_path / "projected"

    result = project_and_persist_argilla_capture(
        repo_root=ROOT,
        assignment_path=assignment_path,
        pin_path=pin_result.path,
        binding_manifest_path=binding.path,
        capture_root=capture_root,
        capture_manifest_path=capture_manifest_path,
        output_root=output_root,
    )

    assert result.run.manifest.response_count == 1
    assert result.run.manifest.capture_manifest_id == result.capture_manifest.manifest_id
    assert result.run.manifest.missing_item_count == 239
    assert result.run.manifest.complete is False
    assert result.run.manifest.backend_origin_verified is True
    assert result.run.manifest.assignment_hmac_verified is False
    assert result.run.manifest.semantic_labels_created is False
    assert result.run.manifest.gold_labels_created is False
    assert result.run.manifest.human_gold_eligible is False
    assert result.run.manifest.training_eligible is False
    assert result.locked_responses_path.name == (
        f"{result.run.manifest.locked_responses_sha256}.jsonl"
    )
    assert result.manifest_path.name == (
        f"{result.run.manifest.manifest_id.rsplit(':', 1)[-1]}.json"
    )
    assert stat.S_IMODE(result.locked_responses_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.manifest_path.stat().st_mode) == 0o600
    restored = load_persisted_argilla_projection(
        manifest_path=result.manifest_path,
        locked_responses_path=result.locked_responses_path,
    )
    assert restored == result.run


def test_capture_projection_rejects_forged_binding_bundle_membership(
    tmp_path: Path,
) -> None:
    assignment_path, pin_result, binding = _binding_setup(tmp_path)
    content_payload = binding.manifest.model_dump(mode="python", exclude={"manifest_id"})
    item_bindings = list(content_payload["item_bindings"])
    unknown_token = "lf023_blind_item_v1:" + "f" * 64
    assert all(item["opaque_item_token"] != unknown_token for item in item_bindings)
    item_bindings[-1]["opaque_item_token"] = unknown_token
    item_bindings.sort(key=lambda item: item["opaque_item_token"])
    content_payload["item_bindings"] = item_bindings
    content = ArgillaProjectionBindingContentV1.model_validate(content_payload)
    forged = ArgillaProjectionBindingManifestV1(
        manifest_id=make_argilla_projection_binding_id(content),
        **content.model_dump(mode="python"),
    )
    forged_path = _write_private_json(
        tmp_path / "forged" / f"{forged.manifest_id.rsplit(':', 1)[-1]}.json",
        forged.model_dump(mode="json"),
    )
    output_root = tmp_path / "must-not-exist"

    with pytest.raises(
        ArgillaProjectionOperationError,
        match="binding token set differs from the exact public blinded bundle",
    ):
        project_and_persist_argilla_capture(
            repo_root=ROOT,
            assignment_path=assignment_path,
            pin_path=pin_result.path,
            binding_manifest_path=forged_path,
            capture_root=tmp_path / "unused-capture",
            capture_manifest_path=tmp_path / "unused-capture.json",
            output_root=output_root,
        )
    assert not output_root.exists()


def test_projection_rejects_capture_byte_drift_without_output(
    tmp_path: Path,
) -> None:
    assignment_path, pin_result, binding = _binding_setup(tmp_path)
    capture_root, capture_manifest_path = _capture(tmp_path, pin_result=pin_result)
    raw_record = next((capture_root / "raw/records").glob("*.json"))
    raw_record.write_bytes(raw_record.read_bytes() + b" ")
    raw_record.chmod(0o600)
    output_root = tmp_path / "must-not-exist"

    with pytest.raises(ArgillaProjectionOperationError, match="hash differs"):
        project_and_persist_argilla_capture(
            repo_root=ROOT,
            assignment_path=assignment_path,
            pin_path=pin_result.path,
            binding_manifest_path=binding.path,
            capture_root=capture_root,
            capture_manifest_path=capture_manifest_path,
            output_root=output_root,
        )
    assert not output_root.exists()


def test_projection_rejects_capture_from_different_backend_pin_without_output(
    tmp_path: Path,
) -> None:
    assignment_path, pin_result, binding = _binding_setup(tmp_path)
    other_pin = write_argilla_backend_pin(
        endpoint="https://other-argilla.internal.example",
        workspace_id=WORKSPACE_ID,
        dataset_id=DATASET_ID,
        annotator_id=ANNOTATOR_ID,
        api_key_env="LF_ARGILLA_PROJECTION_OTHER_TEST_KEY",
        output_dir=tmp_path / "other" / "pins",
    )
    capture_root, capture_manifest_path = _capture(tmp_path / "other", pin_result=other_pin)
    output_root = tmp_path / "must-not-exist"

    with pytest.raises(ArgillaProjectionOperationError, match="different backend pin"):
        project_and_persist_argilla_capture(
            repo_root=ROOT,
            assignment_path=assignment_path,
            pin_path=pin_result.path,
            binding_manifest_path=binding.path,
            capture_root=capture_root,
            capture_manifest_path=capture_manifest_path,
            output_root=output_root,
        )
    assert not output_root.exists()


def test_projection_commands_are_exposed_and_report_no_gold_claims(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    binding_help = runner.invoke(app, ["write-argilla-projection-binding", "--help"])
    projection_help = runner.invoke(app, ["project-argilla-capture", "--help"])

    assert binding_help.exit_code == 0
    assert "--public-bundle-manifest" in binding_help.stdout
    assert "--record-mapping" in binding_help.stdout
    assert projection_help.exit_code == 0
    assert "--capture-manifest" in projection_help.stdout
    assert "--authentication-key" not in projection_help.stdout

    assignment_path, pin_result, binding = _binding_setup(tmp_path)
    capture_root, capture_manifest_path = _capture(tmp_path, pin_result=pin_result)
    result = runner.invoke(
        app,
        [
            "project-argilla-capture",
            "--human-assignment",
            str(assignment_path),
            "--pin",
            str(pin_result.path),
            "--projection-binding",
            str(binding.path),
            "--capture-root",
            str(capture_root),
            "--capture-manifest",
            str(capture_manifest_path),
            "--output-root",
            str(tmp_path / "projected"),
            "--root",
            str(ROOT),
        ],
    )

    assert result.exit_code == 0
    assert "semantic_labels_created=0" in result.stdout
    assert "gold_labels_created=0" in result.stdout
    assert "human_gold_eligible=false" in result.stdout
    assert "training_eligible=0" in result.stdout
    assert "gate_5_closed=false" in result.stdout
    assert "assignment_hmac_verified=false" in result.stdout
