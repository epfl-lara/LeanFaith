from __future__ import annotations

import datetime
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.annotation_support.argilla_backend import (
    ArgillaDirectFetchReceiptV1,
    ArgillaDirectFetchRun,
    ArgillaV28RestTransport,
)
from leanfaith.cli import argilla_operations
from leanfaith.cli.app import app
from leanfaith.cli.argilla_operations import (
    ArgillaCliInputError,
    capture_argilla_submitted_responses,
    write_argilla_backend_pin,
)
from leanfaith.config.hashing import canonical_json_bytes

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
DATASET_ID = "22222222-2222-4222-8222-222222222222"
ANNOTATOR_ID = "33333333-3333-4333-8333-333333333333"
RECORD_ID = "44444444-4444-4444-8444-444444444444"
RESPONSE_ID = "55555555-5555-4555-8555-555555555555"
API_KEY_ENV = "LF_ARGILLA_TEST_API_KEY"
API_KEY = "not-persisted-test-api-key-0123456789"


def _pin(tmp_path: Path) -> Any:
    return write_argilla_backend_pin(
        endpoint="https://argilla.internal.example",
        workspace_id=WORKSPACE_ID,
        dataset_id=DATASET_ID,
        annotator_id=ANNOTATOR_ID,
        api_key_env=API_KEY_ENV,
        output_dir=tmp_path / "pins",
    )


def _manifest(pin_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "manifest_kind": "lf023_argilla_expected_responses_v1",
        "backend_pin_id": pin_id,
        "expected_responses": [
            {
                "schema_version": 1,
                "backend_record_id": RECORD_ID,
                "backend_response_id": RESPONSE_ID,
                "backend_submission_id": RESPONSE_ID,
            }
        ],
        "semantic_labels_included": False,
        "human_gold_eligible": False,
        "training_eligible": False,
    }


def _write_manifest(path: Path, pin_id: str) -> Path:
    path.write_bytes(canonical_json_bytes(_manifest(pin_id)) + b"\n")
    return path


def test_pin_writer_is_content_addressed_private_idempotent_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV, API_KEY)

    first = _pin(tmp_path)
    second = _pin(tmp_path)

    assert first == second
    assert first.path.name == f"{first.pin.pin_id.rsplit(':', 1)[-1]}.json"
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    raw = first.path.read_bytes()
    assert API_KEY.encode() not in raw
    assert json.loads(raw)["api_key_env"] == API_KEY_ENV

    first.path.chmod(0o600)
    first.path.write_text("{}\n")
    with pytest.raises(ArgillaCliInputError, match="divergent bytes"):
        _pin(tmp_path)


def test_capture_reads_only_pinned_environment_secret_and_forces_backend_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin_result = _pin(tmp_path)
    manifest_path = _write_manifest(tmp_path / "expected.json", pin_result.pin.pin_id)
    observed: dict[str, object] = {}

    def fake_fetch(**kwargs: object) -> ArgillaDirectFetchRun:
        observed.update(kwargs)
        output_root = cast(Path, kwargs["output_root"])
        output_root.mkdir(mode=0o700)
        receipt_path = output_root / "receipt.json"
        dataset_path = output_root / "dataset.json"
        record_path = output_root / "record.json"
        for path in (receipt_path, dataset_path, record_path):
            path.write_text("{}")
            path.chmod(0o600)
        receipt = cast(
            ArgillaDirectFetchReceiptV1,
            SimpleNamespace(
                backend_pin_id=pin_result.pin.pin_id,
                backend_record_id=RECORD_ID,
                backend_response_id=RESPONSE_ID,
                backend_submission_id=RESPONSE_ID,
                backend_origin_transport_verified=True,
                fixture_only=False,
                fetched_at=kwargs["fetched_at"],
            ),
        )
        return ArgillaDirectFetchRun(
            receipts=(receipt,),
            receipt_paths=(receipt_path,),
            raw_dataset_payload_paths=(dataset_path,),
            raw_record_payload_paths=(record_path,),
        )

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setattr(argilla_operations, "fetch_argilla_responses", fake_fetch)
    fetched_at = datetime.datetime(2026, 7, 28, 15, 0, tzinfo=datetime.UTC)

    result = capture_argilla_submitted_responses(
        pin_path=pin_result.path,
        expected_manifest_path=manifest_path,
        output_root=tmp_path / "capture",
        fetched_at=fetched_at,
    )

    transport = observed["transport"]
    assert type(transport) is ArgillaV28RestTransport
    assert isinstance(transport, ArgillaV28RestTransport)
    assert transport._uses_production_network_transport() is True
    assert observed["api_key"] == API_KEY
    assert observed["artifact_class"] == "backend_origin"
    assert observed["fetched_at"] == fetched_at
    assert result.pin_sha256
    assert result.expected_manifest_sha256
    assert result.manifest.entry_count == 1
    assert result.manifest_path.is_file()
    assert stat.S_IMODE(result.manifest_path.stat().st_mode) == 0o600
    assert result.manifest.backend_pin.sha256 == result.pin_sha256
    assert result.manifest.expected_response_manifest.sha256 == result.expected_manifest_sha256
    assert API_KEY not in repr(result)
    assert API_KEY.encode() not in pin_result.path.read_bytes()
    assert API_KEY.encode() not in manifest_path.read_bytes()
    manifest_payload = result.manifest.model_dump(mode="json")
    manifest_payload["schema_version"] = True
    with pytest.raises(ValueError):
        argilla_operations.ArgillaCaptureManifestV1.model_validate(manifest_payload)
    manifest_payload = result.manifest.model_dump(mode="json")
    manifest_payload["training_eligible"] = 0
    with pytest.raises(ValueError):
        argilla_operations.ArgillaCaptureManifestV1.model_validate(manifest_payload)


def test_capture_rejects_missing_secret_and_wrong_pin_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _pin(tmp_path / "first")
    second = write_argilla_backend_pin(
        endpoint="https://other-argilla.internal.example",
        workspace_id=WORKSPACE_ID,
        dataset_id=DATASET_ID,
        annotator_id=ANNOTATOR_ID,
        api_key_env=API_KEY_ENV,
        output_dir=tmp_path / "second" / "pins",
    )
    first_manifest = _write_manifest(tmp_path / "first.json", first.pin.pin_id)
    second_manifest = _write_manifest(tmp_path / "second.json", second.pin.pin_id)
    calls = 0

    def forbidden_fetch(**kwargs: object) -> ArgillaDirectFetchRun:
        del kwargs
        nonlocal calls
        calls += 1
        raise AssertionError("transport must not run")

    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setattr(argilla_operations, "fetch_argilla_responses", forbidden_fetch)
    with pytest.raises(ArgillaCliInputError, match="environment variable is unset"):
        capture_argilla_submitted_responses(
            pin_path=first.path,
            expected_manifest_path=first_manifest,
            output_root=tmp_path / "missing-key",
        )

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    with pytest.raises(ArgillaCliInputError, match="different backend pin"):
        capture_argilla_submitted_responses(
            pin_path=first.path,
            expected_manifest_path=second_manifest,
            output_root=tmp_path / "wrong-pin",
        )
    assert calls == 0


@pytest.mark.parametrize(
    "manifest_bytes",
    [
        b"{",
        (
            b'{"schema_version":1,"schema_version":1,'
            b'"manifest_kind":"lf023_argilla_expected_responses_v1"}'
        ),
    ],
    ids=["malformed", "duplicate-key"],
)
def test_capture_rejects_invalid_expected_manifest_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_bytes: bytes,
) -> None:
    pin = _pin(tmp_path)
    manifest_path = tmp_path / "invalid-expected.json"
    manifest_path.write_bytes(manifest_bytes)
    output_root = tmp_path / "must-not-exist"
    calls = 0

    def forbidden_fetch(**kwargs: object) -> ArgillaDirectFetchRun:
        del kwargs
        nonlocal calls
        calls += 1
        raise AssertionError("transport must not run")

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setattr(argilla_operations, "fetch_argilla_responses", forbidden_fetch)

    with pytest.raises(ArgillaCliInputError):
        capture_argilla_submitted_responses(
            pin_path=pin.path,
            expected_manifest_path=manifest_path,
            output_root=output_root,
        )

    assert calls == 0
    assert not output_root.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("semantic_labels_included", 0),
        ("human_gold_eligible", 0),
        ("training_eligible", 0),
    ],
)
def test_capture_rejects_coercible_manifest_types_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    pin = _pin(tmp_path)
    manifest = _manifest(pin.pin.pin_id)
    manifest[field] = value
    manifest_path = tmp_path / "coercible-expected.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    output_root = tmp_path / "must-not-exist"

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setattr(
        argilla_operations,
        "fetch_argilla_responses",
        lambda **kwargs: pytest.fail(f"transport must not run: {kwargs}"),
    )

    with pytest.raises(ArgillaCliInputError, match="strict schema validation"):
        capture_argilla_submitted_responses(
            pin_path=pin.path,
            expected_manifest_path=manifest_path,
            output_root=output_root,
        )
    assert not output_root.exists()


def test_capture_rejects_coercible_nested_response_schema_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = _pin(tmp_path)
    manifest = _manifest(pin.pin.pin_id)
    expected = manifest["expected_responses"]
    assert isinstance(expected, list)
    assert isinstance(expected[0], dict)
    expected[0]["schema_version"] = True
    manifest_path = tmp_path / "coercible-nested-expected.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    output_root = tmp_path / "must-not-exist"

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setattr(
        argilla_operations,
        "fetch_argilla_responses",
        lambda **kwargs: pytest.fail(f"transport must not run: {kwargs}"),
    )

    with pytest.raises(ArgillaCliInputError, match="strict schema validation"):
        capture_argilla_submitted_responses(
            pin_path=pin.path,
            expected_manifest_path=manifest_path,
            output_root=output_root,
        )
    assert not output_root.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("self_hosted", 1),
    ],
)
def test_capture_rejects_coercible_pin_types_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    pin = _pin(tmp_path)
    pin_payload = pin.pin.model_dump(mode="json")
    pin_payload[field] = value
    malformed_pin = tmp_path / "coercible-pin.json"
    malformed_pin.write_bytes(canonical_json_bytes(pin_payload) + b"\n")
    manifest_path = _write_manifest(tmp_path / "expected.json", pin.pin.pin_id)
    output_root = tmp_path / "must-not-exist"

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setattr(
        argilla_operations,
        "fetch_argilla_responses",
        lambda **kwargs: pytest.fail(f"transport must not run: {kwargs}"),
    )

    with pytest.raises(ArgillaCliInputError, match="strict schema validation"):
        capture_argilla_submitted_responses(
            pin_path=malformed_pin,
            expected_manifest_path=manifest_path,
            output_root=output_root,
        )
    assert not output_root.exists()


def test_cli_does_not_echo_rejected_pin_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_sentinel = "MUST-NOT-ENTER-CLI-ERROR-OUTPUT"
    pin = _pin(tmp_path)
    pin_payload = pin.pin.model_dump(mode="json")
    pin_payload["api_key"] = secret_sentinel
    malformed_pin = tmp_path / "pin-with-rejected-secret.json"
    malformed_pin.write_bytes(canonical_json_bytes(pin_payload) + b"\n")
    manifest_path = _write_manifest(tmp_path / "expected.json", pin.pin.pin_id)

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    result = CliRunner().invoke(
        app,
        [
            "capture-argilla-responses",
            "--pin",
            str(malformed_pin),
            "--expected-responses",
            str(manifest_path),
            "--output-root",
            str(tmp_path / "must-not-exist"),
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "failed strict schema validation" in result.output
    assert secret_sentinel not in result.output
    assert not (tmp_path / "must-not-exist").exists()


def test_pin_writer_rejects_public_existing_output_dir_without_chmod(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o755)
    before = stat.S_IMODE(tmp_path.stat().st_mode)

    result = CliRunner().invoke(
        app,
        [
            "write-argilla-backend-pin",
            "--endpoint",
            "https://argilla.internal.example",
            "--workspace-id",
            WORKSPACE_ID,
            "--dataset-id",
            DATASET_ID,
            "--annotator-id",
            ANNOTATOR_ID,
            "--api-key-env",
            API_KEY_ENV,
            "--output-dir",
            ".",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "not private" in result.output
    assert stat.S_IMODE(tmp_path.stat().st_mode) == before
    assert list(tmp_path.glob("*.json")) == []


def test_capture_rejects_symlinked_pin_parent_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = _pin(tmp_path / "real")
    manifest_path = _write_manifest(tmp_path / "expected.json", pin.pin.pin_id)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(pin.path.parent, target_is_directory=True)
    calls = 0

    def forbidden_fetch(**kwargs: object) -> ArgillaDirectFetchRun:
        del kwargs
        nonlocal calls
        calls += 1
        raise AssertionError("transport must not run")

    monkeypatch.setenv(API_KEY_ENV, API_KEY)
    monkeypatch.setattr(argilla_operations, "fetch_argilla_responses", forbidden_fetch)
    with pytest.raises(ArgillaCliInputError, match="stable regular file"):
        capture_argilla_submitted_responses(
            pin_path=linked_parent / pin.path.name,
            expected_manifest_path=manifest_path,
            output_root=tmp_path / "must-not-exist",
        )
    assert calls == 0
    assert not (tmp_path / "must-not-exist").exists()


def test_argilla_cli_commands_are_exposed_without_api_key_value_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    pin_help = runner.invoke(app, ["write-argilla-backend-pin", "--help"])
    capture_help = runner.invoke(app, ["capture-argilla-responses", "--help"])

    assert pin_help.exit_code == 0
    assert "--api-key-env" in pin_help.stdout
    assert capture_help.exit_code == 0
    assert "--api-key " not in capture_help.stdout
    assert "--expected-responses" in capture_help.stdout

    result = runner.invoke(
        app,
        [
            "write-argilla-backend-pin",
            "--endpoint",
            "https://argilla.internal.example",
            "--workspace-id",
            WORKSPACE_ID,
            "--dataset-id",
            DATASET_ID,
            "--annotator-id",
            ANNOTATOR_ID,
            "--api-key-env",
            API_KEY_ENV,
            "--output-dir",
            "pins",
            "--root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert "api_key_persisted=false" in result.stdout
    assert "semantic_labels_created=0" in result.stdout
    assert API_KEY not in result.stdout
    assert len(list((tmp_path / "pins").glob("*.json"))) == 1

    fake_capture = argilla_operations.ArgillaCaptureResult(
        run=ArgillaDirectFetchRun(
            receipts=(),
            receipt_paths=(),
            raw_dataset_payload_paths=(),
            raw_record_payload_paths=(),
        ),
        pin_path=tmp_path / "pin.json",
        pin_sha256="a" * 64,
        expected_manifest_path=tmp_path / "expected.json",
        expected_manifest_sha256="b" * 64,
        output_root=tmp_path / "capture",
        manifest=argilla_operations.ArgillaCaptureManifestV1.model_construct(
            manifest_id="lf023_argilla_capture_manifest_v1:" + "c" * 64
        ),
        manifest_path=tmp_path / "capture" / "manifest.json",
    )
    monkeypatch.setattr(
        argilla_operations,
        "capture_argilla_submitted_responses",
        lambda **kwargs: fake_capture,
    )
    capture = runner.invoke(
        app,
        [
            "capture-argilla-responses",
            "--pin",
            "pin.json",
            "--expected-responses",
            "expected.json",
            "--output-root",
            "capture",
            "--root",
            str(tmp_path),
        ],
    )
    assert capture.exit_code == 0
    assert "submitted_snapshot_only=true" in capture.stdout
    assert "backend_immutability_verified=false" in capture.stdout
    assert "semantic_labels_created=0" in capture.stdout
    assert "gold_labels_created=0" in capture.stdout
    assert "human_gold_eligible=false" in capture.stdout
    assert "training_eligible=0" in capture.stdout
    assert "capture_manifest_id=" in capture.stdout
    assert API_KEY not in capture.stdout
