"""Focused regressions for Gate 3 LeanInteract session isolation."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import ServerMode
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


class _Pool:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _BatchPool(_Pool):
    def __init__(self, responses: list[_Response]) -> None:
        super().__init__()
        self.responses = responses
        self.timeout_calls: list[float] = []

    def run_batch(self, requests: list[object], *, timeout_per_cmd: float) -> list[_Response]:
        if self.closed:
            raise RuntimeError("pool used after close")
        assert len(requests) == len(self.responses)
        self.timeout_calls.append(timeout_per_cmd)
        return self.responses


def _raw(messages: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "declarations": [],
        "env": 0,
        "infotree": None,
        "messages": messages,
        "sorries": [],
        "tactics": [],
    }


def test_reset_session_discards_all_live_state_and_is_idempotent(tmp_path: Path) -> None:
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
        )
    )
    server = _Server(_Response(_raw([])))
    pool = _Pool()
    backend._server = server  # type: ignore[assignment]
    backend._pool = pool  # type: ignore[assignment]
    backend._auto_fallback_active = True

    backend.reset_session()

    assert server.killed
    assert pool.closed
    assert backend._server is None
    assert backend._pool is None
    assert not backend.auto_fallback_active
    backend.reset_session()


def test_command_isolation_prefix_and_async_option_are_opt_in(tmp_path: Path) -> None:
    context_id = "ctx:" + "0" * 64
    code = "theorem line_one : True := trivial"
    request = LeanRequest(request_id="isolated-a", context_id=context_id, code=code)
    isolated = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw-isolated",
            enable_parallel_elaboration=False,
            isolate_incremental_commands=True,
        )
    )
    default = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw-default",
        )
    )

    built = isolated._build_repl_request(request)
    assert built.cmd is not None
    assert built.cmd.endswith(code)
    assert built.cmd != code
    assert built.set_options == [(["Elab", "async"], False)]
    assert isolated._build_repl_request(request).cmd == built.cmd
    other = isolated._build_repl_request(
        LeanRequest(request_id="isolated-b", context_id=context_id, code=code)
    )
    assert other.cmd != built.cmd
    retry_request = LeanRequest(
        request_id=request.request_id,
        context_id=context_id,
        code=code,
        metadata={"attempt": "1"},
    )
    retry = isolated._build_repl_request(retry_request)
    assert isolated._request_hash(retry_request) == isolated._request_hash(request)
    assert retry.cmd != built.cmd

    default_built = default._build_repl_request(request)
    assert default_built.cmd == code
    assert default_built.set_options is None


def test_isolated_response_positions_are_restored_before_normalization(
    monkeypatch: Any, tmp_path: Path
) -> None:
    context_id = "ctx:" + "0" * 64
    request = LeanRequest(
        request_id="position-restore",
        context_id=context_id,
        code="theorem line_one : True := trivial",
        declarations=True,
    )
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            enable_parallel_elaboration=False,
            isolate_incremental_commands=True,
        )
    )
    width = len(backend._command_isolation_prefix(request))
    raw = _raw([])
    raw["messages"] = [
        {
            "severity": "warning",
            "data": "line-one diagnostic",
            "start_pos": {"line": 1, "column": width + 2},
            "end_pos": {"line": 2, "column": 4},
        }
    ]
    raw["declarations"] = [
        {
            "name": "line_one",
            "range": {
                "start": {"line": 1, "column": width},
                "finish": {"line": 1, "column": width + 32},
            },
        }
    ]
    server = _Server(_Response(raw))
    monkeypatch.setattr(backend, "_ensure_server", lambda: server)

    result = backend.run(request)

    assert result.messages[0]["start_pos"] == {"line": 1, "column": 2}
    assert result.messages[0]["end_pos"] == {"line": 2, "column": 4}
    assert result.declarations[0]["range"] == {
        "start": {"line": 1, "column": 0},
        "finish": {"line": 1, "column": 32},
    }
    persisted = json.loads(Path(result.raw_response_path or "").read_text(encoding="utf-8"))
    assert persisted["response"]["declarations"][0]["range"]["start"]["column"] == width
    assert persisted["transport_isolation"]["version"] == ("deterministic_request_nonce_prefix_v1")
    assert persisted["transport_isolation"]["attempt"] == "0"


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


@pytest.mark.parametrize(
    ("import_header", "missing_namespace"),
    (
        ("import Mathlib", "unknown namespace `Real`"),
        ("import Mathlib", "unknown namespace `Classical`"),
        ("import Mathlib", "unknown namespace `Topology`"),
        ("import /- inline -/ Mathlib", "unknown namespace `Real`"),
        ("import Mathlib /- trailing\n-/", "unknown namespace `Real`"),
        (
            "\n".join(
                (
                    "import Mathlib.Analysis.SpecialFunctions.Exp",
                    "import Mathlib.Tactic.Positivity.Core",
                    "import Mathlib.Algebra.Ring.NegOnePow",
                    "import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic",
                )
            ),
            "unknown namespace `Topology`",
        ),
        (
            "\n".join(
                (
                    "/- copyright fixture -/",
                    "module",
                    "",
                    "public import Mathlib.Algebra.Algebra.Equiv",
                    "public import Mathlib.LinearAlgebra.Span.Basic",
                )
            ),
            "unknown namespace `Real`",
        ),
    ),
)
def test_corrupt_mathlib_environment_is_retried_on_fresh_server(
    monkeypatch: Any, tmp_path: Path, import_header: str, missing_namespace: str
) -> None:
    corrupt = _Server(
        _Response(
            _raw(
                [
                    {"severity": "error", "data": missing_namespace},
                    {"severity": "error", "data": "Unknown constant `OfNat`"},
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
        request_id="gate2-mathlib-recovery",
        context_id="ctx:" + "0" * 64,
        code=f"{import_header}\ntheorem recovered : (1 : Nat) = 1 := rfl",
    )
    try:
        result = backend.run(request)
        assert result.status == LeanStatus.VALID
        assert corrupt.calls == 1
        assert corrupt.killed
        assert recovered.calls == 1
        artifacts = sorted((tmp_path / "raw").glob("*.json"))
        assert len(artifacts) == 2
        assert any("core-recovery" in path.name for path in artifacts)
    finally:
        backend.close()


def test_pool_response_uses_same_environment_recovery_path(
    monkeypatch: Any, tmp_path: Path
) -> None:
    corrupt = _Response(
        _raw(
            [
                {"severity": "error", "data": "unknown namespace `Real`"},
                {"severity": "error", "data": "Unknown constant `OfNat`"},
            ]
        )
    )
    recovered_server = _Server(_Response(_raw([])))
    pool = _Pool()
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            server_mode=ServerMode.POOL,
        )
    )
    backend._pool = pool  # type: ignore[assignment]

    def ensure_server() -> _Server:
        if backend._server is None:
            backend._server = recovered_server  # type: ignore[assignment]
        return backend._server  # type: ignore[return-value]

    monkeypatch.setattr(backend, "_ensure_server", ensure_server)
    request = LeanRequest(
        request_id="gate2-pool-mathlib-recovery",
        context_id="ctx:" + "0" * 64,
        code="import Mathlib\ntheorem recovered : (1 : Nat) = 1 := rfl",
    )
    try:
        result = backend._normalize_pool_item(
            request,
            backend._request_hash(request),
            corrupt,
            7,
        )
        assert result.status == LeanStatus.VALID
        assert result.elapsed_ms >= 7
        assert pool.closed
        assert backend._pool is None
        assert recovered_server.calls == 1
        assert recovered_server.killed
        assert len(list((tmp_path / "raw").glob("*.json"))) == 2
    finally:
        backend.close()


def test_pool_recovery_reacquires_pool_for_later_timeout_group(
    monkeypatch: Any, tmp_path: Path
) -> None:
    corrupt = _Response(
        _raw(
            [
                {"severity": "error", "data": "unknown namespace `Real`"},
                {"severity": "error", "data": "Unknown constant `OfNat`"},
            ]
        )
    )
    first_pool = _BatchPool([corrupt])
    second_pool = _BatchPool([_Response(_raw([]))])
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            server_mode=ServerMode.POOL,
        )
    )
    backend._pool = first_pool  # type: ignore[assignment]

    def ensure_pool() -> _BatchPool:
        if backend._pool is None:
            backend._pool = second_pool  # type: ignore[assignment]
        return backend._pool  # type: ignore[return-value]

    monkeypatch.setattr(backend, "_ensure_pool", ensure_pool)
    requests = (
        LeanRequest(
            request_id="pool-corrupt-first-group",
            context_id="ctx:" + "0" * 64,
            code="import Mathlib\ntheorem first : (1 : Nat) = 1 := rfl",
            timeout_seconds=1.0,
        ),
        LeanRequest(
            request_id="pool-valid-second-group",
            context_id="ctx:" + "0" * 64,
            code="import Mathlib\ntheorem second : True := trivial",
            timeout_seconds=2.0,
        ),
    )
    try:
        results = backend.run_batch(requests)
        assert [result.status for result in results] == [LeanStatus.VALID, LeanStatus.VALID]
        assert first_pool.timeout_calls == [1.0]
        assert first_pool.closed
        assert second_pool.timeout_calls == [1.0, 2.0]
    finally:
        backend.close()


def test_pool_coordinates_multiple_corrupt_items_with_one_subset_retry(
    monkeypatch: Any, tmp_path: Path
) -> None:
    corrupt = _Response(
        _raw(
            [
                {"severity": "error", "data": "unknown namespace `Real`"},
                {"severity": "error", "data": "Unknown constant `OfNat`"},
            ]
        )
    )
    first_pool = _BatchPool([corrupt, corrupt, _Response(_raw([]))])
    replacement_pool = _BatchPool([_Response(_raw([])), _Response(_raw([]))])
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            server_mode=ServerMode.POOL,
            workers=3,
        )
    )
    backend._pool = first_pool  # type: ignore[assignment]
    pool_creations = 0

    def ensure_pool() -> _BatchPool:
        nonlocal pool_creations
        if backend._pool is None:
            pool_creations += 1
            backend._pool = replacement_pool  # type: ignore[assignment]
        return backend._pool  # type: ignore[return-value]

    monkeypatch.setattr(backend, "_ensure_pool", ensure_pool)
    monkeypatch.setattr(
        backend,
        "_ensure_server",
        lambda: pytest.fail("coordinated pool recovery must not launch per-item stable servers"),
    )
    requests = tuple(
        LeanRequest(
            request_id=f"coordinated-{index}",
            context_id="ctx:" + "0" * 64,
            code=f"import Mathlib\ntheorem t{index} : True := trivial",
            timeout_seconds=3.0,
        )
        for index in range(3)
    )

    try:
        results = backend.run_batch(requests)
        assert [result.request_id for result in results] == [
            request.request_id for request in requests
        ]
        assert [result.request_hash for result in results] == [
            backend._request_hash(request) for request in requests
        ]
        assert [result.status for result in results] == [LeanStatus.VALID] * 3
        assert first_pool.timeout_calls == [3.0]
        assert first_pool.closed
        assert replacement_pool.timeout_calls == [3.0]
        assert pool_creations == 1
        assert len(list((tmp_path / "raw").glob("*.json"))) == 5
    finally:
        backend.close()


def test_pool_invalid_confirmation_reacquires_pool_for_later_timeout_group(
    monkeypatch: Any, tmp_path: Path
) -> None:
    invalid = _Response(_raw([{"severity": "error", "data": "expected token"}]))
    first_pool = _BatchPool([invalid])
    second_pool = _BatchPool([_Response(_raw([]))])
    oracle_server = _Server(_Response(_raw([])))
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            server_mode=ServerMode.POOL,
            workers=1,
            confirm_invalid_on_fresh_process=True,
        )
    )
    oracle = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            enable_incremental_optimization=False,
            enable_parallel_elaboration=False,
            environment_is_prepared=True,
        )
    )
    backend._pool = first_pool  # type: ignore[assignment]
    oracle._server = oracle_server  # type: ignore[assignment]

    def ensure_pool() -> _BatchPool:
        if backend._pool is None:
            backend._pool = second_pool  # type: ignore[assignment]
        return backend._pool  # type: ignore[return-value]

    monkeypatch.setattr(backend, "_ensure_pool", ensure_pool)
    monkeypatch.setattr(backend, "_fresh_invalid_confirmation_backend", lambda: oracle)
    requests = (
        LeanRequest(
            request_id="pool-provisional-invalid",
            context_id="ctx:" + "0" * 64,
            code="import Mathlib\ntheorem first : True := trivial",
            timeout_seconds=1.0,
        ),
        LeanRequest(
            request_id="pool-valid-after-confirmation",
            context_id="ctx:" + "0" * 64,
            code="import Mathlib\ntheorem second : True := trivial",
            timeout_seconds=2.0,
        ),
    )

    results = backend.run_batch(requests)

    assert [result.status for result in results] == [LeanStatus.VALID, LeanStatus.VALID]
    assert first_pool.timeout_calls == [1.0]
    assert first_pool.closed
    assert second_pool.timeout_calls == [2.0]
    assert oracle_server.calls == 1
    assert oracle_server.killed


def test_repeated_corrupt_environment_returns_infrastructure_failure(
    monkeypatch: Any, tmp_path: Path
) -> None:
    corrupt_raw = _raw(
        [
            {"severity": "error", "data": "unknown namespace `Real`"},
            {"severity": "error", "data": "Unknown constant `OfNat`"},
        ]
    )
    first = _Server(_Response(corrupt_raw))
    second = _Server(_Response(corrupt_raw))
    servers = iter((first, second))
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
    try:
        result = backend.run(
            LeanRequest(
                request_id="gate2-repeated-mathlib-corruption",
                context_id="ctx:" + "0" * 64,
                code="import Mathlib\ntheorem recovered : (1 : Nat) = 1 := rfl",
            )
        )
        assert result.status == LeanStatus.CRASH
        assert result.infrastructure_error == "core_environment_corruption_after_recovery"
        assert first.calls == second.calls == 1
        assert first.killed and second.killed
        assert len(list((tmp_path / "raw").glob("*.json"))) == 2
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


def test_sft_invalid_is_confirmed_by_fresh_nonincremental_process(
    monkeypatch: Any, tmp_path: Path
) -> None:
    partial = _raw([{"severity": "error", "data": "expected token"}])
    partial["declarations"] = [
        {
            "name": "algebra_304127",
            "full_name": "algebra_304127",
            "kind": "theorem",
            "signature": {"pp": "(hf : ∀ x"},
            "type": {"pp": ""},
        }
    ]
    contaminated = _Server(_Response(partial))
    valid = _Server(_Response(_raw([])))
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            enable_incremental_optimization=True,
            enable_parallel_elaboration=False,
            isolate_incremental_commands=True,
            confirm_invalid_on_fresh_process=True,
        )
    )
    oracle = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            enable_incremental_optimization=False,
            enable_parallel_elaboration=False,
            isolate_incremental_commands=False,
            environment_is_prepared=True,
        )
    )
    backend._server = contaminated  # type: ignore[assignment]
    oracle._server = valid  # type: ignore[assignment]
    monkeypatch.setattr(backend, "_fresh_invalid_confirmation_backend", lambda: oracle)
    request = LeanRequest(
        request_id="sft-row-31995",
        context_id="ctx:" + "0" * 64,
        code=(
            "import Mathlib\n"
            "theorem algebra_304127 (hf : ∀ x ∈ Set.Icc 0 1, True) : True := trivial"
        ),
    )

    result = backend.run(request)

    assert result.status == LeanStatus.VALID
    assert contaminated.calls == 1
    assert contaminated.killed
    assert valid.calls == 1
    assert valid.killed
    artifacts = sorted((tmp_path / "raw").glob("*.json"))
    assert len(artifacts) == 2
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in artifacts]
    assert {payload["request_hash"] for payload in payloads} == {result.request_hash}
    assert any("invalid-confirmation" in path.name for path in artifacts)
    confirmation = next(
        payload
        for path, payload in zip(artifacts, payloads, strict=True)
        if "invalid-confirmation" in path.name
    )
    assert confirmation["transport_isolation"] is None


def test_fresh_invalid_confirmation_backend_has_oracle_settings(tmp_path: Path) -> None:
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            server_mode=ServerMode.POOL,
            workers=4,
            enable_incremental_optimization=True,
            enable_parallel_elaboration=True,
            isolate_incremental_commands=True,
            confirm_invalid_on_fresh_process=True,
        )
    )

    oracle = backend._fresh_invalid_confirmation_backend()

    assert oracle._settings.server_mode == ServerMode.STABLE
    assert oracle._settings.workers is None
    assert not oracle._settings.enable_incremental_optimization
    assert not oracle._settings.enable_parallel_elaboration
    assert not oracle._settings.isolate_incremental_commands
    assert not oracle._settings.confirm_invalid_on_fresh_process
    assert oracle._settings.environment_is_prepared


def test_sft_semantic_invalid_remains_invalid_after_fresh_confirmation(
    monkeypatch: Any, tmp_path: Path
) -> None:
    invalid_raw = _raw([{"severity": "error", "data": "type mismatch"}])
    contaminated = _Server(_Response(invalid_raw))
    confirmed_invalid = _Server(_Response(invalid_raw))
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            confirm_invalid_on_fresh_process=True,
        )
    )
    oracle = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            enable_incremental_optimization=False,
            enable_parallel_elaboration=False,
            environment_is_prepared=True,
        )
    )
    backend._server = contaminated  # type: ignore[assignment]
    oracle._server = confirmed_invalid  # type: ignore[assignment]
    monkeypatch.setattr(backend, "_fresh_invalid_confirmation_backend", lambda: oracle)

    result = backend.run(
        LeanRequest(
            request_id="sft-confirm-real-invalid",
            context_id="ctx:" + "0" * 64,
            code="import Mathlib\ntheorem bad : False := trivial",
        )
    )

    assert result.status == LeanStatus.INVALID
    assert contaminated.calls == confirmed_invalid.calls == 1
    assert contaminated.killed and confirmed_invalid.killed
    assert len(tuple((tmp_path / "raw").glob("*.json"))) == 2
    assert result.raw_response_path is not None
    assert "invalid-confirmation" in result.raw_response_path


def test_sft_invalid_confirmation_infrastructure_failure_never_becomes_invalid(
    monkeypatch: Any, tmp_path: Path
) -> None:
    contaminated = _Server(_Response(_raw([{"severity": "error", "data": "expected token"}])))
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            confirm_invalid_on_fresh_process=True,
        )
    )
    oracle = LeanInteractBackend(
        BackendSettings(
            project_dir=tmp_path,
            context_fingerprint="0" * 64,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "raw",
            enable_incremental_optimization=False,
            enable_parallel_elaboration=False,
            environment_is_prepared=True,
        )
    )
    backend._server = contaminated  # type: ignore[assignment]
    monkeypatch.setattr(oracle, "_ensure_server", lambda: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(backend, "_fresh_invalid_confirmation_backend", lambda: oracle)

    result = backend.run(
        LeanRequest(
            request_id="sft-confirm-invalid-infra",
            context_id="ctx:" + "0" * 64,
            code="import Mathlib\ntheorem maybe_bad : True := trivial",
        )
    )

    assert result.status == LeanStatus.SETUP_ERROR
    assert result.infrastructure_error is not None
    assert contaminated.killed
    assert len(tuple((tmp_path / "raw").glob("*.json"))) == 2


def test_ordinary_unknown_namespace_after_mathlib_is_not_retried(
    monkeypatch: Any, tmp_path: Path
) -> None:
    invalid = _Server(
        _Response(
            _raw(
                [
                    {"severity": "error", "data": "unknown namespace `UserTypo`"},
                    {"severity": "error", "data": "Unknown constant `OfNat`"},
                ]
            )
        )
    )
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
                request_id="ordinary-mathlib-invalid",
                context_id="ctx:" + "0" * 64,
                code="import Mathlib\nopen UserTypo\ntheorem ok : True := trivial",
            )
        )
        assert result.status == LeanStatus.INVALID
        assert invalid.calls == 1
        assert not invalid.killed
        assert len(list((tmp_path / "raw").glob("*.json"))) == 1
    finally:
        backend.close()


def test_corruption_markers_without_mathlib_import_are_not_retried(
    monkeypatch: Any, tmp_path: Path
) -> None:
    invalid = _Server(
        _Response(
            _raw(
                [
                    {"severity": "error", "data": "unknown namespace `Real`"},
                    {"severity": "error", "data": "Unknown constant `OfNat`"},
                ]
            )
        )
    )
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
                request_id="ordinary-no-mathlib-invalid",
                context_id="ctx:" + "0" * 64,
                code="theorem bad : False := trivial",
            )
        )
        assert result.status == LeanStatus.INVALID
        assert invalid.calls == 1
        assert not invalid.killed
        assert len(list((tmp_path / "raw").glob("*.json"))) == 1
    finally:
        backend.close()


def test_commented_mathlib_import_does_not_trigger_recovery(
    monkeypatch: Any, tmp_path: Path
) -> None:
    invalid = _Server(
        _Response(
            _raw(
                [
                    {"severity": "error", "data": "unknown namespace `Real`"},
                    {"severity": "error", "data": "Unknown constant `OfNat`"},
                ]
            )
        )
    )
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
                request_id="commented-mathlib-import",
                context_id="ctx:" + "0" * 64,
                code=("prelude\n/-\nimport Mathlib\n-/\nopen Real\n#check (1)"),
            )
        )
        assert result.status == LeanStatus.INVALID
        assert invalid.calls == 1
        assert not invalid.killed
    finally:
        backend.close()


def test_mathlib_import_after_non_header_command_does_not_trigger_recovery(
    monkeypatch: Any, tmp_path: Path
) -> None:
    invalid = _Server(
        _Response(
            _raw(
                [
                    {"severity": "error", "data": "unknown namespace `Real`"},
                    {"severity": "error", "data": "Unknown constant `OfNat`"},
                ]
            )
        )
    )
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
                request_id="late-mathlib-import",
                context_id="ctx:" + "0" * 64,
                code="prelude\n#check True\nimport Mathlib\nopen Real\n#check (1)",
            )
        )
        assert result.status == LeanStatus.INVALID
        assert invalid.calls == 1
        assert not invalid.killed
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
