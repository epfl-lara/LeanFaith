"""Probe framework contracts (PLAN.md §9.2, §9.5, LF-010).

A probe verifies a source's identity/access/schema and archives evidence; it
never fabricates data or APIs. A blocked source produces a structured blocked
outcome and the declared fallback order takes over (§25 item 10, §30.1).
"""

from __future__ import annotations

import datetime
from typing import Protocol

from pydantic import Field

from leanfaith.config.models import StrictModel
from leanfaith.schemas.source import SourceManifest


class SourceProbeError(RuntimeError):
    """Raised when a probe fails in a way that must stop the run (never a
    silent fallback): schema mismatch, hash mismatch, or archival failure."""


class DatasetProbeInfo(StrictModel):
    """What a dataset client reports about a source (client-agnostic)."""

    resolved_id: str
    revision: str
    license: str | None = None
    gated: bool = False
    splits: dict[str, int | None] = Field(default_factory=dict)
    columns: tuple[str, ...] = ()


class ProbeOutcome(StrictModel):
    """Terminal result of one source probe.

    ``sample_jsonl`` holds the canonical-JSONL sample inline until
    ``archive_probe`` writes it to ``data/raw/sources/<source>/`` and strips
    it, leaving ``sample_path``/``sample_hash`` as the durable evidence.
    """

    source: str
    manifest: SourceManifest | None = None
    sample_jsonl: str | None = None
    sample_path: str | None = None
    sample_hash: str | None = None
    sample_row_count: int | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    probed_at: datetime.datetime

    @property
    def accessible(self) -> bool:
        return not self.blocked and self.manifest is not None


class SourceProber(Protocol):
    """One configured source probe; ``probe()`` must be idempotent."""

    source: str

    def probe(self) -> ProbeOutcome: ...
