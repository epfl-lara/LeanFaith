"""Versioned LF-021 postprocess-v2 recovery provenance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from leanfaith.config.hashing import hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.generation import research_postprocess_v2 as module
from leanfaith.generation.local_output_adapter import (
    FinalFenceError,
    FinalFenceErrorCode,
    LeanExtractedCandidate,
    RawLeanCompletion,
)
from leanfaith.generation.local_output_recovery import RECOVERY_PARSER_ID
from leanfaith.generation.prompts import ParsedLeanDeclaration
from leanfaith.generation.research_postprocess import (
    ResearchPostprocessStage,
    ResearchPostprocessStatus,
)
from leanfaith.generation.research_postprocess_v2 import (
    RecoveryStatus,
    _failure_terminal,
    _parse_with_fallback,
    load_research_postprocess_v2,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus

ROOT = find_repo_root(Path(__file__).parent)
COLLECTION = (
    ROOT
    / "data"
    / "raw"
    / "real_outputs"
    / "public_research_v1"
    / "local_collection_v1"
    / "75e16a5cb7ba937463821c92ef612c25475d91e7af00fb38bc2c970fa3dc2393"
)
DATA = ROOT / "data" / "parsed" / "real_outputs" / "public_research_v1"


def _load() -> module.LoadedResearchPostprocessV2:
    if not (COLLECTION / "manifest.json").is_file():
        pytest.skip("immutable LF-021 3x3 collection is unavailable")
    return load_research_postprocess_v2(
        repo_root=ROOT,
        collection_root=COLLECTION,
        problem_pool_records_path=DATA / "problem_pool_records.jsonl",
        context_path=DATA / "context.json",
        import_header_path=ROOT / "examples" / "lf021_public_research_mathlib_header_v1.lean",
        reference_theorems_path=DATA / "reference_theorems.jsonl",
        reference_representations_path=DATA / "reference_representations.jsonl",
        output_root=COLLECTION / "_postprocess_v2_unit_never_written",
    )


def _candidate(name: str) -> LeanExtractedCandidate:
    statement = f"theorem {name} : True"
    digest = sha256_hex(statement.encode())
    return LeanExtractedCandidate(
        parsed=ParsedLeanDeclaration(
            declaration_kind="theorem",
            declaration_name=name,
            statement=statement,
            statement_sha256=digest,
        ),
        fenced=RawLeanCompletion(
            code=statement,
            code_sha256=digest,
            candidate_body=statement,
            candidate_body_sha256=digest,
            included_registered_header=False,
        ),
        source_sha256=digest,
        lean_status=LeanStatus.VALID_WITH_SORRY,
    )


def test_v2_load_binds_v1_recovery_and_v2_modules_without_touching_v1() -> None:
    before = hash_file(COLLECTION / "postprocess_v1" / "manifest.json")
    loaded = _load()
    after = hash_file(COLLECTION / "postprocess_v1" / "manifest.json")
    assert before == after
    assert loaded.base.output_root.name == "_postprocess_v2_unit_never_written"
    assert (
        loaded.input_binding.primary_implementation
        == loaded.input_binding.primary_binding.implementation
    )
    assert loaded.input_binding.recovery_implementation.artifact.endswith(
        "local_output_recovery.py"
    )
    assert loaded.input_binding.implementation.artifact.endswith("research_postprocess_v2.py")
    assert not loaded.base.output_root.exists()


def test_eligible_primary_failure_records_recovery_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _load()
    invocation = loaded.base.invocations[0]
    terminal = loaded.base.collection_terminals[invocation.invocation_id]

    def frozen_parser(**_: Any) -> Any:
        def parse(**__: Any) -> LeanExtractedCandidate:
            raise FinalFenceError(
                FinalFenceErrorCode.DECLARATION_COUNT,
                "harmless preamble",
            )

        return parse

    expected = _candidate(invocation.expected_declaration_name)
    monkeypatch.setattr(module.__dict__["v1"], "_parser", frozen_parser)
    monkeypatch.setattr(
        module,
        "extract_expected_declaration_with_lean",
        lambda **_: expected,
    )
    result = _parse_with_fallback(
        loaded,
        invocation=invocation,
        collection_terminal=terminal,
        raw_output="import Mathlib\ntheorem ignored : True := by sorry",
        backend=cast(LeanInteractBackend, object()),
    )
    parsed, primary_code, status, actual_id, actual_hash, detail = result
    assert parsed is expected
    assert primary_code == FinalFenceErrorCode.DECLARATION_COUNT.value
    assert status is RecoveryStatus.SUCCEEDED
    assert actual_id == RECOVERY_PARSER_ID
    assert actual_hash == loaded.input_binding.recovery_implementation.sha256
    assert detail is None


def test_genuine_primary_lean_invalid_never_calls_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _load()
    invocation = loaded.base.invocations[0]
    terminal = loaded.base.collection_terminals[invocation.invocation_id]

    def frozen_parser(**_: Any) -> Any:
        def parse(**__: Any) -> LeanExtractedCandidate:
            raise FinalFenceError(FinalFenceErrorCode.LEAN_INVALID, "bad type")

        return parse

    called = False

    def forbidden_recovery(**_: Any) -> LeanExtractedCandidate:
        nonlocal called
        called = True
        raise AssertionError("recovery must not run")

    monkeypatch.setattr(module.__dict__["v1"], "_parser", frozen_parser)
    monkeypatch.setattr(
        module,
        "extract_expected_declaration_with_lean",
        forbidden_recovery,
    )
    parsed, primary_code, status, actual_id, actual_hash, detail = _parse_with_fallback(
        loaded,
        invocation=invocation,
        collection_terminal=terminal,
        raw_output="theorem bad : MissingType := by sorry",
        backend=cast(LeanInteractBackend, object()),
    )
    assert parsed is None
    assert primary_code == FinalFenceErrorCode.LEAN_INVALID.value
    assert status is RecoveryStatus.NOT_ELIGIBLE
    assert actual_id is None
    assert actual_hash is None
    assert "bad type" in cast(str, detail)
    assert not called


def test_preparser_failures_use_not_attempted_not_not_eligible() -> None:
    loaded = _load()
    invocation = loaded.base.invocations[0]
    collection = loaded.base.collection_terminals[invocation.invocation_id]
    terminal = _failure_terminal(
        loaded=loaded,
        invocation=invocation,
        collection_terminal=collection,
        status=ResearchPostprocessStatus.RAW_LINEAGE_FAILED,
        stage=ResearchPostprocessStage.RAW_LINEAGE,
        code="lineage_mismatch",
        detail="raw lineage did not verify",
        primary_failure_code=None,
        recovery_status=RecoveryStatus.NOT_ATTEMPTED,
        actual_parser_id=None,
        actual_parser_source_sha256=None,
        parser_executed=False,
    )
    assert terminal.recovery_status is RecoveryStatus.NOT_ATTEMPTED
    assert not terminal.parser_executed


def test_primary_invalid_terminal_records_attempt_but_no_recovery_parser() -> None:
    loaded = _load()
    invocation = loaded.base.invocations[0]
    collection = loaded.base.collection_terminals[invocation.invocation_id]
    terminal = _failure_terminal(
        loaded=loaded,
        invocation=invocation,
        collection_terminal=collection,
        status=ResearchPostprocessStatus.PARSE_FAILED,
        stage=ResearchPostprocessStage.PARSER,
        code=FinalFenceErrorCode.LEAN_INVALID.value,
        detail="Lean rejected the primary candidate",
        primary_failure_code=FinalFenceErrorCode.LEAN_INVALID.value,
        recovery_status=RecoveryStatus.NOT_ELIGIBLE,
        actual_parser_id=None,
        actual_parser_source_sha256=None,
        parser_executed=True,
    )
    assert terminal.parser_executed
    assert terminal.recovery_status is RecoveryStatus.NOT_ELIGIBLE
    assert terminal.actual_parser_id is None
    assert terminal.primary_failure_code == FinalFenceErrorCode.LEAN_INVALID.value
