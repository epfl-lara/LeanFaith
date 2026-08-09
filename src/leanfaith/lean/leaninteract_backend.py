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
from dataclasses import dataclass, replace
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

from leanfaith.config.hashing import hash_file, sha256_hex
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
from leanfaith.lean.source_scan import has_top_level_import_family

METHOD_VERSION = "leaninteract_backend_v3"
COMMAND_ISOLATION_VERSION = "deterministic_request_nonce_prefix_v1"

_CORE_ENVIRONMENT_SECONDARY_ERRORS = (
    "Unknown constant `CoeFun`",
    "Unknown constant `Lean.ParserDescr`",
    "The expected type of `.default`\n  BinderInfo",
)

# A second corruption shape was observed during the frozen Gate-2 replay:
# after a previously valid ``import Mathlib`` prefix had been reused, the REPL
# returned a normal command response whose environment lacked basic Mathlib
# namespaces and even ``OfNat``.  These diagnostics are impossible after a
# successful Mathlib import in the pinned project.  Keep the markers exact and
# require the request itself to import ``Mathlib``; an ordinary
# user typo such as ``unknown namespace `Foo``` must remain a semantic INVALID.
_MATHLIB_ENVIRONMENT_CORRUPTION_ERRORS = (
    "unknown namespace `Real`",
    "unknown namespace `Classical`",
    "unknown namespace `Topology`",
    "Unknown constant `OfNat`",
)


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
    enable_incremental_optimization: bool = True
    enable_parallel_elaboration: bool = True
    isolate_incremental_commands: bool = False
    environment_is_prepared: bool = False


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
        # Scale pipelines prepare the immutable project and REPL once in the
        # parent process before starting workers. Child chunks must not run
        # ``lake exe cache get`` / ``lake build`` for the project or rebuild
        # the same cached REPL. Other callers retain LeanInteract's safe
        # build-on-first-use defaults.
        build_environment = not self._settings.environment_is_prepared
        return LeanREPLConfig(
            project=LocalProject(
                directory=self._settings.project_dir,
                auto_build=build_environment,
            ),
            build_repl=build_environment,
            memory_hard_limit_mb=self._settings.memory_hard_limit_mb,
            enable_incremental_optimization=self._settings.enable_incremental_optimization,
            enable_parallel_elaboration=self._settings.enable_parallel_elaboration,
            verbose=self._settings.verbose,
        )

    @classmethod
    def prepare_environment(cls, settings: BackendSettings) -> None:
        """Build and validate one project's LeanInteract environment.

        This is intentionally a setup-only operation: it does not start a
        server or submit a Lean request. Scale orchestrators call it once in
        the parent process, then construct chunk backends with
        ``environment_is_prepared=True``. LeanInteract therefore retains sole
        ownership of project/REPL setup while avoiding redundant builds.
        """

        if settings.environment_is_prepared:
            raise ValueError("environment preparation requires build-enabled settings")
        cls(settings)._repl_config()

    @staticmethod
    def _core_environment_is_corrupted(request: LeanRequest, raw: dict[str, Any]) -> bool:
        """Detect the impossible core-import failure observed in Gate 3.

        LeanInteract's incremental REPL optimization can occasionally reuse a
        damaged prefix environment.  Two observed, independently replayed
        signatures are recognized: ``import Lean`` followed by missing Lean
        core declarations, and ``import Mathlib`` followed by missing standard
        Mathlib namespaces/basic ``OfNat`` infrastructure.  Both are
        infrastructure/session failures, not theorem verdicts.  Keep the
        detector deliberately narrow so ordinary invalid Lean programs are
        never retried as infrastructure failures.
        """

        code = request.code
        if code is None:
            return False
        stripped_code = code.lstrip("\ufeff \t\r\n")
        imports_mathlib = has_top_level_import_family(stripped_code, "Mathlib")
        errors = "\n".join(
            str(message.get("data", ""))
            for message in raw.get("messages") or ()
            if message.get("severity") == "error"
        )
        lean_corruption = stripped_code.startswith("import Lean\n") and (
            "unknown namespace `Lean`" in errors
            and any(marker in errors for marker in _CORE_ENVIRONMENT_SECONDARY_ERRORS)
        )
        mathlib_corruption = imports_mathlib and (
            "Unknown constant `OfNat`" in errors
            and any(
                marker in errors
                for marker in _MATHLIB_ENVIRONMENT_CORRUPTION_ERRORS
                if marker != "Unknown constant `OfNat`"
            )
        )
        return lean_corruption or mathlib_corruption

    def _recover_corrupted_environment(
        self,
        request: LeanRequest,
        *,
        request_hash: str,
        raw: dict[str, Any],
        raw_path: str,
        elapsed_ms: int,
        originated_from_pool: bool = False,
    ) -> LeanResult | None:
        """Recover one impossible imported environment, independent of mode.

        Stable and pooled requests share this path so a poisoned response can
        never become a semantic INVALID merely because it was dispatched by a
        different server mode. The corrupt raw response is already persisted
        before this method runs.
        """

        if not self._core_environment_is_corrupted(request, raw):
            return None
        if request.metadata.get("core_environment_recovery") == "1":
            # A fresh process returned the same impossible import environment.
            # Never normalize that infrastructure failure as a semantic
            # INVALID. The caller's bounded infrastructure retry policy may
            # now make its next outer attempt with a fresh process; if it also
            # fails, the row remains an explicit crash rather than a false
            # theorem verdict.
            self.reset_session()
            normalized = normalize_response(
                request,
                self._raw_for_normalization(request, raw),
                request_hash=request_hash,
                context_fingerprint=self._settings.context_fingerprint,
                elapsed_ms=elapsed_ms,
                raw_response_path=raw_path,
            )
            return replace(
                normalized,
                status=LeanStatus.CRASH,
                infrastructure_error="core_environment_corruption_after_recovery",
            )

        # Pool workers cannot be individually identified through LeanInteract's
        # returned item. Reset the whole pool/session, then retry this request
        # exactly once through a fresh stable process. Other already-returned
        # pool items remain independently normalizable.
        self.reset_session()
        attempt = str(request.metadata.get("attempt", "0"))
        recovery_request = replace(
            request,
            metadata={
                **dict(request.metadata),
                "attempt": f"{attempt}-core-recovery",
                "core_environment_recovery": "1",
            },
        )
        recovered = self.run(recovery_request)
        if originated_from_pool:
            # Pool recovery deliberately uses a stable one-off process. Do not
            # retain it alongside the replacement pool used by later batch
            # groups.
            self._drop_server()
        return replace(recovered, elapsed_ms=elapsed_ms + recovered.elapsed_ms)

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

    def reset_session(self) -> None:
        """Discard all live REPL state while preserving immutable settings.

        The next request lazily starts a fresh server (or pool).  This remains
        the correctness-oracle and recovery boundary for callers that require
        process isolation; scalable SFT extraction instead uses deterministic
        per-request trie namespaces while sharing the import-header cache.
        """

        self._drop_server()
        if self._pool is not None:
            with contextlib.suppress(Exception):
                self._pool.close()
            self._pool = None
        self._auto_fallback_active = False

    def close(self) -> None:
        self.reset_session()

    # -- raw persistence (§8.4: save before normalization) --------------------

    def _persist_raw(
        self,
        request: LeanRequest,
        request_hash: str,
        raw: dict[str, Any] | None,
        error: str | None,
    ) -> str:
        isolation_prefix = self._command_isolation_prefix(request)
        isolation_attempt = self._normalized_attempt(request)
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
            "transport_isolation": (
                {
                    "version": COMMAND_ISOLATION_VERSION,
                    "attempt": isolation_attempt,
                    "prefix_sha256": sha256_hex(isolation_prefix.encode("ascii")),
                    "prefix_width": len(isolation_prefix),
                }
                if isolation_prefix
                else None
            ),
            "request_hash": request_hash,
            "method_version": self._settings.method_version,
            "response": raw,
            "error": error,
        }
        directory = self._settings.raw_response_dir
        directory.mkdir(parents=True, exist_ok=True)
        # Raw responses are append-only (§8.4): the filename keys on the
        # request hash PLUS a submission digest; retries additionally carry
        # the attempt counter (metadata, excluded from the hash) per §28.4.
        # A deterministic replay can submit the same request ID and attempt
        # while receiving session-local response fields that differ. Never
        # overwrite the first observation in that case: retain the later
        # response under a content-addressed suffix.
        attempt = str(request.metadata.get("attempt", "0"))
        suffix = f".attempt{attempt}" if attempt != "0" else ""
        submission = sha256_hex(request.request_id.encode("utf-8"))[:8]
        path = directory / f"{request_hash}.{submission}{suffix}.json"
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if path.exists():
            if path.read_bytes() == payload:
                return str(path)
            response_digest = sha256_hex(payload)[:16]
            path = directory / (
                f"{request_hash}.{submission}{suffix}.response-{response_digest}.json"
            )
            if path.exists():
                if path.read_bytes() != payload:
                    raise RuntimeError(f"raw Lean response content-address collision at {path}")
                return str(path)
        with path.open("xb") as handle:
            handle.write(payload)
        return str(path)

    # -- request execution -----------------------------------------------------

    def _resolve_file_path(self, request: LeanRequest) -> Path | None:
        if request.file_path is None:
            return None
        file_path = request.file_path
        if not file_path.is_absolute():
            file_path = self._settings.project_dir / file_path
        return file_path

    def _file_content_hash(self, request: LeanRequest) -> str | None:
        """Digest of the resolved file so edited files never hit stale cache
        entries (§8.4). Unreadable files get a distinct sentinel digest; the
        Lean run itself will surface the real error."""
        resolved = self._resolve_file_path(request)
        if resolved is None:
            return None
        try:
            return hash_file(resolved)
        except OSError:
            return sha256_hex(b"__unreadable__:" + str(request.file_path).encode("utf-8"))

    def _request_hash(self, request: LeanRequest) -> str:
        return compute_request_hash(
            request,
            context_fingerprint=self._settings.context_fingerprint,
            environment_schema_version=self._settings.environment_schema_version,
            method_version=self._settings.method_version,
            file_content_hash=self._file_content_hash(request),
        )

    def _command_isolation_prefix(self, request: LeanRequest) -> str:
        """Unique, deterministic REPL-trie namespace for one code request.

        The REPL's import-header cache canonicalizes away comments and
        whitespace, while its incremental-state trie matches the full command
        prefix.  A same-line ASCII comment therefore preserves shared import
        caching but prevents any body snapshot from another request being
        reused.  The request hash keeps equal semantic computations stable;
        the request ID additionally separates independent submissions.
        """

        if not self._settings.isolate_incremental_commands or request.code is None:
            return ""
        nonce = sha256_hex(
            (
                f"{request.request_id}\0{self._request_hash(request)}\0"
                f"{self._normalized_attempt(request)}"
            ).encode()
        )[:24]
        return f"/-leanfaith-isolation:{nonce}-/ "

    @staticmethod
    def _normalized_attempt(request: LeanRequest) -> str:
        attempt = str(request.metadata.get("attempt", "0")).strip()
        return attempt or "0"

    @classmethod
    def _restore_prefixed_positions(cls, value: Any, *, prefix_width: int) -> Any:
        """Return a deep JSON copy with line-one columns mapped to source code."""

        if isinstance(value, list):
            return [
                cls._restore_prefixed_positions(item, prefix_width=prefix_width) for item in value
            ]
        if isinstance(value, dict):
            restored = {
                key: cls._restore_prefixed_positions(item, prefix_width=prefix_width)
                for key, item in value.items()
            }
            line = restored.get("line")
            column = restored.get("column")
            if line == 1 and isinstance(column, int) and column >= prefix_width:
                restored["column"] = column - prefix_width
            return restored
        return value

    def _raw_for_normalization(self, request: LeanRequest, raw: dict[str, Any]) -> dict[str, Any]:
        prefix = self._command_isolation_prefix(request)
        if not prefix:
            return raw
        restored = self._restore_prefixed_positions(raw, prefix_width=len(prefix))
        assert isinstance(restored, dict)
        return restored

    def _build_repl_request(self, request: LeanRequest) -> Command | FileCommand:
        infotree = None if request.infotree == "none" else request.infotree
        # Lean itself defaults ``Elab.async`` to true.  LeanInteract's config
        # flag controls whether it injects ``true``; setting it to false does
        # not inject an explicit false.  Isolation-sensitive callers therefore
        # need both the config flag and this per-request option.
        set_options: list[tuple[list[str], bool | int | str | list[str]]] | None = (
            [(["Elab", "async"], False)] if not self._settings.enable_parallel_elaboration else None
        )
        if request.code is not None:
            return Command(
                cmd=self._command_isolation_prefix(request) + request.code,
                declarations=request.declarations,
                root_goals=request.root_goals,
                infotree=infotree,
                set_options=set_options,
            )
        resolved = self._resolve_file_path(request)
        assert resolved is not None  # validate_request guarantees this
        return FileCommand(
            path=str(resolved),
            declarations=request.declarations,
            root_goals=request.root_goals,
            infotree=infotree,
            set_options=set_options,
        )

    def run(self, request: LeanRequest) -> LeanResult:
        validate_request(request)
        request_hash = self._request_hash(request)
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
        except Exception as exc:  # KeyboardInterrupt/SystemExit must propagate
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
        recovered = self._recover_corrupted_environment(
            request,
            request_hash=request_hash,
            raw=raw,
            raw_path=raw_path,
            elapsed_ms=elapsed_ms,
        )
        if recovered is not None:
            return recovered
        return normalize_response(
            request,
            self._raw_for_normalization(request, raw),
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

    def _setup_error_result(
        self, request: LeanRequest, request_hash: str, exc: Exception, elapsed_ms: int
    ) -> LeanResult:
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

    def _normalize_pool_item(
        self,
        request: LeanRequest,
        request_hash: str,
        item: object,
        elapsed_ms: int,
    ) -> LeanResult:
        if isinstance(item, LeanError):
            raw_path = self._persist_raw(
                request, request_hash, {"message": item.message}, "LeanError"
            )
            return normalize_repl_error(
                request,
                item.message,
                request_hash=request_hash,
                context_fingerprint=self._settings.context_fingerprint,
                elapsed_ms=elapsed_ms,
                raw_response_path=raw_path,
            )
        if isinstance(item, BaseException):
            raw_path = self._persist_raw(
                request, request_hash, None, f"{type(item).__name__}: {item}"
            )
            return normalize_exception(
                request,
                item,
                request_hash=request_hash,
                context_fingerprint=self._settings.context_fingerprint,
                elapsed_ms=elapsed_ms,
                raw_response_path=raw_path,
            )
        raw = item.model_dump(mode="json")  # type: ignore[attr-defined]
        raw_path = self._persist_raw(request, request_hash, raw, None)
        recovered = self._recover_corrupted_environment(
            request,
            request_hash=request_hash,
            raw=raw,
            raw_path=raw_path,
            elapsed_ms=elapsed_ms,
            originated_from_pool=True,
        )
        if recovered is not None:
            return recovered
        return normalize_response(
            request,
            self._raw_for_normalization(request, raw),
            request_hash=request_hash,
            context_fingerprint=self._settings.context_fingerprint,
            elapsed_ms=elapsed_ms,
            raw_response_path=raw_path,
        )

    def _run_batch_pooled(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        hashes: list[str] = []
        for request in requests:
            validate_request(request)
            hashes.append(self._request_hash(request))
        started = time.monotonic()

        # Pool construction failure is a SETUP_ERROR for the whole batch
        # (§8.3), never CRASH/INTERNAL_ERROR.
        try:
            self._ensure_pool()
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return [
                self._setup_error_result(request, request_hash, exc, elapsed_ms)
                for request, request_hash in zip(requests, hashes, strict=True)
            ]

        # Per-request timeouts: LeanServerPool.run_batch takes one
        # timeout_per_cmd, so requests are grouped by timeout and each group
        # is submitted separately; results reassemble in input order. Group
        # order is deterministic (ascending timeout).
        groups: dict[float, list[int]] = {}
        for index, request in enumerate(requests):
            groups.setdefault(request.timeout_seconds, []).append(index)

        results: list[LeanResult | None] = [None] * len(requests)
        for timeout_seconds in sorted(groups):
            indices = groups[timeout_seconds]
            group_started = time.monotonic()
            try:
                # A prior group's corruption recovery resets the pool. Never
                # reuse a captured, already-closed pool across timeout groups.
                pool = self._ensure_pool()
                raw_results = pool.run_batch(
                    [self._build_repl_request(requests[i]) for i in indices],
                    timeout_per_cmd=timeout_seconds,
                )
            except Exception as exc:  # group-level failure: normalize per item
                group_elapsed = int((time.monotonic() - group_started) * 1000)
                for i in indices:
                    raw_path = self._persist_raw(
                        requests[i], hashes[i], None, f"pool: {type(exc).__name__}: {exc}"
                    )
                    results[i] = normalize_exception(
                        requests[i],
                        exc,
                        request_hash=hashes[i],
                        context_fingerprint=self._settings.context_fingerprint,
                        elapsed_ms=group_elapsed,
                        raw_response_path=raw_path,
                    )
                continue
            # Per-item wall time is unavailable from the pool; each item
            # records its group's elapsed time (documented limitation).
            group_elapsed = int((time.monotonic() - group_started) * 1000)
            for i, item in zip(indices, raw_results, strict=True):
                results[i] = self._normalize_pool_item(requests[i], hashes[i], item, group_elapsed)
        final = [result for result in results if result is not None]
        assert len(final) == len(requests)  # every index filled exactly once
        return final
