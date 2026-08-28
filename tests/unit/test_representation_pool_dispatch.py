"""Representation requests use the canonical batch boundary when available."""

from __future__ import annotations

import datetime

from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations import (
    RepresentationBatch,
    TheoremForRepresentation,
    build_representation_batch,
)
from leanfaith.schemas import make_id

_CONTEXT_ID = "ctx:" + "a" * 64
_CREATED_AT = datetime.datetime(2026, 8, 10, tzinfo=datetime.UTC)


class _BatchOnlyBackend:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def run(self, request: LeanRequest) -> LeanResult:
        raise AssertionError(f"sequential run used for {request.request_id}")

    def run_batch(self, requests: list[LeanRequest]) -> list[LeanResult]:
        self.batch_sizes.append(len(requests))
        return [
            LeanResult(
                request_id=request.request_id,
                request_hash=str(index) * 64,
                context_id=request.context_id,
                context_fingerprint="a" * 64,
                status=LeanStatus.VALID,
                messages=(
                    {
                        "severity": "info",
                        "data": (
                            f'LFSIGPPJSON {{"name":"fixture_{index}","signature_pp":"True"}}}}'
                        ),
                    },
                    {
                        "severity": "info",
                        "data": (
                            f'LFSIGEXPLICITJSON {{"name":"fixture_{index}",'
                            '"signature_explicit":"True"}}'
                        ),
                    },
                    {
                        "severity": "info",
                        "data": (
                            f'LFTREEJSON {{"name":"fixture_{index}",'
                            '"tree":{"k":"const","n":"True","us":"[]"}}'
                        ),
                    },
                ),
            )
            for index, request in enumerate(requests)
        ]


def test_representation_batch_dispatches_independent_requests_together() -> None:
    theorems = tuple(
        TheoremForRepresentation(
            theorem_id=make_id("thm", {"pool_dispatch": index}),
            full_name=f"fixture_{index}",
            proof_stripped=f"theorem fixture_{index} : True := by sorry",
            context_id=_CONTEXT_ID,
        )
        for index in range(2)
    )
    backend = _BatchOnlyBackend()

    result = build_representation_batch(
        backend,  # type: ignore[arg-type]
        RepresentationBatch(_CONTEXT_ID, "import Mathlib", theorems),
        created_at=_CREATED_AT,
    )

    assert backend.batch_sizes
    assert backend.batch_sizes[0] == 2
    assert all(size == 2 for size in backend.batch_sizes)
    assert len(result.ordered_representation_records) == 2
