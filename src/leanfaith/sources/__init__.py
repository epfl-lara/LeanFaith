"""Source probe framework (PLAN.md §9, LF-010).

Framework only: probers, archival, and fallback-chain resolution. Full MVP
adapters (mathlib, selected NL source, ProofNetVerif) are LF-011; executing
live probes against private sources is gated on the §9.2 approval decisions.
"""

from leanfaith.sources.base import (
    DatasetProbeInfo,
    ProbeOutcome,
    SourceProbeError,
    SourceProber,
)
from leanfaith.sources.probe import (
    HFDatasetProber,
    HFProbeConfig,
    archive_probe,
    run_fallback_chain,
)
from leanfaith.sources.repository import GitProbeConfig, GitRepositoryProber

__all__ = [
    "DatasetProbeInfo",
    "GitProbeConfig",
    "GitRepositoryProber",
    "HFDatasetProber",
    "HFProbeConfig",
    "ProbeOutcome",
    "SourceProbeError",
    "SourceProber",
    "archive_probe",
    "run_fallback_chain",
]
