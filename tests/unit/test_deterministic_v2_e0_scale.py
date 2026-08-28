"""Pooled deterministic-v2 E0 materialization stays ordered and provisional."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations import TheoremForRepresentation, alpha_identity_fingerprint
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.theorem import RepresentationRecord
from leanfaith.transforms.v2_e0_runtime import build_v2_e0_runtime
from leanfaith.transforms.v2_e0_scale import (
    V2E0MaterializationInput,
    V2E0ScaleError,
    materialize_v2_e0_batch,
)
from tests.unit.record_factories import representation_record, theorem_record

_CONTEXT_ID = "ctx:" + "0" * 64
_P11 = "theorem {name} (s : List Nat) (P : Nat → Prop) : ∀ x ∈ s, P x := by sorry"


def _tree() -> dict[str, object]:
    return {
        "root": {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "sort", "u": "u.1"},
            "body": {"k": "const", "n": "Prop", "us": ["u.1"]},
        }
    }


def _source(name: str, *, context_id: str = _CONTEXT_ID) -> V2E0MaterializationInput:
    code = _P11.format(name=name)
    ancestry = make_id("anc", {"v2_e0_scale": name})
    theorem = theorem_record(
        theorem_id=make_id("thm", {"v2_e0_scale": name}),
        ancestry_id=ancestry,
        root_ancestry_ids=(ancestry,),
        context_id=context_id,
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=code,
        inline_elaboration_source="import LeanFaithFixtures\n" + code,
        statement_content_hash=hashlib.sha256(code.encode()).hexdigest(),
    )
    tree = _tree()
    statuses = {
        view: (
            ViewStatus.OK
            if view
            in {
                "raw_proof_stripped",
                "headless",
                "signature_pp",
                "signature_explicit",
                "semantic_atoms",
                "operator_tree",
            }
            else ViewStatus.NOT_ATTEMPTED
        )
        for view in CANONICAL_VIEW_NAMES
    }
    representation = representation_record(
        representation_id=make_id("repr", {"v2_e0_scale": name}),
        theorem_id=theorem.theorem_id,
        context_id=context_id,
        raw_proof_stripped=code,
        headless="fixture headless",
        signature_pp="fixture type",
        signature_explicit="fixture explicit type",
        semantic_atoms=("const:Membership.mem", "const:Prop"),
        operator_tree=tree,
        alpha_identity_fingerprint=alpha_identity_fingerprint(tree),
        view_status=statuses,
    )
    return V2E0MaterializationInput(
        theorem=theorem,
        representation=representation,
        rule_id="p11_bounded_quantifiers",
        seed=0,
    )


class _BatchBackend:
    def __init__(self, statuses: Sequence[LeanStatus], *, drop_last: bool = False) -> None:
        self.statuses = tuple(statuses)
        self.drop_last = drop_last
        self.batches: list[tuple[LeanRequest, ...]] = []
        self.run_calls = 0

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


def _install_representation_batch(
    monkeypatch: pytest.MonkeyPatch,
    source_by_name: dict[str, RepresentationRecord],
    observed_names: list[str],
) -> None:
    import leanfaith.transforms.v2_e0_scale as module

    def fake_build(
        backend: object,
        inputs: list[TheoremForRepresentation],
        **kwargs: object,
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        records: list[RepresentationRecord] = []
        for item in inputs:
            name = item.full_name
            observed_names.append(name)
            source = source_by_name[name]
            records.append(
                source.model_copy(
                    update={
                        "representation_id": make_id(
                            "repr", {"v2_e0_scale_candidate": item.theorem_id}
                        ),
                        "theorem_id": item.theorem_id,
                        "raw_proof_stripped": item.proof_stripped,
                    }
                )
            )
        return records

    monkeypatch.setattr(module, "build_representations", fake_build)


def test_mixed_context_fails_before_rule_execution_or_lean(tmp_path: Path) -> None:
    backend = _BatchBackend(())
    inputs = (_source("first"), _source("second", context_id="ctx:" + "1" * 64))

    with pytest.raises(V2E0ScaleError, match="rejected before Lean execution"):
        materialize_v2_e0_batch(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_e0_runtime(),
            inputs=inputs,
            context_id=_CONTEXT_ID,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
        )

    assert backend.batches == []
    assert backend.run_calls == 0


def test_candidate_batch_cardinality_mismatch_fails_closed(tmp_path: Path) -> None:
    backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,), drop_last=True)

    with pytest.raises(V2E0ScaleError, match="candidate Lean batch cardinality mismatch"):
        materialize_v2_e0_batch(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_e0_runtime(),
            inputs=(_source("only"),),
            context_id=_CONTEXT_ID,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
        )

    assert len(backend.batches) == 1
    assert backend.run_calls == 0


def test_representation_batch_cardinality_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.v2_e0_scale as module

    backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,))
    monkeypatch.setattr(module, "build_representations", lambda *args, **kwargs: [])

    with pytest.raises(V2E0ScaleError, match="representation batch cardinality mismatch"):
        materialize_v2_e0_batch(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_e0_runtime(),
            inputs=(_source("only"),),
            context_id=_CONTEXT_ID,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
        )


def test_invalid_sibling_is_isolated_and_input_order_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    inputs = (_source("first"), _source("invalid"), _source("third"))
    backend = _BatchBackend(
        (LeanStatus.VALID_WITH_SORRY, LeanStatus.INVALID, LeanStatus.VALID_WITH_SORRY)
    )
    observed_names: list[str] = []
    _install_representation_batch(
        monkeypatch,
        {item.theorem.declaration_full_name: item.representation for item in inputs},  # type: ignore[misc]
        observed_names,
    )

    results = materialize_v2_e0_batch(
        backend=cast(LeanInteractBackend, backend),
        runtime=build_v2_e0_runtime(),
        inputs=inputs,
        context_id=_CONTEXT_ID,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
    )

    assert len(results) == len(inputs)
    assert [result.terminal_status for result in results] == [
        "provisional_variant",
        "candidate_invalid",
        "provisional_variant",
    ]
    assert observed_names == ["first", "third"]
    assert results[0].candidate_theorem is not None
    assert results[0].candidate_theorem.declaration_full_name == "first"
    assert results[1].candidate_theorem is None
    assert results[2].candidate_theorem is not None
    assert results[2].candidate_theorem.declaration_full_name == "third"
    assert len(backend.batches) == 1
    assert len(backend.batches[0]) == 3
    assert backend.run_calls == 0

    for result in results:
        assert result.resolved_label_count == 0
        assert result.promoted_item_count == 0
        assert result.training_eligible is False
        if result.variant is not None:
            assert result.variant.metadata["resolved_semantic_label"] is False
            assert result.variant.metadata["training_eligible"] is False


def test_empty_batch_returns_empty_without_lean(tmp_path: Path) -> None:
    backend = _BatchBackend(())

    assert (
        materialize_v2_e0_batch(
            backend=cast(LeanInteractBackend, backend),
            runtime=build_v2_e0_runtime(),
            inputs=(),
            context_id=_CONTEXT_ID,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
        )
        == ()
    )
    assert backend.batches == []
