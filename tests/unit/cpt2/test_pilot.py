from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.cpt2.pilot import (
    MethodAudit,
    choose_method,
    run_one_example,
    select_oracle_rows,
    serialize_row,
)
from leanfaith.cpt2.source import SourceRow
from leanfaith.cpt2.splitters import (
    DECLARATION_AWARE_METHOD,
    MASKED_REVERSE_METHOD,
    RAW_REVERSE_METHOD,
    split_source,
)


def _audit(method: str, *, coverage: float, agreement: float, speed: float) -> MethodAudit:
    return MethodAudit(
        method=method,
        eligible=100,
        total=100,
        elapsed_seconds=1.0,
        rows_per_second=speed,
        coverage=coverage,
        exact_matches=100,
        oracle_boundaries=100,
        exact_rate=agreement,
    )


def test_raw_method_has_priority_when_it_passes() -> None:
    audits = (
        _audit(RAW_REVERSE_METHOD, coverage=0.98, agreement=0.99, speed=10),
        _audit(MASKED_REVERSE_METHOD, coverage=1.0, agreement=1.0, speed=20),
        _audit(DECLARATION_AWARE_METHOD, coverage=1.0, agreement=1.0, speed=30),
    )
    assert choose_method(audits) == RAW_REVERSE_METHOD


def test_fastest_qualified_nonraw_method_is_selected() -> None:
    audits = (
        _audit(RAW_REVERSE_METHOD, coverage=1.0, agreement=0.98, speed=100),
        _audit(MASKED_REVERSE_METHOD, coverage=1.0, agreement=1.0, speed=20),
        _audit(DECLARATION_AWARE_METHOD, coverage=1.0, agreement=1.0, speed=10),
    )
    assert choose_method(audits) == MASKED_REVERSE_METHOD


def test_no_method_below_threshold_can_be_selected() -> None:
    with pytest.raises(ValueError, match="no CPT2 splitter"):
        choose_method((_audit(RAW_REVERSE_METHOD, coverage=0.97, agreement=1.0, speed=10),))


def test_serialized_schema_and_label_are_exact() -> None:
    split = split_source("theorem x : True := by trivial", DECLARATION_AWARE_METHOD)
    assert split is not None
    row = serialize_row(split, False)
    assert tuple(row) == ("theorem", "body", "label")
    assert row["label"] is False
    with pytest.raises(TypeError, match="source isValid bool"):
        serialize_row(split, 0)  # type: ignore[arg-type]


def test_one_example_resume_does_not_duplicate_rows(tmp_path: Path) -> None:
    first = run_one_example(tmp_path)
    initial = (tmp_path / "data.jsonl").read_bytes()
    second = run_one_example(tmp_path)
    assert first["resumed"] is False
    assert second["resumed"] is True
    assert (tmp_path / "data.jsonl").read_bytes() == initial
    rows = [json.loads(line) for line in initial.decode().splitlines()]
    assert len(rows) == 3
    assert all(tuple(row) == ("theorem", "body", "label") for row in rows)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["data_rows"] == 3


def test_oracle_selection_is_deterministic_and_label_stratified() -> None:
    rows = tuple(
        SourceRow(
            source_id=f"row-{index}",
            row_group=index % 4,
            row_offset=index,
            source_code=f"theorem t{index} : True := by trivial\n" + ("-- x\n" * (index % 5)),
            is_valid=bool(index % 2),
        )
        for index in range(40)
    )
    first = select_oracle_rows(rows, count=20)
    second = select_oracle_rows(rows, count=20)
    assert first == second
    assert {row.is_valid for row in first} == {False, True}


def test_oracle_selection_excludes_rows_unmatched_by_every_candidate() -> None:
    unmatched = SourceRow("unmatched", 0, 0, "#check True\n", False)
    eligible = tuple(
        SourceRow(
            f"row-{index}",
            0,
            index + 1,
            f"theorem t{index} : True := by trivial\n",
            bool(index % 2),
        )
        for index in range(8)
    )
    selected = select_oracle_rows((unmatched, *eligible), count=4)
    assert unmatched not in selected
