"""Self-contained regression for the invalid 2026-07-23 Kimina smoke bundle.

The original run directory is intentionally *not* a test input: ``runs/`` is
ignored and is absent from clean checkouts.  These fixtures preserve only the
typed records needed to reproduce the historical lineage defect.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.generation.local_qualification import (
    LocalMetadataHashes,
    LocalQualificationBundleManifest,
    LocalQualificationReplayError,
    LocalQualificationTerminal,
    QualificationInputBinding,
    RuntimeEnvironmentBinding,
    verify_local_qualification_bundle,
)
from leanfaith.generation.real_outputs import (
    CandidateScreeningRecord,
    RealOutputCandidateOutcome,
)
from leanfaith.schemas.enums import (
    LLMAttemptStatus,
    LLMRole,
    ParseStatus,
)
from leanfaith.schemas.llm import (
    LLMAttemptRecord,
    LLMCallRecord,
    make_llm_attempt_id,
)
from leanfaith.schemas.nl_lean import ProblemPoolRecord

UTC = datetime.datetime(2026, 7, 23, 21, 16, 43, 991026, tzinfo=datetime.UTC)
HEX = "0" * 64
HISTORICAL_RUN_NAME = "kimina_20260723T211643Z"

HISTORICAL_TERMINAL = {
    "admission_at": "2026-07-23T21:16:45.991026Z",
    "admitted_nl_lean_id": (
        "nllean:d060e5c4a98f63855404c9d9ef6552e2d17bc1dc0544f0a163e78e9d0057a531"
    ),
    "admitted_pair_ids": ("pair:19ce17dd1642c7978940ab309b370eb161f27bffa4060bc5cafc6c64fb1edbf3",),
    "artifact_class": "smoke",
    "candidate_theorem_id": (
        "thm:16863857d0529e93f2a87de454e7d69af9caea7f326e002da642d1b46f87a586"
    ),
    "created_at": "2026-07-23T21:16:43.991026Z",
    "error_code": None,
    "error_detail": None,
    "formatted_prompt_hash": ("d637b9f8a0f0fa43f66d4adf7058c29ba0c9c7d02a794a51084eacf25d635bef"),
    "generation_config_hash": ("ea5a31ccfb71341182b4decd4c6cbd0c16a8e245809444dd9f909229387c2600"),
    "llm_attempt_id": (
        "call_attempt:fbfed13d81eb05c95a60abfaca974ec9279bce1b1344525f5944a38bc9d7ee78"
    ),
    "llm_call_id": "call:798057cf7c1eb533e58b3f3678f8517d075cce94b4b00d498c56a8ac824f37e7",
    "materialization_outcome_id": (
        "real_output:3c8f48517d41e5b58f4eff3bc63ee26a81fae081fc863ea2c6fd12741f5fd061"
    ),
    "model": "AI-MO/Kimina-Autoformalizer-7B",
    "model_family": "kimina_autoformalizer_7b",
    "model_revision": "ddd47cb477d93b3ca990468e1c0d5ad6b60973dd",
    "parsed_statement_sha256": ("8d8e2a6c4014db90885b1bce9947d96aa660d5772cd5ef4503948a7d0688d2b5"),
    "parser_id": "lean_final_fence_or_raw_signature_v2",
    "parser_source_sha256": ("ec5fb8abb7903224fe062218cc0b810a16c3826f6adaf4ffc7291d7bc0f4aa74"),
    "problem_record_id": (
        "problem:77d5e9093e75fdb8290dcad8e61486840f4dfac1d32847dee42a21d48310c513"
    ),
    "prompt_render_hash": ("7b450ba0528b2f3b3bc08d114962dadf1963557d73be7e4616848b839821f34e"),
    "prompt_template_hash": ("355a308482842a4b458a51b2b566a1192e86e72d752e25e097c93534028e75ac"),
    "provider_request_artifact": f"runs/lf021_local_qualification/{HISTORICAL_RUN_NAME}/request",
    "provider_request_artifact_sha256": (
        "747aca30856d57ff4a5bb0939d4e15a4632b7c7c82cebba3bc0b18336a1f90f0"
    ),
    "provider_request_hash": ("e4242b988988f88ea883b1c6f4ced825f8a32ed4ec8c63e28af8aad59c448054"),
    "qualification_config_hash": (
        "d52fb9a6edb43b52e226efc5fb72c0e5c115f0dc66195e011fe390b3d8ec879d"
    ),
    "qualifies_for_gate5g": False,
    "raw_response_artifact": f"runs/lf021_local_qualification/{HISTORICAL_RUN_NAME}/raw",
    "raw_response_sha256": ("404fdb8f19cbee1e49eb128e32bbf405fbd37fc211b03b4887e26c6ebd5e3da6"),
    "representation_id": ("repr:bd44d4daa021656e4e5a48262d51841ec86003f1dcf2cb5ac597abb400d60be5"),
    "runtime_hash": "10193edf20da11f84b840dec313ea5bcf3d1edc68483a954fd26130c3a0636ef",
    "schema_version": 1,
    "screening_at": "2026-07-23T21:16:44.991026Z",
    "screening_id": (
        "candidate_screen:4401e3cb1f07c9cc76a0a0924bc7c04e7848c4bdc2c56e7404f46dd875fd442a"
    ),
    "semantic_labels_created": False,
    "status": "qualified_smoke",
    "supervision_eligible": False,
    "terminal_id": (
        "local_qualification:9bc1f768e86196314e4d20e2480a58ead94117e967dad405d878bdd687d19cc3"
    ),
}

HISTORICAL_PENDING_OUTCOME = {
    "call_id": HISTORICAL_TERMINAL["llm_call_id"],
    "candidate_theorem_id": HISTORICAL_TERMINAL["candidate_theorem_id"],
    "created_at": HISTORICAL_TERMINAL["created_at"],
    "declaration_name": "lf021_kimina_generated_nat_comm_20260723",
    "failure_code": None,
    "failure_detail": None,
    "generation_config_hash": HISTORICAL_TERMINAL["generation_config_hash"],
    "nl_lean_id": None,
    "outcome": "materialized_pending_screening",
    "outcome_id": ("real_output:615d4cafb14a539f0da7a445891a58abe8fc20d52fe99d38a7d74f051cbbdff4"),
    "pair_ids": (),
    "parsed_statement_sha256": HISTORICAL_TERMINAL["parsed_statement_sha256"],
    "problem_record_id": HISTORICAL_TERMINAL["problem_record_id"],
    "raw_output_artifact": HISTORICAL_TERMINAL["raw_response_artifact"],
    "representation_id": HISTORICAL_TERMINAL["representation_id"],
    "schema_version": 2,
    "screening_id": None,
    "semantic_pool_eligible": False,
    "validation_status": "elaborates_with_placeholder",
    "variant_id": "var:97ce842bbbc4b9426a52090fe732a84f8c88fbe363e387a0db6902cbdf212ce5",
}

HISTORICAL_SCREENING = {
    "alpha_identity_fingerprint": (
        "b167728ff3a6aff112ab14d4d9840fedb4736606caf46c62607e1dc1a66c07a1"
    ),
    "benchmark_hits": (),
    "call_id": HISTORICAL_TERMINAL["llm_call_id"],
    "candidate_theorem_id": HISTORICAL_TERMINAL["candidate_theorem_id"],
    "canonical_candidate_theorem_id": HISTORICAL_TERMINAL["candidate_theorem_id"],
    "created_at": HISTORICAL_TERMINAL["screening_at"],
    "duplicate_candidate_theorem_ids": (),
    "frozen_registry_hash": ("eac1ab353cfc0108068ff3d6035281f08b52dc97d3b5cb536dd1781e49d05ca3"),
    "headless_sha256": "0234b37be29ef59cbde8f8df9a7daa076f9481a92abdf52b675e841deb1e5c71",
    "is_canonical": True,
    "problem_record_id": HISTORICAL_TERMINAL["problem_record_id"],
    "raw_proof_stripped_sha256": (
        "9e79f3c68314d9d3c2835fa24e51fd08777506c5e3eda34451581948b027be0a"
    ),
    "representation_content_hash": (
        "a5ac308fd202d8f558987d1a074f3072f101ffd139f5ff20a6178b8fe8f8defa"
    ),
    "representation_id": HISTORICAL_TERMINAL["representation_id"],
    "schema_version": 1,
    "screening_id": HISTORICAL_TERMINAL["screening_id"],
    "signature_pp_sha256": ("015e28131ffa8598fe12536e31746e509974c38410fdb2dab25b6d0e29ca37e3"),
    "status": "clean",
    "theorem_statement_content_hash": (
        "0234b37be29ef59cbde8f8df9a7daa076f9481a92abdf52b675e841deb1e5c71"
    ),
}


def _historical_invalid_bundle(
    tmp_path: Path,
) -> tuple[
    LocalQualificationBundleManifest,
    LocalQualificationTerminal,
    RealOutputCandidateOutcome,
    CandidateScreeningRecord,
]:
    """Synthesize the historical bad lineage without reading ignored run bytes."""

    terminal = LocalQualificationTerminal.model_validate(HISTORICAL_TERMINAL)
    outcome = RealOutputCandidateOutcome.model_validate(HISTORICAL_PENDING_OUTCOME)
    screening = CandidateScreeningRecord.model_validate(HISTORICAL_SCREENING)
    call = LLMCallRecord(
        call_id=terminal.llm_call_id,
        provider="local_hf",
        model=terminal.model,
        model_family=terminal.model_family,
        role=LLMRole.AUTOFORMALIZER,
        request_date=UTC,
        prompt_template_hash=terminal.prompt_template_hash,
        prompt_render_hash=terminal.prompt_render_hash,
        input_ids=(terminal.problem_record_id,),
        parsed_output={"lean_statement": "theorem historical_invalid : True"},
        parse_status=ParseStatus.PARSED,
        supervision_eligible=False,
        private_source_content=False,
        denylist_checked=True,
    )
    attempt = LLMAttemptRecord(
        attempt_id=make_llm_attempt_id(call.call_id, 0),
        call_id=call.call_id,
        attempt_index=0,
        execution_mode="local",
        started_at=UTC,
        completed_at=UTC,
        request_artifact="synthetic/request.json",
        raw_response_artifact="synthetic/raw.json",
        status=LLMAttemptStatus.RESPONSE_RECEIVED,
        retryable=False,
        latency_ms=0,
    )
    # The replay must reject the cross-record mismatch before consulting any
    # mutable repository input.  A valid schema-v1 binding makes that ordering
    # explicit without reintroducing a hidden runs/ dependency.
    inputs = QualificationInputBinding(
        schema_version=1,
        qualification_config_artifact="synthetic/config.yaml",
        qualification_config_file_sha256=HEX,
        qualification_config_hash=terminal.qualification_config_hash,
        runtime=RuntimeEnvironmentBinding(
            environment_lock_artifact="synthetic/lock",
            environment_lock_sha256=HEX,
            python_version="test",
            torch_version="test",
            transformers_version="test",
            driver_version="test",
            device_name="test",
            dtype="auto",
            runtime_adapter_artifact="synthetic/runtime.py",
            runtime_adapter_sha256=HEX,
        ),
        prompt_template_artifact="synthetic/prompt.txt",
        prompt_template_sha256=terminal.prompt_template_hash,
        common_suffix_artifact="synthetic/suffix.txt",
        common_suffix_sha256=HEX,
        parser_id=terminal.parser_id,
        parser_source_artifact="synthetic/parser.py",
        parser_source_sha256=terminal.parser_source_sha256,
        model_repo_id=terminal.model,
        model_revision=terminal.model_revision,
        tokenizer_revision=terminal.model_revision,
        model_metadata_hashes=LocalMetadataHashes(
            readme=HEX,
            config=HEX,
            tokenizer_config=HEX,
            generation_config=HEX,
        ),
    )
    records = {
        "attempt": attempt,
        "call": call,
        "materialization_outcome": outcome,
        "qualification_inputs": inputs,
        "screening": screening,
        "terminal": terminal,
    }
    artifacts: dict[str, str] = {}
    digests: dict[str, str] = {}
    bundle_root = tmp_path / "historical_211643"
    bundle_root.mkdir()
    for name, record in records.items():
        payload = canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
        path = bundle_root / f"{name}.json"
        path.write_bytes(payload)
        artifacts[name] = str(path.relative_to(tmp_path))
        digests[name] = sha256_hex(payload)
    manifest_payload = {
        "schema": "lf021_local_qualification_bundle_v1",
        "terminal_id": terminal.terminal_id,
        "artifacts": artifacts,
        "artifact_sha256": digests,
    }
    manifest = LocalQualificationBundleManifest(
        schema_version=1,
        bundle_id="local_qualification_bundle:" + hash_canonical(manifest_payload),
        terminal_id=terminal.terminal_id,
        artifacts=artifacts,
        artifact_sha256=digests,
    )
    return manifest, terminal, outcome, screening


def test_historical_211643_lineage_defect_is_self_contained(tmp_path: Path) -> None:
    manifest, terminal, outcome, screening = _historical_invalid_bundle(tmp_path)

    assert not (tmp_path / "runs").exists()
    assert terminal.materialization_outcome_id != outcome.outcome_id
    assert terminal.screening_id == screening.screening_id
    assert outcome.screening_id is None
    assert terminal.admitted_pair_ids
    assert outcome.pair_ids == ()
    assert terminal.admitted_nl_lean_id is not None
    assert outcome.nl_lean_id is None
    assert not any(name.startswith("input_") for name in manifest.artifacts)


def test_current_replay_rejects_historical_211643_without_runs_directory(
    tmp_path: Path,
) -> None:
    manifest, terminal, _, _ = _historical_invalid_bundle(tmp_path)
    problem = ProblemPoolRecord.model_construct(
        problem_record_id=terminal.problem_record_id,
        import_header_artifact="unused",
        import_header_hash=HEX,
    )

    with pytest.raises(
        LocalQualificationReplayError,
        match="legacy qualification bundle manifest is diagnostic-only",
    ):
        verify_local_qualification_bundle(
            manifest,
            artifact_root=tmp_path,
            repo_root=tmp_path / "repository-does-not-exist",
            problem=problem,
        )
    assert not (tmp_path / "runs").exists()
