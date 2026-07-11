"""ProofNetVerif adapter (PLAN.md LF-011, §9.3): frozen external benchmark.

Row mapping is the verified §9.3 table; the benchmark is evaluation-only —
no parsed row may enter training pools unless a manifest explicitly
designates a trainable partition (§9.1).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from leanfaith.config.models import StrictModel

ADAPTER_VERSION = "proofnetverif_adapter_v1"

#: §9.3 mapping, verbatim.
COLUMN_MAPPING = {
    "problem_id": "id",
    "nl_statement": "nl_statement",
    "lean_header": "lean4_src_header",
    "reference_lean": "lean4_formalization",
    "candidate_lean": "lean4_prediction",
    "source_label": "correct",
}


class ProofNetVerifRow(StrictModel):
    """One mapped benchmark row (evaluation-only)."""

    adapter_version: str = ADAPTER_VERSION
    problem_id: str
    split: str
    nl_statement: str
    lean_header: str
    reference_lean: str
    candidate_lean: str
    source_label: bool
    usage: str = "evaluation_only"
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


def parse_row(row: dict[str, Any], *, split: str) -> ProofNetVerifRow:
    return ProofNetVerifRow(
        problem_id=str(row[COLUMN_MAPPING["problem_id"]]),
        split=split,
        nl_statement=str(row[COLUMN_MAPPING["nl_statement"]]),
        lean_header=str(row[COLUMN_MAPPING["lean_header"]]),
        reference_lean=str(row[COLUMN_MAPPING["reference_lean"]]),
        candidate_lean=str(row[COLUMN_MAPPING["candidate_lean"]]),
        source_label=bool(row[COLUMN_MAPPING["source_label"]]),
    )
