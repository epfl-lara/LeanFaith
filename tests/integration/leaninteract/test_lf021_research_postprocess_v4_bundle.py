"""Real collector-v3 bundle compatibility for postprocess-v4 loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.research_postprocess_v4 import (
    load_research_postprocess_v4,
)

ROOT = find_repo_root(Path(__file__).parent)
CONFIG = ROOT / "configs/generation/local_research_collection_cross_domain_s0_v3.yaml"
COLLECTION = (
    ROOT
    / "data/raw/real_outputs/cross_domain_docstrings_operational_v1/v3"
    / "cross_domain_s0/local_collection"
    / "b5080892f0b71e43735dfe3a1f3bf4e227f7988c362196ea7a09ea703db3846c"
)

pytestmark = pytest.mark.skipif(
    not (COLLECTION / "manifest.json").is_file(),
    reason="the immutable cross-domain collector-v3 bundle is not present",
)


def test_real_collector_v3_bundle_loads_under_v4_without_lean_execution() -> None:
    loaded = load_research_postprocess_v4(
        repo_root=ROOT,
        collection_root=COLLECTION,
        collection_config_path=CONFIG,
    )
    binding = loaded.input_binding

    assert binding.tranche_id == "cross_domain_s0"
    assert binding.pool_dialect == "cross_domain_operational_v1"
    assert binding.pool_source == "mathlib_cross_domain_docstrings_operational_v1"
    assert binding.problem_count == 20
    assert binding.family_count == 3
    assert binding.expected_invocations == 60
    assert len(binding.collection_terminal_artifacts) == 60
    assert len(binding.raw_collection_artifacts_by_invocation) == 60
    assert hash_file(ROOT / binding.recovery_implementation.artifact) == (
        binding.recovery_implementation.sha256
    )
    assert hash_file(ROOT / binding.shared_processing_implementation.artifact) == (
        binding.shared_processing_implementation.sha256
    )
    assert binding.shared_processing_input_binding_hash == (
        loaded.shared_v3.input_binding.binding_hash
    )
