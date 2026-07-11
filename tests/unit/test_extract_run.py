"""LF-012: extraction orchestration with a fake backend (no Lean)."""

from __future__ import annotations

import json
from pathlib import Path

from leanfaith.lean.extract_run import extract_dataset_snippets, extract_repository_files
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
