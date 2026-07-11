"""Benchmark denylist freeze (PLAN.md §19.4, §19.7, LF-013).

Writes ``data/benchmarks/frozen_ids.json`` BEFORE any Phase-4 generation:
exact source IDs, normalized-NL hashes, and raw-text hashes for every
protected benchmark, so contaminated items can be excluded from the problem
pool and no benchmark row leaks into training/calibration/prompts.
Representation-based near-duplicate signatures are appended (never rewritten)
at the end of Phase 3 (§19.4); this module only writes the pre-generation
identity + text signatures.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from pydantic import Field, field_validator

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.schemas.manifest import require_utc

FREEZE_SCHEMA_VERSION = 1

_WS = re.compile(r"\s+")


def normalize_nl(text: str) -> str:
    """Canonical NL form for near-duplicate matching: lowercase, collapse all
    whitespace to single spaces, strip. Deliberately aggressive so trivially
    reformatted problem statements still match."""
    return _WS.sub(" ", text.lower()).strip()


def normalize_lean(text: str) -> str:
    """Canonical Lean-text form: collapse whitespace, strip. Case-preserving
    (identifiers are case-sensitive)."""
    return _WS.sub(" ", text).strip()


def text_hash(normalized: str) -> str:
    return sha256_hex(normalized.encode("utf-8"))


def nl_hash(text: str) -> str:
    return text_hash(normalize_nl(text))


def lean_hash(text: str) -> str:
    return text_hash(normalize_lean(text))


class FrozenBenchmark(StrictModel):
    """One protected benchmark's frozen identity + text signatures."""

    registry_key: str
    source_id: str | None = None
    revision: str | None = None
    role: str = "evaluation_only"
    resolved: bool
    splits: dict[str, int] = Field(default_factory=dict)
    row_ids: tuple[str, ...] = ()
    nl_hashes: tuple[str, ...] = ()
    text_hashes: tuple[str, ...] = ()
    resolution_plan: str = ""

    def all_text_hashes(self) -> frozenset[str]:
        return frozenset(self.nl_hashes) | frozenset(self.text_hashes)


class FrozenRegistry(StrictModel):
    """The machine-readable denylist written before generation (§19.4)."""

    schema_version: int = FREEZE_SCHEMA_VERSION
    frozen_at: datetime.datetime
    policy_version: str = "benchmark_denylist_v1"
    benchmarks: tuple[FrozenBenchmark, ...]
    representation_signatures_appended: bool = False

    _utc = field_validator("frozen_at")(require_utc)


def build_proofnetverif(
    rows_by_split: dict[str, list[dict[str, object]]],
    *,
    source_id: str,
    revision: str,
) -> FrozenBenchmark:
    """Freeze ProofNetVerif: NL statements + reference and candidate Lean.

    §9.3 columns: id, nl_statement, lean4_formalization (reference),
    lean4_prediction (candidate). All three carry contaminating content, so
    NL and both Lean sides are hashed.
    """
    row_ids: list[str] = []
    nl_hashes: set[str] = set()
    text_hashes: set[str] = set()
    splits: dict[str, int] = {}
    for split, rows in sorted(rows_by_split.items()):
        splits[split] = len(rows)
        for row in rows:
            row_ids.append(f"{split}:{row['id']}")
            nl_hashes.add(nl_hash(str(row["nl_statement"])))
            text_hashes.add(lean_hash(str(row["lean4_formalization"])))
            text_hashes.add(lean_hash(str(row["lean4_prediction"])))
    return FrozenBenchmark(
        registry_key="proofnetverif",
        source_id=source_id,
        revision=revision,
        resolved=True,
        splits=splits,
        row_ids=tuple(sorted(row_ids)),
        nl_hashes=tuple(sorted(nl_hashes)),
        text_hashes=tuple(sorted(text_hashes)),
    )


def unresolved_benchmark(registry_key: str, resolution_plan: str) -> FrozenBenchmark:
    """A benchmark denylisted by name whose exact identity is resolved before
    it is used (§19.7 / J.6: recorded, never substituted)."""
    return FrozenBenchmark(
        registry_key=registry_key, resolved=False, resolution_plan=resolution_plan
    )


class DenylistIndex:
    """O(1) membership over frozen NL and Lean text hashes (§9.4 pool filter)."""

    def __init__(self, registry: FrozenRegistry) -> None:
        self._nl: set[str] = set()
        self._text: set[str] = set()
        self._ids: set[str] = set()
        for benchmark in registry.benchmarks:
            self._nl.update(benchmark.nl_hashes)
            self._text.update(benchmark.text_hashes)
            self._ids.update(benchmark.row_ids)

    def contains_nl(self, text: str) -> bool:
        return nl_hash(text) in self._nl

    def contains_lean(self, text: str) -> bool:
        return lean_hash(text) in self._text

    def contains_any(self, *, nl: str | None = None, lean: str | None = None) -> bool:
        return (nl is not None and self.contains_nl(nl)) or (
            lean is not None and self.contains_lean(lean)
        )

    def __len__(self) -> int:
        return len(self._nl) + len(self._text)


def frozen_ids_path(data_dir: Path) -> Path:
    return data_dir / "benchmarks" / "frozen_ids.json"


def write_frozen_registry(registry: FrozenRegistry, path: Path) -> str:
    """Write the frozen registry as canonical JSON; return its file sha256."""
    from leanfaith.config.hashing import canonical_json_bytes, hash_file

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(registry.model_dump(mode="json")) + b"\n"
    path.write_bytes(payload)
    return hash_file(path)


def load_frozen_registry(path: Path) -> FrozenRegistry:
    data = json.loads(path.read_text(encoding="utf-8"))
    return FrozenRegistry.model_validate(data)
