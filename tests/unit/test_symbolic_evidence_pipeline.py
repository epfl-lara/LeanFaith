"""Focused fail-closed tests for the LF-020 symbolic-evidence pipeline."""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.evidence.certificates import ClaimAlignmentSpec
from leanfaith.evidence.config import load_evidence_configs
from leanfaith.evidence.pipeline import (
    EvidenceCollectorSettings,
    EvidencePipelineError,
    SymbolicEvidenceCollector,
)
from leanfaith.lean.axiom_audit import audit_certificate_messages
from leanfaith.lean.cache import EvidenceCache
from leanfaith.lean.commands import (
    EvidenceCommandError,
    PropositionPairSource,
    render_counterexample_check,
    render_directional_proof,
)
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult, LeanStatus
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    ViewStatus,
)
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import RepresentationRecord
from tests.unit.record_factories import (
    ANC_B,
    CTX_FINGERPRINT,
    CTX_ID,
    PAIR_ID,
    REPR_A,
    THM_A,
    THM_B,
    UTC_NOW,
    context_record,
    pair_record,
    representation_record,
    theorem_record,
)

_ROOT = find_repo_root(Path(__file__).parent)
_HASH = "a" * 64


class _ScriptedBackend(LeanBackend):
    """Minimal deterministic backend with certificate-shaped messages."""

    def __init__(self, *, preflight_status: LeanStatus = LeanStatus.VALID) -> None:
        self.preflight_status = preflight_status
        self.requests: list[LeanRequest] = []

    def run(self, request: LeanRequest) -> LeanResult:
        self.requests.append(request)
        stage = request.metadata.get("evidence_stage")
        status = LeanStatus.VALID
        messages: tuple[dict, ...] = ()
        if stage == "proposition_preflight":
            status = self.preflight_status
        elif stage == "counterexample":
            status = LeanStatus.INVALID
        elif stage == "directional_proof":
            certificate_matches = re.findall(r'lfProofAudit "([^"]+)"', request.code or "")
            assert certificate_matches
            certificate = certificate_matches[-1]
            messages = (
                {
                    "severity": "info",
                    "data": (
                        "LFAUDIT "
                        + json.dumps(
                            {
                                "name": certificate,
                                "direct_constants": [],
                                "transitive_constants": [],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                },
                {
                    "severity": "info",
                    "data": f"'{certificate}' does not depend on any axioms",
                },
            )
        return LeanResult(
            request_id=request.request_id,
            request_hash=sha256_hex((request.code or "").encode("utf-8")),
            context_id=request.context_id,
            context_fingerprint=CTX_FINGERPRINT,
            status=status,
            messages=messages,
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


def _representation(
    *,
    theorem_id: str,
    representation_id: str,
    signature: str,
    content_hash: str,
    alpha: str | None = _HASH,
    context_id: str = CTX_ID,
) -> RepresentationRecord:
    base = representation_record()
    status = dict(base.view_status)
    status["signature_explicit"] = ViewStatus.OK
    status["operator_tree"] = ViewStatus.OK
    payload = base.model_dump(mode="python")
    payload.update(
        {
            "theorem_id": theorem_id,
            "representation_id": representation_id,
            "context_id": context_id,
            "signature_explicit": signature,
            "operator_tree": {
                "atom_version": "atoms_v1",
                "root": {
                    "k": "forall",
                    "bi": "default",
                    "dom": {"k": "const", "n": "Nat", "us": "[]"},
                    "body": {"k": "const", "n": "True", "us": "[]"},
                },
            },
            "alpha_identity_fingerprint": alpha,
            "view_status": status,
            "content_hash": content_hash,
        }
    )
    return RepresentationRecord.model_validate(payload)


def _inputs() -> tuple:
    theorem_a = theorem_record(
        theorem_id=THM_A,
        declaration_name="source_a",
        declaration_full_name="Fixture.source_a",
        statement_content_hash="1" * 64,
    )
    theorem_b = theorem_record(
        theorem_id=THM_B,
        ancestry_id=ANC_B,
        root_ancestry_ids=(ANC_B,),
        declaration_name="source_b",
        declaration_full_name="Fixture.source_b",
        statement_content_hash="2" * 64,
    )
    representation_a = _representation(
        theorem_id=THM_A,
        representation_id=REPR_A,
        signature="∀ (x : Nat), x = x",
        content_hash="3" * 64,
    )
    representation_b = _representation(
        theorem_id=THM_B,
        representation_id=make_id("repr", {"theorem": THM_B, "version": "repr_v2"}),
        signature="∀ (y : Nat), y = y",
        content_hash="4" * 64,
    )
    context = context_record(
        header_text="import LeanFaithFixtures.Basic",
        imports=("LeanFaithFixtures.Basic",),
    )
    alignment = ClaimAlignmentSpec(
        pair_id=PAIR_ID,
        alignment_version="claim_alignment_v1",
        template_id="alpha_identity_assumption_v1",
        binder_map={"binder:0": "binder:0"},
        premise_map={},
        conclusion_role_map={"A": "B"},
        direction="both",
    )
    return (
        pair_record(),
        theorem_a,
        theorem_b,
        representation_a,
        representation_b,
        context,
        alignment,
    )


def _collector(
    tmp_path: Path,
    backend: LeanBackend,
) -> SymbolicEvidenceCollector:
    return SymbolicEvidenceCollector(
        backend=backend,
        configs=load_evidence_configs(RepoPaths(root=_ROOT)),
        cache=EvidenceCache(tmp_path / "cache", artifact_root=tmp_path),
        settings=EvidenceCollectorSettings(
            root=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            environment_hash="5" * 64,
            semantic_policy_version="semantic_policy_v1",
            semantic_policy_hash="6" * 64,
            created_at=UTC_NOW,
        ),
    )


def test_generated_commands_are_proof_free_and_kernel_checked() -> None:
    source = PropositionPairSource(
        header_text="import LeanFaithFixtures.Basic",
        proposition_a="True",
        proposition_b="False",
        pair_id=PAIR_ID,
        forbidden_declaration_constants=("lf_trivial",),
    )
    proof = render_directional_proof(
        source,
        direction="A_to_B",
        tactic_body="intro h\nexact True.intro",
        method_id="exact_assumption_v1",
    )
    counterexample = render_counterexample_check(source, direction="A_to_B")
    assert "lf_trivial" not in proof.code
    assert "allow_sorry" not in proof.code
    assert "lfProofAudit" in proof.code
    assert "#print axioms" in proof.code
    assert "native_decide" not in counterexample.code
    assert "\n  decide\n" in counterexample.code


def test_generated_universes_are_declared_once_and_method_id_is_validated() -> None:
    source = PropositionPairSource(
        header_text="",
        proposition_a="@Eq.{u_1} Nat",
        proposition_b="@Eq.{u_1, u_2} Nat",
        pair_id=PAIR_ID,
    )
    rendered = render_directional_proof(
        source,
        direction="A_to_B",
        tactic_body="intro h\nexact h",
        method_id="exact_assumption_v1",
    )
    assert rendered.code.count("universe u_1 u_2") == 1
    with pytest.raises(EvidenceCommandError, match="invalid proof method"):
        render_directional_proof(
            source,
            direction="A_to_B",
            tactic_body="intro h\nexact h",
            method_id="bad-id",
        )


def test_certificate_audit_rejects_transitive_source_dependency() -> None:
    messages = (
        {
            "severity": "info",
            "data": (
                'LFAUDIT {"name":"fresh","direct_constants":["helper"],'
                '"transitive_constants":["helper","Fixture.source_a"]}'
            ),
        },
        {
            "severity": "info",
            "data": "'fresh' does not depend on any axioms",
        },
    )
    audit = audit_certificate_messages(
        certificate_name="fresh",
        messages=messages,
        allowed_axioms=("Classical.choice",),
        forbidden_axioms=("sorryAx",),
        forbidden_constants=("Fixture.source_a",),
    )
    assert not audit.accepted
    assert audit.forbidden_constant_hits == ("Fixture.source_a",)
    assert "forbidden_constant:Fixture.source_a" in audit.violation_codes


def test_certificate_audit_fails_closed_on_missing_reports_or_sorry() -> None:
    missing = audit_certificate_messages(
        certificate_name="fresh",
        messages=(),
        allowed_axioms=(),
        forbidden_axioms=("sorryAx",),
        forbidden_constants=(),
    )
    assert not missing.accepted
    assert set(missing.violation_codes) == {
        "dependency_report_missing",
        "axiom_report_missing",
    }
    admission = audit_certificate_messages(
        certificate_name="fresh",
        messages=(
            {
                "severity": "info",
                "data": (
                    'LFAUDIT {"name":"fresh","direct_constants":[],'
                    '"transitive_constants":["sorryAx"]}'
                ),
            },
            {
                "severity": "info",
                "data": "'fresh' depends on axioms: [sorryAx]",
            },
        ),
        allowed_axioms=(),
        forbidden_axioms=("sorryAx",),
        forbidden_constants=(),
        has_sorries=True,
    )
    assert not admission.accepted
    assert "admission_detected" in admission.violation_codes


def test_certificate_audit_rejects_duplicate_forgeable_reports() -> None:
    dependency = {
        "severity": "info",
        "data": ('LFAUDIT {"name":"fresh","direct_constants":[],"transitive_constants":[]}'),
    }
    axiom = {
        "severity": "info",
        "data": "'fresh' does not depend on any axioms",
    }
    audit = audit_certificate_messages(
        certificate_name="fresh",
        messages=(dependency, dependency, axiom, axiom),
        allowed_axioms=(),
        forbidden_axioms=("sorryAx",),
        forbidden_constants=(),
    )
    assert not audit.accepted
    assert "dependency_report_missing" in audit.violation_codes
    assert "axiom_report_missing" in audit.violation_codes


def test_pipeline_collects_evidence_and_replays_all_five_cache_entries(
    tmp_path: Path,
) -> None:
    backend = _ScriptedBackend()
    collector = _collector(tmp_path, backend)
    pair, theorem_a, theorem_b, rep_a, rep_b, context, alignment = _inputs()

    # This focused contract test intentionally validates the private key builder
    # until the collection CLI exposes a public dry-run key API.
    keys = collector._keys(
        pair=pair,
        theorem_a=theorem_a,
        theorem_b=theorem_b,
        representation_a=rep_a,
        representation_b=rep_b,
        context=context,
        alignment=alignment,
    )
    assert keys["alignment"].evidence_direction == "equivalence_only"

    first = collector.collect_pair(
        pair=pair,
        theorem_a=theorem_a,
        theorem_b=theorem_b,
        representation_a=rep_a,
        representation_b=rep_b,
        context_a=context,
        context_b=context,
        alignment=alignment,
    )
    request_count = len(backend.requests)
    terminal = {
        record.kind: record for record in first.evidence if record.kind != EvidenceKind.AXIOM_AUDIT
    }
    assert first.cache_hits == 0
    assert first.cache_misses == 5
    assert terminal[EvidenceKind.DEFEQ].value.outcome == "equal"  # type: ignore[union-attr]
    assert (
        terminal[EvidenceKind.PROOF_A_IMPLIES_B].value.outcome == "proved"  # type: ignore[union-attr]
    )
    assert terminal[EvidenceKind.CLAIM_ALIGNMENT].value.outcome == "certified"  # type: ignore[union-attr]
    assert terminal[EvidenceKind.COUNTEREXAMPLE].value.outcome == "not_found"  # type: ignore[union-attr]
    assert set(first.pair.evidence_ids) == {record.evidence_id for record in first.evidence}

    second = collector.collect_pair(
        pair=pair,
        theorem_a=theorem_a,
        theorem_b=theorem_b,
        representation_a=rep_a,
        representation_b=rep_b,
        context_a=context,
        context_b=context,
        alignment=alignment,
    )
    assert second.cache_hits == 5
    assert second.cache_misses == 0
    assert len(backend.requests) == request_count
    assert second.evidence == first.evidence


def test_failed_preflight_is_persisted_and_cached(tmp_path: Path) -> None:
    backend = _ScriptedBackend(preflight_status=LeanStatus.INVALID)
    collector = _collector(tmp_path, backend)
    pair, theorem_a, theorem_b, rep_a, rep_b, context, alignment = _inputs()
    kwargs = {
        "pair": pair,
        "theorem_a": theorem_a,
        "theorem_b": theorem_b,
        "representation_a": rep_a,
        "representation_b": rep_b,
        "context_a": context,
        "context_b": context,
        "alignment": alignment,
    }
    first = collector.collect_pair(**kwargs)
    assert first.cache_misses == 5
    assert len(first.evidence) == 5
    assert {record.status for record in first.evidence} == {EvidenceExecutionStatus.ERROR}
    assert all(record.raw_artifact for record in first.evidence)
    request_count = len(backend.requests)
    second = collector.collect_pair(**kwargs)
    assert second.cache_hits == 5
    assert len(backend.requests) == request_count


def test_missing_alpha_fingerprint_is_unsupported_not_rejected(tmp_path: Path) -> None:
    backend = _ScriptedBackend()
    collector = _collector(tmp_path, backend)
    pair, theorem_a, theorem_b, rep_a, rep_b, context, alignment = _inputs()
    rep_b = _representation(
        theorem_id=THM_B,
        representation_id=rep_b.representation_id,
        signature="∀ (y : Nat), y = y",
        content_hash=rep_b.content_hash,
        alpha=None,
    )
    result = collector.collect_pair(
        pair=pair,
        theorem_a=theorem_a,
        theorem_b=theorem_b,
        representation_a=rep_a,
        representation_b=rep_b,
        context_a=context,
        context_b=context,
        alignment=alignment,
    )
    record = next(item for item in result.evidence if item.kind == EvidenceKind.CLAIM_ALIGNMENT)
    assert record.status == EvidenceExecutionStatus.UNSUPPORTED
    assert record.value is not None
    assert record.value.outcome == "unsupported"


def test_partial_identity_alignment_fails_closed_before_lean(tmp_path: Path) -> None:
    backend = _ScriptedBackend()
    collector = _collector(tmp_path, backend)
    pair, theorem_a, theorem_b, rep_a, rep_b, context, alignment = _inputs()
    partial = alignment.model_copy(update={"binder_map": {}})
    with pytest.raises(EvidencePipelineError, match="not total over leading foralls"):
        collector.collect_pair(
            pair=pair,
            theorem_a=theorem_a,
            theorem_b=theorem_b,
            representation_a=rep_a,
            representation_b=rep_b,
            context_a=context,
            context_b=context,
            alignment=partial,
        )
    assert backend.requests == []


def test_lineage_mismatch_fails_before_backend_execution(tmp_path: Path) -> None:
    backend = _ScriptedBackend()
    collector = _collector(tmp_path, backend)
    pair, theorem_a, theorem_b, rep_a, rep_b, context, alignment = _inputs()
    wrong_pair = pair.model_copy(update={"theorem_a_id": THM_B})
    with pytest.raises(EvidencePipelineError, match="pair_theorem_a_mismatch"):
        collector.collect_pair(
            pair=wrong_pair,
            theorem_a=theorem_a,
            theorem_b=theorem_b,
            representation_a=rep_a,
            representation_b=rep_b,
            context_a=context,
            context_b=context,
            alignment=alignment,
        )
    assert backend.requests == []


def test_settings_reject_non_utc_timestamp(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        EvidenceCollectorSettings(
            root=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            environment_hash="7" * 64,
            semantic_policy_version="semantic_policy_v1",
            semantic_policy_hash="8" * 64,
            created_at=datetime.datetime(2026, 7, 23),
        )
