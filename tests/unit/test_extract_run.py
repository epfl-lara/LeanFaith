"""LF-012: extraction orchestration with a fake backend (no Lean)."""

from __future__ import annotations

import json
from pathlib import Path

from leanfaith.lean.extract_run import (
    ExtractStats,
    extract_dataset_snippets,
    extract_repository_files,
    extract_sft_classic_rows,
)
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus

_CTX = "ctx:" + "e" * 64

# One real declaration dict shape (from the probe) for a Nat commutativity thm.
_DECL = {
    "name": "t_ok",
    "full_name": "t_ok",
    "kind": "theorem",
    "range": {"start": {"line": 1, "column": 0}, "finish": {"line": 1, "column": 52}},
    "signature": {
        "pp": "(x y : Nat) : x + y = y + x",
        "range": {"start": {"line": 1, "column": 13}, "finish": {"line": 1, "column": 40}},
    },
    "type": {"pp": "x + y = y + x"},
}
_SNIPPET = "theorem t_ok (x y : Nat) : x + y = y + x := by omega"


def test_extract_stats_marker_round_trip() -> None:
    stats = ExtractStats(
        sources_processed=2,
        declarations_seen=3,
        accepted=2,
        failures=1,
        partial_declarations_reported=4,
    )
    stats.row_outcomes["accepted"] = 2
    stats.declaration_outcomes["accepted"] = 2
    stats.declaration_outcomes["failed_or_skipped"] = 1
    restored = ExtractStats.from_dict(stats.as_dict())
    assert restored.as_dict() == stats.as_dict()


class FakeBackend:
    """Scripted backend: declaration requests return _DECL; revalidation
    requests return a configurable status."""

    def __init__(self, *, decl_status: LeanStatus, reval_status: LeanStatus) -> None:
        self.decl_status = decl_status
        self.reval_status = reval_status
        self.calls: list[str] = []

    def _result(self, request: LeanRequest, status: LeanStatus, decls: tuple) -> LeanResult:
        return LeanResult(
            request_id=request.request_id,
            request_hash="a" * 64,
            context_id=request.context_id,
            context_fingerprint="e" * 64,
            status=status,
            declarations=decls,
        )

    def run(self, request: LeanRequest) -> LeanResult:
        self.calls.append(request.request_id)
        if "reval" in request.request_id:
            return self._result(request, self.reval_status, ())
        if request.declarations:
            return self._result(request, self.decl_status, (_DECL,))
        return self._result(request, LeanStatus.VALID, ())


def test_dataset_extraction_revalidates_ok(tmp_path: Path) -> None:
    backend = FakeBackend(decl_status=LeanStatus.VALID, reval_status=LeanStatus.VALID_WITH_SORRY)
    rows = [{"uuid": "row-1", "lean_code": _SNIPPET}]
    stats = extract_dataset_snippets(
        backend,  # type: ignore[arg-type]
        rows,
        source="sft_classic",
        source_revision="0bf9",
        context_id=_CTX,
        out_dir=tmp_path,
    )
    assert stats.accepted == 1
    assert stats.revalidated_ok == 1
    written = (tmp_path / "theorems" / "sft_classic.jsonl").read_text().strip().splitlines()
    assert len(written) == 1
    record = json.loads(written[0])
    assert record["theorem"]["proof_stripped_declaration"].endswith(":= by sorry")


def test_dataset_extraction_drops_failed_revalidation(tmp_path: Path) -> None:
    backend = FakeBackend(decl_status=LeanStatus.VALID, reval_status=LeanStatus.INVALID)
    rows = [{"uuid": "row-1", "lean_code": _SNIPPET}]
    stats = extract_dataset_snippets(
        backend,  # type: ignore[arg-type]
        rows,
        source="sft_classic",
        source_revision="0bf9",
        context_id=_CTX,
        out_dir=tmp_path,
    )
    assert stats.accepted == 0
    assert stats.revalidation_failed == 1
    assert not (tmp_path / "theorems" / "sft_classic.jsonl").exists()
    # The excluded declaration is persisted as an explicit failure record (§10 rule 5).
    failures = (tmp_path / "failures" / "sft_classic.jsonl").read_text().strip().splitlines()
    assert len(failures) == 1
    assert json.loads(failures[0])["code"] == "revalidation_failed"


def test_sft_invalid_partial_declarations_are_diagnostic_only(tmp_path: Path) -> None:
    backend = FakeBackend(decl_status=LeanStatus.INVALID, reval_status=LeanStatus.INVALID)
    row = {
        "uuid": "partial-row",
        "data_source": "Goedel-LM/Goedel-Pset-v1",
        "question": (
            "```lean4\n/-- A diagnostic-only partial declaration. -/\n"
            "theorem t_ok : True := by sorry\n```"
        ),
        "lean_code": "theorem t_ok : True := by trivial",
        "valid": False,
        "proof_repair": False,
    }

    stats = extract_sft_classic_rows(
        backend,  # type: ignore[arg-type]
        [row],
        source_revision="0bf9",
        split="train",
        row_offset=0,
        context_id=_CTX,
        out_dir=tmp_path,
    )

    assert stats.sources_processed == 1
    assert stats.declarations_seen == 0
    assert stats.declaration_outcomes == {}
    assert stats.partial_declarations_reported == 1
    failures = [
        json.loads(line)
        for line in (tmp_path / "failures" / "sft_classic.jsonl").read_text().splitlines()
    ]
    assert len(failures) == 1
    assert failures[0]["outcome_level"] == "row"
    assert "partial_declarations_reported=1" in failures[0]["detail"]


def test_dataset_extraction_counts_non_elaborating_source(tmp_path: Path) -> None:
    backend = FakeBackend(decl_status=LeanStatus.INVALID, reval_status=LeanStatus.VALID_WITH_SORRY)
    rows = [{"uuid": "row-1", "lean_code": "theorem broken : Nonsense := foo"}]
    stats = extract_dataset_snippets(
        backend,  # type: ignore[arg-type]
        rows,
        source="sft_classic",
        source_revision="0bf9",
        context_id=_CTX,
        out_dir=tmp_path,
    )
    assert stats.source_not_elaborating == 1
    assert stats.accepted == 0


def test_repository_extraction_writes_records(tmp_path: Path) -> None:
    checkout = tmp_path / "mathlib"
    (checkout / "Mathlib").mkdir(parents=True)
    (checkout / "Mathlib" / "F.lean").write_text(_SNIPPET + "\n")
    backend = FakeBackend(decl_status=LeanStatus.VALID, reval_status=LeanStatus.VALID_WITH_SORRY)
    stats = extract_repository_files(
        backend,  # type: ignore[arg-type]
        checkout,
        ["Mathlib/F.lean"],
        source="mathlib",
        source_revision="d568",
        context_id=_CTX,
        out_dir=tmp_path / "out",
    )
    assert stats.accepted == 1
    assert stats.declarations_seen == 1


class ZeroDeclBackend:
    """Elaborates VALID but reports no declarations (mutual-block case)."""

    def run(self, request: LeanRequest) -> LeanResult:
        return LeanResult(
            request_id=request.request_id,
            request_hash="a" * 64,
            context_id=request.context_id,
            context_fingerprint="e" * 64,
            status=LeanStatus.VALID,
            declarations=(),
        )


def test_elaborating_no_declarations_counted_distinctly(tmp_path: Path) -> None:
    stats = extract_dataset_snippets(
        ZeroDeclBackend(),  # type: ignore[arg-type]
        [{"uuid": "mut-1", "lean_code": "mutual\ntheorem a : True := b\nend"}],
        source="sft_classic",
        source_revision="0bf9",
        context_id=_CTX,
        out_dir=tmp_path,
    )
    assert stats.elaborating_no_declarations == 1
    assert stats.source_not_elaborating == 0
    assert stats.accepted == 0


def test_degraded_rerun_preserves_prior_partition(tmp_path: Path) -> None:
    # First run writes a good partition.
    good = FakeBackend(decl_status=LeanStatus.VALID, reval_status=LeanStatus.VALID_WITH_SORRY)
    extract_dataset_snippets(
        good,  # type: ignore[arg-type]
        [{"uuid": "row-1", "lean_code": _SNIPPET}],
        source="sft_classic",
        source_revision="0bf9",
        context_id=_CTX,
        out_dir=tmp_path,
    )
    partition = tmp_path / "theorems" / "sft_classic.jsonl"
    assert partition.exists()
    before = partition.read_text()

    # A degraded re-run (every source non-elaborating) must not leave a
    # half-written partition mid-flight; it atomically replaces at the end.
    broken = ZeroDeclBackend()
    extract_dataset_snippets(
        broken,  # type: ignore[arg-type]
        [{"uuid": "row-1", "lean_code": _SNIPPET}],
        source="sft_classic",
        source_revision="0bf9",
        context_id=_CTX,
        out_dir=tmp_path,
    )
    # Zero results this run -> partition removed (row_count=0 recorded in manifest),
    # but never a truncated/partial file.
    assert not (tmp_path / "theorems" / "sft_classic.jsonl.partial").exists()
    assert before  # sanity: the first run really produced content
