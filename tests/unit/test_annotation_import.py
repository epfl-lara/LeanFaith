from __future__ import annotations

import datetime
import json
import stat
from pathlib import Path

import pytest

import leanfaith.annotation_support.import_ as annotation_import_module
from leanfaith.annotation_support.attestation import (
    HumanAnnotationAssignmentContentV1,
    HumanSubmissionAttestationContentV1,
    authenticate_human_assignment,
    authenticate_human_submission,
    authentication_key_id,
)
from leanfaith.annotation_support.export import (
    EXACT_FRAME_RELATIVE_PATH,
    AnnotationExportRun,
    ArtifactBinding,
    export_blinded_annotation_bundles,
)
from leanfaith.annotation_support.import_ import (
    AnnotationImportError,
    AnnotationImportRun,
    IndependentAnnotationResponseV1,
    LockedAnnotationResponseContentV1,
    LockedAnnotationResponseEnvelopeV1,
    import_blinded_annotation_responses,
    load_verified_annotation_import,
    make_locked_response_id,
)
from leanfaith.annotation_support.operations import (
    AnnotationOperationError,
    attest_human_submission,
    create_authenticated_human_assignment,
    load_authenticated_adjudication_artifact,
    load_authenticated_agreement_artifact,
    write_authenticated_adjudication_queue,
    write_authenticated_agreement,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import AnnotationAnswer, ReferenceIssue, RelationLabel

ROOT = find_repo_root(Path(__file__))
FRAME = ROOT / EXACT_FRAME_RELATIVE_PATH
UTC = datetime.UTC


@pytest.fixture(scope="module")
def annotation_export(tmp_path_factory: pytest.TempPathFactory) -> AnnotationExportRun:
    output = tmp_path_factory.mktemp("lf023-import-export")
    return export_blinded_annotation_bundles(
        repo_root=ROOT,
        frame_path=FRAME,
        output_root=output,
        entropy_by_slot=(bytes(range(32)), bytes(range(32, 64))),
    )


def _response(
    export: AnnotationExportRun,
    *,
    slot_index: int = 0,
    token: str | None = None,
    guideline: ArtifactBinding | None = None,
    annotator_id: str | None = None,
    backend_submission_id: str | None = None,
) -> LockedAnnotationResponseEnvelopeV1:
    bundle = export.bundles[slot_index]
    content = LockedAnnotationResponseContentV1(
        annotator_slot=bundle.manifest.annotator_slot,
        opaque_item_token=token or bundle.items[0].opaque_item_token,
        annotator_id=annotator_id or f"expert_{slot_index + 1}",
        round_id="prevalence_round_1",
        created_at=datetime.datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        bundle_manifest_id=bundle.manifest.manifest_id,
        guideline=guideline or export.private_manifest.annotation_guideline,
        backend_submission_id=backend_submission_id or f"fixture_submission_{slot_index + 1}",
        response=IndependentAnnotationResponseV1(
            same_claim=AnnotationAnswer.SAME_CLAIM,
            relation=RelationLabel.EQUIVALENT,
            confidence=4,
            rationale="",
            reference_issue=ReferenceIssue.NONE,
        ),
    )
    return LockedAnnotationResponseEnvelopeV1(
        response_id=make_locked_response_id(content),
        **content.model_dump(mode="python"),
    )


def _write_responses(path: Path, *responses: LockedAnnotationResponseEnvelopeV1) -> None:
    payload = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in responses
    )
    path.write_bytes(payload)
    path.chmod(0o600)


def _binding(path: Path) -> ArtifactBinding:
    resolved = path.resolve(strict=True)
    root = ROOT.resolve()
    artifact = (
        resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else str(resolved)
    )
    return ArtifactBinding(artifact=artifact, sha256=hash_file(resolved))


def _authenticated_fixture(
    export: AnnotationExportRun,
    *,
    tmp_path: Path,
    response_path: Path,
    slot_index: int = 0,
    annotator_id: str | None = None,
    round_id: str = "prevalence_round_1",
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    key = b"lf023-test-fixture-auth-key-v1!" * 2
    key_path = tmp_path / "annotation-authentication.key"
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    bundle = export.bundles[slot_index]
    assignment_content = HumanAnnotationAssignmentContentV1(
        campaign_id="lf021_prevalence_v1",
        round_id=round_id,
        annotator_slot=bundle.manifest.annotator_slot,
        annotator_id=annotator_id or f"expert_{slot_index + 1}",
        annotator_principal_hash=("1" if slot_index == 0 else "2") * 64,
        assignment_mode="test_fixture",
        backend_id="pytest_fixture_backend",
        assigned_at=datetime.datetime(2026, 7, 28, 11, 0, tzinfo=UTC),
        public_bundle_manifest=_binding(bundle.manifest_path),
        private_linkage_manifest=_binding(export.private_manifest_path),
        bundle_manifest_id=bundle.manifest.manifest_id,
        guideline=export.private_manifest.annotation_guideline,
        authentication_key_id=authentication_key_id(key),
    )
    assignment = authenticate_human_assignment(assignment_content, key=key)
    assignment_path = tmp_path / "human-assignment.json"
    assignment_path.write_bytes(canonical_json_bytes(assignment.model_dump(mode="json")))
    assignment_path.chmod(0o600)
    attestation_content = HumanSubmissionAttestationContentV1(
        assignment_id=assignment.assignment_id,
        assignment_artifact=_binding(assignment_path),
        response_artifact=_binding(response_path),
        backend_export_id="pytest_fixture_export",
        verifier_id="pytest_fixture_verifier",
        attested_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=UTC),
        authentication_key_id=authentication_key_id(key),
        assignment_mode="test_fixture",
        operator_human_origin_asserted=False,
        origin_assurance="test_fixture",
        operator_attestation_verified=True,
        backend_origin_verified=False,
        human_gold_eligible=False,
        fixture_only=True,
    )
    attestation = authenticate_human_submission(attestation_content, key=key)
    attestation_path = tmp_path / "human-submission-attestation.json"
    attestation_path.write_bytes(canonical_json_bytes(attestation.model_dump(mode="json")))
    attestation_path.chmod(0o600)
    return assignment_path, attestation_path, key_path


def _import_fixture(
    export: AnnotationExportRun,
    *,
    tmp_path: Path,
    response_path: Path,
    output_root: Path,
    slot_index: int = 0,
    annotator_id: str | None = None,
) -> AnnotationImportRun:
    assignment_path, attestation_path, key_path = _authenticated_fixture(
        export,
        tmp_path=tmp_path,
        response_path=response_path,
        slot_index=slot_index,
        annotator_id=annotator_id,
    )
    return import_blinded_annotation_responses(
        repo_root=ROOT,
        public_bundle_manifest_path=export.bundles[slot_index].manifest_path,
        private_linkage_manifest_path=export.private_manifest_path,
        human_assignment_path=assignment_path,
        human_submission_attestation_path=attestation_path,
        authentication_key_path=key_path,
        response_path=response_path,
        output_root=output_root,
        allow_test_fixture=True,
    )


def test_import_links_one_locked_response_without_adjudicating(
    annotation_export: AnnotationExportRun,
    tmp_path: Path,
) -> None:
    response = _response(annotation_export)
    response_path = tmp_path / "responses.jsonl"
    _write_responses(response_path, response)

    run = _import_fixture(
        annotation_export,
        tmp_path=tmp_path,
        response_path=response_path,
        output_root=tmp_path / "import",
    )

    assert run.manifest.response_count == 1
    assert run.manifest.missing_item_count == 239
    assert run.manifest.complete is False
    assert run.manifest.raw_annotation_records_created is True
    assert run.manifest.semantic_labels_created is False
    assert run.manifest.fixture_only is True
    assert run.manifest.origin_assurance == "test_fixture"
    assert run.manifest.operator_attestation_verified is True
    assert run.manifest.backend_origin_verified is False
    assert run.manifest.human_gold_eligible is False
    assert run.manifest.gold_labels_created is False
    assert run.manifest.training_eligible is False
    assert run.manifest.adjudications_created is False
    assert len(run.annotations) == 1
    annotation = run.annotations[0]
    assert annotation.same_claim is AnnotationAnswer.SAME_CLAIM
    assert annotation.relation is RelationLabel.EQUIVALENT
    assert annotation.metadata["locked_response_id"] == response.response_id
    assert annotation.metadata["opaque_item_token"] == response.opaque_item_token
    assert annotation.metadata["import_role"] == "raw_annotation_test_fixture"
    assert annotation.metadata["fixture_only"] is True
    assert annotation.metadata["origin_assurance"] == "test_fixture"
    assert annotation.metadata["operator_attestation_verified"] is True
    assert annotation.metadata["backend_origin_verified"] is False
    assert annotation.metadata["human_gold_eligible"] is False
    assert annotation.metadata["gold_label_created"] is False
    assert annotation.metadata["training_eligible"] is False
    assert stat.S_IMODE(run.locked_responses_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(run.annotation_records_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(run.manifest_path.stat().st_mode) == 0o600
    reloaded = load_verified_annotation_import(
        repo_root=ROOT,
        manifest_path=run.manifest_path,
        authentication_key_path=tmp_path / "annotation-authentication.key",
        allow_test_fixture=True,
    )
    assert reloaded.manifest == run.manifest
    assert reloaded.responses == run.responses
    assert reloaded.annotations == run.annotations
    response_path.write_bytes(response_path.read_bytes() + b"\n")
    with pytest.raises(AnnotationImportError, match="bound artifact hash differs"):
        load_verified_annotation_import(
            repo_root=ROOT,
            manifest_path=run.manifest_path,
            authentication_key_path=tmp_path / "annotation-authentication.key",
            allow_test_fixture=True,
        )


def test_import_rejects_wrong_slot_unknown_token_and_duplicate(
    annotation_export: AnnotationExportRun,
    tmp_path: Path,
) -> None:
    first = _response(annotation_export)

    wrong_slot = _response(annotation_export, slot_index=1)
    wrong_slot_path = tmp_path / "wrong-slot.jsonl"
    _write_responses(wrong_slot_path, wrong_slot)
    with pytest.raises(AnnotationImportError, match="annotator slot differs"):
        _import_fixture(
            annotation_export,
            tmp_path=tmp_path / "wrong-slot-auth",
            response_path=wrong_slot_path,
            output_root=tmp_path / "wrong-slot-import",
            slot_index=0,
            annotator_id="expert_2",
        )

    unknown_token = "lf023_blind_item_v1:" + "f" * 64
    assert unknown_token not in {
        item.opaque_item_token for item in annotation_export.bundles[0].items
    }
    unknown = _response(annotation_export, token=unknown_token)
    unknown_path = tmp_path / "unknown.jsonl"
    _write_responses(unknown_path, unknown)
    with pytest.raises(AnnotationImportError, match="unknown item token"):
        _import_fixture(
            annotation_export,
            tmp_path=tmp_path / "unknown-auth",
            response_path=unknown_path,
            output_root=tmp_path / "unknown-import",
        )

    duplicate_path = tmp_path / "duplicate.jsonl"
    _write_responses(duplicate_path, first, first)
    with pytest.raises(AnnotationImportError, match="duplicate opaque item tokens"):
        _import_fixture(
            annotation_export,
            tmp_path=tmp_path / "duplicate-auth",
            response_path=duplicate_path,
            output_root=tmp_path / "duplicate-import",
        )


def test_import_rejects_unbound_guideline(
    annotation_export: AnnotationExportRun,
    tmp_path: Path,
) -> None:
    response = _response(
        annotation_export,
        guideline=ArtifactBinding(
            artifact="annotation/guidelines_other.md",
            sha256="0" * 64,
        ),
    )
    response_path = tmp_path / "wrong-guideline.jsonl"
    _write_responses(response_path, response)
    with pytest.raises(AnnotationImportError, match="guideline binding differs"):
        _import_fixture(
            annotation_export,
            tmp_path=tmp_path / "guideline-auth",
            response_path=response_path,
            output_root=tmp_path / "wrong-guideline-import",
        )


def test_production_import_rejects_fixture_assignment_and_response_tampering(
    annotation_export: AnnotationExportRun,
    tmp_path: Path,
) -> None:
    response = _response(annotation_export)
    response_path = tmp_path / "responses.jsonl"
    _write_responses(response_path, response)
    assignment_path, attestation_path, key_path = _authenticated_fixture(
        annotation_export,
        tmp_path=tmp_path / "auth",
        response_path=response_path,
    )

    with pytest.raises(AnnotationImportError, match="test-fixture assignment"):
        import_blinded_annotation_responses(
            repo_root=ROOT,
            public_bundle_manifest_path=annotation_export.bundles[0].manifest_path,
            private_linkage_manifest_path=annotation_export.private_manifest_path,
            human_assignment_path=assignment_path,
            human_submission_attestation_path=attestation_path,
            authentication_key_path=key_path,
            response_path=response_path,
            output_root=tmp_path / "production-import",
        )

    changed = response.model_copy(
        update={
            "backend_submission_id": "different_submission",
            "response_id": response.response_id,
        }
    )
    # Rebuild a valid content-addressed response but do not re-attest the file.
    changed_content = LockedAnnotationResponseContentV1.model_validate(
        changed.model_dump(mode="json", exclude={"response_id"})
    )
    changed = LockedAnnotationResponseEnvelopeV1(
        response_id=make_locked_response_id(changed_content),
        **changed_content.model_dump(mode="python"),
    )
    _write_responses(response_path, changed)
    with pytest.raises(AnnotationImportError, match="submission attestation differs"):
        import_blinded_annotation_responses(
            repo_root=ROOT,
            public_bundle_manifest_path=annotation_export.bundles[0].manifest_path,
            private_linkage_manifest_path=annotation_export.private_manifest_path,
            human_assignment_path=assignment_path,
            human_submission_attestation_path=attestation_path,
            authentication_key_path=key_path,
            response_path=response_path,
            output_root=tmp_path / "tampered-import",
            allow_test_fixture=True,
        )


def test_import_rejects_tampered_hmac_and_divergent_locked_response(
    annotation_export: AnnotationExportRun,
    tmp_path: Path,
) -> None:
    response = _response(annotation_export)
    first_response_path = tmp_path / "first-responses.jsonl"
    _write_responses(first_response_path, response)
    first_assignment, first_attestation, first_key = _authenticated_fixture(
        annotation_export,
        tmp_path=tmp_path / "first-auth",
        response_path=first_response_path,
    )

    assignment_payload = json.loads(first_assignment.read_bytes())
    assignment_payload["authentication_tag"] = "0" * 64
    first_assignment.write_bytes(canonical_json_bytes(assignment_payload))
    with pytest.raises(AnnotationImportError, match="assignment authentication failed"):
        import_blinded_annotation_responses(
            repo_root=ROOT,
            public_bundle_manifest_path=annotation_export.bundles[0].manifest_path,
            private_linkage_manifest_path=annotation_export.private_manifest_path,
            human_assignment_path=first_assignment,
            human_submission_attestation_path=first_attestation,
            authentication_key_path=first_key,
            response_path=first_response_path,
            output_root=tmp_path / "locked-import",
            allow_test_fixture=True,
        )

    # Restore a valid authenticated assignment and lock the first response.
    first_assignment, first_attestation, first_key = _authenticated_fixture(
        annotation_export,
        tmp_path=tmp_path / "valid-first-auth",
        response_path=first_response_path,
    )
    import_blinded_annotation_responses(
        repo_root=ROOT,
        public_bundle_manifest_path=annotation_export.bundles[0].manifest_path,
        private_linkage_manifest_path=annotation_export.private_manifest_path,
        human_assignment_path=first_assignment,
        human_submission_attestation_path=first_attestation,
        authentication_key_path=first_key,
        response_path=first_response_path,
        output_root=tmp_path / "locked-import",
        allow_test_fixture=True,
    )

    changed_content = LockedAnnotationResponseContentV1.model_validate(
        {
            **response.model_dump(mode="json", exclude={"response_id"}),
            "backend_submission_id": "divergent_submission",
            "response": {
                **response.response.model_dump(mode="json"),
                "confidence": 5,
            },
        }
    )
    changed = LockedAnnotationResponseEnvelopeV1(
        response_id=make_locked_response_id(changed_content),
        **changed_content.model_dump(mode="python"),
    )
    second_response_path = tmp_path / "second-responses.jsonl"
    _write_responses(second_response_path, changed)
    second_assignment, second_attestation, second_key = _authenticated_fixture(
        annotation_export,
        tmp_path=tmp_path / "second-auth",
        response_path=second_response_path,
    )
    with pytest.raises(AnnotationImportError, match="divergent raw response"):
        import_blinded_annotation_responses(
            repo_root=ROOT,
            public_bundle_manifest_path=annotation_export.bundles[0].manifest_path,
            private_linkage_manifest_path=annotation_export.private_manifest_path,
            human_assignment_path=second_assignment,
            human_submission_attestation_path=second_attestation,
            authentication_key_path=second_key,
            response_path=second_response_path,
            output_root=tmp_path / "locked-import",
            allow_test_fixture=True,
        )


@pytest.mark.parametrize(
    "unsafe_round_id",
    ("../../escaped", "/tmp/escaped", "round/with/slashes", ".", ".."),
)
def test_round_id_rejects_non_segment_values(
    annotation_export: AnnotationExportRun,
    unsafe_round_id: str,
) -> None:
    bundle = annotation_export.bundles[0]
    assignment = HumanAnnotationAssignmentContentV1(
        campaign_id="lf021_prevalence_v1",
        round_id="prevalence_round_1",
        annotator_slot=bundle.manifest.annotator_slot,
        annotator_id="expert_1",
        annotator_principal_hash="1" * 64,
        assignment_mode="test_fixture",
        backend_id="pytest_fixture_backend",
        assigned_at=datetime.datetime(2026, 7, 28, 11, 0, tzinfo=UTC),
        public_bundle_manifest=_binding(bundle.manifest_path),
        private_linkage_manifest=_binding(annotation_export.private_manifest_path),
        bundle_manifest_id=bundle.manifest.manifest_id,
        guideline=annotation_export.private_manifest.annotation_guideline,
        authentication_key_id=authentication_key_id(b"x" * 32),
    )
    with pytest.raises(ValueError, match="round_id"):
        HumanAnnotationAssignmentContentV1.model_validate(
            {
                **assignment.model_dump(mode="json"),
                "round_id": unsafe_round_id,
            }
        )

    response = _response(annotation_export)
    with pytest.raises(ValueError, match="round_id"):
        LockedAnnotationResponseContentV1.model_validate(
            {
                **response.model_dump(mode="json", exclude={"response_id"}),
                "round_id": unsafe_round_id,
            }
        )


def test_production_assignment_rejects_symlinked_private_key(
    annotation_export: AnnotationExportRun,
    tmp_path: Path,
) -> None:
    key_target = tmp_path / "real-production.key"
    key_target.write_bytes(b"production-operator-test-key-v1" * 2)
    key_target.chmod(0o600)
    key_symlink = tmp_path / "linked-production.key"
    key_symlink.symlink_to(key_target)
    bundle = annotation_export.bundles[0]

    with pytest.raises(AnnotationImportError, match="not a regular file"):
        create_authenticated_human_assignment(
            repo_root=ROOT,
            public_bundle_manifest_path=bundle.manifest_path,
            private_linkage_manifest_path=annotation_export.private_manifest_path,
            authentication_key_path=key_symlink,
            round_id="prevalence_round_1",
            annotator_slot=bundle.manifest.annotator_slot,
            annotator_id="expert_1",
            annotator_principal_hash="3" * 64,
            backend_id="label_studio",
            assigned_at=datetime.datetime(2026, 7, 28, 11, 0, tzinfo=UTC),
            output_path=tmp_path / "assignment.json",
        )


def test_production_response_lock_is_canonical_across_output_roots(
    annotation_export: AnnotationExportRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_lock_root = tmp_path / "canonical-response-locks"
    monkeypatch.setattr(
        annotation_import_module,
        "_canonical_response_lock_registry",
        lambda _repo_root: canonical_lock_root,
    )
    key_path = tmp_path / "production.key"
    key_path.write_bytes(b"production-operator-test-key-v1" * 2)
    key_path.chmod(0o600)
    bundle = annotation_export.bundles[0]
    assignment_run = create_authenticated_human_assignment(
        repo_root=ROOT,
        public_bundle_manifest_path=bundle.manifest_path,
        private_linkage_manifest_path=annotation_export.private_manifest_path,
        authentication_key_path=key_path,
        round_id="prevalence_round_1",
        annotator_slot=bundle.manifest.annotator_slot,
        annotator_id="expert_1",
        annotator_principal_hash="3" * 64,
        backend_id="label_studio",
        assigned_at=datetime.datetime(2026, 7, 28, 11, 0, tzinfo=UTC),
        output_path=tmp_path / "assignment.json",
    )
    original = _response(
        annotation_export,
        annotator_id="expert_1",
        backend_submission_id="backend-original",
    )
    original_path = tmp_path / "original-responses.jsonl"
    _write_responses(original_path, original)
    original_attestation_path = tmp_path / "original-attestation.json"
    attest_human_submission(
        repo_root=ROOT,
        human_assignment_path=assignment_run.path,
        response_path=original_path,
        authentication_key_path=key_path,
        backend_export_id="original-export",
        verifier_id="operator",
        attested_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=UTC),
        confirm_operator_human_origin_assertion=True,
        confirm_backend_export_locked=True,
        output_path=original_attestation_path,
    )
    import_blinded_annotation_responses(
        repo_root=ROOT,
        public_bundle_manifest_path=bundle.manifest_path,
        private_linkage_manifest_path=annotation_export.private_manifest_path,
        human_assignment_path=assignment_run.path,
        human_submission_attestation_path=original_attestation_path,
        authentication_key_path=key_path,
        response_path=original_path,
        output_root=tmp_path / "first-output-root",
    )

    divergent_content = LockedAnnotationResponseContentV1.model_validate(
        {
            **original.model_dump(mode="json", exclude={"response_id"}),
            "backend_submission_id": "backend-divergent",
            "response": {
                **original.response.model_dump(mode="json"),
                "confidence": 5,
            },
        }
    )
    divergent = LockedAnnotationResponseEnvelopeV1(
        response_id=make_locked_response_id(divergent_content),
        **divergent_content.model_dump(mode="python"),
    )
    divergent_path = tmp_path / "divergent-responses.jsonl"
    _write_responses(divergent_path, divergent)
    divergent_attestation_path = tmp_path / "divergent-attestation.json"
    attest_human_submission(
        repo_root=ROOT,
        human_assignment_path=assignment_run.path,
        response_path=divergent_path,
        authentication_key_path=key_path,
        backend_export_id="divergent-export",
        verifier_id="operator",
        attested_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=UTC),
        confirm_operator_human_origin_assertion=True,
        confirm_backend_export_locked=True,
        output_path=divergent_attestation_path,
    )
    with pytest.raises(AnnotationImportError, match="divergent raw response"):
        import_blinded_annotation_responses(
            repo_root=ROOT,
            public_bundle_manifest_path=bundle.manifest_path,
            private_linkage_manifest_path=annotation_export.private_manifest_path,
            human_assignment_path=assignment_run.path,
            human_submission_attestation_path=divergent_attestation_path,
            authentication_key_path=key_path,
            response_path=divergent_path,
            output_root=tmp_path / "second-output-root",
        )


def test_production_operator_flow_writes_only_raw_agreement_and_routing(
    annotation_export: AnnotationExportRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        annotation_import_module,
        "_canonical_response_lock_registry",
        lambda _repo_root: tmp_path / "canonical-response-locks",
    )
    key_path = tmp_path / "production.key"
    key_path.write_bytes(b"production-operator-test-key-v1" * 2)
    key_path.chmod(0o600)
    import_runs: list[AnnotationImportRun] = []
    for slot_index, bundle in enumerate(annotation_export.bundles):
        annotator_id = f"temporary_test_expert_{slot_index + 1}"
        assignment_path = tmp_path / f"assignment-{slot_index}.json"
        assignment_run = create_authenticated_human_assignment(
            repo_root=ROOT,
            public_bundle_manifest_path=bundle.manifest_path,
            private_linkage_manifest_path=annotation_export.private_manifest_path,
            authentication_key_path=key_path,
            round_id="prevalence_round_1",
            annotator_slot=bundle.manifest.annotator_slot,
            annotator_id=annotator_id,
            annotator_principal_hash=("3" if slot_index == 0 else "4") * 64,
            backend_id="label_studio",
            assigned_at=datetime.datetime(2026, 7, 28, 11, 0, tzinfo=UTC),
            output_path=assignment_path,
        )
        responses = tuple(
            _response(
                annotation_export,
                slot_index=slot_index,
                token=item.opaque_item_token,
                annotator_id=annotator_id,
                backend_submission_id=f"temporary_backend_{slot_index}_{item_index}",
            )
            for item_index, item in enumerate(bundle.items)
        )
        response_path = tmp_path / f"responses-{slot_index}.jsonl"
        _write_responses(response_path, *responses)
        attestation_path = tmp_path / f"attestation-{slot_index}.json"
        with pytest.raises(AnnotationOperationError, match="explicitly confirm"):
            attest_human_submission(
                repo_root=ROOT,
                human_assignment_path=assignment_path,
                response_path=response_path,
                authentication_key_path=key_path,
                backend_export_id=f"temporary_export_{slot_index}",
                verifier_id="temporary_test_operator",
                attested_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=UTC),
                confirm_operator_human_origin_assertion=False,
                confirm_backend_export_locked=True,
                output_path=attestation_path,
            )
        attest_human_submission(
            repo_root=ROOT,
            human_assignment_path=assignment_path,
            response_path=response_path,
            authentication_key_path=key_path,
            backend_export_id=f"temporary_export_{slot_index}",
            verifier_id="temporary_test_operator",
            attested_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=UTC),
            confirm_operator_human_origin_assertion=True,
            confirm_backend_export_locked=True,
            output_path=attestation_path,
        )
        imported = import_blinded_annotation_responses(
            repo_root=ROOT,
            public_bundle_manifest_path=bundle.manifest_path,
            private_linkage_manifest_path=annotation_export.private_manifest_path,
            human_assignment_path=assignment_run.path,
            human_submission_attestation_path=attestation_path,
            authentication_key_path=key_path,
            response_path=response_path,
            output_root=tmp_path / "production-imports",
        )
        assert imported.manifest.complete is True
        assert imported.manifest.origin_assurance == "operator_attested"
        assert imported.manifest.operator_attestation_verified is True
        assert imported.manifest.backend_origin_verified is False
        assert imported.manifest.human_gold_eligible is False
        assert imported.manifest.semantic_labels_created is False
        import_runs.append(imported)

    agreement_path = tmp_path / "agreement.json"
    agreement = write_authenticated_agreement(
        repo_root=ROOT,
        first_import_manifest_path=import_runs[0].manifest_path,
        second_import_manifest_path=import_runs[1].manifest_path,
        authentication_key_path=key_path,
        output_path=agreement_path,
    )
    assert agreement.artifact.report.target_count == 240
    assert agreement.artifact.report.same_claim_raw_agreement == 1.0
    assert agreement.artifact.semantic_labels_created is False
    assert agreement.artifact.resolved_labels_created is False
    assert agreement.artifact.gold_labels_created is False
    assert agreement.artifact.training_eligible is False
    assert (
        load_authenticated_agreement_artifact(
            repo_root=ROOT,
            path=agreement_path,
            authentication_key_path=key_path,
        )
        == agreement.artifact
    )

    queue_path = tmp_path / "adjudication-queue.json"
    queue = write_authenticated_adjudication_queue(
        repo_root=ROOT,
        first_import_manifest_path=import_runs[0].manifest_path,
        second_import_manifest_path=import_runs[1].manifest_path,
        authentication_key_path=key_path,
        output_path=queue_path,
    )
    assert queue.artifact.queue.input_target_count == 240
    assert queue.artifact.queue.routed_target_count == 0
    assert queue.artifact.semantic_labels_created is False
    assert queue.artifact.adjudications_created is False
    assert queue.artifact.automatic_resolutions_created is False
    assert queue.artifact.gold_labels_created is False
    assert queue.artifact.training_eligible is False
    assert (
        load_authenticated_adjudication_artifact(
            repo_root=ROOT,
            path=queue_path,
            authentication_key_path=key_path,
        )
        == queue.artifact
    )
