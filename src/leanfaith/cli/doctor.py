"""Environment doctor (PLAN.md §7.2 phases 0-1, §8.7, B.1, LF-007).

Checks the Python/LeanInteract/toolchain environment against the lock and the
binding §6.2 range. Direct shell probes here (elan/lake presence) are the
sanctioned §34.6 diagnostic exception; they never create semantic records.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import sys
from pathlib import Path

from pydantic import Field

from leanfaith.config.loading import ConfigError
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.lean.api_probe import PINNED_VERSION
from leanfaith.lean.project_registry import (
    ProjectKind,
    ToolchainViolation,
    check_project_toolchain,
    load_environment_lock,
    load_project_registry,
)


class DoctorCheck(StrictModel):
    name: str
    passed: bool
    severity: str = "error"  # "error" | "warning"
    detail: str = ""


class DoctorReport(StrictModel):
    schema_version: int = 1
    checks: tuple[DoctorCheck, ...]
    warnings: tuple[str, ...] = ()
    ok: bool = Field(default=False)


def _detect_total_ram_mb() -> int | None:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):  # pragma: no cover - platform-specific
        return None
    if page_size <= 0 or page_count <= 0:  # pragma: no cover
        return None
    return (page_size * page_count) // (1024 * 1024)


def run_doctor(paths: RepoPaths) -> DoctorReport:
    checks: list[DoctorCheck] = []
    warnings: list[str] = []

    # Python version window (§6.1).
    python_ok = sys.version_info[:2] == (3, 12)
    checks.append(
        DoctorCheck(
            name="python_version_in_window",
            passed=python_ok,
            detail=f"running {sys.version.split()[0]}; project requires >=3.12,<3.13",
        )
    )

    # LeanInteract pin (§8.2).
    try:
        installed = importlib.metadata.version("lean-interact")
        pin_ok = installed == PINNED_VERSION
        detail = f"installed {installed}, pinned {PINNED_VERSION}"
    except importlib.metadata.PackageNotFoundError:
        pin_ok, detail = False, "lean-interact not installed"
    checks.append(DoctorCheck(name="lean_interact_pin", passed=pin_ok, detail=detail))

    # API probe artifact present and ok (LF-006 gate input).
    probe_path = paths.reports / "compatibility" / "leaninteract_api.json"
    probe_ok = False
    probe_detail = f"{probe_path} missing; run `leanfaith probe-api`"
    if probe_path.is_file():
        try:
            probe_ok = bool(json.loads(probe_path.read_text(encoding="utf-8")).get("ok"))
            probe_detail = f"{probe_path} ok={probe_ok}"
        except json.JSONDecodeError:
            probe_detail = f"{probe_path} is not valid JSON"
    checks.append(DoctorCheck(name="api_probe_report_ok", passed=probe_ok, detail=probe_detail))

    # elan/lake availability (diagnostic-only shell exception, §34.6).
    for tool in ("elan", "lake"):
        location = shutil.which(tool)
        checks.append(
            DoctorCheck(
                name=f"{tool}_available",
                passed=location is not None,
                detail=location or f"{tool} not found on PATH",
            )
        )

    # Environment lock (B.1): reject unresolved values.
    lock = None
    try:
        lock = load_environment_lock(paths)
        checks.append(
            DoctorCheck(
                name="environment_lock_resolved",
                passed=True,
                detail=(
                    f"mode={lock.toolchain_lock.mode}, "
                    f"accepted_lean={lock.toolchain_lock.accepted_lean}"
                ),
            )
        )
        if lock.lean_interact.version != PINNED_VERSION:
            checks.append(
                DoctorCheck(
                    name="environment_lock_matches_pin",
                    passed=False,
                    detail=(
                        f"lock pins lean-interact {lock.lean_interact.version}, "
                        f"code pins {PINNED_VERSION}"
                    ),
                )
            )
    except (ConfigError, OSError) as exc:
        checks.append(
            DoctorCheck(
                name="environment_lock_resolved",
                passed=False,
                detail=f"configs/environment.lock.yaml unusable: {exc}",
            )
        )

    # Project registry and checked-out toolchains (§6.2; no silent overrides).
    if lock is not None:
        try:
            registry = load_project_registry(paths)
        except (ConfigError, ValueError, OSError) as exc:
            registry = {}
            checks.append(
                DoctorCheck(
                    name="project_registry_loads",
                    passed=False,
                    detail=str(exc),
                )
            )
        for key, spec in registry.items():
            directory = spec.local_directory(paths)
            if spec.kind != ProjectKind.LOCAL:
                continue
            if directory is None or not directory.is_dir():
                warnings.append(f"project {key}: local directory not checked out yet")
                continue
            try:
                version = check_project_toolchain(spec, directory, lock.toolchain_lock)
                checks.append(
                    DoctorCheck(
                        name=f"project_toolchain:{key}",
                        passed=True,
                        detail=f"{directory} pins Lean {version}",
                    )
                )
            except ToolchainViolation as exc:
                checks.append(
                    DoctorCheck(name=f"project_toolchain:{key}", passed=False, detail=str(exc))
                )

        # Memory-product safety report (§8.7): informational, never a mandate.
        workers = lock.lean_backend.workers
        limit = lock.lean_backend.memory_hard_limit_mb
        if workers and limit:
            total = _detect_total_ram_mb()
            product = workers * limit
            if total is not None and product > total:
                warnings.append(
                    f"workers x memory_hard_limit_mb = {product} MiB exceeds detected "
                    f"RAM {total} MiB (limit applies per REPL process, Linux-only)"
                )

    ok = all(check.passed for check in checks)
    return DoctorReport(checks=tuple(checks), warnings=tuple(warnings), ok=ok)


def doctor_report_path(paths: RepoPaths) -> Path:
    return paths.reports / "gates" / "doctor_latest.json"
