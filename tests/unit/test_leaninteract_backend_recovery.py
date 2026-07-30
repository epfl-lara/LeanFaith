"""Focused regressions for Gate 3 LeanInteract session isolation."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations import (
    RepresentationBatch,
    TheoremForRepresentation,
    build_representation_batch,
)
from leanfaith.schemas.ids import make_id


class _Response:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self._raw


class _Server:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.killed = False
        self.calls = 0

    def run(self, request: object, *, timeout: float) -> _Response:
        del request, timeout
        self.calls += 1
        return self.response

    def kill(self) -> None:
        self.killed = True


def _raw(messages: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "declarations": [],
        "env": 0,
        "infotree": None,
        "messages": messages,
        "sorries": [],
        "tactics": [],
    }


def test_corrupt_core_environment_is_retried_on_fresh_server(
    monkeypatch: Any, tmp_path: Path
) -> None:
    corrupt = _Server(
        _Response(
            _raw(
                [
                    {"severity": "error", "data": "unknown namespace `Lean`"},
                    {"severity": "error", "data": "Unknown constant `CoeFun`"},
                ]
            )
        )
    )
    recovered = _Server(_Response(_raw([])))
    servers = iter((corrupt, recovered))
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
        )
    )

    def ensure_server() -> _Server:
        if backend._server is None:
            backend._server = next(servers)  # type: ignore[assignment]
        return backend._server  # type: ignore[return-value]

    monkeypatch.setattr(backend, "_ensure_server", ensure_server)
    request = LeanRequest(
        request_id="gate3-core-recovery",
        context_id="ctx:" + "0" * 64,
        code="import Lean\nimport Mathlib\ntheorem recovered : True := trivial",
    )
    try:
        result = backend.run(request)
        assert result.status == LeanStatus.VALID
        assert corrupt.calls == 1
        assert corrupt.killed
        assert recovered.calls == 1
        artifacts = sorted((tmp_path / "raw").glob("*.json"))
        assert len(artifacts) == 2
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in artifacts]
        assert {payload["request_hash"] for payload in payloads} == {result.request_hash}
        assert {payload["request"]["request_id"] for payload in payloads} == {request.request_id}
        assert any("core-recovery" in path.name for path in artifacts)
    finally:
        backend.close()


def test_ordinary_invalid_response_is_not_retried(monkeypatch: Any, tmp_path: Path) -> None:
    invalid = _Server(_Response(_raw([{"severity": "error", "data": "type mismatch"}])))
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
        )
    )
    monkeypatch.setattr(backend, "_ensure_server", lambda: invalid)
    try:
        result = backend.run(
            LeanRequest(
                request_id="ordinary-invalid",
                context_id="ctx:" + "0" * 64,
                code="import Lean\ntheorem bad : False := trivial",
            )
        )
        assert result.status == LeanStatus.INVALID
        assert invalid.calls == 1
        assert not invalid.killed
        assert len(list((tmp_path / "raw").glob("*.json"))) == 1
    finally:
        backend.close()


def test_raw_response_resubmission_never_overwrites_first_observation(
    tmp_path: Path,
) -> None:
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
        )
    )
    request = LeanRequest(
        request_id="immutable-raw",
        context_id="ctx:" + "0" * 64,
        code="theorem immutable_raw : True := trivial",
    )
    request_hash = "a" * 64
    first = backend._persist_raw(request, request_hash, {"env": 11}, None)
    second = backend._persist_raw(request, request_hash, {"env": 12}, None)
    repeated_second = backend._persist_raw(request, request_hash, {"env": 12}, None)

    assert first != second
    assert second == repeated_second
    assert json.loads(Path(first).read_text(encoding="utf-8"))["response"] == {"env": 11}
    assert json.loads(Path(second).read_text(encoding="utf-8"))["response"] == {"env": 12}
    assert len(tuple((tmp_path / "raw").glob("*.json"))) == 2


def _representation_result(
    request: LeanRequest,
    status: LeanStatus,
    messages: tuple[dict[str, str], ...] = (),
) -> LeanResult:
    return LeanResult(
        request_id=request.request_id,
        request_hash="a" * 64,
        context_id=request.context_id,
        context_fingerprint="a" * 64,
        status=status,
        messages=messages,
    )


def _representation_fixture() -> tuple[str, TheoremForRepresentation]:
    context_id = "ctx:" + "a" * 64
    theorem = TheoremForRepresentation(
        theorem_id=make_id("thm", {"retry": "fixture"}),
        full_name="fixture",
        proof_stripped="theorem fixture : True := by sorry",
        context_id=context_id,
        inline_declaration=True,
    )
    return context_id, theorem


def test_representation_combined_crash_retries_with_attempt_lineage() -> None:
    messages = (
        {
            "severity": "info",
            "data": 'LFSIGPPJSON {"name":"fixture","signature_pp":"True"}',
        },
        {
            "severity": "info",
            "data": ('LFSIGEXPLICITJSON {"name":"fixture","signature_explicit":"True"}'),
        },
        {
            "severity": "info",
            "data": ('LFTREEJSON {"name":"fixture","tree":{"k":"const","n":"True","us":"[]"}}'),
        },
    )

    class CrashThenValidBackend:
        def __init__(self) -> None:
            self.seen: list[LeanRequest] = []

        def run(self, request: LeanRequest) -> LeanResult:
            self.seen.append(request)
            if len(self.seen) == 1:
                return _representation_result(request, LeanStatus.CRASH)
            return _representation_result(request, LeanStatus.VALID_WITH_SORRY, messages)

    context_id, theorem = _representation_fixture()
    backend = CrashThenValidBackend()
    result = build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(context_id, "import Mathlib", (theorem,)),
        created_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.UTC),
    )

    (record,) = result.ordered_representation_records
    assert not result.per_theorem_failures
    assert record.signature_pp == "True"
    assert record.signature_explicit == "True"
    assert [request.metadata["attempt"] for request in backend.seen] == ["0", "1"]
    assert len({request.request_id for request in backend.seen}) == 1
    assert all((request.code or "").startswith("import Lean\n") for request in backend.seen)


def test_representation_single_check_crash_retries_with_attempt_lineage() -> None:
    combined_messages = (
        {
            "severity": "info",
            "data": 'LFSIGPPJSON {"name":"fixture","signature_pp":"True"}',
        },
        {
            "severity": "info",
            "data": ('LFTREEJSON {"name":"fixture","tree":{"k":"const","n":"True","us":"[]"}}'),
        },
    )
    explicit_messages = (
        {
            "severity": "info",
            "data": ('LFSIGEXPLICITJSON {"name":"fixture","signature_explicit":"True"}'),
        },
    )

    class CheckCrashThenValidBackend:
        def __init__(self) -> None:
            self.seen: list[LeanRequest] = []
            self.explicit_attempts = 0

        def run(self, request: LeanRequest) -> LeanResult:
            self.seen.append(request)
            if request.request_id.endswith("-combined"):
                return _representation_result(
                    request, LeanStatus.VALID_WITH_SORRY, combined_messages
                )
            assert request.request_id.endswith("-signature_explicit")
            self.explicit_attempts += 1
            if self.explicit_attempts == 1:
                return _representation_result(request, LeanStatus.CRASH)
            return _representation_result(request, LeanStatus.VALID_WITH_SORRY, explicit_messages)

    context_id, theorem = _representation_fixture()
    backend = CheckCrashThenValidBackend()
    result = build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(context_id, "import Mathlib", (theorem,)),
        created_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.UTC),
    )

    (record,) = result.ordered_representation_records
    assert not result.per_theorem_failures
    assert record.signature_explicit == "True"
    assert [request.metadata["attempt"] for request in backend.seen] == ["0", "0", "1"]
    assert all((request.code or "").startswith("import Lean\n") for request in backend.seen)


def test_representation_invalid_results_are_terminal() -> None:
    class InvalidBackend:
        def __init__(self) -> None:
            self.seen: list[LeanRequest] = []

        def run(self, request: LeanRequest) -> LeanResult:
            self.seen.append(request)
            return _representation_result(request, LeanStatus.INVALID)

    context_id, theorem = _representation_fixture()
    backend = InvalidBackend()
    build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(context_id, "import Mathlib", (theorem,)),
        created_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.UTC),
    )

    assert [request.metadata["attempt"] for request in backend.seen] == ["0"] * 4
    assert len({request.request_id for request in backend.seen}) == 4
    assert all((request.code or "").startswith("import Lean\n") for request in backend.seen)


def test_representation_correctness_requests_never_concatenate_theorems() -> None:
    class CapturingBackend:
        def __init__(self) -> None:
            self.codes: list[str] = []

        def run(self, request: LeanRequest) -> LeanResult:
            self.codes.append(request.code or "")
            return LeanResult(
                request_id=request.request_id,
                request_hash="a" * 64,
                context_id=request.context_id,
                context_fingerprint="a" * 64,
                status=LeanStatus.INVALID,
            )

    context_id = "ctx:" + "a" * 64
    first = TheoremForRepresentation(
        theorem_id=make_id("thm", {"isolation": 1}),
        full_name="isolated_first",
        proof_stripped="theorem isolated_first : True := by sorry",
        context_id=context_id,
        inline_declaration=True,
    )
    second = TheoremForRepresentation(
        theorem_id=make_id("thm", {"isolation": 2}),
        full_name="isolated_second",
        proof_stripped="theorem isolated_second : True := by sorry",
        context_id=context_id,
        inline_declaration=True,
    )
    backend = CapturingBackend()
    build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(context_id, "import Mathlib", (first, second)),
        created_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.UTC),
    )

    assert backend.codes
    for code in backend.codes:
        assert not ("theorem isolated_first" in code and "theorem isolated_second" in code)
