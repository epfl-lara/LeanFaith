from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.numina_source import load_numina_source
from leanfaith.sft2b.pins import verify_runtime_pins

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_HELPER = _REPO_ROOT / "src/leanfaith/sft2b/lean_helper.lean"
_LOCAL_PREREQUISITES = (
    _REPO_ROOT / "data/raw/real_outputs/public_research_v1",
    _REPO_ROOT / "data/raw/real_outputs/gate3_docstrings_operational_v1",
    _REPO_ROOT / "data/raw/real_outputs/cross_domain_docstrings_operational_v1",
    _REPO_ROOT / "data/parsed/real_outputs/public_research_v1/reference_representations.jsonl",
    _REPO_ROOT / "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/"
    "reference_representations.jsonl",
    Path(
        "/storage/milikic/models/hub/datasets--formalmathatepfl--sft_classic_numina/"
        "snapshots/b3e537486452a88406507c4c2d6f347d46077f61"
    ),
)


def test_one_numina_source_is_exactly_pinned_audited_and_not_bulk_eligible() -> None:
    if any(not path.exists() for path in _LOCAL_PREREQUISITES):
        pytest.skip("frozen ignored Numina/301 evidence is unavailable in this worktree")
    pins = verify_runtime_pins(_REPO_ROOT, helper_path=_HELPER)
    source, receipt = load_numina_source(
        _REPO_ROOT,
        config_path=_REPO_ROOT / "configs/sft2b/numina_multiples_smoke_v1.json",
        helper_path=_HELPER,
        pins=pins,
    )

    assert source.reference_declaration_name == "algebra_20786"
    assert source.nl_statement == "How many positive multiples of 7 are less than 150?"
    assert source.training_eligible is False
    assert receipt.absent_from_existing_301 is True
    assert all(receipt.audit_checks.values())
