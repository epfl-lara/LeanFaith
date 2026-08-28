"""Production-safe Argilla 2.8 provisioning for the LF-021 prevalence round.

This module owns only the remote resource boundary.  It creates two isolated
annotator workspaces/datasets and an owner-only adjudication workspace, uploads
the already-blinded public records, and verifies that each annotator cannot
read the peer or adjudication resources.

Provisioning is deliberately label-free.  It never creates a response,
semantic label, adjudication, gold record, or training-eligible record.
Project-owned immutable bindings are written by
``leanfaith.cli.argilla_provisioning_operations``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from leanfaith.annotation_support.export import BlindedAnnotationItemV1

ArgillaSlot = Literal["independent_annotator_1", "independent_annotator_2"]

EXPECTED_ARGILLA_VERSION = "2.8.0"
SAME_CLAIM_VALUES = (
    "same_claim",
    "not_same_claim",
    "ambiguous",
    "cannot_assess_yet",
)
RELATION_VALUES = (
    "equivalent",
    "A_stronger",
    "B_stronger",
    "incomparable",
    "unrelated",
    "ambiguous",
)
CONFIDENCE_VALUES = (1, 2, 3, 4, 5)
REFERENCE_ISSUE_VALUES = ("none", "suspected", "definite")
ERROR_TYPE_VALUES = tuple(f"E{index:02d}" for index in range(1, 31))


class ArgillaProvisioningError(ValueError):
    """Raised when production resources cannot be provisioned safely."""


@dataclass(frozen=True, slots=True)
class ArgillaProvisioningSlotRequest:
    """One exact blinded bundle and the existing human backend identity."""

    annotator_slot: ArgillaSlot
    workspace_name: str
    workspace_id: str
    dataset_name: str
    annotator_backend_id: str
    annotator_api_key: str
    items: tuple[BlindedAnnotationItemV1, ...]


@dataclass(frozen=True, slots=True)
class ArgillaProvisioningRequest:
    """Complete remote request for the two-slot prevalence round."""

    endpoint: str
    owner_api_key: str
    adjudication_workspace_name: str
    adjudication_workspace_id: str
    guideline_text: str
    slots: tuple[ArgillaProvisioningSlotRequest, ArgillaProvisioningSlotRequest]


@dataclass(frozen=True, slots=True)
class ArgillaProvisionedRecord:
    """One backend record identity bound to its opaque public item token."""

    opaque_item_token: str
    backend_record_id: str
    initial_response_count: int


@dataclass(frozen=True, slots=True)
class ArgillaPeerIsolationCheck:
    """Direct and collection-level isolation evidence for one annotator."""

    annotator_slot: ArgillaSlot
    own_workspace_visible: bool
    own_dataset_visible: bool
    own_record_count: int
    peer_workspace_visible: bool
    adjudication_workspace_visible: bool
    peer_workspace_direct_status: int
    adjudication_workspace_direct_status: int
    peer_dataset_visible: bool
    peer_dataset_direct_status: int
    peer_record_direct_statuses: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ArgillaProvisionedSlot:
    """Remote identities and pre-response membership for one annotator."""

    annotator_slot: ArgillaSlot
    workspace_name: str
    workspace_id: str
    dataset_name: str
    dataset_id: str
    annotator_backend_id: str
    records: tuple[ArgillaProvisionedRecord, ...]
    isolation: ArgillaPeerIsolationCheck


@dataclass(frozen=True, slots=True)
class ArgillaProvisioningResult:
    """Verified remote state returned before local immutable publication."""

    endpoint: str
    sdk_version: str
    server_version: str
    adjudication_workspace_name: str
    adjudication_workspace_id: str
    slots: tuple[ArgillaProvisionedSlot, ArgillaProvisionedSlot]
    response_count: int


class ArgillaProvisioningTransport(Protocol):
    """Injectable remote provisioning boundary."""

    def provision(self, request: ArgillaProvisioningRequest) -> ArgillaProvisioningResult:
        """Create and verify all resources or raise after rolling them back."""

    def rollback(self, result: ArgillaProvisioningResult) -> None:
        """Delete resources created for ``result`` after a local publication failure."""


class ArgillaProvisioningRecoverySink(Protocol):
    """Durably record remote side effects before and during provisioning."""

    def mark_remote_provisioning_started(self) -> None:
        """Record that the first remote side effect is about to run."""

    def record_workspace_created(self, *, workspace_id: str, workspace_name: str) -> None:
        """Record one created workspace."""

    def record_dataset_created(
        self,
        *,
        dataset_id: str,
        dataset_name: str,
        workspace_id: str,
    ) -> None:
        """Record one created dataset."""

    def record_dataset_deleted(
        self,
        *,
        dataset_id: str,
        dataset_name: str,
        workspace_id: str,
    ) -> None:
        """Record one verified dataset deletion."""

    def record_workspace_deleted(self, *, workspace_id: str) -> None:
        """Record one verified workspace deletion."""


def _uuid_text(value: object, *, owner: str) -> str:
    try:
        normalized = str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        raise ArgillaProvisioningError(f"{owner} did not expose a UUID") from None
    if normalized != str(value).lower():
        raise ArgillaProvisioningError(f"{owner} UUID is not canonical")
    return normalized


def _server_record_id(record: object) -> str:
    value = getattr(record, "_server_id", None)
    return _uuid_text(value, owner="Argilla record")


def _lean_markdown(value: str) -> str:
    return f"```lean\n{value}\n```"


def _context_markdown(item: BlindedAnnotationItemV1) -> str:
    context = item.permitted_context
    notices = "\n".join(f"- {notice}" for notice in context.view_unavailable_notices)
    if not notices:
        notices = "- none"
    return (
        "### Minimal imports\n"
        f"```lean\n{context.minimal_import_text}\n```\n"
        "### Namespace\n"
        f"```lean\n{context.namespace_text}\n```\n"
        "### Local notation\n"
        f"```lean\n{context.local_notation_text}\n```\n"
        "### Required type information\n"
        f"{context.required_type_information}\n"
        "### View-unavailable notices\n"
        f"{notices}"
    )


def rendered_record_fields(item: BlindedAnnotationItemV1) -> dict[str, str]:
    """Return the exact allowlisted Argilla fields for one blinded item."""

    return {
        "opaque_item_token": item.opaque_item_token,
        "natural_language_statement": item.natural_language_statement,
        "lean_a_headless": _lean_markdown(item.lean_a.headless),
        "lean_a_signature_pp": _lean_markdown(item.lean_a.signature_pp),
        "lean_a_signature_explicit": _lean_markdown(item.lean_a.signature_explicit),
        "lean_b_headless": _lean_markdown(item.lean_b.headless),
        "lean_b_signature_pp": _lean_markdown(item.lean_b.signature_pp),
        "lean_b_signature_explicit": _lean_markdown(item.lean_b.signature_explicit),
        "permitted_context": _context_markdown(item),
    }


def build_argilla_settings(
    rg: Any,
    *,
    guideline_text: str,
    client: Any | None = None,
) -> Any:
    resource_kwargs = {} if client is None else {"client": client}
    fields = [
        rg.TextField(
            name="opaque_item_token",
            title="Opaque item token",
            use_markdown=False,
            **resource_kwargs,
        ),
        rg.TextField(
            name="natural_language_statement",
            title="Natural-language statement",
            use_markdown=True,
            **resource_kwargs,
        ),
    ]
    for side, title in (("a", "Reference Lean A"), ("b", "Candidate Lean B")):
        for view, view_title in (
            ("headless", "headless"),
            ("signature_pp", "pretty-printed signature"),
            ("signature_explicit", "explicit signature"),
        ):
            fields.append(
                rg.TextField(
                    name=f"lean_{side}_{view}",
                    title=f"{title}: {view_title}",
                    use_markdown=True,
                    **resource_kwargs,
                )
            )
    fields.append(
        rg.TextField(
            name="permitted_context",
            title="Permitted Lean context",
            use_markdown=True,
            **resource_kwargs,
        )
    )
    return rg.Settings(
        fields=fields,
        questions=[
            rg.LabelQuestion(
                name="same_claim",
                title="Do A and B express the same mathematical claim?",
                labels=list(SAME_CLAIM_VALUES),
                **resource_kwargs,
            ),
            rg.LabelQuestion(
                name="relation",
                title="Semantic relation",
                labels=list(RELATION_VALUES),
                required=False,
                **resource_kwargs,
            ),
            rg.RatingQuestion(
                name="confidence",
                title="Confidence",
                values=list(CONFIDENCE_VALUES),
                **resource_kwargs,
            ),
            rg.TextQuestion(
                name="rationale",
                title="Rationale",
                required=True,
                use_markdown=True,
                **resource_kwargs,
            ),
            rg.LabelQuestion(
                name="reference_issue",
                title="Does the registered reference have an issue?",
                labels=list(REFERENCE_ISSUE_VALUES),
                **resource_kwargs,
            ),
            rg.MultiLabelQuestion(
                name="error_types",
                title="Optional E01-E30 analysis metadata",
                labels=list(ERROR_TYPE_VALUES),
                required=False,
                **resource_kwargs,
            ),
        ],
        guidelines=guideline_text,
        allow_extra_metadata=False,
    )


def _response_items(payload: object, *, owner: str) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise ArgillaProvisioningError(f"{owner} response is not a JSON object")
    responses = payload.get("responses")
    if not isinstance(responses, list):
        raise ArgillaProvisioningError(f"{owner} responses field is not an array")
    for index, response in enumerate(responses):
        if not isinstance(response, dict):
            raise ArgillaProvisioningError(f"{owner} response at index {index} is not an object")
    return cast(list[dict[str, object]], responses)


def _require_exact_record_fields(
    actual: object,
    expected: dict[str, str],
    *,
    owner: str,
) -> None:
    if not isinstance(actual, dict) or actual != expected:
        raise ArgillaProvisioningError(
            f"{owner} record fields differ from the exact blinded bundle"
        )


class ArgillaV28ProvisioningTransport:
    """Concrete Argilla 2.8 SDK/REST provisioning transport."""

    def __init__(self, *, recovery_sink: ArgillaProvisioningRecoverySink | None = None) -> None:
        self._owner: Any | None = None
        self._created_workspaces: list[Any] = []
        self._created_datasets: list[Any] = []
        self._result: ArgillaProvisioningResult | None = None
        self._recovery_sink = recovery_sink

    @staticmethod
    def _get_json(client: Any, path: str, *, owner: str) -> tuple[int, dict[str, object]]:
        response = client.http_client.get(path)
        status = int(response.status_code)
        try:
            payload = response.json()
        except Exception:
            raise ArgillaProvisioningError(f"{owner} returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise ArgillaProvisioningError(f"{owner} returned a non-object")
        return status, cast(dict[str, object], payload)

    def _cleanup_created(self) -> None:
        errors: list[str] = []
        retained_datasets: list[Any] = []
        for dataset in reversed(self._created_datasets):
            try:
                dataset_id = _uuid_text(dataset.id, owner="Argilla rollback dataset")
                dataset_name = str(dataset.name)
                dataset_workspace_id = _uuid_text(
                    dataset.workspace.id,
                    owner="Argilla rollback dataset workspace",
                )
                dataset.delete()
                if self._owner is None:
                    raise ArgillaProvisioningError("Argilla rollback owner is unavailable")
                status = int(
                    self._owner.http_client.get(f"/api/v1/datasets/{dataset_id}").status_code
                )
                if status != 404:
                    raise ArgillaProvisioningError(
                        f"Argilla rollback dataset remained visible with HTTP {status}"
                    )
                if self._recovery_sink is not None:
                    self._recovery_sink.record_dataset_deleted(
                        dataset_id=dataset_id,
                        dataset_name=dataset_name,
                        workspace_id=dataset_workspace_id,
                    )
            except Exception as exc:  # pragma: no cover - live backend contingency
                retained_datasets.append(dataset)
                errors.append(f"dataset:{type(exc).__name__}")
        self._created_datasets = list(reversed(retained_datasets))

        retained_workspaces: list[Any] = []
        if self._created_datasets:
            retained_workspaces = list(self._created_workspaces)
            errors.append("workspace:deferred-until-datasets-delete")
        else:
            for workspace in reversed(self._created_workspaces):
                try:
                    workspace_id = _uuid_text(
                        workspace.id,
                        owner="Argilla rollback workspace",
                    )
                    workspace.delete()
                    if self._owner is None:
                        raise ArgillaProvisioningError("Argilla rollback owner is unavailable")
                    status = int(
                        self._owner.http_client.get(
                            f"/api/v1/workspaces/{workspace_id}"
                        ).status_code
                    )
                    if status != 404:
                        raise ArgillaProvisioningError(
                            f"Argilla rollback workspace remained visible with HTTP {status}"
                        )
                    if self._recovery_sink is not None:
                        self._recovery_sink.record_workspace_deleted(workspace_id=workspace_id)
                except Exception as exc:  # pragma: no cover - live backend contingency
                    retained_workspaces.append(workspace)
                    errors.append(f"workspace:{type(exc).__name__}")
            self._created_workspaces = list(reversed(retained_workspaces))
        if errors:
            raise ArgillaProvisioningError(
                "Argilla rollback could not delete every created resource: " + ", ".join(errors)
            )

    def rollback(self, result: ArgillaProvisioningResult) -> None:
        if self._result != result:
            raise ArgillaProvisioningError(
                "Argilla rollback result differs from this transport invocation"
            )
        self._cleanup_created()
        self._result = None

    def provision(self, request: ArgillaProvisioningRequest) -> ArgillaProvisioningResult:
        if self._created_workspaces or self._result is not None:
            raise ArgillaProvisioningError("Argilla provisioning transport is single-use")
        if len(request.slots) != 2 or {slot.annotator_slot for slot in request.slots} != {
            "independent_annotator_1",
            "independent_annotator_2",
        }:
            raise ArgillaProvisioningError("Argilla provisioning requires the two exact slots")
        if len(request.owner_api_key.encode("utf-8")) < 16:
            raise ArgillaProvisioningError("Argilla owner API key is unexpectedly short")
        if any(len(slot.annotator_api_key.encode("utf-8")) < 16 for slot in request.slots):
            raise ArgillaProvisioningError("Argilla annotator API key is unexpectedly short")

        try:
            rg = importlib.import_module("argilla")
        except ImportError:
            raise ArgillaProvisioningError(
                "Argilla SDK is unavailable; run from annotation/platforms/argilla"
            ) from None
        sdk_version = importlib.metadata.version("argilla")
        if sdk_version != EXPECTED_ARGILLA_VERSION:
            raise ArgillaProvisioningError(
                f"expected Argilla SDK {EXPECTED_ARGILLA_VERSION}, got {sdk_version}"
            )
        owner = rg.Argilla(api_url=request.endpoint, api_key=request.owner_api_key)
        self._owner = owner
        if str(owner.me.role) != "owner":
            raise ArgillaProvisioningError("Argilla provisioning key does not belong to an owner")
        status, version_payload = self._get_json(
            owner,
            "/api/v1/version",
            owner="Argilla version endpoint",
        )
        server_version = version_payload.get("version")
        if status != 200 or server_version != EXPECTED_ARGILLA_VERSION:
            raise ArgillaProvisioningError(
                f"expected Argilla server {EXPECTED_ARGILLA_VERSION}, got {server_version!r}"
            )

        workspace_names = [slot.workspace_name for slot in request.slots] + [
            request.adjudication_workspace_name
        ]
        dataset_names = [slot.dataset_name for slot in request.slots]
        if len(set(workspace_names)) != 3:
            raise ArgillaProvisioningError("Argilla workspace names must be distinct")
        if len(set(dataset_names)) != 2:
            raise ArgillaProvisioningError("Argilla dataset names must be distinct")
        visible_workspace_names = {str(workspace.name) for workspace in owner.workspaces}
        collisions = sorted(set(workspace_names) & visible_workspace_names)
        if collisions:
            raise ArgillaProvisioningError(
                "Argilla production workspace already exists: " + ", ".join(collisions)
            )

        annotator_users: dict[ArgillaSlot, Any] = {}
        annotator_clients: dict[ArgillaSlot, Any] = {}
        for slot in request.slots:
            try:
                user = rg.User(
                    id=uuid.UUID(slot.annotator_backend_id),
                    client=owner,
                ).get()
            except Exception:
                raise ArgillaProvisioningError(
                    f"Argilla annotator user is unavailable for {slot.annotator_slot}"
                ) from None
            if _uuid_text(user.id, owner="Argilla annotator") != slot.annotator_backend_id:
                raise ArgillaProvisioningError("Argilla annotator UUID differs")
            if str(user.role) != "annotator":
                raise ArgillaProvisioningError("Argilla slot user must have annotator role")
            client = rg.Argilla(api_url=request.endpoint, api_key=slot.annotator_api_key)
            if (
                _uuid_text(client.me.id, owner="authenticated Argilla annotator")
                != slot.annotator_backend_id
                or str(client.me.role) != "annotator"
            ):
                raise ArgillaProvisioningError(
                    f"Argilla annotator API key identity differs for {slot.annotator_slot}"
                )
            annotator_users[slot.annotator_slot] = user
            annotator_clients[slot.annotator_slot] = client

        created_by_slot: dict[ArgillaSlot, tuple[Any, Any]] = {}
        try:
            if self._recovery_sink is not None:
                self._recovery_sink.mark_remote_provisioning_started()
            adjudication = rg.Workspace(
                name=request.adjudication_workspace_name,
                id=uuid.UUID(request.adjudication_workspace_id),
                client=owner,
            ).create()
            self._created_workspaces.append(adjudication)
            adjudication_id = _uuid_text(
                adjudication.id,
                owner="Argilla adjudication workspace",
            )
            if adjudication_id != request.adjudication_workspace_id:
                raise ArgillaProvisioningError(
                    "Argilla adjudication workspace UUID differs from the recovery plan"
                )
            if self._recovery_sink is not None:
                self._recovery_sink.record_workspace_created(
                    workspace_id=adjudication_id,
                    workspace_name=request.adjudication_workspace_name,
                )
            for slot in request.slots:
                workspace = rg.Workspace(
                    name=slot.workspace_name,
                    id=uuid.UUID(slot.workspace_id),
                    client=owner,
                ).create()
                self._created_workspaces.append(workspace)
                workspace_id = _uuid_text(
                    workspace.id,
                    owner="Argilla annotator workspace",
                )
                if workspace_id != slot.workspace_id:
                    raise ArgillaProvisioningError(
                        "Argilla annotator workspace UUID differs from the recovery plan"
                    )
                if self._recovery_sink is not None:
                    self._recovery_sink.record_workspace_created(
                        workspace_id=workspace_id,
                        workspace_name=slot.workspace_name,
                    )
                workspace.add_user(annotator_users[slot.annotator_slot])
                dataset = rg.Dataset(
                    name=slot.dataset_name,
                    workspace=workspace.name,
                    settings=build_argilla_settings(
                        rg,
                        guideline_text=request.guideline_text,
                        client=owner,
                    ),
                    client=owner,
                ).create()
                self._created_datasets.append(dataset)
                dataset_id = _uuid_text(dataset.id, owner="Argilla annotator dataset")
                if self._recovery_sink is not None:
                    self._recovery_sink.record_dataset_created(
                        dataset_id=dataset_id,
                        dataset_name=slot.dataset_name,
                        workspace_id=workspace_id,
                    )
                records = [rg.Record(fields=rendered_record_fields(item)) for item in slot.items]
                dataset.records.log(records)
                created_by_slot[slot.annotator_slot] = (workspace, dataset)

            records_by_slot: dict[ArgillaSlot, tuple[ArgillaProvisionedRecord, ...]] = {}
            for slot in request.slots:
                _, dataset = created_by_slot[slot.annotator_slot]
                backend_records = list(dataset.records())
                if len(backend_records) != 240:
                    raise ArgillaProvisioningError(
                        f"Argilla dataset for {slot.annotator_slot} does not contain 240 records"
                    )
                token_records: list[ArgillaProvisionedRecord] = []
                expected_tokens = {item.opaque_item_token for item in slot.items}
                expected_fields = {
                    item.opaque_item_token: rendered_record_fields(item) for item in slot.items
                }
                seen_tokens: set[str] = set()
                for record in backend_records:
                    fields = getattr(record, "fields", None)
                    if not isinstance(fields, dict):
                        raise ArgillaProvisioningError("Argilla record fields are unavailable")
                    token = fields.get("opaque_item_token")
                    if not isinstance(token, str) or token not in expected_tokens:
                        raise ArgillaProvisioningError(
                            "Argilla record token differs from the exact blinded bundle"
                        )
                    if token in seen_tokens:
                        raise ArgillaProvisioningError(
                            "Argilla dataset contains duplicate opaque item tokens"
                        )
                    seen_tokens.add(token)
                    _require_exact_record_fields(
                        fields,
                        expected_fields[token],
                        owner="Argilla SDK",
                    )
                    record_id = _server_record_id(record)
                    record_status, raw_record = self._get_json(
                        owner,
                        f"/api/v1/records/{record_id}",
                        owner="Argilla record",
                    )
                    if record_status != 200:
                        raise ArgillaProvisioningError(
                            f"Argilla owner record verification returned HTTP {record_status}"
                        )
                    responses = _response_items(raw_record, owner="Argilla record")
                    if responses:
                        raise ArgillaProvisioningError(
                            "newly provisioned Argilla record already contains responses"
                        )
                    _require_exact_record_fields(
                        raw_record.get("fields"),
                        expected_fields[token],
                        owner="Argilla REST",
                    )
                    token_records.append(
                        ArgillaProvisionedRecord(
                            opaque_item_token=token,
                            backend_record_id=record_id,
                            initial_response_count=0,
                        )
                    )
                if seen_tokens != expected_tokens:
                    raise ArgillaProvisioningError(
                        "Argilla record membership differs from the exact blinded bundle"
                    )
                records_by_slot[slot.annotator_slot] = tuple(
                    sorted(token_records, key=lambda item: item.opaque_item_token)
                )

            provisioned_slots: list[ArgillaProvisionedSlot] = []
            slot_by_name = {slot.annotator_slot: slot for slot in request.slots}
            for own_slot, peer_slot in (
                ("independent_annotator_1", "independent_annotator_2"),
                ("independent_annotator_2", "independent_annotator_1"),
            ):
                own = slot_by_name[cast(ArgillaSlot, own_slot)]
                peer = slot_by_name[cast(ArgillaSlot, peer_slot)]
                own_workspace, own_dataset = created_by_slot[own.annotator_slot]
                peer_workspace, peer_dataset = created_by_slot[peer.annotator_slot]
                client = annotator_clients[own.annotator_slot]
                visible_workspaces = {str(workspace.name) for workspace in client.workspaces}
                visible_datasets = {
                    (str(dataset.workspace.name), str(dataset.name)) for dataset in client.datasets
                }
                peer_dataset_status, _ = self._get_json(
                    client,
                    f"/api/v1/datasets/{_uuid_text(peer_dataset.id, owner='peer dataset')}",
                    owner="Argilla peer dataset denial",
                )
                peer_workspace_status, _ = self._get_json(
                    client,
                    f"/api/v1/workspaces/{_uuid_text(peer_workspace.id, owner='peer workspace')}",
                    owner="Argilla peer workspace denial",
                )
                adjudication_workspace_status, _ = self._get_json(
                    client,
                    f"/api/v1/workspaces/{adjudication_id}",
                    owner="Argilla adjudication workspace denial",
                )
                peer_record_statuses = tuple(
                    self._get_json(
                        client,
                        f"/api/v1/records/{record.backend_record_id}",
                        owner="Argilla peer record denial",
                    )[0]
                    for record in records_by_slot[peer.annotator_slot]
                )
                isolation = ArgillaPeerIsolationCheck(
                    annotator_slot=own.annotator_slot,
                    own_workspace_visible=own.workspace_name in visible_workspaces,
                    own_dataset_visible=(
                        own.workspace_name,
                        own.dataset_name,
                    )
                    in visible_datasets,
                    own_record_count=len(
                        list(
                            client.datasets(
                                name=own.dataset_name,
                                workspace=own.workspace_name,
                            ).records()
                        )
                    ),
                    peer_workspace_visible=peer.workspace_name in visible_workspaces,
                    adjudication_workspace_visible=(
                        request.adjudication_workspace_name in visible_workspaces
                    ),
                    peer_workspace_direct_status=peer_workspace_status,
                    adjudication_workspace_direct_status=adjudication_workspace_status,
                    peer_dataset_visible=(
                        peer.workspace_name,
                        peer.dataset_name,
                    )
                    in visible_datasets,
                    peer_dataset_direct_status=peer_dataset_status,
                    peer_record_direct_statuses=peer_record_statuses,
                )
                if (
                    not isolation.own_workspace_visible
                    or not isolation.own_dataset_visible
                    or isolation.own_record_count != 240
                    or isolation.peer_workspace_visible
                    or isolation.adjudication_workspace_visible
                    or isolation.peer_workspace_direct_status not in {403, 404}
                    or isolation.adjudication_workspace_direct_status not in {403, 404}
                    or isolation.peer_dataset_visible
                    or isolation.peer_dataset_direct_status not in {403, 404}
                    or any(status not in {403, 404} for status in peer_record_statuses)
                ):
                    raise ArgillaProvisioningError(
                        f"Argilla peer isolation verification failed for {own.annotator_slot}"
                    )
                provisioned_slots.append(
                    ArgillaProvisionedSlot(
                        annotator_slot=own.annotator_slot,
                        workspace_name=own.workspace_name,
                        workspace_id=_uuid_text(
                            own_workspace.id,
                            owner="Argilla annotator workspace",
                        ),
                        dataset_name=own.dataset_name,
                        dataset_id=_uuid_text(own_dataset.id, owner="Argilla annotator dataset"),
                        annotator_backend_id=own.annotator_backend_id,
                        records=records_by_slot[own.annotator_slot],
                        isolation=isolation,
                    )
                )
            result = ArgillaProvisioningResult(
                endpoint=request.endpoint,
                sdk_version=sdk_version,
                server_version=server_version,
                adjudication_workspace_name=request.adjudication_workspace_name,
                adjudication_workspace_id=adjudication_id,
                slots=cast(
                    tuple[ArgillaProvisionedSlot, ArgillaProvisionedSlot],
                    tuple(sorted(provisioned_slots, key=lambda item: item.annotator_slot)),
                ),
                response_count=0,
            )
            self._result = result
            return result
        except Exception as exc:
            try:
                self._cleanup_created()
            except ArgillaProvisioningError as rollback_exc:
                raise ArgillaProvisioningError(
                    f"Argilla provisioning failed ({exc}); rollback also failed ({rollback_exc})"
                ) from exc
            if isinstance(exc, ArgillaProvisioningError):
                raise
            raise ArgillaProvisioningError(
                f"Argilla provisioning failed: {type(exc).__name__}"
            ) from exc


__all__ = [
    "CONFIDENCE_VALUES",
    "ERROR_TYPE_VALUES",
    "EXPECTED_ARGILLA_VERSION",
    "REFERENCE_ISSUE_VALUES",
    "RELATION_VALUES",
    "SAME_CLAIM_VALUES",
    "ArgillaPeerIsolationCheck",
    "ArgillaProvisionedRecord",
    "ArgillaProvisionedSlot",
    "ArgillaProvisioningError",
    "ArgillaProvisioningRecoverySink",
    "ArgillaProvisioningRequest",
    "ArgillaProvisioningResult",
    "ArgillaProvisioningSlotRequest",
    "ArgillaProvisioningTransport",
    "ArgillaV28ProvisioningTransport",
    "build_argilla_settings",
    "rendered_record_fields",
]
