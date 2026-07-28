#!/usr/bin/env python3
"""Run a disposable, public-fixture-only Argilla 2.8 integration check.

The check creates two isolated annotator workspaces and one adjudication
workspace, submits one synthetic response per annotator through Argilla's own
HTTP API, verifies peer-workspace isolation, fetches the exact backend response
objects, and deletes every disposable resource.

It does not import a LeanFaith annotation, resolve a semantic label, create
human gold, or make any record training eligible.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import importlib.metadata
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from leanfaith.annotation_support.argilla_backend import ArgillaV28RestTransport
from leanfaith.config.hashing import canonical_json_bytes, sha256_hex

EXPECTED_ARGILLA_VERSION = "2.8.0"
REPORT_KIND = "lf023_argilla_local_integration_run_v2"
INDEX_KIND = "lf023_argilla_local_integration_index_v1"
FIXTURE_VALUES = {
    "same_claim": {"value": "same_claim"},
    "relation": {"value": "equivalent"},
    "confidence": {"value": 5},
    "rationale": {"value": "Disposable integration fixture only."},
    "reference_issue": {"value": "no_issue"},
}


def _require_loopback_http(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("integration validation accepts only a loopback HTTP Argilla URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Argilla URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Argilla URL must not contain a query or fragment")


def _settings(rg: Any) -> Any:
    return rg.Settings(
        fields=[
            rg.TextField(
                name="natural_language_statement",
                title="Natural-language statement",
                use_markdown=True,
            ),
            rg.TextField(
                name="lean_a",
                title="Reference Lean statement",
                use_markdown=True,
            ),
            rg.TextField(
                name="lean_b",
                title="Candidate Lean statement",
                use_markdown=True,
            ),
        ],
        questions=[
            rg.LabelQuestion(
                name="same_claim",
                title="Do A and B express the same mathematical claim?",
                labels=[
                    "same_claim",
                    "not_same_claim",
                    "ambiguous",
                    "cannot_assess_yet",
                ],
            ),
            rg.LabelQuestion(
                name="relation",
                title="Semantic relation",
                labels=[
                    "equivalent",
                    "A_stronger",
                    "B_stronger",
                    "incomparable",
                    "unrelated",
                    "ambiguous",
                ],
                required=False,
            ),
            rg.RatingQuestion(
                name="confidence",
                title="Confidence",
                values=[1, 2, 3, 4, 5],
            ),
            rg.TextQuestion(
                name="rationale",
                title="Rationale",
                required=False,
            ),
            rg.LabelQuestion(
                name="reference_issue",
                title="Does the reference have an issue?",
                labels=["no_issue", "possible_issue", "definite_issue"],
            ),
        ],
        guidelines=(
            "Disposable LF-023 integration fixture. No response from this "
            "dataset is human gold or training eligible."
        ),
    )


def _fixture_record(rg: Any, slot: int) -> Any:
    return rg.Record(
        fields={
            "natural_language_statement": f"For every natural number n, n = n. Slot {slot}.",
            "lean_a": "```lean\ntheorem reference (n : Nat) : n = n\n```",
            "lean_b": "```lean\ntheorem candidate (n : Nat) : n = n\n```",
        }
    )


def _server_record_id(record: Any) -> str:
    server_id = getattr(record, "_server_id", None)
    if server_id is None:
        raise RuntimeError("Argilla SDK record did not expose its server identity")
    return str(server_id)


def _submit_fixture_response(client: Any, record_id: str) -> dict[str, object]:
    response = client.http_client.post(
        "/api/v1/me/responses/bulk",
        json={
            "items": [
                {
                    "status": "submitted",
                    "record_id": record_id,
                    "values": FIXTURE_VALUES,
                }
            ]
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("Argilla response submission returned an unexpected schema")
    items = payload["items"]
    if len(items) != 1 or not isinstance(items[0], dict):
        raise RuntimeError("Argilla response submission did not return exactly one item")
    result = items[0]
    if result.get("error") is not None or not isinstance(result.get("item"), dict):
        raise RuntimeError("Argilla response submission returned a per-item error")
    item = result["item"]
    if item.get("status") != "submitted" or item.get("record_id") != record_id:
        raise RuntimeError("Argilla response submission identity/status mismatch")
    return cast(dict[str, object], item)


def _query_server_version(client: Any) -> tuple[str, bytes]:
    response = client.http_client.get("/api/v1/version")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("version"), str):
        raise RuntimeError("Argilla version endpoint returned an unexpected schema")
    return payload["version"], response.content


def _fetch_user_api_key(owner: Any, user_id: str) -> str:
    response = owner.http_client.get(f"/api/v1/users/{user_id}")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Argilla user fetch returned a non-object")
    api_key = payload.get("api_key")
    if not isinstance(api_key, str) or len(api_key.encode("utf-8")) < 16:
        raise RuntimeError("Argilla user fetch omitted a usable API key")
    return api_key


def _assert_peer_isolation(
    *,
    client: Any,
    own_workspace: str,
    own_dataset: str,
    peer_workspace: str,
    peer_dataset: str,
    peer_dataset_id: str,
    peer_record_id: str,
) -> dict[str, int]:
    visible_workspaces = {workspace.name for workspace in client.workspaces}
    if own_workspace not in visible_workspaces:
        raise RuntimeError("annotator cannot see its assigned workspace")
    if peer_workspace in visible_workspaces:
        raise RuntimeError("annotator can see the peer workspace")
    if (
        client.datasets(
            name=own_dataset,
            workspace=own_workspace,
        )
        is None
    ):
        raise RuntimeError("annotator cannot read its assigned dataset")
    visible_datasets = {(dataset.workspace.name, dataset.name) for dataset in client.datasets}
    if (peer_workspace, peer_dataset) in visible_datasets:
        raise RuntimeError("annotator can read the peer workspace dataset")
    status_by_resource: dict[str, int] = {}
    for resource, path in (
        ("dataset", f"/api/v1/datasets/{peer_dataset_id}"),
        ("record", f"/api/v1/records/{peer_record_id}"),
    ):
        response = client.http_client.get(path)
        if response.status_code not in {403, 404}:
            raise RuntimeError(
                "annotator peer "
                f"{resource} fetch returned HTTP {response.status_code}, expected 403/404"
            )
        status_by_resource[resource] = response.status_code
    return status_by_resource


def _parse_json_object(raw: bytes, *, owner: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f"{owner} returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{owner} returned a non-object")
    return cast(dict[str, object], payload)


def run(*, api_url: str, api_key: str) -> dict[str, object]:
    _require_loopback_http(api_url)
    if len(api_key.encode("utf-8")) < 16:
        raise ValueError("Argilla integration API key is unexpectedly short")

    import argilla as rg  # type: ignore[import-untyped]

    sdk_version = importlib.metadata.version("argilla")
    if sdk_version != EXPECTED_ARGILLA_VERSION:
        raise RuntimeError(f"expected Argilla SDK {EXPECTED_ARGILLA_VERSION}, got {sdk_version}")

    owner = rg.Argilla(api_url=api_url, api_key=api_key)
    if owner.me.role != "owner":
        raise RuntimeError("integration API key does not belong to an Argilla owner")
    server_version, raw_version_response = _query_server_version(owner)
    if server_version != EXPECTED_ARGILLA_VERSION:
        raise RuntimeError(
            f"expected Argilla server {EXPECTED_ARGILLA_VERSION}, got {server_version}"
        )

    token = secrets.token_hex(6)
    workspace_names = {
        "slot_1": f"lf023-it-{token}-slot-1",
        "slot_2": f"lf023-it-{token}-slot-2",
        "adjudication": f"lf023-it-{token}-adjudication",
    }
    dataset_names = {
        "slot_1": f"lf023-it-{token}-dataset-1",
        "slot_2": f"lf023-it-{token}-dataset-2",
    }
    usernames = {
        "slot_1": f"lf023-it-{token}-expert-1",
        "slot_2": f"lf023-it-{token}-expert-2",
    }
    passwords = {
        "slot_1": secrets.token_urlsafe(24),
        "slot_2": secrets.token_urlsafe(24),
    }

    workspaces: dict[str, Any] = {}
    users: dict[str, Any] = {}
    datasets: dict[str, Any] = {}
    cleanup_complete = False
    record_response_hashes: list[str] = []
    dataset_response_hashes: list[str] = []
    response_ids: list[str] = []
    record_ids: list[str] = []
    peer_fetch_statuses: list[dict[str, int]] = []

    try:
        for slot, name in workspace_names.items():
            workspaces[slot] = rg.Workspace(name=name, client=owner).create()

        for slot in ("slot_1", "slot_2"):
            user = rg.User(
                username=usernames[slot],
                first_name=f"LF023 integration {slot}",
                role="annotator",
                password=passwords[slot],
                client=owner,
            ).create()
            users[slot] = user
            workspaces[slot].add_user(user)

            dataset = rg.Dataset(
                name=dataset_names[slot],
                workspace=workspace_names[slot],
                settings=_settings(rg),
                client=owner,
            ).create()
            dataset.records.log([_fixture_record(rg, 1 if slot == "slot_1" else 2)])
            datasets[slot] = dataset

        record_ids_by_slot: dict[str, str] = {}
        for slot in ("slot_1", "slot_2"):
            records = list(datasets[slot].records())
            if len(records) != 1:
                raise RuntimeError("assigned Argilla dataset does not contain one record")
            record_ids_by_slot[slot] = _server_record_id(records[0])

        annotator_clients = {
            slot: rg.Argilla(
                api_url=api_url,
                api_key=_fetch_user_api_key(owner, str(users[slot].id)),
            )
            for slot in ("slot_1", "slot_2")
        }

        for slot, peer in (("slot_1", "slot_2"), ("slot_2", "slot_1")):
            peer_fetch_statuses.append(
                _assert_peer_isolation(
                    client=annotator_clients[slot],
                    own_workspace=workspace_names[slot],
                    own_dataset=dataset_names[slot],
                    peer_workspace=workspace_names[peer],
                    peer_dataset=dataset_names[peer],
                    peer_dataset_id=str(datasets[peer].id),
                    peer_record_id=record_ids_by_slot[peer],
                )
            )
            own_dataset = annotator_clients[slot].datasets(
                name=dataset_names[slot],
                workspace=workspace_names[slot],
            )
            if own_dataset is None:
                raise RuntimeError("assigned Argilla dataset disappeared")
            record_id = record_ids_by_slot[slot]
            submitted = _submit_fixture_response(annotator_clients[slot], record_id)
            direct = ArgillaV28RestTransport().fetch_response(
                endpoint=api_url.rstrip("/"),
                workspace_id=str(workspaces[slot].id),
                dataset_id=str(datasets[slot].id),
                annotator_id=str(users[slot].id),
                backend_record_id=record_id,
                backend_response_id=str(submitted["id"]),
                api_key=api_key,
            )
            fetched = _parse_json_object(
                direct.raw_record_payload,
                owner="Argilla direct record fetch",
            )
            responses = fetched.get("responses")
            if not isinstance(responses, list) or len(responses) != 1:
                raise RuntimeError("direct Argilla fetch did not contain one response")
            response = responses[0]
            if not isinstance(response, dict):
                raise RuntimeError("Argilla response is not an object")
            if response.get("id") != submitted.get("id"):
                raise RuntimeError("Argilla response identity changed during direct fetch")
            if response.get("user_id") != str(users[slot].id):
                raise RuntimeError("Argilla response has the wrong annotator identity")
            if response.get("status") != "submitted":
                raise RuntimeError("Argilla response is not submitted")
            if response.get("values") != FIXTURE_VALUES:
                raise RuntimeError("Argilla response values changed during direct fetch")
            record_response_hashes.append(sha256_hex(direct.raw_record_payload))
            dataset_response_hashes.append(sha256_hex(direct.raw_dataset_payload))
            response_ids.append(str(response["id"]))
            record_ids.append(record_id)

        if len(set(record_response_hashes)) != 2 or len(set(response_ids)) != 2:
            raise RuntimeError("independent Argilla fixtures did not retain distinct identities")
    finally:
        for dataset in reversed(tuple(datasets.values())):
            with contextlib.suppress(Exception):
                dataset.delete()
        for user in reversed(tuple(users.values())):
            with contextlib.suppress(Exception):
                user.delete()
        for workspace in reversed(tuple(workspaces.values())):
            with contextlib.suppress(Exception):
                workspace.delete()

        remaining_workspaces = {workspace.name for workspace in owner.workspaces}
        remaining_users = {user.username for user in owner.users}
        remaining_datasets = {(dataset.workspace.name, dataset.name) for dataset in owner.datasets}
        cleanup_complete = not (
            set(workspace_names.values()) & remaining_workspaces
            or set(usernames.values()) & remaining_users
            or {(workspace_names[slot], dataset_names[slot]) for slot in ("slot_1", "slot_2")}
            & remaining_datasets
        )

    if not cleanup_complete:
        raise RuntimeError("disposable Argilla resources were not fully deleted")

    return {
        "schema_version": 2,
        "report_kind": REPORT_KIND,
        "artifact_class": "diagnostic",
        "argilla_sdk_version": sdk_version,
        "argilla_server_version": server_version,
        "argilla_version_http_response_sha256": sha256_hex(raw_version_response),
        "api_url_kind": "loopback_http",
        "checks": {
            "owner_authenticated": True,
            "two_annotator_users_created": True,
            "two_isolated_workspaces_created": True,
            "separate_adjudication_workspace_created": True,
            "lean_pair_schema_created": True,
            "peer_workspace_isolation_verified": True,
            "peer_dataset_direct_fetch_denied": 2,
            "peer_record_direct_fetch_denied": 2,
            "submitted_responses_created": 2,
            "concrete_rest_transport_fetches_verified": 2,
            "response_identity_verified": True,
            "annotator_identity_verified": True,
            "raw_payloads_distinct": True,
            "deployment_object_cleanup_verified": True,
        },
        "peer_fetch_http_statuses": peer_fetch_statuses,
        "response_ids": response_ids,
        "record_ids": record_ids,
        "raw_dataset_http_response_sha256": dataset_response_hashes,
        "raw_record_http_response_sha256": record_response_hashes,
        "deployment_object_cleanup": {
            "disposable_argilla_objects_deleted": True,
            "remaining_disposable_workspaces": 0,
            "remaining_disposable_users": 0,
            "remaining_disposable_datasets": 0,
            "docker_stack_stop_attempted": False,
            "docker_stack_state": "not_managed_or_asserted_by_validator",
        },
        "fixture_only": True,
        "backend_snapshot_only": True,
        "backend_immutability_verified": False,
        "project_logical_lock_created": False,
        "semantic_labels_created": False,
        "gold_labels_created": False,
        "training_eligible_records_created": False,
        "human_identity_verified": False,
        "human_gold_admission_enabled": False,
        "production_annotation_round_started": False,
        "completed_at": datetime.datetime.now(tz=datetime.UTC).isoformat(),
    }


def _artifact_entry(path: Path, payload: bytes, report: dict[str, object]) -> dict[str, object]:
    return {
        "sha256": sha256_hex(payload),
        "path": path.name,
        "report_kind": report.get("report_kind"),
        "schema_version": report.get("schema_version"),
        "completed_at": report.get("completed_at"),
    }


def _archive_legacy_report(index_path: Path, raw: bytes) -> dict[str, object]:
    payload = _parse_json_object(raw, owner="legacy Argilla integration report")
    if payload.get("report_kind") != "lf023_argilla_local_integration_v1":
        raise RuntimeError("existing Argilla integration index has an unsupported schema")
    runs_dir = index_path.parent / "argilla_local_integration_runs"
    digest = sha256_hex(raw)
    run_path = runs_dir / f"{digest}.json"
    runs_dir.mkdir(parents=True, exist_ok=True)
    if run_path.exists() and run_path.read_bytes() != raw:
        raise RuntimeError("legacy content-addressed report path contains divergent bytes")
    if not run_path.exists():
        with run_path.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    return _artifact_entry(run_path, raw, payload)


def _load_index(index_path: Path) -> dict[str, object]:
    if not index_path.exists():
        return {
            "schema_version": 1,
            "report_kind": INDEX_KIND,
            "runs": [],
        }
    raw = index_path.read_bytes()
    payload = _parse_json_object(raw, owner="Argilla integration report index")
    if payload.get("report_kind") == "lf023_argilla_local_integration_v1":
        return {
            "schema_version": 1,
            "report_kind": INDEX_KIND,
            "runs": [_archive_legacy_report(index_path, raw)],
        }
    if payload.get("schema_version") != 1 or payload.get("report_kind") != INDEX_KIND:
        raise RuntimeError("existing Argilla integration index has an unsupported schema")
    runs = payload.get("runs")
    if not isinstance(runs, list) or not all(isinstance(item, dict) for item in runs):
        raise RuntimeError("Argilla integration index runs must be a list of objects")
    for item in runs:
        run_path_raw = item.get("path")
        expected_hash = item.get("sha256")
        if not isinstance(run_path_raw, str) or not isinstance(expected_hash, str):
            raise RuntimeError("Argilla integration index contains an invalid run entry")
        if (
            len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or run_path_raw != f"{expected_hash}.json"
        ):
            raise RuntimeError("Argilla integration index contains an unsafe run path")
        run_path = index_path.parent / "argilla_local_integration_runs" / run_path_raw
        if not run_path.is_file() or sha256_hex(run_path.read_bytes()) != expected_hash:
            raise RuntimeError("Argilla integration index references missing or changed evidence")
    return payload


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _write_report(*, index_path: Path, report: dict[str, object]) -> tuple[Path, str]:
    report_payload = canonical_json_bytes(report) + b"\n"
    digest = sha256_hex(report_payload)
    runs_dir = index_path.parent / "argilla_local_integration_runs"
    run_path = runs_dir / f"{digest}.json"
    lock_key = sha256_hex(str(index_path.resolve()).encode("utf-8"))
    lock_path = Path(tempfile.gettempdir()) / f"leanfaith_argilla_report_{lock_key}.lock"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        index = _load_index(index_path)
        runs = cast(list[dict[str, object]], index["runs"])
        if run_path.exists() and run_path.read_bytes() != report_payload:
            raise RuntimeError("content-addressed report path contains divergent bytes")
        if not run_path.exists():
            run_path.parent.mkdir(parents=True, exist_ok=True)
            with run_path.open("xb") as output:
                output.write(report_payload)
                output.flush()
                os.fsync(output.fileno())
        entry = _artifact_entry(run_path, report_payload, report)
        if not any(item.get("sha256") == digest for item in runs):
            runs.append(entry)
        runs.sort(key=lambda item: str(item["sha256"]))
        index["latest_sha256"] = digest
        index["latest_path"] = run_path.name
        _atomic_write(index_path, canonical_json_bytes(index) + b"\n")
    return run_path, digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--api-url-env",
        default="ARGILLA_API_URL",
        help="Environment variable containing the loopback Argilla URL.",
    )
    parser.add_argument(
        "--api-key-env",
        default="ARGILLA_API_KEY",
        help="Environment variable containing the Argilla owner API key.",
    )
    parser.add_argument(
        "--report-index",
        type=Path,
        default=Path("reports/annotation/argilla_local_integration_v1.json"),
        help=(
            "Stable append-only index. Each run is stored separately under a "
            "content-addressed path."
        ),
    )
    args = parser.parse_args()

    api_url = os.environ.get(args.api_url_env, "")
    api_key = os.environ.get(args.api_key_env, "")
    if not api_url or not api_key:
        print(
            f"FAILED: {args.api_url_env} and {args.api_key_env} must be set",
            file=sys.stderr,
        )
        return 2

    try:
        report = run(api_url=api_url, api_key=api_key)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    try:
        report_path, digest = _write_report(index_path=args.report_index, report=report)
    except Exception as exc:
        print(f"FAILED to persist immutable diagnostic evidence: {exc}", file=sys.stderr)
        return 1
    print(f"report={report_path}")
    print(f"report_index={args.report_index}")
    print(f"sha256={digest}")
    print("semantic_labels_created=0")
    print("gold_labels_created=0")
    print("training_eligible_records_created=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
