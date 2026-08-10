"""First executable deterministic-v2 slice: conservative P11/P12 E0 rules."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanResult, LeanStatus
from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.enums import QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.positives.v2_e0 import (
    P11BoundedQuantifierRule,
    V2E0RuleError,
    apply_presentation_trace,
    enumerate_p11_sites,
    enumerate_p12_sites,
)
from leanfaith.transforms.v2_contract import load_v2_portfolio
from leanfaith.transforms.v2_e0_materializer import materialize_v2_e0_candidate
from leanfaith.transforms.v2_e0_runtime import (
    V2E0ExecutionConfig,
    V2E0ExecutionError,
    build_v2_e0_runtime,
    load_v2_e0_execution_config,
)
from tests.unit.record_factories import representation_record, theorem_record

_P11_SOURCE = "theorem bounded (s : List Nat) (P : Nat → Prop) : ∀ x ∈ s, P x := by sorry"
_P12_SOURCE = "theorem arrow (P Q : Prop) : P → Q := by sorry"


def _tree() -> dict[str, object]:
    return {
        "root": {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "sort", "u": "u.1"},
            "body": {"k": "const", "n": "Prop", "us": ["u.1"]},
        }
    }


def _source_records(source: str, key: str) -> tuple[TheoremRecord, RepresentationRecord]:
    tree = _tree()
    theorem = theorem_record(
        theorem_id=make_id("thm", {"v2_e0": key}),
        ancestry_id=make_id("anc", {"v2_e0": key}),
        root_ancestry_ids=(make_id("anc", {"v2_e0": key}),),
        declaration_name=key,
        declaration_full_name=key,
        proof_stripped_declaration=source,
        inline_elaboration_source="import LeanFaithFixtures\n" + source,
        statement_content_hash=__import__("hashlib").sha256(source.encode()).hexdigest(),
    )
    statuses = {
        name: (
            ViewStatus.OK
            if name
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
        for name in CANONICAL_VIEW_NAMES
    }
    representation = representation_record(
        representation_id=make_id("repr", {"v2_e0": key}),
        theorem_id=theorem.theorem_id,
        raw_proof_stripped=source,
        headless=source.split(":", 1)[-1].split(":=", 1)[0],
        signature_pp="fixture type",
        signature_explicit="fixture explicit type",
        semantic_atoms=("const:Membership.mem", "const:Prop"),
        operator_tree=tree,
        alpha_identity_fingerprint=alpha_identity_fingerprint(tree),
        view_status=statuses,
    )
    return theorem, representation


def _candidate_representation(
    candidate: TheoremRecord,
    source_representation: RepresentationRecord,
    *,
    alpha: str | None = None,
) -> RepresentationRecord:
    return source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": candidate.proof_stripped_declaration,
            "alpha_identity_fingerprint": (
                source_representation.alpha_identity_fingerprint if alpha is None else alpha
            ),
        }
    )


def test_v2_execution_profile_is_exact_and_additive() -> None:
    portfolio = load_v2_portfolio()
    loaded = load_v2_e0_execution_config()
    runtime = build_v2_e0_runtime()
    assert runtime.rule_ids == ("p11_bounded_quantifiers", "p12_proof_arrow_binder")
    assert loaded.config.portfolio_config_hash == portfolio.config_hash
    assert loaded.config.accepted_v1_effective_registry_hash == (
        portfolio.config.accepted_v1.effective_registry_hash
    )
    assert loaded.config.resolved_label_count == 0
    assert loaded.config.promoted_item_count == 0
    assert loaded.config.training_eligible is False
    with pytest.raises(V2E0ExecutionError, match="outside profile"):
        theorem, representation = _source_records(_P11_SOURCE, "outside")
        runtime.execute("p01_alpha", theorem, representation, 0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("training_eligible", True),
        ("resolved_label_count", 1),
        ("promoted_item_count", 1),
        ("active_rules", ()),
    ],
)
def test_v2_execution_profile_rejects_scope_or_semantic_credit_drift(
    field: str,
    value: object,
) -> None:
    payload = load_v2_e0_execution_config().config.model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError):
        V2E0ExecutionConfig.model_validate(payload)


@pytest.mark.parametrize(
    "source,operation",
    [
        (_P11_SOURCE, "bounded_to_explicit"),
        (
            "theorem e (s : List Nat) (P : Nat → Prop) : ∃ x ∈ s, P x := by sorry",
            "bounded_to_explicit",
        ),
        (
            "theorem c (s : List Nat) (P : Nat → Prop) : ∀ x, x ∈ s → P x := by sorry",
            "explicit_to_bounded",
        ),
    ],
)
def test_p11_sites_have_exact_forward_inverse(source: str, operation: str) -> None:
    site = enumerate_p11_sites(source)[0]
    assert site.operation == operation
    rule = P11BoundedQuantifierRule(
        generation_config_hash="a" * 64,
        candidate_pool="fixture",
    )
    theorem, representation = _source_records(source, operation)
    draft = rule.generate(theorem, representation, 9)[0]
    assert draft.candidate_code != source
    assert draft.inverse_trace is not None
    assert apply_presentation_trace(draft.candidate_code, draft.inverse_trace) == source


def test_p11_ignores_comment_text_and_rejects_unterminated_comments() -> None:
    source = "theorem t : True /- ∀ x ∈ s, P x -/ := by sorry"
    assert enumerate_p11_sites(source) == ()
    with pytest.raises(V2E0RuleError, match="unterminated_block_comment"):
        enumerate_p11_sites("theorem t : True /- bad := by sorry")


def test_p12_is_root_only_and_requires_an_unused_prop_binder() -> None:
    arrow = enumerate_p12_sites(_P12_SOURCE)
    assert len(arrow) == 1
    assert arrow[0].operation == "arrow_to_binder"
    reverse = enumerate_p12_sites("theorem reverse (P Q : Prop) : (_h : P) → Q := by sorry")
    assert len(reverse) == 1
    assert reverse[0].operation == "binder_to_arrow"
    assert enumerate_p12_sites("theorem used (P : Prop) : (h : P) → h = h := by sorry") == ()
    assert enumerate_p12_sites("theorem nested (P Q R : Prop) : P ∧ (Q → R) := by sorry") == ()
    assert enumerate_p12_sites("theorem data : Nat → Nat := by sorry") == ()


@pytest.mark.parametrize(
    ("rule_id", "source"),
    [
        ("p11_bounded_quantifiers", _P11_SOURCE),
        ("p12_proof_arrow_binder", _P12_SOURCE),
    ],
)
def test_clean_audit_is_provisional_and_alpha_mismatch_quarantines(
    rule_id: str,
    source: str,
) -> None:
    runtime = build_v2_e0_runtime()
    theorem, representation = _source_records(source, rule_id)
    execution = runtime.execute(rule_id, theorem, representation, 3)
    draft = execution.drafts[0]
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(theorem,),
        primary_source_id=theorem.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source=("import LeanFaithFixtures\n" + draft.candidate_code),
    )
    candidate_representation = _candidate_representation(candidate, representation)
    audit = runtime.audit(
        rule_id,
        theorem,
        representation,
        candidate,
        candidate_representation,
        draft,
    )
    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False

    mismatch = _candidate_representation(candidate, representation, alpha="f" * 64)
    rejected = runtime.audit(
        rule_id,
        theorem,
        representation,
        candidate,
        mismatch,
        draft,
    )
    assert "alpha_identity_fingerprint_mismatch" in rejected.violation_codes
    assert rejected.recommended_validation_status == ValidationStatus.QUARANTINED
    assert rejected.recommended_quality_tier == QualityTier.UNKNOWN

    tampered_trace = [dict(item) for item in draft.transformation_trace]
    tampered_trace[0]["replacement_text"] = "True"
    tampered = draft.model_copy(update={"transformation_trace": tuple(tampered_trace)})
    tampered_audit = runtime.audit(
        rule_id,
        theorem,
        representation,
        candidate,
        candidate_representation,
        tampered,
    )
    assert "draft_id_mismatch" in tampered_audit.violation_codes
    assert "site_contract_mismatch" in tampered_audit.violation_codes


class _ValidBackend:
    def run(self, request: object) -> LeanResult:
        return LeanResult(
            request_id=request.request_id,  # type: ignore[attr-defined]
            request_hash="d" * 64,
            context_id=request.context_id,  # type: ignore[attr-defined]
            context_fingerprint="0" * 64,
            status=LeanStatus.VALID_WITH_SORRY,
        )


class _InvalidBackend(_ValidBackend):
    def run(self, request: object) -> LeanResult:
        return LeanResult(
            request_id=request.request_id,  # type: ignore[attr-defined]
            request_hash="e" * 64,
            context_id=request.context_id,  # type: ignore[attr-defined]
            context_fingerprint="0" * 64,
            status=LeanStatus.INVALID,
        )


def test_materializer_emits_variant_only_after_clean_e0_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.v2_e0_materializer as module

    theorem, representation = _source_records(_P11_SOURCE, "materialize")

    def fake_representations(
        backend: object,
        inputs: list[object],
        **kwargs: object,
    ) -> tuple[RepresentationRecord, ...]:
        del backend, kwargs
        item = inputs[0]
        candidate = theorem.model_copy(
            update={
                "theorem_id": item.theorem_id,  # type: ignore[attr-defined]
                "proof_stripped_declaration": item.proof_stripped,  # type: ignore[attr-defined]
            }
        )
        return (_candidate_representation(candidate, representation),)

    monkeypatch.setattr(module, "build_representations", fake_representations)
    runtime = build_v2_e0_runtime()
    accepted = materialize_v2_e0_candidate(
        backend=cast(LeanInteractBackend, _ValidBackend()),
        runtime=runtime,
        theorem=theorem,
        representation=representation,
        rule_id="p11_bounded_quantifiers",
        seed=0,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
    )
    assert accepted.terminal_status == "provisional_variant"
    assert accepted.variant is not None
    assert accepted.variant.quality_tier == QualityTier.PROVISIONAL
    assert accepted.resolved_label_count == 0
    assert accepted.promoted_item_count == 0
    assert accepted.training_eligible is False

    invalid = materialize_v2_e0_candidate(
        backend=cast(LeanInteractBackend, _InvalidBackend()),
        runtime=runtime,
        theorem=theorem,
        representation=representation,
        rule_id="p11_bounded_quantifiers",
        seed=0,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
    )
    assert invalid.terminal_status == "candidate_invalid"
    assert invalid.variant is None
    assert invalid.resolved_label_count == 0
    assert invalid.training_eligible is False
