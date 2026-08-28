"""Dataset construction (PLAN.md §19).

The experimental-corpus module depends on deterministic transformation
receipts.  Keep those exports lazy so importing a low-level transformation
module cannot loop back through this package initializer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from leanfaith.datasets.denylist import (
    ActiveBenchmarkRegistry,
    BenchmarkRegistryPreflightError,
    DenylistIndex,
    FrozenBenchmark,
    FrozenRegistry,
    RepresentationSignatureManifest,
    append_representation_signatures,
    build_formalrx_test,
    load_active_benchmark_registry,
    load_frozen_registry,
    normalize_lean,
    normalize_nl,
    text_hash,
    write_frozen_registry,
)

if TYPE_CHECKING:
    from leanfaith.datasets.experimental_machine_supervision import (
        ExperimentalMachineSupervisionArtifacts,
        ExperimentalMachineSupervisionConfig,
        ExperimentalMachineSupervisionError,
        ExperimentalMachineSupervisionManifest,
        ExperimentalMachineSupervisionRecord,
        ExperimentalMachineSupervisionSummary,
        ExperimentalSplitAssignment,
        freeze_experimental_machine_supervision,
        load_experimental_machine_supervision,
        load_experimental_machine_supervision_config,
        verify_experimental_machine_supervision,
    )


_EXPERIMENTAL_EXPORTS = frozenset(
    {
        "ExperimentalMachineSupervisionArtifacts",
        "ExperimentalMachineSupervisionConfig",
        "ExperimentalMachineSupervisionError",
        "ExperimentalMachineSupervisionManifest",
        "ExperimentalMachineSupervisionRecord",
        "ExperimentalMachineSupervisionSummary",
        "ExperimentalSplitAssignment",
        "freeze_experimental_machine_supervision",
        "load_experimental_machine_supervision",
        "load_experimental_machine_supervision_config",
        "verify_experimental_machine_supervision",
    }
)


def __getattr__(name: str) -> Any:
    """Load transformation-dependent experimental exports on first access."""

    if name not in _EXPERIMENTAL_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from leanfaith.datasets import experimental_machine_supervision

    value = getattr(experimental_machine_supervision, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy public names to introspection and editor completion."""

    return sorted(set(globals()) | _EXPERIMENTAL_EXPORTS)


__all__ = [
    "ActiveBenchmarkRegistry",
    "BenchmarkRegistryPreflightError",
    "DenylistIndex",
    "ExperimentalMachineSupervisionArtifacts",
    "ExperimentalMachineSupervisionConfig",
    "ExperimentalMachineSupervisionError",
    "ExperimentalMachineSupervisionManifest",
    "ExperimentalMachineSupervisionRecord",
    "ExperimentalMachineSupervisionSummary",
    "ExperimentalSplitAssignment",
    "FrozenBenchmark",
    "FrozenRegistry",
    "RepresentationSignatureManifest",
    "append_representation_signatures",
    "build_formalrx_test",
    "freeze_experimental_machine_supervision",
    "load_active_benchmark_registry",
    "load_experimental_machine_supervision",
    "load_experimental_machine_supervision_config",
    "load_frozen_registry",
    "normalize_lean",
    "normalize_nl",
    "text_hash",
    "verify_experimental_machine_supervision",
    "write_frozen_registry",
]
