"""Public mathlib-fixture context and fail-closed preflight tests."""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

import leanfaith.generation.local_qualification as qualification
from leanfaith.cli.collect_real_outputs import (
    LF021FoundationError,
    _load_offline_fixture,
    _offline_context,
    _qualification_fixture_header_path,
)
from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.datasets.denylist import (
    DenylistIndex,
    FrozenBenchmark,
    FrozenRegistry,
)
from leanfaith.generation.candidate_screening import CandidateScreeningIndex
from leanfaith.generation.local_qualification import (
    LocalQualificationConfigError,
    preflight_local_qualification_fixture,
)
from leanfaith.generation.problem_pool import ProblemPoolDenylistBinding
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.representations.pipeline import (
    RepresentationBatchResult,
)
from leanfaith.schemas.enums import NLTrust, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import ANCESTRY_PREFIX, REPRESENTATION_PREFIX, THEOREM_PREFIX, make_id
from leanfaith.schemas.nl_lean import ProblemPoolRecord, make_problem_record_id
from leanfaith.schemas.theorem import (
    CANONICAL_VIEW_NAMES,
    ContextRecord,
    RepresentationRecord,
    TheoremRecord,
)

ROOT = find_repo_root(Path(__file__).parent)
FIXTURE_PATH = ROOT / "examples" / "lf021_kimina_mathlib_nat_comm_20260723_v1.json"
UTC = datetime.datetime(2026, 7, 23, 23, 0, tzinfo=datetime.UTC)
HEADER = "import Mathlib\n"
CTX_FP = "a" * 64
CTX_ID = f"ctx:{CTX_FP}"


def _write_environment_lock(paths: RepoPaths) -> None:
    path = paths.configs / "environment.lock.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "environment_schema_version: 1\n"
        "python:\n"
        "  version: '3.12'\n"
        "lean_interact:\n"
        "  package: lean-interact\n"
        "  version: 0.11.4\n"
        "  advertised_lean_min: v4.8.0-rc1\n"
        "  advertised_lean_max: v4.31.0-rc1\n"
        "  repl_fork: augustepoiroux/repl\n"
        "toolchain_lock:\n"
        "  mode: advertised_range\n"
        "  accepted_lean: v4.31.0-rc1\n"
        "lean_backend: {}\n",
        encoding="utf-8",
    )


def _git_mathlib_fixture(tmp_path: Path) -> tuple[RepoPaths, Path, str]:
    paths = RepoPaths(root=tmp_path / "repo")
    paths.root.mkdir()
    _write_environment_lock(paths)
    project = tmp_path / "mathlib4"
    project.mkdir()
    (project / "lean-toolchain").write_text(
        "leanprover/lean4:v4.31.0-rc1\n",
        encoding="utf-8",
    )
    (project / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage mathlib\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=LeanFaith",
            "-c",
            "user.email=fixture@example.invalid",
            "add",
            ".",
        ],
        cwd=project,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=LeanFaith",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        cwd=project,
        check=True,
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    projects = paths.configs / "projects"
    projects.mkdir(parents=True)
    (projects / "mathlib.yaml").write_text(
        "registry_key: mathlib\n"
        "kind: git\n"
        "uri: https://github.com/leanprover-community/mathlib4\n"
        f"revision: '{revision}'\n"
        "expected_toolchain: v4.31.0-rc1\n"
        "root_module: Mathlib\n"
        "globs:\n"
        "  - 'Mathlib/**/*.lean'\n"
        "role: mvp_source\n",
        encoding="utf-8",
    )
    return paths, project, revision


def test_versioned_mathlib_fixture_binds_exact_header_and_generated_name() -> None:
    fixture = _load_offline_fixture(FIXTURE_PATH)
    header = _qualification_fixture_header_path(fixture, paths=RepoPaths(ROOT))

    assert fixture.schema_version == 2
    assert fixture.resolved_project_registry_key == "mathlib"
    assert fixture.resolved_generated_declaration_name == "lf021_kimina_generated_nat_comm_20260723"
    assert header == ROOT / "examples" / "lf021_kimina_mathlib_nat_header_v1.lean"
    assert header.read_text(encoding="utf-8") == fixture.imports == HEADER

    legacy = _load_offline_fixture(ROOT / "examples" / "lf021_offline_smoke_v1.json")
    assert legacy.resolved_project_registry_key == "fixtures"
    assert legacy.resolved_import_header_artifact == ("examples/lf021_offline_smoke_header_v1.lean")
    assert legacy.resolved_generated_declaration_name == ("lf021_offline_generated_identity")


def test_fixture_header_mismatch_fails_closed(tmp_path: Path) -> None:
    paths = RepoPaths(tmp_path)
    source = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture_path = tmp_path / "examples" / FIXTURE_PATH.name
    fixture_path.parent.mkdir(parents=True)
    fixture_path.write_text(json.dumps(source), encoding="utf-8")
    header = tmp_path / source["import_header_artifact"]
    header.write_text("import Lean\n", encoding="utf-8")

    fixture = _load_offline_fixture(fixture_path)
    with pytest.raises(LF021FoundationError, match="exactly match"):
        _qualification_fixture_header_path(fixture, paths=paths)


def test_mathlib_context_uses_requested_registry_pin_and_git_revision(
    tmp_path: Path,
) -> None:
    paths, project, revision = _git_mathlib_fixture(tmp_path)
    context = _offline_context(
        paths,
        project_dir=project,
        imports_text=HEADER,
        project_registry_key="mathlib",
    )

    assert context.project_registry_key == "mathlib"
    assert context.project_revision == revision
    assert context.project_kind == "git"
    assert context.imports == ("Mathlib",)
    assert context.header_text == HEADER

    (paths.configs / "projects" / "mathlib.yaml").write_text(
        (paths.configs / "projects" / "mathlib.yaml")
        .read_text(encoding="utf-8")
        .replace(revision, "0" * 40),
        encoding="utf-8",
    )
    with pytest.raises(LF021FoundationError, match="revision preflight failed"):
        _offline_context(
            paths,
            project_dir=project,
            imports_text=HEADER,
            project_registry_key="mathlib",
        )


def _context() -> ContextRecord:
    return ContextRecord(
        environment_schema_version=1,
        context_id=CTX_ID,
        context_fingerprint=CTX_FP,
        project_kind="git",
        project_uri="https://github.com/leanprover-community/mathlib4",
        project_revision="b" * 40,
        project_registry_key="mathlib",
        lean_version="v4.31.0-rc1",
        lean_interact_version="0.11.4",
        repl_revision="augustepoiroux/repl@lean-interact-0.11.4",
        imports=("Mathlib",),
        header_text=HEADER,
        header_hash=sha256_hex(HEADER.encode("utf-8")),
    )


def _reference() -> TheoremRecord:
    theorem_id = make_id(THEOREM_PREFIX, {"fixture": "kimina-mathlib-reference"})
    ancestry_id = make_id(ANCESTRY_PREFIX, {"fixture": "kimina-mathlib-reference"})
    statement = (
        "theorem lf021_kimina_reference_nat_comm_20260723 (n : Nat) : n + 20260723 = 20260723 + n"
    )
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="lf021_kimina_mathlib_public_fixture",
        source_revision="v1",
        source_split="smoke",
        source_record="public-nat-comm-20260723-1",
        context_id=CTX_ID,
        declaration_kind="theorem",
        declaration_name="lf021_kimina_reference_nat_comm_20260723",
        declaration_full_name="lf021_kimina_reference_nat_comm_20260723",
        proof_stripped_declaration=statement + " := by sorry",
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=sha256_hex(statement.encode("utf-8")),
        nl_source_link="repo://examples/lf021_kimina_mathlib_nat_comm_20260723_v1.json",
        nl_trust=NLTrust.TRUSTED,
    )


def _representation(reference: TheoremRecord) -> RepresentationRecord:
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for view in (
        "raw_proof_stripped",
        "headless",
        "signature_pp",
        "signature_explicit",
        "semantic_atoms",
        "operator_tree",
    ):
        statuses[view] = ViewStatus.OK
    alpha = "d" * 64
    return RepresentationRecord(
        representation_id=make_id(
            REPRESENTATION_PREFIX,
            {"theorem_id": reference.theorem_id, "normalization_version": "repr_v2"},
        ),
        theorem_id=reference.theorem_id,
        normalization_version="repr_v2",
        context_id=CTX_ID,
        raw_proof_stripped=reference.proof_stripped_declaration,
        headless="(n : Nat) : n + 20260723 = 20260723 + n",
        signature_pp="(n : Nat) : n + 20260723 = 20260723 + n",
        signature_explicit=("(n : Nat) : Eq Nat (HAdd.hAdd n 20260723) (HAdd.hAdd 20260723 n)"),
        semantic_atoms=("Eq", "HAdd.hAdd", "Nat"),
        operator_tree={"kind": "app", "name": "Eq"},
        alpha_identity_fingerprint=alpha,
        view_status=statuses,
        content_hash=hash_canonical({"reference": reference.theorem_id, "views": "v1"}),
        created_at=UTC,
    )


def _screening_index(
    *,
    protected_alpha: str | None = None,
) -> CandidateScreeningIndex:
    benchmark = FrozenBenchmark(
        registry_key="fixture-protected",
        resolved=True,
        representation_hashes=(() if protected_alpha is None else (protected_alpha,)),
    )
    registry = FrozenRegistry(frozen_at=UTC, benchmarks=(benchmark,))
    index = DenylistIndex(registry)
    return CandidateScreeningIndex(
        denylist=ProblemPoolDenylistBinding(
            index=index,
            manifest_path="data/benchmarks/test-manifest.json",
            manifest_sha256="1" * 64,
            active_registry_sha256="2" * 64,
            registry_content_hash=index.registry_content_hash,
        )
    )


def _problem(
    reference: TheoremRecord,
    screening: CandidateScreeningIndex,
) -> ProblemPoolRecord:
    problem_id = make_problem_record_id(
        source="lf021_kimina_mathlib_public_fixture",
        source_revision="v1",
        source_split="smoke",
        source_record_id="public-nat-comm-20260723-1",
        problem_id="lf021-public-nat-comm-20260723",
    )
    return ProblemPoolRecord(
        schema_version=2,
        problem_record_id=problem_id,
        problem_id="lf021-public-nat-comm-20260723",
        problem_group="nl-problem:lf021-public-nat-comm-20260723",
        source="lf021_kimina_mathlib_public_fixture",
        source_revision="v1",
        source_split="smoke",
        source_record_id="public-nat-comm-20260723-1",
        source_record_content_hash="3" * 64,
        source_config_sha256="4" * 64,
        source_authorization_hash="5" * 64,
        source_license="CC0-1.0",
        nl_statement=(
            "For every natural number n, prove that adding 20260723 to n gives "
            "the same result as adding n to 20260723."
        ),
        nl_trust=NLTrust.TRUSTED,
        nl_source_link="repo://examples/lf021_kimina_mathlib_nat_comm_20260723_v1.json",
        context_id=CTX_ID,
        import_header_artifact="examples/lf021_kimina_mathlib_nat_header_v1.lean",
        import_header_hash=sha256_hex(HEADER.encode("utf-8")),
        reference_theorem_ids=(reference.theorem_id,),
        private_source_content=False,
        external_provider_eligible=True,
        release_eligible=False,
        eligibility="eligible",
        denylist_checked=True,
        denylist_manifest_path=screening.denylist.manifest_path,
        denylist_manifest_sha256=screening.denylist.manifest_sha256,
        denylist_active_registry_sha256=screening.denylist.active_registry_sha256,
        denylist_registry_content_hash=screening.denylist.registry_content_hash,
    )


def test_preflight_checks_problem_and_candidate_active_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _reference()
    representation = _representation(reference)

    def fake_build(*args: object, **kwargs: object) -> RepresentationBatchResult:
        return RepresentationBatchResult((representation,), ())

    monkeypatch.setattr(qualification, "build_representation_batch", fake_build)
    clean = _screening_index()
    report = preflight_local_qualification_fixture(
        fixture_id="lf021_kimina_mathlib_nat_comm_20260723_v1",
        fixture_sha256="6" * 64,
        import_header_artifact="examples/lf021_kimina_mathlib_nat_header_v1.lean",
        problem=_problem(reference, clean),
        reference=reference,
        context=_context(),
        registered_header=HEADER,
        backend=cast(LeanInteractBackend, object()),
        screening_index=clean,
        created_at=UTC,
    )
    assert report.candidate_benchmark_hits == ()
    assert report.model_execution_performed is False
    assert report.semantic_labels_created is False
    assert report.qualifies_for_gate5g is False

    contaminated_problem = _problem(reference, clean).model_copy(
        update={
            "eligibility": "excluded",
            "denylist_hits": ("normalized_nl:fixture",),
            "exclusion_reasons": ("denylist_hit",),
            "external_provider_eligible": False,
        }
    )
    with pytest.raises(LocalQualificationConfigError, match="clean active-registry problem"):
        preflight_local_qualification_fixture(
            fixture_id="lf021_kimina_mathlib_nat_comm_20260723_v1",
            fixture_sha256="6" * 64,
            import_header_artifact="examples/lf021_kimina_mathlib_nat_header_v1.lean",
            problem=contaminated_problem,
            reference=reference,
            context=_context(),
            registered_header=HEADER,
            backend=cast(LeanInteractBackend, object()),
            screening_index=clean,
            created_at=UTC,
        )

    protected = _screening_index(
        protected_alpha=cast(str, representation.alpha_identity_fingerprint)
    )
    with pytest.raises(LocalQualificationConfigError, match="overlaps"):
        preflight_local_qualification_fixture(
            fixture_id="lf021_kimina_mathlib_nat_comm_20260723_v1",
            fixture_sha256="6" * 64,
            import_header_artifact="examples/lf021_kimina_mathlib_nat_header_v1.lean",
            problem=_problem(reference, protected),
            reference=reference,
            context=_context(),
            registered_header=HEADER,
            backend=cast(LeanInteractBackend, object()),
            screening_index=protected,
            created_at=UTC,
        )
