"""LF-009 integration: pool/auto modes, equivalence, retry recovery (§8.7, §8.12)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.lean.session_policy import (
    RetryPolicy,
    ServerMode,
    run_with_retries,
    semantic_identity,
)

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_FIXTURES = _REPO_ROOT / "tests" / "lean_fixtures"
_CTX_FINGERPRINT = "0" * 64
_CTX = f"ctx:{_CTX_FINGERPRINT}"

_BATCH_CODES = [
    "theorem p1 (x y : Nat) : x + y = y + x := by omega",
    "theorem p2 : False := trivial",
    "theorem p3 : True := sorry",
    "theorem p4 (n : Nat) : 0 + n = n := Nat.zero_add n",
]


def _settings(tmp: Path, mode: ServerMode, workers: int | None = None) -> BackendSettings:
    return BackendSettings(
        project_dir=_FIXTURES,
        context_fingerprint=_CTX_FINGERPRINT,
        environment_schema_version=1,
        raw_response_dir=tmp,
        server_mode=mode,
        workers=workers,
    )


def _requests() -> list[LeanRequest]:
    return [
        LeanRequest(request_id=f"batch-{index}", context_id=_CTX, code=code)
        for index, code in enumerate(_BATCH_CODES)
    ]


@pytest.fixture(scope="module")
def pool_backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    backend = LeanInteractBackend(
        _settings(tmp_path_factory.mktemp("raw_pool"), ServerMode.POOL, workers=2)
    )
    yield backend
    backend.close()


def test_pool_batch_order_and_statuses(pool_backend: LeanInteractBackend) -> None:
    results = pool_backend.run_batch(_requests())
    assert [r.request_id for r in results] == [f"batch-{i}" for i in range(4)]
    assert [r.status for r in results] == [
        LeanStatus.VALID,
        LeanStatus.INVALID,
        LeanStatus.VALID_WITH_SORRY,
        LeanStatus.VALID,
    ]


def test_one_vs_multiworker_semantic_equivalence(
    pool_backend: LeanInteractBackend, tmp_path: Path
) -> None:
    sequential = LeanInteractBackend(_settings(tmp_path, ServerMode.STABLE))
    try:
        sequential_results = sequential.run_batch(_requests())
    finally:
        sequential.close()
    pooled_results = pool_backend.run_batch(_requests())
    assert semantic_identity(sequential_results) == semantic_identity(pooled_results)


def test_auto_server_mode_runs_and_recovers(tmp_path: Path) -> None:
    backend = LeanInteractBackend(_settings(tmp_path, ServerMode.AUTO))
    try:
        first = backend.run(
            LeanRequest(request_id="auto-1", context_id=_CTX, code="theorem a1 : True := trivial")
        )
        assert first.status == LeanStatus.VALID
        timed_out = backend.run(
            LeanRequest(
                request_id="auto-2",
                context_id=_CTX,
                code="theorem a2 (x y : Nat) : x + y = y + x := by omega",
                timeout_seconds=0.001,
            )
        )
        assert timed_out.status == LeanStatus.TIMEOUT
        recovered = backend.run(
            LeanRequest(request_id="auto-3", context_id=_CTX, code="theorem a3 : True := trivial")
        )
        assert recovered.status == LeanStatus.VALID
    finally:
        backend.close()


def test_retry_lineage_preserves_raw_artifacts(tmp_path: Path) -> None:
    backend = LeanInteractBackend(_settings(tmp_path, ServerMode.STABLE))
    try:
        request = LeanRequest(
            request_id="retry-timeout",
            context_id=_CTX,
            code="theorem rt (x y : Nat) : x + y = y + x := by omega",
            timeout_seconds=0.001,
        )
        policy = RetryPolicy(
            max_attempts=2,
            retry_statuses=frozenset({LeanStatus.CRASH, LeanStatus.TIMEOUT}),
        )
        outcome = run_with_retries(backend.run, request, policy)
        assert len(outcome.attempts) == 2
        raw_paths = {result.raw_response_path for result in outcome.attempts}
        assert len(raw_paths) == 2  # lineage appended, not overwritten (§28.4)
        for path in raw_paths:
            assert path is not None and Path(path).is_file()
        # attempt metadata never fragments the cache key
        assert len({result.request_hash for result in outcome.attempts}) == 1
    finally:
        backend.close()


def test_infotree_smoke_none_substantive_full(tmp_path: Path) -> None:
    backend = LeanInteractBackend(_settings(tmp_path, ServerMode.STABLE))
    try:
        for level, expect_entries in (("none", False), ("substantive", False), ("full", True)):
            result = backend.run(
                LeanRequest(
                    request_id=f"tree-{level}",
                    context_id=_CTX,
                    code="theorem tr : True := trivial",
                    infotree=level,  # type: ignore[arg-type]
                )
            )
            assert result.status == LeanStatus.VALID
            if expect_entries:
                assert len(result.infotree) > 0
    finally:
        backend.close()
