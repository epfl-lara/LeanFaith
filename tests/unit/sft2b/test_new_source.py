from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.new_source import load_new_source
from leanfaith.sft2b.pins import verify_runtime_pins

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_HELPER = _REPO_ROOT / "src/leanfaith/sft2b/lean_helper.lean"
_EXISTING_301_LOCAL_PREREQUISITES = (
    _REPO_ROOT / "data/raw/real_outputs/public_research_v1",
    _REPO_ROOT / "data/raw/real_outputs/gate3_docstrings_operational_v1",
    _REPO_ROOT / "data/raw/real_outputs/cross_domain_docstrings_operational_v1",
    _REPO_ROOT / "data/parsed/real_outputs/public_research_v1/reference_representations.jsonl",
    _REPO_ROOT / "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/"
    "reference_representations.jsonl",
)
_NEW_SOURCE_CATALOGS = (
    _REPO_ROOT / "data/parsed/real_outputs/gate3_docstrings_operational_v1/"
    "problem_pool_records.jsonl",
    _REPO_ROOT / "data/parsed/real_outputs/gate3_docstrings_operational_v1/"
    "reference_representations.jsonl",
    _REPO_ROOT / "data/parsed/real_outputs/gate3_docstrings_operational_v1/"
    "reference_theorems.jsonl",
    _REPO_ROOT / "data/parsed/real_outputs/gate3_docstrings_operational_v1/record_audits.jsonl",
)


def _require_local_new_source_evidence(source_path: Path) -> None:
    prerequisites = (*_EXISTING_301_LOCAL_PREREQUISITES, *_NEW_SOURCE_CATALOGS, source_path)
    if any(not path.exists() for path in prerequisites):
        pytest.skip("frozen ignored new-source/301 evidence is unavailable in this worktree")


def test_new_smoke_source_replays_prior_quality_and_contamination_audits() -> None:
    _require_local_new_source_evidence(
        Path("/storage/milikic/leanfaith/mathlib4/Mathlib/Algebra/Group/Action/Faithful.lean")
    )
    pins = verify_runtime_pins(_REPO_ROOT, helper_path=_HELPER)
    source, receipt = load_new_source(
        _REPO_ROOT,
        config_path=_REPO_ROOT / "configs/sft2b/new_source_smoke_v1.json",
        helper_path=_HELPER,
        pins=pins,
    )

    assert source.reference_declaration_name == "RightCancelMonoid.faithfulSMul"
    assert source.standalone_nl is True
    assert source.trusted_reference is True
    assert source.training_eligible is False
    assert receipt.absent_from_existing_301 is True
    assert all(receipt.audit_checks.values())


def test_elementary_new_source_replays_golden_and_prior_audits() -> None:
    _require_local_new_source_evidence(
        Path("/storage/milikic/leanfaith/mathlib4/Mathlib/Algebra/Group/Ideal.lean")
    )
    pins = verify_runtime_pins(_REPO_ROOT, helper_path=_HELPER)
    source, receipt = load_new_source(
        _REPO_ROOT,
        config_path=_REPO_ROOT / "configs/sft2b/new_source_semigroup_ideal_smoke_v2.json",
        helper_path=_HELPER,
        pins=pins,
    )

    assert source.reference_declaration_name == "SemigroupIdeal.coe_closure'"
    assert source.nl_statement.startswith("In a monoid, the semigroup ideal")
    assert receipt.absent_from_existing_301 is True
    assert receipt.golden_blocklist_sha256 is not None
    assert receipt.golden_checks is not None and all(receipt.golden_checks.values())
