from __future__ import annotations

from pathlib import Path

from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.new_source import load_new_source
from leanfaith.sft2b.pins import verify_runtime_pins

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_HELPER = _REPO_ROOT / "src/leanfaith/sft2b/lean_helper.lean"


def test_new_smoke_source_replays_prior_quality_and_contamination_audits() -> None:
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
