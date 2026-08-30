from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from leanfaith.cpt2.oracle import boundary_from_declarations, run_oracle
from leanfaith.cpt2.source import SourceRow
from leanfaith.cpt2.splitters import DECLARATION_AWARE_METHOD, split_source
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus


def _position(source: str, offset: int) -> dict[str, int]:
    prefix = source[:offset]
    return {"line": prefix.count("\n") + 1, "column": len(prefix.rsplit("\n", 1)[-1])}


def _declaration(source: str) -> dict[str, object]:
    declaration_start = source.index("theorem")
    signature_finish = source.index(":=", declaration_start)
    return {
        "name": "target",
        "kind": "theorem",
        "range": {
            "start": _position(source, declaration_start),
            "finish": _position(source, len(source)),
        },
        "signature": {
            "range": {
                "start": _position(source, declaration_start),
                "finish": _position(source, signature_finish),
            }
        },
    }


class FakeBackend:
    def __init__(self, original: str) -> None:
        self.original = original
        self.calls = 0

    def run(self, request: LeanRequest) -> LeanResult:
        return self.run_batch([request])[0]

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        results: list[LeanResult] = []
        for request in requests:
            self.calls += 1
            results.append(
                LeanResult(
                    request_id=request.request_id,
                    request_hash="a" * 64,
                    context_id=request.context_id,
                    context_fingerprint="f" * 64,
                    status=LeanStatus.VALID_WITH_SORRY,
                    declarations=(_declaration(self.original),),
                    elapsed_ms=4,
                )
            )
        return results

    def close(self) -> None:
        return None


def test_boundary_oracle_anchors_at_lean_signature_range() -> None:
    source = (
        "lemma helper : True := by trivial\n"
        "theorem target : True := by\n  have h : True := by trivial\n  exact h\n"
    )
    expected = split_source(source, DECLARATION_AWARE_METHOD)
    assert expected is not None
    assert boundary_from_declarations(source, [_declaration(source)]) == expected.by_offset


def test_oracle_cache_avoids_duplicate_lean_requests(tmp_path: Path) -> None:
    source = "theorem target : True := by trivial\n"
    row = SourceRow("row", 0, 0, source, True)
    backend = FakeBackend(source)
    cache = tmp_path / "oracle.jsonl"
    first = run_oracle(
        backend,
        [row],
        context_id="ctx:" + "f" * 64,
        context_fingerprint="f" * 64,
        cache_path=cache,
    )
    second = run_oracle(
        backend,
        [row],
        context_id="ctx:" + "f" * 64,
        context_fingerprint="f" * 64,
        cache_path=cache,
    )
    assert backend.calls == 1
    assert first[0].cache_hit is False
    assert second[0].cache_hit is True
    assert len(cache.read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads(cache.read_text(encoding="utf-8"))["boundary"] is not None


def test_oracle_hard_cap_is_500(tmp_path: Path) -> None:
    source = "theorem target : True := by trivial\n"
    rows = [SourceRow(str(index), 0, index, source + f"-- {index}\n", True) for index in range(501)]
    with pytest.raises(ValueError, match="capped at 500"):
        run_oracle(
            FakeBackend(source),
            rows,
            context_id="ctx:" + "f" * 64,
            context_fingerprint="f" * 64,
            cache_path=tmp_path / "cache.jsonl",
        )
