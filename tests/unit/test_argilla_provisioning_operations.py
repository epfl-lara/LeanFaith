from __future__ import annotations

import datetime
import os
import stat
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from leanfaith.annotation_support import argilla_provisioning
from leanfaith.annotation_support.argilla_provisioning import (
    REFERENCE_ISSUE_VALUES,
    ArgillaPeerIsolationCheck,
    ArgillaProvisionedRecord,
    ArgillaProvisionedSlot,
    ArgillaProvisioningError,
    ArgillaProvisioningRequest,
    ArgillaProvisioningResult,
    ArgillaV28ProvisioningTransport,
    build_argilla_settings,
    rendered_record_fields,
)
from leanfaith.annotation_support.attestation import (
    HumanAnnotationAssignmentContentV1,
    authenticate_human_assignment,
    authentication_key_id,
)
from leanfaith.annotation_support.export import (
    ArtifactBinding,
    BlindedAnnotationItemV1,
    BlindedBundleManifestV1,
)
from leanfaith.cli import argilla_provisioning_operations
from leanfaith.cli.argilla_projection_operations import ArgillaRecordAllocationInputV1
from leanfaith.cli.argilla_provisioning_operations import (
    CANONICAL_PUBLIC_BUNDLE_MANIFESTS,
    ArgillaProvisioningOperationError,
    ArgillaProvisioningRecoveryJournalV1,
    ArgillaProvisioningSlotInput,
    ArgillaRecoveryDatasetV1,
    ArgillaRecoveryWorkspaceV1,
    cleanup_argilla_provisioning_recovery,
    provision_argilla_prevalence_round,
)
from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from tests.argilla_public_fixture import (
    ArgillaPublicFixture,
    build_argilla_public_fixture,
)

ROOT = Path(__file__).parents[2]
SOURCE_ROOT = ROOT
KEY = b"k" * 32
SLOT_1_USER_ID = "11111111-1111-4111-8111-111111111111"
SLOT_2_USER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(scope="module", autouse=True)
def _public_fixture(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ArgillaPublicFixture]:
    global ROOT, CANONICAL_PUBLIC_BUNDLE_MANIFESTS
    previous_root = ROOT
    previous_registry = CANONICAL_PUBLIC_BUNDLE_MANIFESTS
    fixture = build_argilla_public_fixture(
        repo_root=tmp_path_factory.mktemp("argilla-public-fixture-repo"),
        source_repo_root=SOURCE_ROOT,
    )
    ROOT = fixture.repo_root
    CANONICAL_PUBLIC_BUNDLE_MANIFESTS = fixture.bundle_manifests  # type: ignore[assignment]
    argilla_provisioning_operations.CANONICAL_PUBLIC_BUNDLE_MANIFESTS = (
        fixture.bundle_manifests  # type: ignore[assignment]
    )
    yield fixture
    ROOT = previous_root
    CANONICAL_PUBLIC_BUNDLE_MANIFESTS = previous_registry
    argilla_provisioning_operations.CANONICAL_PUBLIC_BUNDLE_MANIFESTS = previous_registry


def _public_manifest(slot: str) -> tuple[Path, BlindedBundleManifestV1]:
    relative, _, _ = CANONICAL_PUBLIC_BUNDLE_MANIFESTS[slot]  # type: ignore[index]
    path = ROOT / relative
    return path, BlindedBundleManifestV1.model_validate_json(path.read_bytes())


def _private_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))
    path.chmod(0o600)
    return path


def _key_path(tmp_path: Path) -> Path:
    path = tmp_path / "operator" / "annotation.key"
    path.parent.mkdir(parents=True)
    path.write_bytes(KEY)
    path.chmod(0o600)
    return path


def _assignment(
    tmp_path: Path,
    *,
    slot: str,
    annotator_id: str,
    principal: str,
) -> Path:
    manifest_path, manifest = _public_manifest(slot)
    relative, manifest_sha, _ = CANONICAL_PUBLIC_BUNDLE_MANIFESTS[slot]  # type: ignore[index]
    content = HumanAnnotationAssignmentContentV1(
        campaign_id="lf021_prevalence_v1",
        round_id="prevalence_round_1",
        annotator_slot=slot,  # type: ignore[arg-type]
        annotator_id=annotator_id,
        annotator_principal_hash=principal,
        assignment_mode="operator_attested_human",
        backend_id="argilla",
        assigned_at=datetime.datetime(2026, 7, 28, 12, 0, tzinfo=datetime.UTC),
        public_bundle_manifest=ArtifactBinding(
            artifact=relative,
            sha256=manifest_sha,
        ),
        private_linkage_manifest=ArtifactBinding(
            artifact=(tmp_path / "private-linkage.json").as_posix(),
            sha256="a" * 64,
        ),
        bundle_manifest_id=manifest.manifest_id,
        guideline=ArtifactBinding(
            artifact="annotation/guidelines_v1.md",
            sha256="604eeade46c6328646bd71641f9a3c69cb0588462ac725c5eaf251a89a4b779f",
        ),
        authentication_key_id=authentication_key_id(KEY),
    )
    assignment = authenticate_human_assignment(content, key=KEY)
    del manifest_path
    return _private_json(
        tmp_path / "assignments" / f"{slot}.json",
        assignment.model_dump(mode="json"),
    )


def _record_id(slot_index: int, index: int) -> str:
    return str(uuid.UUID(int=(slot_index << 64) + index, version=4))


class _FakeProvisioningTransport:
    def __init__(
        self,
        *,
        peer_record_status: int = 404,
        peer_workspace_status: int = 404,
    ) -> None:
        self.peer_record_status = peer_record_status
        self.peer_workspace_status = peer_workspace_status
        self.request: ArgillaProvisioningRequest | None = None
        self.rolled_back = False

    def provision(self, request: ArgillaProvisioningRequest) -> ArgillaProvisioningResult:
        self.request = request
        slots: list[ArgillaProvisionedSlot] = []
        for slot_index, requested in enumerate(request.slots, start=1):
            records = tuple(
                ArgillaProvisionedRecord(
                    opaque_item_token=item.opaque_item_token,
                    backend_record_id=_record_id(slot_index, index),
                    initial_response_count=0,
                )
                for index, item in enumerate(
                    sorted(requested.items, key=lambda item: item.opaque_item_token),
                    start=1,
                )
            )
            isolation = ArgillaPeerIsolationCheck(
                annotator_slot=requested.annotator_slot,
                own_workspace_visible=True,
                own_dataset_visible=True,
                own_record_count=240,
                peer_workspace_visible=False,
                adjudication_workspace_visible=False,
                peer_workspace_direct_status=self.peer_workspace_status,
                adjudication_workspace_direct_status=404,
                peer_dataset_visible=False,
                peer_dataset_direct_status=404,
                peer_record_direct_statuses=(self.peer_record_status,) * 240,
            )
            slots.append(
                ArgillaProvisionedSlot(
                    annotator_slot=requested.annotator_slot,
                    workspace_name=requested.workspace_name,
                    workspace_id=requested.workspace_id,
                    dataset_name=requested.dataset_name,
                    dataset_id=(
                        "66666666-6666-4666-8666-666666666666"
                        if slot_index == 1
                        else "77777777-7777-4777-8777-777777777777"
                    ),
                    annotator_backend_id=requested.annotator_backend_id,
                    records=records,
                    isolation=isolation,
                )
            )
        return ArgillaProvisioningResult(
            endpoint=request.endpoint,
            sdk_version="2.8.0",
            server_version="2.8.0",
            adjudication_workspace_name=request.adjudication_workspace_name,
            adjudication_workspace_id=request.adjudication_workspace_id,
            slots=(slots[0], slots[1]),
            response_count=0,
        )

    def rollback(self, result: ArgillaProvisioningResult) -> None:
        del result
        self.rolled_back = True


def _inputs(tmp_path: Path) -> tuple[ArgillaProvisioningSlotInput, ArgillaProvisioningSlotInput]:
    slot_1_manifest, _ = _public_manifest("independent_annotator_1")
    slot_2_manifest, _ = _public_manifest("independent_annotator_2")
    return (
        ArgillaProvisioningSlotInput(
            annotator_slot="independent_annotator_1",
            assignment_path=_assignment(
                tmp_path,
                slot="independent_annotator_1",
                annotator_id="expert_1",
                principal="1" * 64,
            ),
            public_bundle_manifest_path=slot_1_manifest,
            workspace_name="lf021-prevalence-slot-1",
            dataset_name="lf021-prevalence-items-1",
            annotator_backend_id=SLOT_1_USER_ID,
            annotator_api_key_env="LF_ARGILLA_SLOT_1_API_KEY",
        ),
        ArgillaProvisioningSlotInput(
            annotator_slot="independent_annotator_2",
            assignment_path=_assignment(
                tmp_path,
                slot="independent_annotator_2",
                annotator_id="expert_2",
                principal="2" * 64,
            ),
            public_bundle_manifest_path=slot_2_manifest,
            workspace_name="lf021-prevalence-slot-2",
            dataset_name="lf021-prevalence-items-2",
            annotator_backend_id=SLOT_2_USER_ID,
            annotator_api_key_env="LF_ARGILLA_SLOT_2_API_KEY",
        ),
    )


def _set_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LF_ARGILLA_OWNER_API_KEY", "owner-key-" + "x" * 32)
    monkeypatch.setenv("LF_ARGILLA_SLOT_1_API_KEY", "slot-one-key-" + "x" * 32)
    monkeypatch.setenv("LF_ARGILLA_SLOT_2_API_KEY", "slot-two-key-" + "x" * 32)


def test_renderer_includes_all_three_views_on_both_sides() -> None:
    manifest_path, manifest = _public_manifest("independent_annotator_1")
    bundle_path = ROOT / manifest.bundle.artifact
    item = BlindedAnnotationItemV1.model_validate_json(
        bundle_path.read_text(encoding="utf-8").splitlines()[0]
    )
    fields = rendered_record_fields(item)

    assert set(fields) == {
        "opaque_item_token",
        "natural_language_statement",
        "lean_a_headless",
        "lean_a_signature_pp",
        "lean_a_signature_explicit",
        "lean_b_headless",
        "lean_b_signature_pp",
        "lean_b_signature_explicit",
        "permitted_context",
    }
    assert fields["lean_a_headless"] == f"```lean\n{item.lean_a.headless}\n```"
    assert fields["lean_b_signature_explicit"] == (
        f"```lean\n{item.lean_b.signature_explicit}\n```"
    )
    del manifest_path


class _Definition:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _FakeArgillaModule:
    TextField = _Definition
    LabelQuestion = _Definition
    RatingQuestion = _Definition
    TextQuestion = _Definition
    MultiLabelQuestion = _Definition
    Settings = _Definition


def test_settings_use_exact_canonical_response_template_values() -> None:
    settings = build_argilla_settings(_FakeArgillaModule, guideline_text="guideline")
    questions = {question.name: question for question in settings.questions}

    assert questions["reference_issue"].labels == list(REFERENCE_ISSUE_VALUES)
    assert questions["relation"].labels == [
        "equivalent",
        "A_stronger",
        "B_stronger",
        "incomparable",
        "unrelated",
        "ambiguous",
    ]
    assert questions["error_types"].labels == [f"E{index:02d}" for index in range(1, 31)]
    assert questions["rationale"].required is True
    assert len(settings.fields) == 9
    assert settings.allow_extra_metadata is False


def test_record_readback_requires_every_exact_rendered_field() -> None:
    expected = {"opaque_item_token": "opaque:1", "lean_a_headless": "A"}
    argilla_provisioning._require_exact_record_fields(
        dict(expected),
        expected,
        owner="test",
    )

    with pytest.raises(ArgillaProvisioningError, match="exact blinded bundle"):
        argilla_provisioning._require_exact_record_fields(
            {"opaque_item_token": "opaque:1", "lean_a_headless": "changed"},
            expected,
            owner="test",
        )


def test_provisioner_publishes_two_exact_private_bindings_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    transport = _FakeProvisioningTransport()
    output_root = tmp_path / "operator-private" / "argilla-production"

    run = provision_argilla_prevalence_round(
        repo_root=ROOT,
        authentication_key_path=_key_path(tmp_path),
        endpoint="https://argilla.internal.example",
        owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
        adjudication_workspace_name="lf021-prevalence-adjudication",
        slot_inputs=_inputs(tmp_path),
        provisioned_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=datetime.UTC),
        output_root=output_root,
        transport=transport,
    )

    assert transport.request is not None
    assert transport.rolled_back is False
    assert run.output_root == output_root
    assert stat.S_IMODE(output_root.stat().st_mode) == 0o700
    assert run.manifest.response_count == 0
    assert run.manifest.semantic_labels_created is False
    assert run.manifest.gold_labels_created is False
    assert run.manifest.training_eligible is False
    assert run.manifest.peer_isolation_verified is True
    assert transport.request is not None
    assert run.manifest.adjudication_workspace_id == transport.request.adjudication_workspace_id
    assert run.manifest.annotation_template.artifact == (
        "annotation/templates/lf021_prevalence_v1.json"
    )
    assert run.recovery_journal_path.is_file()
    assert stat.S_IMODE(run.recovery_journal_path.stat().st_mode) == 0o600
    journal = ArgillaProvisioningRecoveryJournalV1.model_validate_json(
        run.recovery_journal_path.read_bytes()
    )
    assert journal.state == "published"
    assert journal.published_runtime_manifest_id == run.manifest.manifest_id
    assert run.manifest_path.name == (f"{run.manifest.manifest_id.rsplit(':', 1)[-1]}.json")
    for file_path in output_root.rglob("*"):
        if file_path.is_file():
            assert stat.S_IMODE(file_path.stat().st_mode) == 0o600

    for slot in run.manifest.slots:
        mapping_path = output_root / slot.record_mapping.artifact
        mapping = ArgillaRecordAllocationInputV1.model_validate_json(mapping_path.read_bytes())
        tokens = tuple(item.opaque_item_token for item in mapping.item_bindings)
        records = tuple(item.backend_record_id for item in mapping.item_bindings)
        assert len(tokens) == len(set(tokens)) == 240
        assert len(records) == len(set(records)) == 240
        assert tokens == tuple(sorted(tokens))
        assert mapping.response_values_included is False
        assert mapping.semantic_labels_included is False
        assert mapping.human_gold_eligible is False
        assert mapping.training_eligible is False
        projection_path = output_root / slot.projection_binding.artifact
        assert projection_path.is_file()

    persisted = run.manifest_path.read_bytes()
    assert b"owner-key-" not in persisted
    assert b"slot-one-key-" not in persisted
    assert sha256_hex(persisted) == sha256_hex(run.manifest_path.read_bytes())


@pytest.mark.parametrize(
    ("peer_record_status", "peer_workspace_status"),
    ((200, 404), (404, 200)),
)
def test_invalid_peer_isolation_rolls_back_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peer_record_status: int,
    peer_workspace_status: int,
) -> None:
    _set_keys(monkeypatch)
    transport = _FakeProvisioningTransport(
        peer_record_status=peer_record_status,
        peer_workspace_status=peer_workspace_status,
    )
    output_root = tmp_path / "operator-private" / "argilla-production"

    with pytest.raises(ArgillaProvisioningOperationError, match="peer isolation"):
        provision_argilla_prevalence_round(
            repo_root=ROOT,
            authentication_key_path=_key_path(tmp_path),
            endpoint="https://argilla.internal.example",
            owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
            adjudication_workspace_name="lf021-prevalence-adjudication",
            slot_inputs=_inputs(tmp_path),
            provisioned_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=datetime.UTC),
            output_root=output_root,
            transport=transport,
        )

    assert transport.rolled_back is True
    assert not output_root.exists()
    journal = ArgillaProvisioningRecoveryJournalV1.model_validate_json(
        (output_root.parent / ".argilla-production.argilla-recovery-v1.json").read_bytes()
    )
    assert journal.state == "rolled_back"


def test_local_publication_failure_rolls_back_remote_and_removes_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    transport = _FakeProvisioningTransport()
    output_root = tmp_path / "operator-private" / "argilla-production"

    def fail_pin(**_kwargs: object) -> object:
        raise ValueError("injected pin write failure")

    monkeypatch.setattr(
        argilla_provisioning_operations,
        "write_argilla_backend_pin",
        fail_pin,
    )
    with pytest.raises(
        ArgillaProvisioningOperationError,
        match="injected pin write failure",
    ):
        provision_argilla_prevalence_round(
            repo_root=ROOT,
            authentication_key_path=_key_path(tmp_path),
            endpoint="https://argilla.internal.example",
            owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
            adjudication_workspace_name="lf021-prevalence-adjudication",
            slot_inputs=_inputs(tmp_path),
            provisioned_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=datetime.UTC),
            output_root=output_root,
            transport=transport,
        )

    assert transport.rolled_back is True
    assert not output_root.exists()
    assert not list(output_root.parent.glob(".argilla-production.staging-*"))
    journal = ArgillaProvisioningRecoveryJournalV1.model_validate_json(
        (output_root.parent / ".argilla-production.argilla-recovery-v1.json").read_bytes()
    )
    assert journal.state == "rolled_back"


def test_copied_public_manifest_path_is_rejected_before_remote_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    slot_inputs = list(_inputs(tmp_path))
    copied = tmp_path / "copied-slot-1-manifest.json"
    copied.write_bytes(slot_inputs[0].public_bundle_manifest_path.read_bytes())
    slot_inputs[0] = replace(
        slot_inputs[0],
        public_bundle_manifest_path=copied,
    )
    transport = _FakeProvisioningTransport()

    with pytest.raises(ArgillaProvisioningOperationError, match="exact frozen"):
        provision_argilla_prevalence_round(
            repo_root=ROOT,
            authentication_key_path=_key_path(tmp_path),
            endpoint="https://argilla.internal.example",
            owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
            adjudication_workspace_name="lf021-prevalence-adjudication",
            slot_inputs=(slot_inputs[0], slot_inputs[1]),
            provisioned_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=datetime.UTC),
            output_root=tmp_path / "output",
            transport=transport,
        )

    assert transport.request is None


def test_output_root_must_be_new_before_remote_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    output_root = tmp_path / "existing"
    output_root.mkdir()
    transport = _FakeProvisioningTransport()

    with pytest.raises(ArgillaProvisioningOperationError, match="must not already exist"):
        provision_argilla_prevalence_round(
            repo_root=ROOT,
            authentication_key_path=_key_path(tmp_path),
            endpoint="https://argilla.internal.example",
            owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
            adjudication_workspace_name="lf021-prevalence-adjudication",
            slot_inputs=_inputs(tmp_path),
            provisioned_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=datetime.UTC),
            output_root=output_root,
            transport=transport,
        )

    assert transport.request is None


def test_no_secret_environment_value_is_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    transport = _FakeProvisioningTransport()
    output_root = tmp_path / "private-output"
    run = provision_argilla_prevalence_round(
        repo_root=ROOT,
        authentication_key_path=_key_path(tmp_path),
        endpoint="https://argilla.internal.example",
        owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
        adjudication_workspace_name="lf021-prevalence-adjudication",
        slot_inputs=_inputs(tmp_path),
        provisioned_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=datetime.UTC),
        output_root=output_root,
        transport=transport,
    )

    all_bytes = b"".join(path.read_bytes() for path in output_root.rglob("*") if path.is_file())
    for variable in (
        "LF_ARGILLA_OWNER_API_KEY",
        "LF_ARGILLA_SLOT_1_API_KEY",
        "LF_ARGILLA_SLOT_2_API_KEY",
    ):
        assert os.environ[variable].encode() not in all_bytes
    assert run.manifest.owner_api_key_env == "LF_ARGILLA_OWNER_API_KEY"


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, object]:
        return self._payload


class _RollbackWorkspace:
    def __init__(self, *, resource_id: str, log: list[str]) -> None:
        self.id = resource_id
        self.name = "workspace"
        self.deleted = False
        self._log = log

    def delete(self) -> None:
        self._log.append("workspace")
        self.deleted = True


class _RollbackDataset:
    def __init__(
        self,
        *,
        resource_id: str,
        workspace: _RollbackWorkspace,
        log: list[str],
    ) -> None:
        self.id = resource_id
        self.name = "dataset"
        self.workspace = workspace
        self.deleted = False
        self.fail_once = True
        self._log = log

    def delete(self) -> None:
        self._log.append("dataset")
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected dataset deletion failure")
        self.deleted = True


class _RollbackHttpClient:
    def __init__(
        self,
        *,
        dataset: _RollbackDataset,
        workspace: _RollbackWorkspace,
    ) -> None:
        self.dataset = dataset
        self.workspace = workspace

    def get(self, path: str) -> _Response:
        if "/datasets/" in path:
            return _Response(404 if self.dataset.deleted else 200)
        return _Response(404 if self.workspace.deleted else 200)


class _RollbackOwner:
    def __init__(
        self,
        *,
        dataset: _RollbackDataset,
        workspace: _RollbackWorkspace,
    ) -> None:
        self.http_client = _RollbackHttpClient(
            dataset=dataset,
            workspace=workspace,
        )


def test_transport_rollback_deletes_datasets_before_workspaces_and_is_retryable() -> None:
    log: list[str] = []
    workspace = _RollbackWorkspace(
        resource_id="88888888-8888-4888-8888-888888888888",
        log=log,
    )
    dataset = _RollbackDataset(
        resource_id="99999999-9999-4999-8999-999999999999",
        workspace=workspace,
        log=log,
    )
    transport = ArgillaV28ProvisioningTransport()
    transport._owner = _RollbackOwner(dataset=dataset, workspace=workspace)
    transport._created_datasets = [dataset]
    transport._created_workspaces = [workspace]

    with pytest.raises(ArgillaProvisioningError, match="deferred-until-datasets"):
        transport._cleanup_created()

    assert log == ["dataset"]
    assert transport._created_datasets == [dataset]
    assert transport._created_workspaces == [workspace]

    transport._cleanup_created()

    assert log == ["dataset", "dataset", "workspace"]
    assert transport._created_datasets == []
    assert transport._created_workspaces == []


class _AbsentRecoveryHttpClient:
    def get(self, path: str) -> _Response:
        if path == "/api/v1/version":
            return _Response(200, {"version": "2.8.0"})
        return _Response(404)


class _AbsentRecoveryOwner:
    def __init__(self) -> None:
        self.me = _Definition(role="owner")
        self.http_client = _AbsentRecoveryHttpClient()


class _AbsentRecoveryArgillaModule:
    @staticmethod
    def Argilla(*, api_url: str, api_key: str) -> _AbsentRecoveryOwner:
        assert api_url == "https://argilla.internal.example"
        assert api_key.startswith("owner-key-")
        return _AbsentRecoveryOwner()


def _recovery_journal(tmp_path: Path) -> Path:
    workspaces = (
        ArgillaRecoveryWorkspaceV1(
            role="adjudication",
            workspace_name="adjudication",
            workspace_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        ),
        ArgillaRecoveryWorkspaceV1(
            role="independent_annotator_1",
            workspace_name="slot-1",
            workspace_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        ),
        ArgillaRecoveryWorkspaceV1(
            role="independent_annotator_2",
            workspace_name="slot-2",
            workspace_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        ),
    )
    journal = ArgillaProvisioningRecoveryJournalV1(
        journal_kind="lf023_argilla_provisioning_recovery_v1",
        recovery_operation_id="lf023_argilla_recovery_v1:" + "d" * 64,
        endpoint="https://argilla.internal.example",
        owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
        intended_output_root=(tmp_path / "output").as_posix(),
        created_at=datetime.datetime(2026, 7, 28, 13, 0, tzinfo=datetime.UTC),
        state="remote_provisioning",
        workspaces=workspaces,
        datasets=(
            ArgillaRecoveryDatasetV1(
                annotator_slot="independent_annotator_1",
                dataset_name="dataset-1",
                workspace_id=workspaces[1].workspace_id,
            ),
            ArgillaRecoveryDatasetV1(
                annotator_slot="independent_annotator_2",
                dataset_name="dataset-2",
                workspace_id=workspaces[2].workspace_id,
            ),
        ),
    )
    path = tmp_path / "recovery.json"
    path.write_bytes(canonical_json_bytes(journal.model_dump(mode="json")) + b"\n")
    path.chmod(0o600)
    return path


def test_recovery_cleanup_verifies_absent_planned_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    monkeypatch.setattr(
        "leanfaith.cli.argilla_provisioning_operations.importlib.metadata.version",
        lambda _name: "2.8.0",
    )
    monkeypatch.setattr(
        "leanfaith.cli.argilla_provisioning_operations.importlib.import_module",
        lambda _name: _AbsentRecoveryArgillaModule,
    )
    journal_path = _recovery_journal(tmp_path)

    result = cleanup_argilla_provisioning_recovery(
        journal_path=journal_path,
        owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
    )

    assert result.deleted_dataset_count == 0
    assert result.deleted_workspace_count == 0
    assert result.journal.state == "rolled_back"
    assert {item.status for item in result.journal.datasets} == {"deleted"}
    assert {item.status for item in result.journal.workspaces} == {"deleted"}


def test_recovery_cleanup_refuses_when_private_output_was_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_keys(monkeypatch)
    journal_path = _recovery_journal(tmp_path)
    (tmp_path / "output").mkdir()

    with pytest.raises(
        ArgillaProvisioningOperationError,
        match="private output root exists",
    ):
        cleanup_argilla_provisioning_recovery(
            journal_path=journal_path,
            owner_api_key_env="LF_ARGILLA_OWNER_API_KEY",
        )
