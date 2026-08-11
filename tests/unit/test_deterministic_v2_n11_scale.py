"""Pooled LF-034 N11 materialization stays ordered and provisional."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from leanfaith.lean.leaninteract_backend import BackendExecutionBinding, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.lean.session_policy import ServerMode
from leanfaith.representations import (
    NORMALIZATION_VERSION,
    TheoremForRepresentation,
    alpha_identity_fingerprint,
)
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import make_id
from leanfaith.schemas.theorem import RepresentationRecord
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_d0_runtime import build_v2_d0_runtime
from leanfaith.transforms.v2_d0_scale import (
    V2D0MaterializationInput,
    V2D0ScaleError,
    materialize_v2_d0_batch,
)
from tests.unit.test_deterministic_v2_n11 import _records, _root

_CONTEXT_ID = "ctx:" + "0" * 64
_SOURCE = "theorem {name} (x y : Nat) : x = y := by sorry"


def _source(name: str, *, context_id: str = _CONTEXT_ID) -> V2D0MaterializationInput:
    theorem, representation = _records(_SOURCE.format(name=name), name, _root(1, 0))
    theorem = theorem.model_copy(
        update={
            "context_id": context_id,
            "declaration_name": name,
            "declaration_full_name": name,
            "inline_elaboration_source": "import LeanFaithFixtures\n" + _SOURCE.format(name=name),
        }
    )
    representation = representation.model_copy(update={"context_id": context_id})
    return V2D0MaterializationInput(
        theorem=theorem,
        representation=representation,
        rule_id="n11_bound_variable_substitution",
        seed=7,
    )


class _BatchBackend:
    def __init__(
        self,
        statuses: Sequence[LeanStatus],
        *,
        drop_last: bool = False,
        workers: int = 1,
        memory_hard_limit_mb: int | None = None,
    ) -> None:
        self.statuses = tuple(statuses)
        self.drop_last = drop_last
        self.batches: list[tuple[LeanRequest, ...]] = []
        self.run_calls = 0
        self.execution_binding = BackendExecutionBinding(
            server_mode=ServerMode.POOL,
            workers=workers,
            memory_hard_limit_mb=memory_hard_limit_mb,
        )

    def run(self, request: LeanRequest) -> LeanResult:
        self.run_calls += 1
        raise AssertionError(f"sequential run used for {request.request_id}")

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        batch = tuple(requests)
        self.batches.append(batch)
        results = [
            LeanResult(
                request_id=request.request_id,
                request_hash=f"{index + 1:064x}",
                context_id=request.context_id,
                context_fingerprint=request.context_id.removeprefix("ctx:"),
                status=self.statuses[index],
            )
            for index, request in enumerate(batch)
        ]
        return results[:-1] if self.drop_last and results else results


def _install_candidate_representations(
    monkeypatch: pytest.MonkeyPatch,
    source_by_name: dict[str, RepresentationRecord],
) -> None:
    import leanfaith.transforms.v2_d0_scale as module

    def fake_build(
        backend: object,
        inputs: list[TheoremForRepresentation],
        **kwargs: object,
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        records: list[RepresentationRecord] = []
        for item in inputs:
            source = source_by_name[item.full_name]
            candidate_root = _root(0, 0) if " : y = y :=" in item.proof_stripped else _root(1, 1)
            candidate = source.model_copy(
                update={
                    "representation_id": make_id("repr", {"n11_scale_candidate": item.theorem_id}),
                    "theorem_id": item.theorem_id,
                    "normalization_version": NORMALIZATION_VERSION,
                    "raw_proof_stripped": item.proof_stripped,
                    "semantic_atoms": semantic_atoms(candidate_root),
                    "operator_tree": operator_tree(candidate_root),
                    "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
                    "content_hash": "0" * 64,
                }
            )
            records.append(
                candidate.model_copy(
                    update={"content_hash": _representation_payload_hash(candidate)}
                )
            )
        return records

    monkeypatch.setattr(module, "build_representations", fake_build)


def test_n11_scale_rejects_mixed_context_before_lean(tmp_path: Path) -> None:
    backend = _BatchBackend(())
    with pytest.raises(V2D0ScaleError, match="rejected before Lean execution"):
        materialize_v2_d0_batch(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_d0_runtime(),
            inputs=(_source("first"), _source("second", context_id="ctx:" + "1" * 64)),
            context_id=_CONTEXT_ID,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
        )
    assert backend.batches == []


def test_n11_scale_isolates_invalid_sibling_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = (_source("first"), _source("invalid"), _source("third"))
    backend = _BatchBackend(
        (LeanStatus.VALID_WITH_SORRY, LeanStatus.INVALID, LeanStatus.VALID_WITH_SORRY)
    )
    _install_candidate_representations(
        monkeypatch,
        {cast(str, item.theorem.declaration_full_name): item.representation for item in inputs},
    )
    results = materialize_v2_d0_batch(
        backend=cast(LeanInteractBackend, backend),
        runtime=build_v2_d0_runtime(),
        inputs=inputs,
        context_id=_CONTEXT_ID,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
    )
    assert [result.terminal_status for result in results] == [
        "provisional_variant",
        "candidate_invalid",
        "provisional_variant",
    ]
    assert len(backend.batches) == 1
    assert backend.run_calls == 0
    for result in results:
        assert result.resolved_label_count == 0
        assert result.promoted_item_count == 0
        assert result.training_eligible is False
        if result.variant is not None:
            assert result.variant.polarity_metadata.value == "negative"
            assert result.variant.metadata["evidence_class"] == "D0"
            assert result.variant.metadata["resolved_semantic_label"] is False
            assert result.variant.metadata["training_eligible"] is False


def test_n11_scale_classifies_lean_crash_as_infrastructure_error(tmp_path: Path) -> None:
    backend = _BatchBackend((LeanStatus.CRASH,))
    results = materialize_v2_d0_batch(
        backend=cast(LeanInteractBackend, backend),
        runtime=build_v2_d0_runtime(),
        inputs=(_source("crashed"),),
        context_id=_CONTEXT_ID,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
    )

    assert len(results) == 1
    assert results[0].terminal_status == "candidate_infrastructure_error"
    assert results[0].failure_codes == ("lean_crash",)
    assert results[0].variant is None


def test_n11_scale_batch_cardinality_mismatch_fails_closed(tmp_path: Path) -> None:
    backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,), drop_last=True)
    with pytest.raises(V2D0ScaleError, match="candidate Lean batch cardinality mismatch"):
        materialize_v2_d0_batch(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_d0_runtime(),
            inputs=(_source("only"),),
            context_id=_CONTEXT_ID,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
        )


def test_n11_empty_scale_batch_uses_no_lean(tmp_path: Path) -> None:
    backend = _BatchBackend(())
    assert (
        materialize_v2_d0_batch(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_d0_runtime(),
            inputs=(),
            context_id=_CONTEXT_ID,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
        )
        == ()
    )
    assert backend.batches == []
