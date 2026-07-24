"""Live LeanInteract coverage for the LF-021 recovery parser."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.local_output_recovery import (
    RecoveryError,
    RecoveryErrorCode,
    extract_expected_declaration_with_lean,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord

ROOT = find_repo_root(Path(__file__).parent)
FIXTURES = ROOT / "tests" / "lean_fixtures"
UTC = datetime.datetime(2026, 7, 23, 23, 45, tzinfo=datetime.UTC)
FINGERPRINT = "e" * 64
CONTEXT_ID = f"ctx:{FINGERPRINT}"
HEADER = "import LeanFaithFixtures.Basic"
NAME = "lf021_recovery_live_identity"

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]


def _problem() -> ProblemPoolRecord:
    return ProblemPoolRecord.model_construct(
        problem_record_id="problem:" + "f" * 64,
        context_id=CONTEXT_ID,
    )


def _context() -> ContextRecord:
    return ContextRecord.model_construct(
        environment_schema_version=1,
        context_id=CONTEXT_ID,
        context_fingerprint=FINGERPRINT,
        header_text=HEADER,
        header_hash=sha256_hex(HEADER.encode()),
    )


def _backend(tmp_path: Path) -> LeanInteractBackend:
    return LeanInteractBackend(
        BackendSettings(
            project_dir=FIXTURES,
            context_fingerprint=FINGERPRINT,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "lean_raw",
        )
    )


def test_live_recovery_normalizes_elaborated_type_and_drops_proof(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    sentinel = "LIVE_RECOVERY_PROOF_SENTINEL"
    try:
        candidate = extract_expected_declaration_with_lean(
            raw_output=(
                "The scratch explanation is intentionally outside the final candidate.\n"
                "```text\nnot Lean\n```\n"
                "```lean4\n"
                "open Nat\n"
                "set_option maxRecDepth 10000\n"
                f"theorem {NAME} (n : Nat) : n = n := by\n"
                f"  -- {sentinel}\n"
                "  exact rfl\n"
                "```\n"
            ),
            expected_declaration_name=NAME,
            registered_header=HEADER,
            problem=_problem(),
            context=_context(),
            backend=backend,
            created_at=UTC,
        )
    finally:
        backend.close()

    assert candidate.parsed.statement == f"theorem {NAME} : ∀ (n : Nat), n = n"
    assert sentinel not in candidate.parsed.statement
    assert ":= by" not in candidate.parsed.statement


def test_live_recovery_rejects_lean_invalid_candidate(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    try:
        with pytest.raises(RecoveryError) as error:
            extract_expected_declaration_with_lean(
                raw_output=f"theorem {NAME} : Nat.succ True = 1 := by trivial",
                expected_declaration_name=NAME,
                registered_header=HEADER,
                problem=_problem(),
                context=_context(),
                backend=backend,
                created_at=UTC,
            )
    finally:
        backend.close()
    assert error.value.code is RecoveryErrorCode.LEAN_INVALID
