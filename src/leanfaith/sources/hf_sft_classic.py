"""sft_classic adapter (PLAN.md LF-011, §9.3, §9.4).

Verified structure at pinned revision 0bf9f424 (probe 2026-07-10):
``question`` is a prompt-wrapped, often *truncated*, proof-stripped Lean
statement whose ``/-- ... -/`` docstring carries the NL problem statement;
``lean_code`` is the complete (typechecked when ``valid``) proof;
``data_source`` names the upstream corpus per row.

The adapter extracts the NL statement from the docstring and takes the
complete Lean from ``lean_code`` (statement isolation/proof-stripping is
LF-012's Lean-aware job — never regex, §12.2). Rows whose docstring cannot
be recovered are kept as explicit failure records, not dropped (§10 rule 5).

NL trust (§9.4): mapped per ``data_source`` from the versioned table below;
nothing is upgraded to trusted-human-NL until provenance is verified.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import NLTrust

ADAPTER_VERSION = "sft_classic_adapter_v1"

#: §9.4 per-row trust map (versioned adapter policy). Every upstream corpus
#: is `uncertain` until its NL provenance chain is verified and recorded;
#: Lean-Workbook-derived rows would be `synthetic` (none observed in probe).
DATA_SOURCE_TRUST: dict[str, NLTrust] = {
    "Goedel-LM/Goedel-Pset-v1": NLTrust.UNCERTAIN,
    "Goedel-LM/SFT_dataset_v2": NLTrust.UNCERTAIN,
    "formalmathatepfl/solved_problems_finetuning_iter1": NLTrust.UNCERTAIN,
    "AI-MO/NuminaMath-LEAN": NLTrust.UNCERTAIN,
    "uw-math-ai/APRIL": NLTrust.UNCERTAIN,
}

_LEAN_FENCE = re.compile(r"```lean4?\s*\n(.*?)(?:```|\Z)", re.DOTALL)
_DOCSTRING = re.compile(r"/--(.*?)-/", re.DOTALL)
_HEADER_LINE = re.compile(r"^\s*(import\s|set_option\s|open\s)")

#: Lean-Workbook-derived rows surface across ALL data_source values and are
#: identifiable by their declaration names (verified on the probe sample:
#: 33/100 rows). They are always synthetic NL and overlap-tagged
#: (§9.1 ReForm x Lean-Workbook rule, §9.4).
_LEAN_WORKBOOK_MARKER = re.compile(
    r"^\s*(?:theorem|lemma)\s+lean_workbook|^lean_workbook", re.MULTILINE
)


def classify_trust(problem_id: str, data_source: str, lean_source: str) -> NLTrust:
    """Per-row §9.4 trust: Lean-Workbook-derived rows (by uuid or declaration
    name) are synthetic; every other upstream corpus stays uncertain until
    its NL provenance chain is verified."""
    if _LEAN_WORKBOOK_MARKER.search(problem_id) or _LEAN_WORKBOOK_MARKER.search(lean_source):
        return NLTrust.SYNTHETIC
    return DATA_SOURCE_TRUST.get(data_source, NLTrust.UNCERTAIN)


class UnwrappedQuestion(StrictModel):
    """Parse product of one ``question`` field."""

    header_lines: tuple[str, ...] = ()
    nl_statement: str | None = None
    statement_fragment: str | None = None
    fence_found: bool = False
    truncated: bool = False


class ParsedRow(StrictModel):
    """One parsed sft_classic row (parsed partition record)."""

    adapter_version: str = ADAPTER_VERSION
    problem_id: str
    data_source: str
    nl_statement: str | None
    nl_trust: NLTrust
    lean_source: str
    question_statement_fragment: str | None
    source_valid: bool
    proof_repair: bool
    parse_status: str  # "parsed" | "no_docstring" | "no_fence"
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @property
    def eligible_nl(self) -> bool:
        """§9.4 eligibility precondition: nonempty NL separated from solution."""
        return self.parse_status == "parsed" and bool((self.nl_statement or "").strip())


def unwrap_question(question: str) -> UnwrappedQuestion:
    """Extract header, NL docstring, and statement fragment from a prompt."""
    fence = _LEAN_FENCE.search(question)
    if fence is None:
        return UnwrappedQuestion(fence_found=False)
    block = fence.group(1)
    truncated = "```" not in question[fence.start(1) :]
    header_lines = tuple(line.strip() for line in block.splitlines() if _HEADER_LINE.match(line))
    doc = _DOCSTRING.search(block)
    nl_statement = None
    statement_fragment = None
    if doc is not None:
        nl_statement = " ".join(doc.group(1).split()) or None
        statement_fragment = block[doc.end() :].strip() or None
    else:
        # No docstring: the statement fragment starts at the first declaration.
        decl = re.search(r"^\s*(theorem|lemma|example)\b", block, re.MULTILINE)
        if decl is not None:
            statement_fragment = block[decl.start() :].strip() or None
    return UnwrappedQuestion(
        header_lines=header_lines,
        nl_statement=nl_statement,
        statement_fragment=statement_fragment,
        fence_found=True,
        truncated=truncated,
    )


def parse_row(row: dict[str, Any]) -> ParsedRow:
    """Parse one raw sft_classic row into the parsed-partition record."""
    unwrapped = unwrap_question(str(row.get("question", "")))
    if not unwrapped.fence_found:
        parse_status = "no_fence"
    elif unwrapped.nl_statement is None:
        parse_status = "no_docstring"
    else:
        parse_status = "parsed"
    data_source = str(row.get("data_source", ""))
    problem_id = str(row["uuid"])
    lean_source = str(row.get("lean_code", ""))
    return ParsedRow(
        problem_id=problem_id,
        data_source=data_source,
        nl_statement=unwrapped.nl_statement,
        nl_trust=classify_trust(problem_id, data_source, lean_source),
        lean_source=lean_source,
        question_statement_fragment=unwrapped.statement_fragment,
        source_valid=bool(row.get("valid", False)),
        proof_repair=bool(row.get("proof_repair", False)),
        parse_status=parse_status,
        metadata={
            "token_count": row.get("token_count"),
            "tactic_count": row.get("tactic_count"),
            "question_truncated": unwrapped.truncated,
        },
    )
