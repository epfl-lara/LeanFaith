"""Executable LeanInteract API-shape probe (PLAN.md LF-006, §8.5, A.1, A.8).

Verifies every Appendix A symbol, signature, field, and default of the pinned
``lean-interact`` distribution before any adapter code may be written, and
writes the A.8 compatibility artifacts.

Boundary note: this module loads LeanInteract dynamically via ``importlib``
and inspects it as *data*; it issues no Lean semantic operations and creates
no semantic records. Static ``import lean_interact`` remains confined to
``leanfaith.lean.leaninteract_backend`` (§8.1, §8.12 item 12).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
from pathlib import Path
from types import ModuleType

from pydantic import Field

from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths

PINNED_DISTRIBUTION = "lean-interact"
PINNED_VERSION = "0.11.4"
ADVERTISED_LEAN_MIN = "v4.8.0-rc1"
ADVERTISED_LEAN_MAX = "v4.31.0-rc1"
EXPECTED_REPL_FORK = "https://github.com/augustepoiroux/repl"

#: (module, symbol) pairs from Appendix A.2 plus the §8.3 project table.
REQUIRED_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("lean_interact", "AutoLeanServer"),
    ("lean_interact", "Command"),
    ("lean_interact", "FileCommand"),
    ("lean_interact", "LeanREPLConfig"),
    ("lean_interact", "LeanServer"),
    ("lean_interact", "LeanServerPool"),
    ("lean_interact", "LocalProject"),
    ("lean_interact", "GitProject"),
    ("lean_interact", "TempRequireProject"),
    ("lean_interact", "TemporaryProject"),
    ("lean_interact.interface", "CommandResponse"),
    ("lean_interact.interface", "DeclarationInfo"),
    ("lean_interact.interface", "InfoTreeOptions"),
    ("lean_interact.interface", "LeanError"),
)


class SymbolCheck(StrictModel):
    module: str
    symbol: str
    present: bool
    kind: str | None = None
    signature: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


class CaveatCheck(StrictModel):
    name: str
    passed: bool
    observed: str
    expectation: str


class ApiProbeReport(StrictModel):
    """The A.8 API-shape report serialized to reports/compatibility/."""

    schema_version: int = 1
    distribution: str
    installed_version: str
    pinned_version: str
    version_match: bool
    advertised_lean_min: str
    advertised_lean_max: str
    repl_git_url: str | None
    repl_default_version: str | None
    repl_fork_match: bool
    symbols: tuple[SymbolCheck, ...]
    caveats: tuple[CaveatCheck, ...]
    ok: bool


def _model_fields(obj: object) -> dict[str, str]:
    fields = getattr(obj, "model_fields", None)
    if isinstance(fields, dict):
        return {name: str(info.annotation) for name, info in fields.items()}
    return {}


def _check_symbol(module: ModuleType, symbol: str) -> SymbolCheck:
    obj = getattr(module, symbol, None)
    if obj is None:
        return SymbolCheck(module=module.__name__, symbol=symbol, present=False)
    kind = "class" if inspect.isclass(obj) else type(obj).__name__
    signature: str | None
    try:
        signature = str(inspect.signature(obj))
    except (TypeError, ValueError):
        signature = None
    return SymbolCheck(
        module=module.__name__,
        symbol=symbol,
        present=True,
        kind=kind,
        signature=signature,
        fields=_model_fields(obj),
    )


class _MissingParameter:
    def __repr__(self) -> str:  # pragma: no cover - repr only surfaces in reports
        return "<parameter missing>"


def _param_default(callable_obj: object, param: str) -> object:
    """Default of a parameter, or a sentinel when the API shape diverges —
    the probe must report a failed caveat, never crash (A.8)."""
    try:
        signature = inspect.signature(callable_obj)  # type: ignore[arg-type]
        return signature.parameters[param].default
    except (TypeError, ValueError, KeyError):
        return _MissingParameter()


def _has_param(callable_obj: object, param: str) -> bool:
    try:
        return param in inspect.signature(callable_obj).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _caveats(root: ModuleType, interface: ModuleType) -> list[CaveatCheck]:
    checks: list[CaveatCheck] = []

    response = getattr(interface, "CommandResponse", None)
    validator = getattr(response, "lean_code_is_valid", None)
    default = _param_default(validator, "allow_sorry") if validator is not None else None
    checks.append(
        CaveatCheck(
            name="lean_code_is_valid_default_is_permissive",
            passed=default is True,
            observed=repr(default),
            expectation="allow_sorry defaults to True, so every LeanFaith call passes it "
            "explicitly (§8.5)",
        )
    )

    pool = getattr(root, "LeanServerPool", None)
    run_batch = getattr(pool, "run_batch", None)
    return_ann = ""
    if run_batch is not None:
        try:
            return_ann = str(inspect.signature(run_batch).return_annotation)
        except (TypeError, ValueError):
            return_ann = "<unavailable>"
    checks.append(
        CaveatCheck(
            name="run_batch_returns_per_item_exceptions",
            passed="Exception" in return_ann and "LeanError" in return_ann,
            observed=return_ann,
            expectation="run_batch may return a response, LeanError, or Exception per item "
            "(§8.5); the adapter normalizes each independently",
        )
    )

    config = getattr(root, "LeanREPLConfig", None)
    checks.append(
        CaveatCheck(
            name="memory_hard_limit_mb_supported",
            passed=config is not None and _has_param(config, "memory_hard_limit_mb"),
            observed=str(sorted(inspect.signature(config).parameters) if config else None),
            expectation="LeanREPLConfig accepts memory_hard_limit_mb (Linux-only, per REPL "
            "process; §8.5)",
        )
    )

    server = getattr(root, "LeanServer", None)
    run = getattr(server, "run", None)
    checks.append(
        CaveatCheck(
            name="server_run_accepts_timeout",
            passed=run is not None and _has_param(run, "timeout"),
            observed=str(inspect.signature(run)) if run is not None else "absent",
            expectation="LeanServer.run(request, timeout=...) exists (A.4)",
        )
    )

    command = getattr(root, "Command", None)
    command_fields = set(_model_fields(command)) if command is not None else set()
    required_fields = {"cmd", "env", "declarations", "root_goals", "infotree"}
    checks.append(
        CaveatCheck(
            name="command_supports_declarations_and_root_goals",
            passed=required_fields.issubset(command_fields),
            observed=str(sorted(command_fields)),
            expectation="Command exposes cmd/env/declarations/root_goals/infotree (A.4; "
            "rootGoals is only the wire alias)",
        )
    )

    file_command = getattr(root, "FileCommand", None)
    file_fields = set(_model_fields(file_command)) if file_command is not None else set()
    checks.append(
        CaveatCheck(
            name="file_command_supports_path_and_declarations",
            passed={"path", "declarations"}.issubset(file_fields),
            observed=str(sorted(file_fields)),
            expectation="FileCommand exposes path/declarations (§8.9)",
        )
    )

    auto = getattr(root, "AutoLeanServer", None)
    checks.append(
        CaveatCheck(
            name="auto_lean_server_available_as_experimental",
            passed=auto is not None,
            observed="present" if auto is not None else "absent",
            expectation="AutoLeanServer exists but stays experimental with a tested "
            "LeanServer fallback (§8.5)",
        )
    )
    return checks


def run_api_probe() -> ApiProbeReport:
    """Introspect the pinned LeanInteract distribution and build the A.8 report."""
    installed_version = importlib.metadata.version(PINNED_DISTRIBUTION)
    root = importlib.import_module("lean_interact")
    interface = importlib.import_module("lean_interact.interface")
    config_module = importlib.import_module("lean_interact.config")

    symbols = tuple(
        _check_symbol(root if module_name == "lean_interact" else interface, symbol)
        for module_name, symbol in REQUIRED_SYMBOLS
    )
    caveats = tuple(_caveats(root, interface))

    repl_git_url = getattr(config_module, "DEFAULT_REPL_GIT_URL", None)
    repl_default_version = getattr(config_module, "DEFAULT_REPL_VERSION", None)
    version_match = installed_version == PINNED_VERSION
    repl_fork_match = repl_git_url == EXPECTED_REPL_FORK
    ok = (
        version_match
        and repl_fork_match
        and all(symbol.present for symbol in symbols)
        and all(caveat.passed for caveat in caveats)
    )
    return ApiProbeReport(
        distribution=PINNED_DISTRIBUTION,
        installed_version=installed_version,
        pinned_version=PINNED_VERSION,
        version_match=version_match,
        advertised_lean_min=ADVERTISED_LEAN_MIN,
        advertised_lean_max=ADVERTISED_LEAN_MAX,
        repl_git_url=repl_git_url,
        repl_default_version=repl_default_version,
        repl_fork_match=repl_fork_match,
        symbols=symbols,
        caveats=caveats,
        ok=ok,
    )


def probe_report_paths(paths: RepoPaths) -> tuple[Path, Path]:
    """(canonical report path, raw artifact path) for the A.8 outputs."""
    report = paths.reports / "compatibility" / "leaninteract_api.json"
    artifact = paths.artifacts / "compatibility" / f"leaninteract_api_{PINNED_VERSION}.json"
    return report, artifact
