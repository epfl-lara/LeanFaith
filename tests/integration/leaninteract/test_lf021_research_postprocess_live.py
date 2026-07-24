"""One-record live Lean test for the LF-021 research postprocess parser boundary."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.local_output_adapter import (
    RAW_OR_FINAL_PARSER_ID,
    parser_source_sha256,
)
from leanfaith.generation.research_collection import ResearchCollectionInvocation
from leanfaith.generation.research_postprocess import _parser
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.nl_lean import ProblemPoolRecord, make_problem_record_id
from leanfaith.schemas.theorem import ContextRecord

ROOT = find_repo_root(Path(__file__).parent)
FIXTURES = ROOT / "tests" / "lean_fixtures"
PARSER_ARTIFACT = "src/leanfaith/generation/local_output_adapter.py"
UTC = datetime.datetime(2026, 7, 23, 22, 0, tzinfo=datetime.UTC)
CONTEXT_FINGERPRINT = "d" * 64
CONTEXT_ID = f"ctx:{CONTEXT_FINGERPRINT}"
HEADER = "import LeanFaithFixtures.Basic\nnamespace LF021ResearchPostprocess"
EXPECTED_NAME = "lf021_research_postprocess_identity"

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]


def test_frozen_research_parser_uses_lean_and_structurally_drops_the_proof(
    tmp_path: Path,
) -> None:
    context = ContextRecord(
        environment_schema_version=1,
        context_id=CONTEXT_ID,
        context_fingerprint=CONTEXT_FINGERPRINT,
        project_kind="local",
        project_uri="tests/lean_fixtures",
        project_revision="fixture",
        project_registry_key="fixtures",
        lean_version="v4.31.0-rc1",
        lean_interact_version="0.11.4",
        repl_revision="fixture",
        imports=("LeanFaithFixtures.Basic",),
        namespace_context=("LF021ResearchPostprocess",),
        header_text=HEADER,
        header_hash=sha256_hex(HEADER.encode()),
    )
    problem_id = make_problem_record_id(
        source="public_fixture",
        source_revision="v1",
        source_split="test",
        source_record_id="lf021-postprocess-live",
        problem_id="lf021-postprocess-live",
    )
    problem = ProblemPoolRecord(
        schema_version=2,
        problem_record_id=problem_id,
        problem_id="lf021-postprocess-live",
        problem_group="nl-problem:lf021-postprocess-live",
        source="public_fixture",
        source_revision="v1",
        source_split="test",
        source_record_id="lf021-postprocess-live",
        source_record_content_hash="1" * 64,
        source_license="Apache-2.0",
        source_config_sha256="6" * 64,
        source_authorization_hash="7" * 64,
        nl_statement="Every natural number is equal to itself.",
        nl_trust=NLTrust.TRUSTED,
        nl_source_link="repo://tests/lf021-postprocess-live",
        context_id=CONTEXT_ID,
        import_header_artifact="tests/lean_fixtures/lf021-postprocess-header.lean",
        import_header_hash=context.header_hash,
        reference_theorem_ids=("thm:" + "2" * 64,),
        private_source_content=False,
        external_provider_eligible=True,
        release_eligible=True,
        eligibility="eligible",
        denylist_checked=True,
        denylist_manifest_path="data/benchmarks/manifests/representation_signatures_v1.json",
        denylist_manifest_sha256="3" * 64,
        denylist_active_registry_sha256="4" * 64,
        denylist_registry_content_hash="5" * 64,
    )
    parser_hash = parser_source_sha256()
    invocation = ResearchCollectionInvocation.model_construct(
        parser_id=RAW_OR_FINAL_PARSER_ID,
        parser_source_sha256=parser_hash,
    )
    selected = _parser(
        invocation=invocation,
        family_parser_artifact=PARSER_ARTIFACT,
        family_parser_sha256=parser_hash,
        repo_root=ROOT,
    )
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=FIXTURES,
            context_fingerprint=CONTEXT_FINGERPRINT,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "lean_raw",
        )
    )
    try:
        candidate = selected(
            raw_output=(f"theorem {EXPECTED_NAME} (n : Nat) : n = n := by\n  exact rfl\n"),
            expected_declaration_name=EXPECTED_NAME,
            registered_header=HEADER,
            problem=problem,
            context=context,
            backend=backend,
            created_at=UTC,
        )
    finally:
        backend.close()

    assert candidate.parsed.statement == (f"theorem {EXPECTED_NAME} (n : Nat) : n = n")
    assert "exact rfl" not in candidate.parsed.statement
