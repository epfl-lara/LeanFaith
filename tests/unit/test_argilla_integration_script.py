from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex


def _load_script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "44_validate_argilla_integration.py"
    spec = importlib.util.spec_from_file_location("validate_argilla_integration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_script()


def _report(completed_at: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "report_kind": SCRIPT.REPORT_KIND,
        "completed_at": completed_at,
        "semantic_labels_created": False,
        "gold_labels_created": False,
        "training_eligible_records_created": False,
    }


def test_report_writer_preserves_every_content_addressed_run(tmp_path: Path) -> None:
    index_path = tmp_path / "argilla_local_integration_v1.json"
    first_path, first_hash = SCRIPT._write_report(
        index_path=index_path,
        report=_report("2026-07-28T12:00:00+00:00"),
    )
    first_bytes = first_path.read_bytes()

    second_path, second_hash = SCRIPT._write_report(
        index_path=index_path,
        report=_report("2026-07-28T12:01:00+00:00"),
    )

    assert first_hash != second_hash
    assert first_path.read_bytes() == first_bytes
    assert first_path.name == f"{first_hash}.json"
    assert second_path.name == f"{second_hash}.json"
    index = json.loads(index_path.read_bytes())
    assert index["latest_sha256"] == second_hash
    assert {item["sha256"] for item in index["runs"]} == {first_hash, second_hash}


def test_report_writer_archives_legacy_fixed_path_before_replacing_it(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "argilla_local_integration_v1.json"
    legacy = {
        "schema_version": 1,
        "report_kind": "lf023_argilla_local_integration_v1",
        "completed_at": "2026-07-28T11:00:00+00:00",
    }
    legacy_bytes = canonical_json_bytes(legacy) + b"\n"
    index_path.write_bytes(legacy_bytes)

    SCRIPT._write_report(
        index_path=index_path,
        report=_report("2026-07-28T12:00:00+00:00"),
    )

    legacy_hash = sha256_hex(legacy_bytes)
    archived = tmp_path / "argilla_local_integration_runs" / f"{legacy_hash}.json"
    assert archived.read_bytes() == legacy_bytes
    index = json.loads(index_path.read_bytes())
    assert legacy_hash in {item["sha256"] for item in index["runs"]}


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpClient:
    def __init__(self, statuses: dict[str, int]) -> None:
        self.statuses = statuses

    def get(self, path: str) -> _Response:
        return _Response(self.statuses[path])


class _Workspace:
    def __init__(self, name: str) -> None:
        self.name = name


class _Dataset:
    def __init__(self, workspace: str, name: str) -> None:
        self.workspace = _Workspace(workspace)
        self.name = name


class _Datasets:
    def __init__(self) -> None:
        self.own = _Dataset("own-workspace", "own-dataset")

    def __iter__(self) -> Any:
        yield self.own

    def __call__(self, *, name: str, workspace: str) -> _Dataset | None:
        if (workspace, name) == ("own-workspace", "own-dataset"):
            return self.own
        return None


class _Client:
    def __init__(self, statuses: dict[str, int]) -> None:
        self.workspaces = [_Workspace("own-workspace")]
        self.datasets = _Datasets()
        self.http_client = _HttpClient(statuses)


def test_peer_isolation_requires_direct_dataset_and_record_denial() -> None:
    statuses = {
        "/api/v1/datasets/peer-dataset-id": 403,
        "/api/v1/records/peer-record-id": 404,
    }
    result = SCRIPT._assert_peer_isolation(
        client=_Client(statuses),
        own_workspace="own-workspace",
        own_dataset="own-dataset",
        peer_workspace="peer-workspace",
        peer_dataset="peer-dataset",
        peer_dataset_id="peer-dataset-id",
        peer_record_id="peer-record-id",
    )
    assert result == {"dataset": 403, "record": 404}


def test_peer_isolation_rejects_direct_peer_record_access() -> None:
    statuses = {
        "/api/v1/datasets/peer-dataset-id": 403,
        "/api/v1/records/peer-record-id": 200,
    }
    with pytest.raises(RuntimeError, match="record fetch returned HTTP 200"):
        SCRIPT._assert_peer_isolation(
            client=_Client(statuses),
            own_workspace="own-workspace",
            own_dataset="own-dataset",
            peer_workspace="peer-workspace",
            peer_dataset="peer-dataset",
            peer_dataset_id="peer-dataset-id",
            peer_record_id="peer-record-id",
        )
