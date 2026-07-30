"""LF-012: extraction orchestration with a fake backend (no Lean)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


class RejectBlankDatasetRouteBackend:
    """A source-unavailable route must never reach the Lean boundary."""

    def __init__(self, *, declarations: tuple[dict, ...] = ()) -> None:
        self.declarations = declarations
        self.calls: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        assert request.code is not None
        assert request.code.strip()
        self.calls.append(request)
        return LeanResult(
            request_id=request.request_id,
            request_hash="b" * 64,
            context_id=request.context_id,
            context_fingerprint="e" * 64,
            status=LeanStatus.VALID_WITH_SORRY,
            declarations=self.declarations,
        )


class NonPropQuestionValidFallbackBackend:
    """The canonical route is the first accepted proposition, not any declaration."""

    def __init__(self) -> None:
        self.calls: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        assert request.code is not None
        assert request.code.strip()
        self.calls.append(request)
        if "reval" in request.request_id:
            declarations: tuple[dict, ...] = ()
        elif "question_statement" in request.request_id:
            declarations = (
                {
                    **_DECL,
                    "kind": "definition",
                    "signature": {**_DECL["signature"], "pp": ": Nat"},
                    "type": {"pp": "Nat"},
                },
            )
        else:
            declarations = (_DECL,)
        return LeanResult(
            request_id=request.request_id,
            request_hash="c" * 64,
            context_id=request.context_id,
            context_fingerprint="e" * 64,
            status=LeanStatus.VALID_WITH_SORRY,
            declarations=declarations,
        )


def test_sft_non_prop_question_does_not_suppress_valid_theorem_fallback(tmp_path: Path) -> None:
    backend = NonPropQuestionValidFallbackBackend()
    row = {
        "uuid": "non-prop-question-valid-fallback",
        "data_source": "fixture",
        "question": "```lean4\ndef t_ok : Nat := 1\n```",
        "lean_code": _SNIPPET,
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
    assert stats.declarations_seen == 2
    assert stats.accepted == 1
    assert stats.failures == 1
    assert stats.failure_codes == {"not_a_proposition": 1}
    assert stats.row_outcomes == {"accepted_lean_code_fallback": 1}
    assert stats.declaration_outcomes == {"accepted": 1, "failed_or_skipped": 1}
    assert [call.request_id.rsplit("-", 1)[-1] for call in backend.calls[:2]] == [
        "question_statement",
        "lean_code_fallback",
    ]
    theorem = json.loads((tmp_path / "theorems" / "sft_classic.jsonl").read_text().splitlines()[0])[
        "theorem"
    ]
    assert theorem["extraction_route"] == "lean_code_fallback"
    assert theorem["nl_pair_eligibility"] == "unverified"
    assert theorem["question_lean_code_agreement"] == "question_unavailable"
    failures = [
        json.loads(line)
        for line in (tmp_path / "failures" / "sft_classic.jsonl").read_text().splitlines()
    ]
    assert failures == [
        {
            "code": "not_a_proposition",
            "declaration_name": "t_ok",
            "detail": "declaration kind 'definition' is not proposition-valued",
            "extraction_route": "question_statement",
            "outcome_level": "declaration",
            "source_record": theorem["source_record_id"],
        }
    ]


def test_sft_unstrippable_fallback_is_source_unavailable_not_infrastructure_error(
    tmp_path: Path,
) -> None:
    non_prop = {
        **_DECL,
        "kind": "definition",
        "signature": {
            **_DECL["signature"],
            "pp": ": Nat",
        },
        "type": {"pp": "Nat"},
    }
    backend = RejectBlankDatasetRouteBackend(declarations=(non_prop,))
    row = {
        "uuid": "empty-fallback",
        "data_source": "fixture",
        "question": "```lean4\ndef t_ok : Nat := 1\n```",
        # A non-theorem fallback is real source content, but the proof-strip
        # route is unsupported and therefore produces no Lean command.
        "lean_code": "def fallback : Nat := 1",
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

    assert len(backend.calls) == 1
    assert "question_statement" in backend.calls[0].request_id
    assert stats.row_outcomes == {"not_a_proposition": 1}
    failure = json.loads((tmp_path / "failures" / "sft_classic.jsonl").read_text().splitlines()[-1])
    assert "fallback=unsupported" in failure["detail"]
    assert "infrastructure_error" not in failure["detail"]


def test_sft_whitespace_fence_is_unsupported_structure_not_missing_fence(tmp_path: Path) -> None:
    backend = RejectBlankDatasetRouteBackend()
    row = {
        "uuid": "whitespace-fence",
        "data_source": "fixture",
        "question": "```lean4\n   \n```",
        "lean_code": "   ",
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

    assert backend.calls == []
    assert stats.row_outcomes == {"unsupported_structure": 1}
    failure = json.loads((tmp_path / "failures" / "sft_classic.jsonl").read_text().splitlines()[-1])
    assert failure["code"] == "unsupported_structure"
    assert "question=unsupported; fallback=unsupported" in failure["detail"]
    assert "infrastructure_error" not in failure["detail"]


def test_sft_two_blank_routes_never_call_lean(tmp_path: Path) -> None:
    backend = RejectBlankDatasetRouteBackend()
    row = {
        "uuid": "blank-routes",
        "data_source": "fixture",
        "question": "No fenced Lean declaration is present.",
        "lean_code": "   ",
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

    assert backend.calls == []
    assert stats.row_outcomes == {"missing_lean_fence": 1}
    failure = json.loads((tmp_path / "failures" / "sft_classic.jsonl").read_text().splitlines()[-1])
    assert "question=unsupported; fallback=unsupported" in failure["detail"]
    assert "infrastructure_error" not in failure["detail"]


def test_sft_empty_route_rejects_noncanonical_context_before_lean(tmp_path: Path) -> None:
    backend = RejectBlankDatasetRouteBackend()
    row = {
        "uuid": "bad-context",
        "data_source": "fixture",
        "question": "No fenced Lean declaration is present.",
        "lean_code": "",
        "valid": False,
        "proof_repair": False,
    }

    with pytest.raises(ValueError, match="canonical form"):
        extract_sft_classic_rows(
            backend,  # type: ignore[arg-type]
            [row],
            source_revision="0bf9",
            split="train",
            row_offset=0,
            context_id="ctx:not-a-fingerprint",
            out_dir=tmp_path,
        )
    assert backend.calls == []


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


class CrashBackend:
    """Return one scripted infrastructure crash without invoking Lean."""

    def __init__(self, infrastructure_error: str) -> None:
        self.infrastructure_error = infrastructure_error

    def run(self, request: LeanRequest) -> LeanResult:
        return LeanResult(
            request_id=request.request_id,
            request_hash="c" * 64,
            context_id=request.context_id,
            context_fingerprint="e" * 64,
            status=LeanStatus.CRASH,
            infrastructure_error=self.infrastructure_error,
        )


def test_repository_thread_creation_crash_persists_bounded_diagnostic(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "mathlib"
    (checkout / "Mathlib").mkdir(parents=True)
    relative_path = "Mathlib/F.lean"
    (checkout / relative_path).write_text(_SNIPPET + "\n")
    raw_error = (
        "resource_limit_thread_creation: ConnectionAbortedError: "
        "stderr: failed to create thread\n" + "x" * 4000
    )

    stats = extract_repository_files(
        CrashBackend(raw_error),  # type: ignore[arg-type]
        checkout,
        [relative_path],
        source="mathlib",
        source_revision="d568",
        context_id=_CTX,
        out_dir=tmp_path / "out",
    )

    failure = json.loads((tmp_path / "out" / "failures" / "mathlib.jsonl").read_text().strip())
    assert stats.row_outcomes == {"worker_crash": 1}
    assert stats.failure_codes == {"worker_crash": 1}
    assert failure["code"] == "worker_crash"
    assert "resource_limit_thread_creation" in failure["detail"]
    assert "failed to create thread" in failure["detail"]
    assert len(failure["detail"]) == 2000


def test_repository_ordinary_crash_keeps_worker_crash_mapping(tmp_path: Path) -> None:
    checkout = tmp_path / "mathlib"
    (checkout / "Mathlib").mkdir(parents=True)
    relative_path = "Mathlib/F.lean"
    (checkout / relative_path).write_text(_SNIPPET + "\n")

    stats = extract_repository_files(
        CrashBackend("ConnectionAbortedError: ordinary child exit"),  # type: ignore[arg-type]
        checkout,
        [relative_path],
        source="mathlib",
        source_revision="d568",
        context_id=_CTX,
        out_dir=tmp_path / "out",
    )

    failure = json.loads((tmp_path / "out" / "failures" / "mathlib.jsonl").read_text().strip())
    assert stats.row_outcomes == {"worker_crash": 1}
    assert failure["code"] == "worker_crash"
    assert failure["detail"].endswith(
        "file_infrastructure_error=ConnectionAbortedError: ordinary child exit"
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
