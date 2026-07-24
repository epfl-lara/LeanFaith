"""Model-free live preflight for the public Kimina mathlib fixture."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import pytest

from leanfaith.cli.collect_real_outputs import (
    _load_offline_fixture,
    _offline_context,
    _offline_problem,
    _offline_reference,
    _qualification_fixture_header_path,
)
from leanfaith.cli.pipeline import default_mathlib_checkout
from leanfaith.config.hashing import hash_file
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.datasets.denylist import load_active_benchmark_registry
from leanfaith.generation.candidate_screening import CandidateScreeningIndex
from leanfaith.generation.local_qualification import (
    preflight_local_qualification_fixture,
)
from leanfaith.generation.problem_pool import ProblemPoolDenylistBinding
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend

ROOT = find_repo_root(Path(__file__).parent)
PATHS = RepoPaths(ROOT)
PROJECT = default_mathlib_checkout()
FIXTURE_PATH = ROOT / "examples" / "lf021_kimina_mathlib_nat_comm_20260723_v1.json"
UTC = datetime.datetime(2026, 7, 23, 23, 30, tzinfo=datetime.UTC)

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
    pytest.mark.skipif(
        not (PROJECT / "lean-toolchain").is_file(),
        reason="pinned mathlib checkout unavailable",
    ),
]


def test_public_mathlib_fixture_passes_active_problem_and_candidate_preflight(
    tmp_path: Path,
) -> None:
    fixture = _load_offline_fixture(FIXTURE_PATH)
    fixture_hash = hash_file(FIXTURE_PATH)
    header_path = _qualification_fixture_header_path(fixture, paths=PATHS)
    context = _offline_context(
        PATHS,
        project_dir=PROJECT,
        imports_text=fixture.imports,
        project_registry_key="mathlib",
    )
    reference = _offline_reference(
        fixture=fixture,
        fixture_hash=fixture_hash,
        context=context,
    )
    active = load_active_benchmark_registry(repo_root=ROOT)
    denylist = ProblemPoolDenylistBinding.from_active_registry(
        active,
        repo_root=ROOT,
    )
    problem, _, _ = _offline_problem(
        fixture=fixture,
        fixture_hash=fixture_hash,
        fixture_path=FIXTURE_PATH,
        import_header_path=header_path,
        paths=PATHS,
        context=context,
        reference=reference,
        denylist=denylist,
    )
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=PROJECT,
            context_fingerprint=context.context_fingerprint,
            environment_schema_version=context.environment_schema_version,
            raw_response_dir=tmp_path / "lean_raw",
        )
    )
    try:
        report = preflight_local_qualification_fixture(
            fixture_id=fixture.fixture_id,
            fixture_sha256=fixture_hash,
            import_header_artifact=str(header_path.relative_to(ROOT)),
            problem=problem,
            reference=reference,
            context=context,
            registered_header=fixture.imports,
            backend=backend,
            screening_index=CandidateScreeningIndex(denylist=denylist),
            created_at=UTC,
        )
    finally:
        backend.close()

    assert report.project_registry_key == "mathlib"
    assert report.project_revision == ("d568c8c09630de097a046763c17b9ea99f95f950")
    assert report.candidate_benchmark_hits == ()
    assert report.model_execution_performed is False
    assert report.semantic_labels_created is False
    assert report.qualifies_for_gate5g is False
