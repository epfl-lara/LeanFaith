"""The single LeanInteract adapter (PLAN.md §8, §34).

This is the only module in the codebase allowed to import LeanInteract
(§8.1; enforced by test). It maps the Appendix A.5 protocol onto the pinned
0.11.4 API: ``Command``/``FileCommand`` construction, explicit raw-response
persistence *before* normalization, per-item exception normalization, and the
§8.6 status mapping via ``response_normalization``.

Server lifecycle here is deliberately minimal (one stable ``LeanServer``,
recreated after a fatal request); pools, the experimental ``AutoLeanServer``,
retries, and recovery policy land with LF-009 in ``session_policy``.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lean_interact import (
    AutoLeanServer,
    Command,
    FileCommand,
    LeanREPLConfig,
    LeanServer,
    LeanServerPool,
    LocalProject,
)
from lean_interact.interface import LeanError

from leanfaith.lean.protocol import (
    LeanRequest,
    LeanResult,
    LeanStatus,
    compute_request_hash,
    validate_request,
)
from leanfaith.lean.response_normalization import (
    normalize_exception,
    normalize_repl_error,
    normalize_response,
)
from leanfaith.lean.session_policy import ServerMode

METHOD_VERSION = "leaninteract_backend_v1"


@dataclass(frozen=True, slots=True)
class BackendSettings:
    """Pinned identity and storage locations for one backend instance."""

    project_dir: Path
    context_fingerprint: str
    environment_schema_version: int
    raw_response_dir: Path
    server_mode: ServerMode = ServerMode.STABLE
    workers: int | None = None
    memory_hard_limit_mb: int | None = None
    method_version: str = METHOD_VERSION
    verbose: bool = False


class LeanInteractBackend:
    """Implementation of the A.5 ``LeanBackend`` protocol over LeanInteract.

    Modes (§8.7): ``stable`` uses one ``LeanServer``; ``auto`` uses the
    experimental ``AutoLeanServer`` with a tested stable fallback; ``pool``
    additionally routes ``run_batch`` through ``LeanServerPool`` for
    independent requests. One-worker and multiworker runs yield identical
    semantic (request_hash, status) results after normalization.
    """

    def __init__(self, settings: BackendSettings) -> None:
        self._settings = settings
        self._server: LeanServer | None = None
        self._pool: LeanServerPool | None = None
        self._auto_fallback_active = False

    # -- lifecycle -----------------------------------------------------------

    @property
    def auto_fallback_active(self) -> bool:
        """True when experimental AUTO mode fell back to the stable server."""
        return self._auto_fallback_active

    def _repl_config(self) -> LeanREPLConfig:
        return LeanREPLConfig(
            project=LocalProject(directory=self._settings.project_dir),
            memory_hard_limit_mb=self._settings.memory_hard_limit_mb,
            verbose=self._settings.verbose,
        )

    def _ensure_server(self) -> LeanServer:
        if self._server is None:
            config = self._repl_config()
            if self._settings.server_mode == ServerMode.AUTO:
                try:
                    self._server = AutoLeanServer(config)
                except Exception:  # pragma: no cover - depends on auto-server failure
                    # §8.5: AutoLeanServer is experimental; fall back to the
                    # tested stable server rather than failing the request.
                    self._server = LeanServer(config)
                    self._auto_fallback_active = True
            else:
                self._server = LeanServer(config)
        return self._server

    def _ensure_pool(self) -> LeanServerPool:
        if self._pool is None:
            self._pool = LeanServerPool(self._repl_config(), num_workers=self._settings.workers)
        return self._pool

    def _drop_server(self) -> None:
        if self._server is not None:
            # Teardown must not mask the request error being normalized.
            with contextlib.suppress(Exception):
                self._server.kill()
            self._server = None

    def close(self) -> None:
        self._drop_server()
        if self._pool is not None:
            with contextlib.suppress(Exception):
                self._pool.close()
            self._pool = None

    # -- raw persistence (§8.4: save before normalization) --------------------

    def _persist_raw(
        self,
        request: LeanRequest,
        request_hash: str,
        raw: dict[str, Any] | None,
        error: str | None,
    ) -> str:
        record = {
            "request": {
                "request_id": request.request_id,
                "context_id": request.context_id,
                "code": request.code,
                "file_path": str(request.file_path) if request.file_path else None,
                "declarations": request.declarations,
                "root_goals": request.root_goals,
                "infotree": request.infotree,
                "allow_sorry": request.allow_sorry,
                "timeout_seconds": request.timeout_seconds,
            },
            "request_hash": request_hash,
            "method_version": self._settings.method_version,
            "response": raw,
            "error": error,
        }
        directory = self._settings.raw_response_dir
        directory.mkdir(parents=True, exist_ok=True)
        # Retries append lineage rather than overwrite raw failures (§28.4):
        # the attempt counter is metadata (excluded from the hash) but keys
        # the raw artifact filename.
        attempt = str(request.metadata.get("attempt", "0"))
        suffix = f".attempt{attempt}" if attempt != "0" else ""
        path = directory / f"{request_hash}{suffix}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return str(path)

    # -- request execution -----------------------------------------------------

    def _build_repl_request(self, request: LeanRequest) -> Command | FileCommand:
        infotree = None if request.infotree == "none" else request.infotree
        if request.code is not None:
            return Command(
                cmd=request.code,
                declarations=request.declarations,
                root_goals=request.root_goals,
                infotree=infotree,
            )
        assert request.file_path is not None  # validate_request guarantees this
        file_path = request.file_path
        if not file_path.is_absolute():
            file_path = self._settings.project_dir / file_path
        return FileCommand(
            path=str(file_path),
            declarations=request.declarations,
            root_goals=request.root_goals,
            infotree=infotree,
        )

    def run(self, request: LeanRequest) -> LeanResult:
        validate_request(request)
        request_hash = compute_request_hash(
            request,
            context_fingerprint=self._settings.context_fingerprint,
            environment_schema_version=self._settings.environment_schema_version,
            method_version=self._settings.method_version,
        )
        started = time.monotonic()

        try:
            server = self._ensure_server()
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            raw_path = self._persist_raw(request, request_hash, None, f"setup: {exc}")
            return LeanResult(
                request_id=request.request_id,
                request_hash=request_hash,
                context_id=request.context_id,
                context_fingerprint=self._settings.context_fingerprint,
                status=LeanStatus.SETUP_ERROR,
                elapsed_ms=elapsed_ms,
                raw_response_path=raw_path,
                infrastructure_error=f"{type(exc).__name__}: {exc}"[:2000],
            )

        try:
            response = server.run(
                self._build_repl_request(request), timeout=request.timeout_seconds
            )
        except BaseException as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            raw_path = self._persist_raw(
                request, request_hash, None, f"{type(exc).__name__}: {exc}"
            )
            self._drop_server()  # timeout/crash kills the pinned server; recreate lazily
            return normalize_exception(
                request,
                exc,
                request_hash=request_hash,
                context_fingerprint=self._settings.context_fingerprint,
                elapsed_ms=elapsed_ms,
                raw_response_path=raw_path,
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if isinstance(response, LeanError):
            raw_path = self._persist_raw(
                request, request_hash, {"message": response.message}, "LeanError"
            )
            return normalize_repl_error(
                request,
                response.message,
                request_hash=request_hash,
                context_fingerprint=self._settings.context_fingerprint,
                elapsed_ms=elapsed_ms,
                raw_response_path=raw_path,
            )

        raw = response.model_dump(mode="json")
        raw_path = self._persist_raw(request, request_hash, raw, None)
        return normalize_response(
            request,
            raw,
            request_hash=request_hash,
            context_fingerprint=self._settings.context_fingerprint,
            elapsed_ms=elapsed_ms,
            raw_response_path=raw_path,
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        """One terminal result per request, in input order (§8.4).

        POOL mode fans independent requests over ``LeanServerPool`` and
        normalizes each returned response/``LeanError``/``Exception``
        independently (§8.5); other modes execute sequentially.
        """
        if self._settings.server_mode != ServerMode.POOL or not requests:
            return [self.run(request) for request in requests]
        return self._run_batch_pooled(requests)

    def _run_batch_pooled(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        hashes: list[str] = []
        for request in requests:
            validate_request(request)
            hashes.append(
                compute_request_hash(
                    request,
                    context_fingerprint=self._settings.context_fingerprint,
                    environment_schema_version=self._settings.environment_schema_version,
                    method_version=self._settings.method_version,
                )
            )
        started = time.monotonic()
        try:
            pool = self._ensure_pool()
            raw_results = pool.run_batch(
                [self._build_repl_request(request) for request in requests],
                timeout_per_cmd=max(request.timeout_seconds for request in requests),
            )
        except Exception as exc:  # pool-level failure: normalize per item
            elapsed_ms = int((time.monotonic() - started) * 1000)
            results = []
            for request, request_hash in zip(requests, hashes, strict=True):
                raw_path = self._persist_raw(
                    request, request_hash, None, f"pool: {type(exc).__name__}: {exc}"
                )
                results.append(
                    normalize_exception(
                        request,
                        exc,
                        request_hash=request_hash,
                        context_fingerprint=self._settings.context_fingerprint,
                        elapsed_ms=elapsed_ms,
                        raw_response_path=raw_path,
                    )
                )
            return results

        elapsed_ms = int((time.monotonic() - started) * 1000)
        results = []
        for request, request_hash, item in zip(requests, hashes, raw_results, strict=True):
            if isinstance(item, LeanError):
                raw_path = self._persist_raw(
                    request, request_hash, {"message": item.message}, "LeanError"
                )
                results.append(
                    normalize_repl_error(
                        request,
                        item.message,
                        request_hash=request_hash,
                        context_fingerprint=self._settings.context_fingerprint,
                        elapsed_ms=elapsed_ms,
                        raw_response_path=raw_path,
                    )
                )
            elif isinstance(item, BaseException):
                raw_path = self._persist_raw(
                    request, request_hash, None, f"{type(item).__name__}: {item}"
                )
                results.append(
                    normalize_exception(
                        request,
                        item,
                        request_hash=request_hash,
                        context_fingerprint=self._settings.context_fingerprint,
                        elapsed_ms=elapsed_ms,
                        raw_response_path=raw_path,
                    )
                )
            else:
                raw = item.model_dump(mode="json")
                raw_path = self._persist_raw(request, request_hash, raw, None)
                results.append(
                    normalize_response(
                        request,
                        raw,
                        request_hash=request_hash,
                        context_fingerprint=self._settings.context_fingerprint,
                        elapsed_ms=elapsed_ms,
                        raw_response_path=raw_path,
                    )
                )
        return results
