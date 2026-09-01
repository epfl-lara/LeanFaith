from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import sha256_hex
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations.goal_v1 import (
    ClosedExprBatchResult,
    ClosedExprSidecar,
    CompileContext,
)
from leanfaith.sft2b import matched_pilot_lean_audit as audit
from leanfaith.sft2b.lean import PropositionEndpoint
from leanfaith.sft2b.pins import RuntimePins
from leanfaith.sft2b.schemas import (
    CompileContextRecord,
    CompileStatus,
    SourceProvenance,
    SourceRecord,
    stable_id,
)

_HASH = "1" * 64
_REVISION = "2" * 40


class _FakeBackend:
    def __init__(self, context: CompileContext) -> None:
        self.context = context

    def run(self, request: LeanRequest) -> LeanResult:
        return LeanResult(
            request_id=request.request_id,
            request_hash=_HASH,
            context_id=request.context_id,
            context_fingerprint=self.context.fingerprint,
            status=LeanStatus.VALID,
            elapsed_ms=7,
        )

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [self.run(request) for request in requests]

    def close(self) -> None:
        return None


class _Sidecar:
    def __init__(self, endpoint_id: str, goal: str) -> None:
        self.record = SimpleNamespace(endpoint_id=endpoint_id)
        self.goal = goal

    def to_dict(self) -> dict[str, object]:
        return {"record": {"endpoint_id": self.record.endpoint_id}, "goal": self.goal}

    def core_text(self) -> str:
        return self.goal


def _pins() -> RuntimePins:
    return RuntimePins(
        repr_freeze_commit=_REVISION,
        repr_spec_hash=_HASH,
        repr_implementation_set_hash=_HASH,
        repr_api_hash=_HASH,
        repr_config_file_hash=_HASH,
        lean_renderer_path="LeanFaith/Meta/GoalV1.lean",
        lean_renderer_hash=_HASH,
        injected_helper_hash=_HASH,
        python_module_path="src/leanfaith/representations/goal_v1.py",
        python_module_hash=_HASH,
        renderer_semantic_hash=_HASH,
        universe_profile_id="universe-v1",
        universe_profile_hash=_HASH,
        render_context_id="render-v1",
        render_context_hash=_HASH,
        coverage_receipt_hash=_HASH,
        sft2b_helper_path="src/leanfaith/sft2b/lean_helper.lean",
        sft2b_helper_hash=_HASH,
    )


def _source() -> tuple[SourceRecord, CompileContext]:
    context = CompileContext(
        project_id="mathlib",
        project_revision=_REVISION,
        lean_version="v4.31.0-rc1",
        import_header="import Mathlib\n",
        command_preamble="namespace LeanFaith.SFT2B.Helper\nend LeanFaith.SFT2B.Helper",
    )
    provenance = SourceProvenance(
        source_family="algebra",
        source_url="https://example.test/mathlib4",
        source_revision=_REVISION,
        source_path="Mathlib/Test.lean",
        source_file_sha256=_HASH,
        manifest_path="manifest.json",
        manifest_sha256=_HASH,
        source_recipe_sha256=_HASH,
        license_card_value="Apache-2.0",
        redistribution_note="test",
        nl_extraction_rule="test",
        trusted_reference_basis="test",
    )
    nl = "A proposition used by the bounded test."
    theorem_id = "thm:test"
    source_id = stable_id(
        "sft2b_source",
        {
            "reference_theorem_id": theorem_id,
            "nl_statement": nl,
            "source_revision": _REVISION,
        },
    )
    source = SourceRecord(
        source_id=source_id,
        nl_statement=nl,
        reference_theorem_id=theorem_id,
        reference_declaration_name="test",
        reference_proposition="True",
        reference_proposition_sha256=sha256_hex(b"True"),
        compile_context=CompileContextRecord(
            source_context_id=f"ctx:{'3' * 64}",
            render_compile_context_id=context.compile_context_id,
            project_id="mathlib",
            project_revision=_REVISION,
            project_path="/tmp/mathlib",
            lean_version="v4.31.0-rc1",
            import_header="import Mathlib\n",
            source_context_path="context.json",
            source_context_sha256=_HASH,
            helper_path="helper.lean",
            helper_sha256=_HASH,
        ),
        provenance=provenance,
        standalone_nl=True,
        trusted_reference=True,
        training_eligible=True,
    )
    return source, context


def _config(tmp_path: Path) -> audit.MatchedPilotLeanAuditConfig:
    bundle = {
        "repo_id": "private/test",
        "revision": _REVISION,
        "path_prefix": "pilot",
        "local_root": tmp_path,
        "files": {"sources.jsonl": _HASH, "candidates.jsonl": _HASH},
    }
    return audit.MatchedPilotLeanAuditConfig.model_validate(
        {
            "schema_version": audit.SCHEMA_VERSION,
            "owner_session": "test",
            "consumer_config_path": "consumer.json",
            "helper_path": "helper.lean",
            "mathlib_project_path": "/tmp/mathlib",
            "mathlib_named_reference_catalog_path": tmp_path / "named.jsonl",
            "mathlib_named_reference_catalog_sha256": _HASH,
            "explicit_reference_theorem_ids": [],
            "output_parent": tmp_path,
            "input_bundle": bundle,
            "output_bundle": bundle,
            "thresholds": {
                "expected_sources": 500,
                "expected_requests": 2000,
                "expected_candidates": 1242,
                "expected_unique_signatures": 1147,
                "expected_render_contexts": 35,
                "expected_source_contexts": 36,
                "minimum_valid_candidates": 500,
                "minimum_valid_candidate_fraction_of_admitted": 0.4,
                "minimum_valid_candidate_fraction_of_requests": 0.25,
                "minimum_sources_with_valid_candidate": 250,
                "maximum_infrastructure_failure_fraction": 0.02,
            },
            "lean_timeout_seconds": 30.0,
            "maximum_infrastructure_attempts": 3,
            "claimed_lean_rss_gib": 4.0,
        }
    )


def test_config_refuses_weaker_sprint_threshold(tmp_path: Path) -> None:
    payload = _config(tmp_path).model_dump(mode="json")
    payload["thresholds"]["minimum_valid_candidate_fraction_of_admitted"] = 0.39
    with pytest.raises(ValidationError):
        audit.MatchedPilotLeanAuditConfig.model_validate(payload)


def test_inline_compile_context_replays_frozen_render_identity() -> None:
    source, expected = _source()
    observed = audit._compile_context(source, helper_body=expected.command_preamble)
    assert observed == expected
    assert observed.compile_context_id == source.compile_context.render_compile_context_id


def test_audit_level_names_include_explicit_constant_universe() -> None:
    assert audit._audit_level_names("CommRingCat.{u_1}") == ("u_1",)


def test_source_material_accepts_content_pinned_snapshot_symlink(tmp_path: Path) -> None:
    source, _ = _source()
    blob = tmp_path / "blob"
    blob.write_bytes(b"content-addressed source bytes")
    snapshot = tmp_path / "snapshot.parquet"
    snapshot.symlink_to(blob)
    compile_context = source.compile_context.model_copy(
        update={
            "source_context_path": str(snapshot),
            "source_context_sha256": sha256_hex(blob.read_bytes()),
        }
    )
    source = source.model_copy(update={"compile_context": compile_context})
    observed = audit._verify_source_material((source,), tmp_path)
    assert observed[str(snapshot)] == sha256_hex(blob.read_bytes())


def test_reference_inputs_require_family_specific_frozen_catalogs(tmp_path: Path) -> None:
    algebra, _ = _source()
    cross_theorem_id = "thm:cross"
    cross = algebra.model_copy(
        update={
            "source_id": stable_id(
                "sft2b_source",
                {
                    "reference_theorem_id": cross_theorem_id,
                    "nl_statement": algebra.nl_statement,
                    "source_revision": _REVISION,
                },
            ),
            "reference_theorem_id": cross_theorem_id,
            "reference_declaration_name": "Cross.test",
            "provenance": algebra.provenance.model_copy(update={"source_family": "cross_domain"}),
        }
    )
    algebra_catalog = tmp_path / "algebra.jsonl"
    cross_catalog = tmp_path / "cross.jsonl"
    algebra_catalog.write_text(
        json.dumps(
            {
                "theorem_id": algebra.reference_theorem_id,
                "signature_pp": "True",
                "signature_explicit": "(True)",
                "raw_proof_stripped": "theorem test : True := by trivial",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    cross_catalog.write_text(
        json.dumps(
            {
                "representation": {
                    "theorem_id": cross.reference_theorem_id,
                    "signature_pp": "True",
                    "signature_explicit": "⋯",
                    "raw_proof_stripped": "theorem Cross.test : True := by trivial",
                },
                "theorem": {
                    "theorem_id": cross.reference_theorem_id,
                    "declaration_full_name": "Cross.test",
                    "source_file": cross.provenance.source_path,
                    "source_revision": _REVISION,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    named_catalog = tmp_path / "named.jsonl"
    named_catalog.write_text(
        json.dumps(
            {
                "theorem": {
                    "theorem_id": algebra.reference_theorem_id,
                    "declaration_full_name": "Namespace.test",
                    "source_file": algebra.provenance.source_path,
                    "source_revision": _REVISION,
                }
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "source_catalogs": {
            "mathlib_docstrings": {
                "reference_catalog_path": str(algebra_catalog),
                "reference_catalog_sha256": audit.hash_file(algebra_catalog),
                "cross_domain_catalog_path": str(cross_catalog),
                "cross_domain_catalog_sha256": audit.hash_file(cross_catalog),
            }
        }
    }

    observed, receipt = audit._reference_elaboration_inputs(
        (algebra, cross),
        ("library_docstring", "library_docstring"),
        manifest,
        named_catalog_path=named_catalog,
        named_catalog_sha256=audit.hash_file(named_catalog),
        explicit_theorem_ids=frozenset({algebra.reference_theorem_id}),
    )

    assert observed[algebra.source_id] == audit.ReferenceElaborationInput(
        method="frozen_reference_signature_explicit",
        carrier="(True)",
        raw_statement="theorem test : True := by trivial",
    )
    assert observed[cross.source_id] == audit.ReferenceElaborationInput(
        method="frozen_reference_constant_type",
        carrier="Cross.test",
        raw_statement="theorem Cross.test : True := by trivial",
    )
    assert receipt["selected_mathlib_rows"] == 2
    assert receipt["catalogs"] == {
        "algebra": {
            "path": str(algebra_catalog),
            "sha256": audit.hash_file(algebra_catalog),
            "selected_rows": 1,
        },
        "cross_domain": {
            "path": str(cross_catalog),
            "sha256": audit.hash_file(cross_catalog),
            "selected_rows": 1,
        },
    }
    assert receipt["named_reference_catalog"] == {
        "path": str(named_catalog),
        "sha256": audit.hash_file(named_catalog),
        "selected_rows": 1,
    }


def test_reference_only_source_uses_probe_but_emits_no_candidate_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = _source()
    capturing = audit._CapturingBackend(_FakeBackend(context))

    def fake_render(
        backend: audit._CapturingBackend,
        *,
        endpoints: Sequence[PropositionEndpoint],
        compile_context: CompileContext,
        render_scope_id: str,
        request_id: str,
        timeout_seconds: float,
        reference_constant_name: str | None = None,
        reference_raw_statement: str | None = None,
    ) -> ClosedExprBatchResult:
        assert reference_constant_name is None
        assert reference_raw_statement == source.reference_proposition
        assert len(endpoints) == 2
        assert endpoints[1].proposition == "True"
        request = LeanRequest(
            request_id=request_id,
            context_id=compile_context.compile_context_id,
            code="import Mathlib\n#check True",
            timeout_seconds=timeout_seconds,
        )
        result = backend.run(request)
        return ClosedExprBatchResult(
            sidecars=cast(
                tuple[ClosedExprSidecar, ...],
                (
                    _Sidecar("reference", "⊢ True"),
                    _Sidecar(endpoints[1].endpoint_id, "⊢ True"),
                ),
            ),
            failures=(),
            request_hash=result.request_hash,
            elapsed_ms=result.elapsed_ms,
            raw_response_path=None,
            render_scope_id=render_scope_id,
        )

    monkeypatch.setattr(audit, "_render_propositions_isolated", fake_render)
    terminal, request_count = audit._execute_source(
        backend=capturing,
        source=source,
        source_ordinal=0,
        source_class="library_docstring",
        reference_input=audit.ReferenceElaborationInput(
            method="source_signature_pp",
            carrier=source.reference_proposition,
            raw_statement=source.reference_proposition,
        ),
        candidates=(),
        context=context,
        pins=_pins(),
        config=_config(tmp_path),
        run_id=stable_id("sft2b_matched_lean_audit", {"test": True}),
        run_root=tmp_path,
    )
    assert terminal is not None
    assert request_count == 1
    assert terminal.reference.status.value == "valid"
    assert terminal.candidate_ids == ()
    assert terminal.candidates == ()


def test_isolated_tolerant_body_rolls_back_diagnostics_and_rethrows_interrupts() -> None:
    source, _ = _source()
    endpoints = audit._endpoints(source, ())
    body = audit._build_isolated_tolerant_session_body(endpoints, render_scope_id="scope:test")
    assert "Lean.Core.resetMessageLog" in body
    assert "Lean.restoreState saved" in body
    assert "Lean.Elab.Term.restoreState" not in body
    assert "Lean.Core.setMessageLog" in body
    assert "if ex.isInterrupt || ex.isRuntime then" in body
    assert "throw ex" in body


def test_named_reference_body_loads_theorem_type_without_text_elaboration() -> None:
    source, _ = _source()
    endpoints = audit._endpoints(source, ())
    body = audit._build_isolated_tolerant_session_body(
        endpoints,
        render_scope_id="scope:test",
        reference_constant_name="Namespace.test",
    )
    prefix = body.split("let endpoint1?", maxsplit=1)[0]
    assert '(← Lean.getEnv).find? "Namespace.test".toName' in prefix
    assert "some (.thmInfo info)" in prefix
    assert '"loaded_constant_type" endpoint0' in body
    assert "elaborateProposition" not in prefix


def test_valid_closed_prop_with_forbidden_render_is_repr_invalid() -> None:
    source, context = _source()
    candidate_id = stable_id("sft2b_candidate", {"repr": "invalid"})
    endpoint = PropositionEndpoint(
        endpoint_id=candidate_id,
        endpoint_role="candidate",
        proposition="True",
        source_id=source.source_id,
        candidate_id=candidate_id,
    )

    observed = audit._valid_candidate_record_or_repr_failure(
        endpoint=endpoint,
        source=source,
        context=context,
        pins=_pins(),
        sidecar=_Sidecar(candidate_id, "⊢ M ⋯ x"),
    )

    assert observed.status == CompileStatus.INVALID
    assert observed.error_class == "candidate_repr_invalid"
    assert observed.goal_v1 is None


def test_terminal_rejects_candidate_order_mismatch() -> None:
    source, context = _source()
    endpoint = PropositionEndpoint(
        endpoint_id=stable_id("sft2b_candidate", {"candidate": 1}),
        endpoint_role="candidate",
        proposition="True",
        source_id=source.source_id,
        candidate_id=stable_id("sft2b_candidate", {"candidate": 1}),
    )
    candidate = audit._endpoint_record(
        endpoint=endpoint,
        source=source,
        context=context,
        pins=_pins(),
        status=CompileStatus.VALID,
        sidecar=_Sidecar(endpoint.endpoint_id, "⊢ True"),
    )
    reference = audit._endpoint_record(
        endpoint=audit._endpoints(source, ())[0],
        source=source,
        context=context,
        pins=_pins(),
        status=CompileStatus.VALID,
        sidecar=_Sidecar("reference", "⊢ True"),
    )
    with pytest.raises(ValidationError):
        audit.SourceAuditTerminal(
            schema_version="sft2b_matched_pilot_source_terminal_v1",
            run_id=stable_id("sft2b_matched_lean_audit", {"test": True}),
            source_id=source.source_id,
            source_ordinal=0,
            source_class="library_docstring",
            source_context_id=source.compile_context.source_context_id,
            render_compile_context_id=context.compile_context_id,
            reference_elaboration_method="source_signature_pp",
            reference_elaboration_sha256=source.reference_proposition_sha256,
            candidate_ids=(),
            reference=reference,
            candidates=(candidate,),
            request_hash=_HASH,
            request_status="valid",
            elapsed_ms=1,
            infrastructure_attempts=0,
            backend_method_version="test",
            peak_rss_bytes=1,
        )
