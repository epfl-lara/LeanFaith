"""LF-011: sft_classic prompt unwrapping and row parsing (§9.3, §9.4).

Synthetic fixtures mirror the verified probe structure; the real archived
sample (private, local-only) is exercised by a skippable test at the end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.enums import NLTrust
from leanfaith.sources.hf_sft_classic import (
    DATA_SOURCE_TRUST,
    parse_row,
    unwrap_question,
)

_FULL_QUESTION = """Solve the following problem with Lean 4 code and explanatory comments:

```lean4
import Mathlib
import Aesop

set_option maxHeartbeats 0

open BigOperators Real Nat Topology Rat

/-- Given that Sophie receives 20 oranges per day, over 30 days
    she receives 600 oranges in total. -/
theorem oranges_total (daily : ℕ) (h : daily = 20) : daily * 30 = 600 := by sorry
```

Replace every sorry statement with an appropriate proof."""

_TRUNCATED_QUESTION = """Solve the following problem with Lean 4 code and explanatory comments:

```lean4
import Mathlib

/-- A truncated prompt whose statement is cut mid-binder. -/
theorem truncated_example
  (x : ℕ := by sorry"""


def _row(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "uuid": "Goedel-Pset-000001",
        "data_source": "Goedel-LM/Goedel-Pset-v1",
        "question": _FULL_QUESTION,
        "answer": "The complete typechecked Lean 4 proof is: ...",
        "proof_plan": None,
        "valid": True,
        "proof_repair": False,
        "lean_code": "import Mathlib\ntheorem oranges_total : True := trivial",
        "token_count": 100.0,
        "tactic_count": 2.0,
        "lean_score": -1.0,
        "lean_rank": 1.0,
    }
    payload.update(overrides)
    return payload


def test_unwrap_extracts_docstring_nl_and_fragment() -> None:
    unwrapped = unwrap_question(_FULL_QUESTION)
    assert unwrapped.fence_found and not unwrapped.truncated
    assert unwrapped.nl_statement is not None
    assert unwrapped.nl_statement.startswith("Given that Sophie receives 20 oranges")
    assert "  " not in unwrapped.nl_statement  # whitespace normalized
    assert unwrapped.statement_fragment is not None
    assert unwrapped.statement_fragment.startswith("theorem oranges_total")
    assert unwrapped.header_lines == (
        "import Mathlib",
        "import Aesop",
        "set_option maxHeartbeats 0",
        "open BigOperators Real Nat Topology Rat",
    )


def test_unwrap_flags_truncated_prompts() -> None:
    unwrapped = unwrap_question(_TRUNCATED_QUESTION)
    assert unwrapped.fence_found and unwrapped.truncated
    assert unwrapped.nl_statement is not None
    assert unwrapped.statement_fragment is not None


def test_unwrap_without_fence() -> None:
    unwrapped = unwrap_question("plain text, no code block")
    assert not unwrapped.fence_found
    assert unwrapped.nl_statement is None


def test_unwrap_without_docstring_finds_declaration() -> None:
    question = "```lean4\nimport Mathlib\ntheorem bare : True := by sorry\n```"
    unwrapped = unwrap_question(question)
    assert unwrapped.nl_statement is None
    assert unwrapped.statement_fragment is not None
    assert unwrapped.statement_fragment.startswith("theorem bare")


def test_parse_row_full() -> None:
    parsed = parse_row(_row())
    assert parsed.parse_status == "parsed"
    assert parsed.eligible_nl
    assert parsed.problem_id == "Goedel-Pset-000001"
    assert parsed.nl_trust is NLTrust.UNCERTAIN
    assert parsed.lean_source.startswith("import Mathlib")
    assert parsed.metadata["question_truncated"] is False


def test_parse_row_no_docstring_is_failure_record() -> None:
    parsed = parse_row(_row(question="```lean4\nimport Mathlib\ntheorem t : True := by sorry\n```"))
    assert parsed.parse_status == "no_docstring"
    assert not parsed.eligible_nl


def test_parse_row_no_fence_is_failure_record() -> None:
    parsed = parse_row(_row(question="no lean here"))
    assert parsed.parse_status == "no_fence"
    assert not parsed.eligible_nl


def test_unknown_data_source_defaults_to_uncertain() -> None:
    parsed = parse_row(_row(data_source="some/new-corpus"))
    assert parsed.nl_trust is NLTrust.UNCERTAIN


def test_trust_table_never_grants_trusted() -> None:
    # §9.4: trusted-human-NL requires verified provenance; the v1 table has none.
    assert all(trust is not NLTrust.TRUSTED for trust in DATA_SOURCE_TRUST.values())


_SAMPLE = (
    find_repo_root(Path(__file__).parent)
    / "data"
    / "raw"
    / "sources"
    / "sft_classic"
    / "probe_sample.jsonl"
)


@pytest.mark.skipif(not _SAMPLE.is_file(), reason="private probe sample not present")
def test_real_probe_sample_parses() -> None:
    rows = [json.loads(line) for line in _SAMPLE.read_text().strip().splitlines()]
    assert len(rows) == 100
    parsed = [parse_row(row) for row in rows]
    eligible = [p for p in parsed if p.eligible_nl]
    statuses = {p.parse_status for p in parsed}
    # Measured structure (probe 2026-07-10): ~37% of rows carry a docstring
    # NL statement; the rest are proof-SFT-only rows kept as explicit
    # no_docstring records (still usable on the Lean side).
    assert statuses <= {"parsed", "no_docstring", "no_fence"}
    assert len(eligible) >= 25, f"only {len(eligible)}/100 eligible; statuses={statuses}"
    assert all(p.lean_source for p in parsed)
    # Every parsed row is trust-tagged; nothing is trusted without provenance.
    assert all(p.nl_trust in (NLTrust.UNCERTAIN, NLTrust.SYNTHETIC) for p in parsed)


def test_lean_workbook_uuid_rows_are_synthetic() -> None:
    parsed = parse_row(
        _row(uuid="lean_workbook_plus_27542", data_source="Goedel-LM/SFT_dataset_v2")
    )
    assert parsed.nl_trust is NLTrust.SYNTHETIC


def test_lean_workbook_theorem_name_rows_are_synthetic() -> None:
    # Verified on the probe sample: workbook-derived rows keep Goedel uuids
    # but carry lean_workbook declaration names (33/100 rows).
    parsed = parse_row(
        _row(
            uuid="Goedel-Pset-1561612",
            lean_code=("import Mathlib\ntheorem lean_workbook_plus_27542 (a : ℝ) : a = a := rfl"),
        )
    )
    assert parsed.nl_trust is NLTrust.SYNTHETIC
