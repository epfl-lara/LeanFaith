"""Atomic private persistence for production LF-021 Argilla provisioning."""

from __future__ import annotations

import datetime
import importlib
import importlib.metadata
import os
import shutil
import stat
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from leanfaith.annotation_support.argilla_backend import write_argilla_private_immutable
from leanfaith.annotation_support.argilla_projection import ArgillaRecordItemBindingV1
from leanfaith.annotation_support.argilla_provisioning import (
    CONFIDENCE_VALUES,
    ERROR_TYPE_VALUES,
    EXPECTED_ARGILLA_VERSION,
    REFERENCE_ISSUE_VALUES,
    RELATION_VALUES,
    SAME_CLAIM_VALUES,
    ArgillaProvisionedSlot,
    ArgillaProvisioningRecoverySink,
    ArgillaProvisioningRequest,
    ArgillaProvisioningResult,
    ArgillaProvisioningSlotRequest,
    ArgillaProvisioningTransport,
    ArgillaSlot,
    ArgillaV28ProvisioningTransport,
)
from leanfaith.annotation_support.attestation import (
    HumanAnnotationAssignmentEnvelopeV1,
    load_authentication_key,
    verify_human_assignment,
)
from leanfaith.annotation_support.export import (
    ANNOTATION_TEMPLATE_PATH,
    ANNOTATION_TEMPLATE_SHA256,
    ArtifactBinding,
)
from leanfaith.cli.argilla_operations import write_argilla_backend_pin
from leanfaith.cli.argilla_projection_operations import (
    ArgillaRecordAllocationInputV1,
    _load_exact_public_bundle,
    _load_private_model,
    _read_private,
    _read_public,
    _repo_bound_path,
    _strict_json_object,
    write_argilla_projection_binding,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

_HEX64 = r"^[0-9a-f]{64}$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_RUNTIME_ID = r"^lf023_argilla_provisioning_runtime_v1:[0-9a-f]{64}$"
_RECOVERY_OPERATION_ID = r"^lf023_argilla_recovery_v1:[0-9a-f]{64}$"

CANONICAL_PUBLIC_BUNDLE_MANIFESTS: dict[
    ArgillaSlot,
    tuple[str, str, str],
] = {
    "independent_annotator_1": (
        "annotation/exports/lf021_prevalence_v1/annotator_manifests/"
        "acffa0f85555b50776b1d1964d96671edf63f1c619ddbca913f1b5ad3a2a7168.json",
        "92e279468c7f96357c22215af3d6c2b030ec6492363054b90b4e5babc430900d",
        "lf023_blinded_bundle_manifest_v1:"
        "acffa0f85555b50776b1d1964d96671edf63f1c619ddbca913f1b5ad3a2a7168",
    ),
    "independent_annotator_2": (
        "annotation/exports/lf021_prevalence_v1/annotator_manifests/"
        "7c590a6d6de8d22cb39deb5f584f3a8ff55fc7c186721126a3331050f86e9841.json",
        "a5eb47323cf1b46af0069ae717a6ed6de302a4d538cbbd2363a5b4efd9e48768",
        "lf023_blinded_bundle_manifest_v1:"
        "7c590a6d6de8d22cb39deb5f584f3a8ff55fc7c186721126a3331050f86e9841",
    ),
}


class ArgillaProvisioningOperationError(ValueError):
    """Raised before an unsafe or incomplete provisioning result is published."""


class ArgillaProvisionedSlotBindingV1(StrictModel):
    """Private exact local/remote lineage for one independent annotator slot."""

    schema_version: Literal[1] = 1
    annotator_slot: ArgillaSlot
    assignment_id: str = Field(pattern=r"^lf023_human_assignment_v1:[0-9a-f]{64}$")
    assignment: ArtifactBinding
    public_bundle_manifest_id: str = Field(
        pattern=r"^lf023_blinded_bundle_manifest_v1:[0-9a-f]{64}$"
    )
    public_bundle_manifest: ArtifactBinding
    workspace_name: str = Field(min_length=1)
    workspace_id: str = Field(pattern=_UUID)
    dataset_name: str = Field(min_length=1)
    dataset_id: str = Field(pattern=_UUID)
    annotator_backend_id: str = Field(pattern=_UUID)
    api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    backend_pin_id: str = Field(pattern=r"^lf023_argilla_backend_pin_v1:[0-9a-f]{64}$")
    backend_pin: ArtifactBinding
    record_mapping: ArtifactBinding
    projection_binding_id: str = Field(
        pattern=r"^lf023_argilla_projection_binding_v1:[0-9a-f]{64}$"
    )
    projection_binding: ArtifactBinding
    item_count: Literal[240] = 240
    initial_response_count: Literal[0] = 0
    own_workspace_visible: Literal[True] = True
    own_dataset_visible: Literal[True] = True
    peer_workspace_visible: Literal[False] = False
    adjudication_workspace_visible: Literal[False] = False
    peer_dataset_visible: Literal[False] = False
    peer_workspace_direct_denied: Literal[True] = True
    adjudication_workspace_direct_denied: Literal[True] = True
    peer_dataset_direct_denied: Literal[True] = True
    peer_record_direct_denied_count: Literal[240] = 240


class ArgillaProvisioningRuntimeContentV1(StrictModel):
    """Label-free content of one complete production provisioning transaction."""

    schema_version: Literal[1] = 1
    manifest_kind: Literal["lf023_argilla_provisioning_runtime_v1"]
    campaign_id: Literal["lf021_prevalence_v1"]
    round_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    endpoint: str = Field(min_length=1)
    owner_api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    recovery_operation_id: str = Field(pattern=_RECOVERY_OPERATION_ID)
    recovery_journal_path: str = Field(min_length=1)
    annotation_template: ArtifactBinding
    sdk_version: Literal["2.8.0"]
    server_version: Literal["2.8.0"]
    provisioned_at: datetime.datetime
    adjudication_workspace_name: str = Field(min_length=1)
    adjudication_workspace_id: str = Field(pattern=_UUID)
    slots: tuple[ArgillaProvisionedSlotBindingV1, ArgillaProvisionedSlotBindingV1]
    item_count_per_slot: Literal[240] = 240
    total_record_count: Literal[480] = 480
    response_count: Literal[0] = 0
    peer_isolation_verified: Literal[True] = True
    owner_only_adjudication_workspace_verified: Literal[True] = True
    exact_public_bundles_verified: Literal[True] = True
    canonical_template_values_verified: Literal[True] = True
    all_required_lean_views_rendered: Literal[True] = True
    response_values_included: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    adjudications_created: Literal[False] = False
    gold_labels_created: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    private: Literal[True] = True
    release_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        require_utc(self.provisioned_at)
        if tuple(slot.annotator_slot for slot in self.slots) != (
            "independent_annotator_1",
            "independent_annotator_2",
        ):
            raise ValueError("Argilla provisioning slots must use canonical order")
        workspace_ids = [slot.workspace_id for slot in self.slots] + [
            self.adjudication_workspace_id
        ]
        if len(set(workspace_ids)) != 3:
            raise ValueError("Argilla provisioning workspace IDs must be distinct")
        if len({slot.dataset_id for slot in self.slots}) != 2:
            raise ValueError("Argilla provisioning dataset IDs must be distinct")
        if len({slot.annotator_backend_id for slot in self.slots}) != 2:
            raise ValueError("Argilla provisioning annotator IDs must be distinct")
        return self


class ArgillaProvisioningRuntimeManifestV1(ArgillaProvisioningRuntimeContentV1):
    """Content-addressed private provisioning/runtime binding."""

    manifest_id: str = Field(pattern=_RUNTIME_ID)

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"manifest_id"})
        expected = "lf023_argilla_provisioning_runtime_v1:" + hash_canonical(
            {"schema": "lf023_argilla_provisioning_runtime_v1", **payload}
        )
        if self.manifest_id != expected:
            raise ValueError("Argilla provisioning runtime ID differs from normalized content")
        return self


@dataclass(frozen=True, slots=True)
class ArgillaProvisioningRun:
    """Published, reloaded production provisioning artifacts."""

    manifest: ArgillaProvisioningRuntimeManifestV1
    manifest_path: Path
    output_root: Path
    recovery_journal_path: Path
    backend_result: ArgillaProvisioningResult


@dataclass(frozen=True, slots=True)
class ArgillaProvisioningSlotInput:
    """Operator-selected inputs for one exact annotator slot."""

    annotator_slot: ArgillaSlot
    assignment_path: Path
    public_bundle_manifest_path: Path
    workspace_name: str
    dataset_name: str
    annotator_backend_id: str
    annotator_api_key_env: str


class ArgillaRecoveryWorkspaceV1(StrictModel):
    """One deterministic workspace identity in the crash-recovery journal."""

    role: Literal[
        "adjudication",
        "independent_annotator_1",
        "independent_annotator_2",
    ]
    workspace_name: str = Field(min_length=1)
    workspace_id: str = Field(pattern=_UUID)
    status: Literal["planned", "created", "deleted"] = "planned"


class ArgillaRecoveryDatasetV1(StrictModel):
    """One dataset name/identity recoverable even across an ambiguous create."""

    annotator_slot: ArgillaSlot
    dataset_name: str = Field(min_length=1)
    dataset_id: str | None = Field(default=None, pattern=_UUID)
    workspace_id: str = Field(pattern=_UUID)
    status: Literal["planned", "created", "deleted"] = "planned"


class ArgillaProvisioningRecoveryJournalV1(StrictModel):
    """Mutable, private operation journal written before remote side effects."""

    schema_version: Literal[1] = 1
    journal_kind: Literal["lf023_argilla_provisioning_recovery_v1"]
    recovery_operation_id: str = Field(pattern=_RECOVERY_OPERATION_ID)
    endpoint: str = Field(min_length=1)
    owner_api_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    intended_output_root: str = Field(min_length=1)
    created_at: datetime.datetime
    state: Literal[
        "planned",
        "remote_provisioning",
        "remote_verified",
        "published",
        "cleanup_required",
        "rolled_back",
    ]
    workspaces: tuple[
        ArgillaRecoveryWorkspaceV1,
        ArgillaRecoveryWorkspaceV1,
        ArgillaRecoveryWorkspaceV1,
    ]
    datasets: tuple[ArgillaRecoveryDatasetV1, ArgillaRecoveryDatasetV1]
    published_runtime_manifest_id: str | None = Field(default=None, pattern=_RUNTIME_ID)
    secret_values_included: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    human_gold_eligible: Literal[False] = False
    training_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _coherent_recovery_plan(self) -> Self:
        require_utc(self.created_at)
        if tuple(item.role for item in self.workspaces) != (
            "adjudication",
            "independent_annotator_1",
            "independent_annotator_2",
        ):
            raise ValueError("Argilla recovery workspaces must use canonical order")
        workspace_ids = tuple(item.workspace_id for item in self.workspaces)
        if len(set(workspace_ids)) != 3:
            raise ValueError("Argilla recovery workspace IDs must be distinct")
        if tuple(item.annotator_slot for item in self.datasets) != (
            "independent_annotator_1",
            "independent_annotator_2",
        ):
            raise ValueError("Argilla recovery datasets must use canonical order")
        expected_workspace_by_slot = {
            item.role: item.workspace_id for item in self.workspaces if item.role != "adjudication"
        }
        if any(
            item.workspace_id != expected_workspace_by_slot[item.annotator_slot]
            for item in self.datasets
        ):
            raise ValueError("Argilla recovery dataset workspace identity differs")
        if self.state == "published" and self.published_runtime_manifest_id is None:
            raise ValueError("published Argilla recovery journal requires runtime manifest ID")
        return self


@dataclass(frozen=True, slots=True)
class ArgillaRecoveryCleanupRun:
    """Verified deletion of every resource named by a recovery journal."""

    journal: ArgillaProvisioningRecoveryJournalV1
    journal_path: Path
    deleted_dataset_count: int
    deleted_workspace_count: int


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _artifact(path: Path, *, relative_to: Path | None = None) -> ArtifactBinding:
    absolute, raw = _read_public(path, owner="Argilla provisioning artifact")
    if relative_to is None:
        name = absolute.as_posix()
    else:
        try:
            name = absolute.relative_to(relative_to).as_posix()
        except ValueError:
            raise ArgillaProvisioningOperationError(
                "Argilla provisioning artifact escaped its private root"
            ) from None
    return ArtifactBinding(artifact=name, sha256=sha256_hex(raw))


def _write_private(path: Path, payload: bytes) -> None:
    try:
        write_argilla_private_immutable(path, payload)
    except ValueError as exc:
        raise ArgillaProvisioningOperationError(str(exc)) from None


def _canonical_private_model(path: Path, model: StrictModel) -> ArtifactBinding:
    payload = canonical_json_bytes(model.model_dump(mode="json")) + b"\n"
    _write_private(path, payload)
    absolute, raw = _read_private(path, owner="Argilla provisioned private artifact")
    if raw != payload:
        raise ArgillaProvisioningOperationError(
            "Argilla provisioned private artifact differs after reload"
        )
    return ArtifactBinding(artifact=absolute.as_posix(), sha256=sha256_hex(raw))


def _private_relative_binding(path: Path, *, root: Path) -> ArtifactBinding:
    absolute, raw = _read_private(path, owner="Argilla provisioned private artifact")
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        raise ArgillaProvisioningOperationError(
            "Argilla provisioned artifact escaped staging root"
        ) from None
    return ArtifactBinding(artifact=relative.as_posix(), sha256=sha256_hex(raw))


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        endpoint != endpoint.strip()
        or "\x00" in endpoint
        or parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or endpoint.endswith("/")
    ):
        raise ArgillaProvisioningOperationError(
            "production Argilla endpoint must be a bare self-hosted HTTPS origin"
        )


def _require_environment_secret(name: str, *, owner: str) -> str:
    if not name or not name.isidentifier() or name.upper() != name:
        raise ArgillaProvisioningOperationError(f"{owner} environment-variable name is invalid")
    value = os.environ.get(name)
    if value is None:
        raise ArgillaProvisioningOperationError(
            f"required {owner} environment variable is unset: {name}"
        )
    if len(value.encode("utf-8")) < 16:
        raise ArgillaProvisioningOperationError(f"{owner} secret is unexpectedly short")
    return value


def _load_canonical_annotation_template(repo_root: Path) -> ArtifactBinding:
    template_path = repo_root / ANNOTATION_TEMPLATE_PATH
    _, raw = _read_public(template_path, owner="canonical annotation template")
    if sha256_hex(raw) != ANNOTATION_TEMPLATE_SHA256:
        raise ArgillaProvisioningOperationError("canonical annotation template hash differs")
    payload = _strict_json_object(raw, owner="canonical annotation template")
    response_schema = payload.get("response_schema")
    if not isinstance(response_schema, dict):
        raise ArgillaProvisioningOperationError(
            "canonical annotation template omits response_schema"
        )
    required = response_schema.get("required")
    properties = response_schema.get("properties")
    if not isinstance(required, list) or not isinstance(properties, dict):
        raise ArgillaProvisioningOperationError(
            "canonical annotation response schema has invalid required/properties"
        )
    if set(required) != {
        "same_claim",
        "relation",
        "confidence",
        "rationale",
        "reference_issue",
    }:
        raise ArgillaProvisioningOperationError(
            "canonical annotation response required fields differ"
        )

    def property_object(name: str) -> dict[str, object]:
        value = properties.get(name)
        if not isinstance(value, dict):
            raise ArgillaProvisioningOperationError(
                f"canonical annotation response property {name!r} is invalid"
            )
        return cast(dict[str, object], value)

    same_claim = property_object("same_claim")
    relation = property_object("relation")
    confidence = property_object("confidence")
    reference_issue = property_object("reference_issue")
    error_types = property_object("error_types")
    relation_branches = relation.get("oneOf")
    relation_enum: object = None
    if isinstance(relation_branches, list):
        for branch in relation_branches:
            if isinstance(branch, dict) and "enum" in branch:
                relation_enum = branch["enum"]
                break
    error_item = error_types.get("items")
    if (
        same_claim.get("enum") != list(SAME_CLAIM_VALUES)
        or relation_enum != list(RELATION_VALUES)
        or confidence.get("minimum") != min(CONFIDENCE_VALUES)
        or confidence.get("maximum") != max(CONFIDENCE_VALUES)
        or reference_issue.get("enum") != list(REFERENCE_ISSUE_VALUES)
        or not isinstance(error_item, dict)
        or error_item.get("pattern") != "^E(0[1-9]|[12][0-9]|30)$"
        or tuple(ERROR_TYPE_VALUES) != tuple(f"E{index:02d}" for index in range(1, 31))
    ):
        raise ArgillaProvisioningOperationError(
            "canonical annotation response values differ from Argilla settings"
        )
    return ArtifactBinding(
        artifact=ANNOTATION_TEMPLATE_PATH.as_posix(),
        sha256=ANNOTATION_TEMPLATE_SHA256,
    )


def _recovery_journal_path(output_root: Path) -> Path:
    return output_root.parent / f".{output_root.name}.argilla-recovery-v1.json"


def _atomic_write_recovery_journal(
    path: Path,
    journal: ArgillaProvisioningRecoveryJournalV1,
    *,
    require_absent: bool,
) -> None:
    payload = canonical_json_bytes(journal.model_dump(mode="json")) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if require_absent and path.exists():
        raise ArgillaProvisioningOperationError(
            "Argilla recovery journal already exists; run the recovery cleanup command first"
        )
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise ArgillaProvisioningOperationError(
            "cannot durably write the Argilla crash-recovery journal"
        ) from exc
    path.chmod(0o600)
    absolute, restored_raw = _read_private(path, owner="Argilla recovery journal")
    try:
        restored = ArgillaProvisioningRecoveryJournalV1.model_validate(
            _strict_json_object(restored_raw, owner="Argilla recovery journal")
        )
    except ValueError as exc:
        raise ArgillaProvisioningOperationError(
            "Argilla recovery journal failed strict reload"
        ) from exc
    if absolute != path or restored != journal or restored_raw != payload:
        raise ArgillaProvisioningOperationError(
            "Argilla recovery journal differs after durable reload"
        )


class _ArgillaRecoveryJournalWriter(ArgillaProvisioningRecoverySink):
    """Synchronous fail-closed writer used by the concrete remote transport."""

    def __init__(
        self,
        *,
        path: Path,
        journal: ArgillaProvisioningRecoveryJournalV1,
        require_absent: bool = True,
    ) -> None:
        self.path = path
        self.journal = journal
        _atomic_write_recovery_journal(path, journal, require_absent=require_absent)

    @classmethod
    def open_existing(
        cls,
        *,
        path: Path,
        journal: ArgillaProvisioningRecoveryJournalV1,
    ) -> _ArgillaRecoveryJournalWriter:
        """Open an already validated journal without replacing it."""

        writer = cls.__new__(cls)
        writer.path = path
        writer.journal = journal
        return writer

    def _replace(self, **updates: object) -> None:
        payload = self.journal.model_dump(mode="python")
        payload.update(updates)
        updated = ArgillaProvisioningRecoveryJournalV1.model_validate(payload)
        _atomic_write_recovery_journal(self.path, updated, require_absent=False)
        self.journal = updated

    def mark_remote_provisioning_started(self) -> None:
        self._replace(state="remote_provisioning")

    def record_workspace_created(self, *, workspace_id: str, workspace_name: str) -> None:
        changed = False
        workspaces: list[ArgillaRecoveryWorkspaceV1] = []
        for item in self.journal.workspaces:
            if item.workspace_id == workspace_id:
                if item.workspace_name != workspace_name or item.status == "deleted":
                    raise ArgillaProvisioningOperationError(
                        "Argilla created workspace differs from its recovery plan"
                    )
                workspaces.append(
                    ArgillaRecoveryWorkspaceV1(
                        **{
                            **item.model_dump(mode="python"),
                            "status": "created",
                        }
                    )
                )
                changed = True
            else:
                workspaces.append(item)
        if not changed:
            raise ArgillaProvisioningOperationError("Argilla created an unplanned workspace")
        self._replace(workspaces=tuple(workspaces))

    def record_dataset_created(
        self,
        *,
        dataset_id: str,
        dataset_name: str,
        workspace_id: str,
    ) -> None:
        changed = False
        datasets: list[ArgillaRecoveryDatasetV1] = []
        for item in self.journal.datasets:
            if item.dataset_name == dataset_name and item.workspace_id == workspace_id:
                if item.status == "deleted" or (
                    item.dataset_id is not None and item.dataset_id != dataset_id
                ):
                    raise ArgillaProvisioningOperationError(
                        "Argilla created dataset differs from its recovery plan"
                    )
                datasets.append(
                    ArgillaRecoveryDatasetV1(
                        **{
                            **item.model_dump(mode="python"),
                            "dataset_id": dataset_id,
                            "status": "created",
                        }
                    )
                )
                changed = True
            else:
                datasets.append(item)
        if not changed:
            raise ArgillaProvisioningOperationError("Argilla created an unplanned dataset")
        self._replace(datasets=tuple(datasets))

    def record_dataset_deleted(
        self,
        *,
        dataset_id: str,
        dataset_name: str,
        workspace_id: str,
    ) -> None:
        changed = False
        datasets: list[ArgillaRecoveryDatasetV1] = []
        for item in self.journal.datasets:
            if (
                item.dataset_name == dataset_name
                and item.workspace_id == workspace_id
                and (item.dataset_id is None or item.dataset_id == dataset_id)
            ):
                datasets.append(
                    ArgillaRecoveryDatasetV1(
                        **{
                            **item.model_dump(mode="python"),
                            "dataset_id": dataset_id,
                            "status": "deleted",
                        }
                    )
                )
                changed = True
            else:
                datasets.append(item)
        if not changed:
            raise ArgillaProvisioningOperationError("Argilla rollback deleted an unbound dataset")
        self._replace(datasets=tuple(datasets))

    def mark_dataset_absent(self, *, dataset_name: str, workspace_id: str) -> None:
        """Record that an exact planned dataset was verified absent."""

        changed = False
        datasets: list[ArgillaRecoveryDatasetV1] = []
        for item in self.journal.datasets:
            if item.dataset_name == dataset_name and item.workspace_id == workspace_id:
                datasets.append(
                    ArgillaRecoveryDatasetV1(
                        **{
                            **item.model_dump(mode="python"),
                            "status": "deleted",
                        }
                    )
                )
                changed = True
            else:
                datasets.append(item)
        if not changed:
            raise ArgillaProvisioningOperationError(
                "Argilla recovery verified an unplanned dataset"
            )
        self._replace(datasets=tuple(datasets))

    def record_workspace_deleted(self, *, workspace_id: str) -> None:
        changed = False
        workspaces: list[ArgillaRecoveryWorkspaceV1] = []
        for item in self.journal.workspaces:
            if item.workspace_id == workspace_id:
                workspaces.append(
                    ArgillaRecoveryWorkspaceV1(
                        **{
                            **item.model_dump(mode="python"),
                            "status": "deleted",
                        }
                    )
                )
                changed = True
            else:
                workspaces.append(item)
        if not changed:
            raise ArgillaProvisioningOperationError("Argilla rollback deleted an unbound workspace")
        self._replace(workspaces=tuple(workspaces))

    def mark_workspace_absent(self, *, workspace_id: str) -> None:
        """Record that an exact planned workspace was verified absent."""

        self.record_workspace_deleted(workspace_id=workspace_id)

    def mark_remote_verified(self) -> None:
        self._replace(state="remote_verified")

    def mark_cleanup_required(self) -> None:
        self._replace(state="cleanup_required")

    def mark_rolled_back(self) -> None:
        if any(item.status != "deleted" for item in self.journal.datasets) or any(
            item.status != "deleted" for item in self.journal.workspaces
        ):
            raise ArgillaProvisioningOperationError(
                "Argilla recovery journal cannot close before every resource is verified absent"
            )
        self._replace(state="rolled_back")

    def mark_published(self, *, runtime_manifest_id: str) -> None:
        self._replace(
            state="published",
            published_runtime_manifest_id=runtime_manifest_id,
        )


def _load_assignment(
    *,
    path: Path,
    authentication_key: bytes,
) -> tuple[Path, bytes, HumanAnnotationAssignmentEnvelopeV1]:
    absolute, raw, assignment = _load_private_model(
        path,
        owner="Argilla human assignment",
        model_type=HumanAnnotationAssignmentEnvelopeV1,
    )
    try:
        verify_human_assignment(assignment, key=authentication_key)
    except ValueError as exc:
        raise ArgillaProvisioningOperationError(
            f"Argilla human assignment authentication failed: {exc}"
        ) from exc
    if (
        assignment.backend_id != "argilla"
        or assignment.assignment_mode != "operator_attested_human"
    ):
        raise ArgillaProvisioningOperationError(
            "Argilla provisioning requires operator-attested Argilla assignments"
        )
    return absolute, raw, assignment


def _validate_backend_result(
    *,
    result: ArgillaProvisioningResult,
    request: ArgillaProvisioningRequest,
) -> None:
    if (
        result.endpoint != request.endpoint
        or result.sdk_version != "2.8.0"
        or result.server_version != "2.8.0"
        or result.response_count != 0
        or result.adjudication_workspace_name != request.adjudication_workspace_name
        or result.adjudication_workspace_id != request.adjudication_workspace_id
    ):
        raise ArgillaProvisioningOperationError(
            "Argilla backend result differs from the exact provisioning request"
        )
    expected = {slot.annotator_slot: slot for slot in request.slots}
    actual = {slot.annotator_slot: slot for slot in result.slots}
    if set(actual) != set(expected) or len(actual) != 2:
        raise ArgillaProvisioningOperationError("Argilla backend returned incorrect slots")
    all_record_ids: set[str] = set()
    for slot_name, requested in expected.items():
        provisioned = actual[slot_name]
        expected_tokens = tuple(sorted(item.opaque_item_token for item in requested.items))
        actual_tokens = tuple(item.opaque_item_token for item in provisioned.records)
        if (
            provisioned.workspace_name != requested.workspace_name
            or provisioned.workspace_id != requested.workspace_id
            or provisioned.dataset_name != requested.dataset_name
            or provisioned.annotator_backend_id != requested.annotator_backend_id
            or len(provisioned.records) != 240
            or actual_tokens != expected_tokens
            or any(item.initial_response_count != 0 for item in provisioned.records)
        ):
            raise ArgillaProvisioningOperationError(
                f"Argilla backend membership differs for {slot_name}"
            )
        record_ids = {item.backend_record_id for item in provisioned.records}
        if len(record_ids) != 240 or all_record_ids & record_ids:
            raise ArgillaProvisioningOperationError(
                "Argilla backend record identities are not globally unique"
            )
        all_record_ids.update(record_ids)
        isolation = provisioned.isolation
        if (
            isolation.annotator_slot != slot_name
            or not isolation.own_workspace_visible
            or not isolation.own_dataset_visible
            or isolation.own_record_count != 240
            or isolation.peer_workspace_visible
            or isolation.adjudication_workspace_visible
            or isolation.peer_workspace_direct_status not in {403, 404}
            or isolation.adjudication_workspace_direct_status not in {403, 404}
            or isolation.peer_dataset_visible
            or isolation.peer_dataset_direct_status not in {403, 404}
            or len(isolation.peer_record_direct_statuses) != 240
            or any(status not in {403, 404} for status in isolation.peer_record_direct_statuses)
        ):
            raise ArgillaProvisioningOperationError(
                f"Argilla peer isolation failed for {slot_name}"
            )


def _safe_remove_staging(path: Path) -> None:
    if not path.exists():
        return
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ArgillaProvisioningOperationError(
            "Argilla provisioning staging path changed type during cleanup"
        )
    shutil.rmtree(path)


def _load_recovery_journal(
    path: Path,
) -> tuple[Path, ArgillaProvisioningRecoveryJournalV1]:
    absolute, raw = _read_private(path, owner="Argilla recovery journal")
    try:
        journal = ArgillaProvisioningRecoveryJournalV1.model_validate(
            _strict_json_object(raw, owner="Argilla recovery journal")
        )
    except ValueError as exc:
        raise ArgillaProvisioningOperationError("Argilla recovery journal is invalid") from exc
    expected = canonical_json_bytes(journal.model_dump(mode="json")) + b"\n"
    if raw != expected:
        raise ArgillaProvisioningOperationError("Argilla recovery journal is not canonical")
    return absolute, journal


def _journal_record_result_created(
    writer: _ArgillaRecoveryJournalWriter,
    result: ArgillaProvisioningResult,
) -> None:
    writer.record_workspace_created(
        workspace_id=result.adjudication_workspace_id,
        workspace_name=result.adjudication_workspace_name,
    )
    for slot in result.slots:
        writer.record_workspace_created(
            workspace_id=slot.workspace_id,
            workspace_name=slot.workspace_name,
        )
        writer.record_dataset_created(
            dataset_id=slot.dataset_id,
            dataset_name=slot.dataset_name,
            workspace_id=slot.workspace_id,
        )


def _journal_record_result_deleted(
    writer: _ArgillaRecoveryJournalWriter,
    result: ArgillaProvisioningResult,
) -> None:
    for slot in result.slots:
        writer.record_dataset_deleted(
            dataset_id=slot.dataset_id,
            dataset_name=slot.dataset_name,
            workspace_id=slot.workspace_id,
        )
    for slot in result.slots:
        writer.record_workspace_deleted(workspace_id=slot.workspace_id)
    writer.record_workspace_deleted(workspace_id=result.adjudication_workspace_id)


def _rollback_verified_result(
    *,
    backend: ArgillaProvisioningTransport,
    result: ArgillaProvisioningResult,
    writer: _ArgillaRecoveryJournalWriter,
) -> None:
    try:
        backend.rollback(result)
        _journal_record_result_deleted(writer, result)
        writer.mark_rolled_back()
    except Exception:
        with suppress(Exception):
            writer.mark_cleanup_required()
        raise


def _direct_status(client: Any, path: str) -> int:
    try:
        return int(client.http_client.get(path).status_code)
    except Exception as exc:
        raise ArgillaProvisioningOperationError(
            f"Argilla recovery could not inspect {path}"
        ) from exc


def cleanup_argilla_provisioning_recovery(
    *,
    journal_path: Path,
    owner_api_key_env: str,
) -> ArgillaRecoveryCleanupRun:
    """Delete and verify every remote resource bound by a crash journal."""

    absolute, journal = _load_recovery_journal(journal_path)
    if owner_api_key_env != journal.owner_api_key_env:
        raise ArgillaProvisioningOperationError(
            "Argilla recovery owner-key environment differs from the journal"
        )
    if journal.state == "published":
        raise ArgillaProvisioningOperationError(
            "published Argilla provisioning cannot be removed by crash recovery"
        )
    if os.path.lexists(journal.intended_output_root):
        raise ArgillaProvisioningOperationError(
            "Argilla recovery refused because the intended private output root exists; "
            "reconcile the published runtime binding before any remote deletion"
        )
    _validate_endpoint(journal.endpoint)
    owner_api_key = _require_environment_secret(
        owner_api_key_env,
        owner="Argilla recovery owner API key",
    )
    try:
        rg = importlib.import_module("argilla")
    except ImportError:
        raise ArgillaProvisioningOperationError(
            "Argilla SDK is unavailable; run from annotation/platforms/argilla"
        ) from None
    sdk_version = importlib.metadata.version("argilla")
    if sdk_version != EXPECTED_ARGILLA_VERSION:
        raise ArgillaProvisioningOperationError(
            f"expected Argilla SDK {EXPECTED_ARGILLA_VERSION}, got {sdk_version}"
        )
    owner = rg.Argilla(api_url=journal.endpoint, api_key=owner_api_key)
    if str(owner.me.role) != "owner":
        raise ArgillaProvisioningOperationError("Argilla recovery key does not belong to an owner")
    version_response = owner.http_client.get("/api/v1/version")
    try:
        version_payload = version_response.json()
    except Exception as exc:
        raise ArgillaProvisioningOperationError(
            "Argilla recovery server version response is invalid"
        ) from exc
    if (
        int(version_response.status_code) != 200
        or not isinstance(version_payload, dict)
        or version_payload.get("version") != EXPECTED_ARGILLA_VERSION
    ):
        raise ArgillaProvisioningOperationError(
            f"expected Argilla server {EXPECTED_ARGILLA_VERSION}"
        )

    writer = _ArgillaRecoveryJournalWriter.open_existing(
        path=absolute,
        journal=journal,
    )
    workspace_by_id = {item.workspace_id: item for item in journal.workspaces}
    deleted_dataset_count = 0
    deleted_workspace_count = 0
    try:
        for plan in reversed(journal.datasets):
            dataset: Any | None = None
            if plan.dataset_id is not None:
                status = _direct_status(owner, f"/api/v1/datasets/{plan.dataset_id}")
                if status == 200:
                    dataset = owner.datasets(id=plan.dataset_id)
                    if dataset is None:
                        raise ArgillaProvisioningOperationError(
                            "Argilla recovery dataset GET succeeded but SDK lookup failed"
                        )
                elif status != 404:
                    raise ArgillaProvisioningOperationError(
                        f"Argilla recovery dataset lookup returned HTTP {status}"
                    )
            else:
                workspace_plan = workspace_by_id[plan.workspace_id]
                workspace_status = _direct_status(
                    owner,
                    f"/api/v1/workspaces/{plan.workspace_id}",
                )
                if workspace_status == 200:
                    workspace = owner.workspaces(id=plan.workspace_id)
                    if workspace is None or str(workspace.name) != workspace_plan.workspace_name:
                        raise ArgillaProvisioningOperationError(
                            "Argilla recovery workspace differs from its deterministic plan"
                        )
                    dataset = owner.datasets(
                        name=plan.dataset_name,
                        workspace=workspace,
                    )
                elif workspace_status != 404:
                    raise ArgillaProvisioningOperationError(
                        f"Argilla recovery workspace lookup returned HTTP {workspace_status}"
                    )

            if dataset is None:
                writer.mark_dataset_absent(
                    dataset_name=plan.dataset_name,
                    workspace_id=plan.workspace_id,
                )
                continue
            dataset_id = str(uuid.UUID(str(dataset.id)))
            dataset_workspace_id = str(uuid.UUID(str(dataset.workspace.id)))
            if (
                str(dataset.name) != plan.dataset_name
                or dataset_workspace_id != plan.workspace_id
                or (plan.dataset_id is not None and dataset_id != plan.dataset_id)
            ):
                raise ArgillaProvisioningOperationError(
                    "Argilla recovery refused to delete a dataset outside its exact plan"
                )
            dataset.delete()
            if _direct_status(owner, f"/api/v1/datasets/{dataset_id}") != 404:
                raise ArgillaProvisioningOperationError(
                    "Argilla recovery dataset remained visible after deletion"
                )
            writer.record_dataset_deleted(
                dataset_id=dataset_id,
                dataset_name=plan.dataset_name,
                workspace_id=plan.workspace_id,
            )
            deleted_dataset_count += 1

        for workspace_plan in reversed(journal.workspaces):
            status = _direct_status(
                owner,
                f"/api/v1/workspaces/{workspace_plan.workspace_id}",
            )
            if status == 404:
                writer.mark_workspace_absent(workspace_id=workspace_plan.workspace_id)
                continue
            if status != 200:
                raise ArgillaProvisioningOperationError(
                    f"Argilla recovery workspace lookup returned HTTP {status}"
                )
            workspace = owner.workspaces(id=workspace_plan.workspace_id)
            if workspace is None or str(workspace.name) != workspace_plan.workspace_name:
                raise ArgillaProvisioningOperationError(
                    "Argilla recovery refused to delete a workspace outside its exact plan"
                )
            workspace.delete()
            if (
                _direct_status(
                    owner,
                    f"/api/v1/workspaces/{workspace_plan.workspace_id}",
                )
                != 404
            ):
                raise ArgillaProvisioningOperationError(
                    "Argilla recovery workspace remained visible after deletion"
                )
            writer.record_workspace_deleted(workspace_id=workspace_plan.workspace_id)
            deleted_workspace_count += 1
        writer.mark_rolled_back()
    except Exception as exc:
        with suppress(Exception):
            writer.mark_cleanup_required()
        if isinstance(exc, ArgillaProvisioningOperationError):
            raise
        raise ArgillaProvisioningOperationError(
            f"Argilla recovery cleanup failed: {type(exc).__name__}"
        ) from exc
    return ArgillaRecoveryCleanupRun(
        journal=writer.journal,
        journal_path=absolute,
        deleted_dataset_count=deleted_dataset_count,
        deleted_workspace_count=deleted_workspace_count,
    )


def provision_argilla_prevalence_round(
    *,
    repo_root: Path,
    authentication_key_path: Path,
    endpoint: str,
    owner_api_key_env: str,
    adjudication_workspace_name: str,
    slot_inputs: tuple[ArgillaProvisioningSlotInput, ArgillaProvisioningSlotInput],
    provisioned_at: datetime.datetime,
    output_root: Path,
    transport: ArgillaProvisioningTransport | None = None,
) -> ArgillaProvisioningRun:
    """Provision both blinded datasets and atomically publish private bindings."""

    root = repo_root.resolve(strict=True)
    _validate_endpoint(endpoint)
    require_utc(provisioned_at)
    final_root = _absolute(output_root)
    if final_root.exists():
        raise ArgillaProvisioningOperationError(
            "Argilla provisioning output root must not already exist"
        )
    if tuple(slot.annotator_slot for slot in slot_inputs) != (
        "independent_annotator_1",
        "independent_annotator_2",
    ):
        raise ArgillaProvisioningOperationError(
            "Argilla provisioning slot inputs must use canonical order"
        )
    if len({slot.annotator_backend_id for slot in slot_inputs}) != 2:
        raise ArgillaProvisioningOperationError("Argilla annotator backend IDs must be distinct")
    if len({slot.annotator_api_key_env for slot in slot_inputs}) != 2:
        raise ArgillaProvisioningOperationError(
            "Argilla annotator API-key environment names must be distinct"
        )

    key_absolute, key_raw = _read_private(
        authentication_key_path,
        owner="annotation authentication key",
    )
    try:
        authentication_key = load_authentication_key(key_absolute)
    except ValueError as exc:
        raise ArgillaProvisioningOperationError(str(exc)) from exc
    if authentication_key != key_raw:
        raise ArgillaProvisioningOperationError("annotation authentication key changed while read")

    assignments: dict[ArgillaSlot, tuple[Path, bytes, HumanAnnotationAssignmentEnvelopeV1]] = {}
    bundle_items = {}
    public_manifest_bindings: dict[ArgillaSlot, ArtifactBinding] = {}
    guideline_text: str | None = None
    round_id: str | None = None
    for slot in slot_inputs:
        assignment_path, assignment_raw, assignment = _load_assignment(
            path=slot.assignment_path,
            authentication_key=authentication_key,
        )
        if assignment.annotator_slot != slot.annotator_slot:
            raise ArgillaProvisioningOperationError(
                "Argilla assignment slot differs from operator input"
            )
        expected_relative, expected_sha, expected_manifest_id = CANONICAL_PUBLIC_BUNDLE_MANIFESTS[
            slot.annotator_slot
        ]
        expected_path = root / expected_relative
        supplied_path = _absolute(slot.public_bundle_manifest_path)
        if supplied_path != expected_path or assignment.public_bundle_manifest.artifact != (
            expected_relative
        ):
            raise ArgillaProvisioningOperationError(
                f"Argilla {slot.annotator_slot} must use the exact frozen public manifest path"
            )
        manifest_path, manifest_raw = _read_public(
            supplied_path,
            owner="canonical public bundle manifest",
        )
        if (
            sha256_hex(manifest_raw) != expected_sha
            or assignment.public_bundle_manifest.sha256 != expected_sha
            or assignment.bundle_manifest_id != expected_manifest_id
        ):
            raise ArgillaProvisioningOperationError(
                f"Argilla {slot.annotator_slot} public manifest hash/ID differs"
            )
        manifest, items = _load_exact_public_bundle(
            repo_root=root,
            public_bundle_manifest_path=manifest_path,
            assignment=assignment,
        )
        if manifest.manifest_id != expected_manifest_id or len(items) != 240:
            raise ArgillaProvisioningOperationError(
                f"Argilla {slot.annotator_slot} public bundle is not the frozen 240-item bundle"
            )
        assignments[slot.annotator_slot] = (
            assignment_path,
            assignment_raw,
            assignment,
        )
        bundle_items[slot.annotator_slot] = items
        public_manifest_bindings[slot.annotator_slot] = ArtifactBinding(
            artifact=expected_relative,
            sha256=expected_sha,
        )
        guideline_path = _repo_bound_path(
            repo_root=root,
            binding=assignment.guideline,
            owner="annotation guideline",
        )
        _, guideline_raw = _read_public(guideline_path, owner="annotation guideline")
        if sha256_hex(guideline_raw) != assignment.guideline.sha256:
            raise ArgillaProvisioningOperationError("annotation guideline hash differs")
        try:
            current_guideline = guideline_raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ArgillaProvisioningOperationError("annotation guideline is not UTF-8") from None
        if guideline_text is None:
            guideline_text = current_guideline
        elif guideline_text != current_guideline:
            raise ArgillaProvisioningOperationError("Argilla assignments bind different guidelines")
        if round_id is None:
            round_id = assignment.round_id
        elif round_id != assignment.round_id:
            raise ArgillaProvisioningOperationError(
                "Argilla assignments belong to different rounds"
            )

    assignment_values = [item[2] for item in assignments.values()]
    if (
        len({item.assignment_id for item in assignment_values}) != 2
        or len({item.annotator_id for item in assignment_values}) != 2
        or len({item.annotator_principal_hash for item in assignment_values}) != 2
    ):
        raise ArgillaProvisioningOperationError(
            "Argilla assignments must identify two distinct independent annotators"
        )
    assert guideline_text is not None
    assert round_id is not None

    annotation_template = _load_canonical_annotation_template(root)
    owner_api_key = _require_environment_secret(owner_api_key_env, owner="Argilla owner API key")
    adjudication_workspace_id = str(uuid.uuid4())
    workspace_ids: dict[ArgillaSlot, str] = {
        slot.annotator_slot: str(uuid.uuid4()) for slot in slot_inputs
    }
    recovery_path = _recovery_journal_path(final_root)
    recovery_workspaces = (
        ArgillaRecoveryWorkspaceV1(
            role="adjudication",
            workspace_name=adjudication_workspace_name,
            workspace_id=adjudication_workspace_id,
        ),
        ArgillaRecoveryWorkspaceV1(
            role="independent_annotator_1",
            workspace_name=slot_inputs[0].workspace_name,
            workspace_id=workspace_ids["independent_annotator_1"],
        ),
        ArgillaRecoveryWorkspaceV1(
            role="independent_annotator_2",
            workspace_name=slot_inputs[1].workspace_name,
            workspace_id=workspace_ids["independent_annotator_2"],
        ),
    )
    recovery_datasets = cast(
        tuple[ArgillaRecoveryDatasetV1, ArgillaRecoveryDatasetV1],
        tuple(
            ArgillaRecoveryDatasetV1(
                annotator_slot=slot.annotator_slot,
                dataset_name=slot.dataset_name,
                workspace_id=workspace_ids[slot.annotator_slot],
            )
            for slot in slot_inputs
        ),
    )
    recovery_payload = {
        "schema": "lf023_argilla_recovery_v1",
        "endpoint": endpoint,
        "owner_api_key_env": owner_api_key_env,
        "intended_output_root": final_root.as_posix(),
        "created_at": provisioned_at.isoformat(),
        "workspaces": [item.model_dump(mode="json") for item in recovery_workspaces],
        "datasets": [item.model_dump(mode="json") for item in recovery_datasets],
    }
    recovery_operation_id = "lf023_argilla_recovery_v1:" + hash_canonical(recovery_payload)
    recovery_writer = _ArgillaRecoveryJournalWriter(
        path=recovery_path,
        journal=ArgillaProvisioningRecoveryJournalV1(
            journal_kind="lf023_argilla_provisioning_recovery_v1",
            recovery_operation_id=recovery_operation_id,
            endpoint=endpoint,
            owner_api_key_env=owner_api_key_env,
            intended_output_root=final_root.as_posix(),
            created_at=provisioned_at,
            state="planned",
            workspaces=recovery_workspaces,
            datasets=recovery_datasets,
        ),
    )

    requests: list[ArgillaProvisioningSlotRequest] = []
    for slot in slot_inputs:
        requests.append(
            ArgillaProvisioningSlotRequest(
                annotator_slot=slot.annotator_slot,
                workspace_name=slot.workspace_name,
                workspace_id=workspace_ids[slot.annotator_slot],
                dataset_name=slot.dataset_name,
                annotator_backend_id=slot.annotator_backend_id,
                annotator_api_key=_require_environment_secret(
                    slot.annotator_api_key_env,
                    owner=f"Argilla {slot.annotator_slot} API key",
                ),
                items=bundle_items[slot.annotator_slot],
            )
        )
    request = ArgillaProvisioningRequest(
        endpoint=endpoint,
        owner_api_key=owner_api_key,
        adjudication_workspace_name=adjudication_workspace_name,
        adjudication_workspace_id=adjudication_workspace_id,
        guideline_text=guideline_text,
        slots=cast(
            tuple[ArgillaProvisioningSlotRequest, ArgillaProvisioningSlotRequest],
            tuple(requests),
        ),
    )
    backend = transport or ArgillaV28ProvisioningTransport(recovery_sink=recovery_writer)
    result: ArgillaProvisioningResult | None = None
    try:
        result = backend.provision(request)
        _journal_record_result_created(recovery_writer, result)
        _validate_backend_result(result=result, request=request)
        recovery_writer.mark_remote_verified()
    except Exception as exc:
        if result is not None:
            try:
                _rollback_verified_result(
                    backend=backend,
                    result=result,
                    writer=recovery_writer,
                )
            except Exception as rollback_exc:
                raise ArgillaProvisioningOperationError(
                    f"Argilla provisioning validation failed ({exc}); "
                    f"remote rollback failed ({rollback_exc})"
                ) from exc
        else:
            with suppress(Exception):
                recovery_writer.mark_cleanup_required()
        raise
    assert result is not None

    final_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = final_root.parent / f".{final_root.name}.staging-{uuid.uuid4().hex}"
    try:
        os.mkdir(staging, 0o700)
    except OSError as exc:
        try:
            _rollback_verified_result(
                backend=backend,
                result=result,
                writer=recovery_writer,
            )
        except Exception as rollback_exc:
            raise ArgillaProvisioningOperationError(
                "cannot create private Argilla provisioning staging root; "
                f"remote rollback failed ({rollback_exc})"
            ) from exc
        raise ArgillaProvisioningOperationError(
            "cannot create private Argilla provisioning staging root"
        ) from exc

    try:
        slot_bindings: list[ArgillaProvisionedSlotBindingV1] = []
        result_by_slot = {slot.annotator_slot: slot for slot in result.slots}
        for slot_input in slot_inputs:
            slot_name = slot_input.annotator_slot
            provisioned: ArgillaProvisionedSlot = result_by_slot[slot_name]
            assignment_path, assignment_raw, assignment = assignments[slot_name]
            pin_result = write_argilla_backend_pin(
                endpoint=endpoint,
                workspace_id=provisioned.workspace_id,
                dataset_id=provisioned.dataset_id,
                annotator_id=provisioned.annotator_backend_id,
                api_key_env=slot_input.annotator_api_key_env,
                output_dir=staging / "backend_pins" / slot_name,
            )
            mapping = ArgillaRecordAllocationInputV1(
                mapping_kind="lf023_argilla_record_item_mapping_v1",
                assignment_id=assignment.assignment_id,
                backend_pin_id=pin_result.pin.pin_id,
                item_bindings=tuple(
                    ArgillaRecordItemBindingV1(
                        opaque_item_token=item.opaque_item_token,
                        backend_record_id=item.backend_record_id,
                    )
                    for item in provisioned.records
                ),
            )
            mapping_payload = canonical_json_bytes(mapping.model_dump(mode="json")) + b"\n"
            mapping_sha = sha256_hex(mapping_payload)
            mapping_path = staging / "record_mappings" / slot_name / f"{mapping_sha}.json"
            _write_private(mapping_path, mapping_payload)
            projection = write_argilla_projection_binding(
                repo_root=root,
                assignment_path=assignment_path,
                public_bundle_manifest_path=slot_input.public_bundle_manifest_path,
                pin_path=pin_result.path,
                mapping_path=mapping_path,
                output_root=staging / "projection_bindings" / slot_name,
            )
            slot_bindings.append(
                ArgillaProvisionedSlotBindingV1(
                    annotator_slot=slot_name,
                    assignment_id=assignment.assignment_id,
                    assignment=ArtifactBinding(
                        artifact=assignment_path.as_posix(),
                        sha256=sha256_hex(assignment_raw),
                    ),
                    public_bundle_manifest_id=assignment.bundle_manifest_id,
                    public_bundle_manifest=public_manifest_bindings[slot_name],
                    workspace_name=provisioned.workspace_name,
                    workspace_id=provisioned.workspace_id,
                    dataset_name=provisioned.dataset_name,
                    dataset_id=provisioned.dataset_id,
                    annotator_backend_id=provisioned.annotator_backend_id,
                    api_key_env=slot_input.annotator_api_key_env,
                    backend_pin_id=pin_result.pin.pin_id,
                    backend_pin=_private_relative_binding(pin_result.path, root=staging),
                    record_mapping=_private_relative_binding(mapping_path, root=staging),
                    projection_binding_id=projection.manifest.manifest_id,
                    projection_binding=_private_relative_binding(
                        projection.path,
                        root=staging,
                    ),
                    initial_response_count=0,
                    peer_workspace_direct_denied=True,
                    adjudication_workspace_direct_denied=True,
                    peer_dataset_direct_denied=True,
                    peer_record_direct_denied_count=240,
                )
            )
        content = ArgillaProvisioningRuntimeContentV1(
            manifest_kind="lf023_argilla_provisioning_runtime_v1",
            campaign_id="lf021_prevalence_v1",
            round_id=round_id,
            endpoint=endpoint,
            owner_api_key_env=owner_api_key_env,
            recovery_operation_id=recovery_operation_id,
            recovery_journal_path=recovery_path.as_posix(),
            annotation_template=annotation_template,
            sdk_version=cast(Literal["2.8.0"], result.sdk_version),
            server_version=cast(Literal["2.8.0"], result.server_version),
            provisioned_at=provisioned_at,
            adjudication_workspace_name=result.adjudication_workspace_name,
            adjudication_workspace_id=result.adjudication_workspace_id,
            slots=cast(
                tuple[
                    ArgillaProvisionedSlotBindingV1,
                    ArgillaProvisionedSlotBindingV1,
                ],
                tuple(slot_bindings),
            ),
        )
        manifest_id = "lf023_argilla_provisioning_runtime_v1:" + hash_canonical(
            {
                "schema": "lf023_argilla_provisioning_runtime_v1",
                **content.model_dump(mode="json"),
            }
        )
        runtime_manifest = ArgillaProvisioningRuntimeManifestV1(
            manifest_id=manifest_id,
            **content.model_dump(mode="python"),
        )
        manifest_path = staging / "runtime_bindings" / f"{manifest_id.rsplit(':', 1)[-1]}.json"
        _canonical_private_model(manifest_path, runtime_manifest)
        os.rename(staging, final_root)
        published_manifest_path = final_root / manifest_path.relative_to(staging)
        _, published_raw = _read_private(
            published_manifest_path,
            owner="published Argilla provisioning runtime",
        )
        restored = ArgillaProvisioningRuntimeManifestV1.model_validate_json(published_raw)
        if restored != runtime_manifest:
            raise ArgillaProvisioningOperationError(
                "published Argilla provisioning runtime differs after reload"
            )
        recovery_writer.mark_published(runtime_manifest_id=restored.manifest_id)
        return ArgillaProvisioningRun(
            manifest=restored,
            manifest_path=published_manifest_path,
            output_root=final_root,
            recovery_journal_path=recovery_path,
            backend_result=result,
        )
    except Exception as exc:
        if staging.exists():
            _safe_remove_staging(staging)
        elif final_root.exists():
            _safe_remove_staging(final_root)
        try:
            _rollback_verified_result(
                backend=backend,
                result=result,
                writer=recovery_writer,
            )
        except Exception as rollback_exc:
            raise ArgillaProvisioningOperationError(
                f"local Argilla publication failed ({exc}); remote rollback failed ({rollback_exc})"
            ) from exc
        if isinstance(exc, ArgillaProvisioningOperationError):
            raise
        raise ArgillaProvisioningOperationError(
            f"Argilla provisioning publication failed: {exc}"
        ) from exc


__all__ = [
    "CANONICAL_PUBLIC_BUNDLE_MANIFESTS",
    "ArgillaProvisionedSlotBindingV1",
    "ArgillaProvisioningOperationError",
    "ArgillaProvisioningRecoveryJournalV1",
    "ArgillaProvisioningRun",
    "ArgillaProvisioningRuntimeManifestV1",
    "ArgillaProvisioningSlotInput",
    "ArgillaRecoveryCleanupRun",
    "cleanup_argilla_provisioning_recovery",
    "provision_argilla_prevalence_round",
]
