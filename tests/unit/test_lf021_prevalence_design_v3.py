from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig
from leanfaith.evaluation import prevalence as prevalence_v2
from leanfaith.evaluation import prevalence_design_v3 as subject

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "policies/lf021_prevalence_design_v3.yaml"


def test_v3_binds_v2_and_changes_only_frame_source_contract() -> None:
    loaded = subject.load_prevalence_design_policy_v3(POLICY)
    base = subject.verify_prevalence_design_policy_v3(
        repo_root=ROOT,
        loaded_policy=loaded,
    )
    assert loaded.config.base_v2_design.sha256 == hash_file(base.path)
    assert loaded.config.primary == base.config.primary
    assert loaded.config.secondary == base.config.secondary
    assert loaded.config.ambiguity == base.config.ambiguity
    assert loaded.config.nonresponse == base.config.nonresponse
    assert loaded.config.source_proxy == base.config.source_proxy
    assert loaded.config.scope == base.config.scope
    assert loaded.config.target_population.frame_source_kind == "post_exhaustion_extended_frame_v1"


def test_v3_rejects_a_rewritten_v2_estimand() -> None:
    loaded = subject.load_prevalence_design_policy_v3(POLICY)
    changed_secondary = loaded.config.secondary.model_copy(update={"confidence_level": 0.90})
    changed = loaded.config.model_copy(update={"secondary": changed_secondary})
    tampered = LoadedConfig(
        config=changed,
        path=loaded.path,
        raw=changed.model_dump(mode="json"),
        config_hash=hash_canonical(changed.model_dump(mode="json")),
    )
    with pytest.raises(
        prevalence_v2.PrevalenceInputError,
        match="changes frozen v2 field secondary",
    ):
        subject.verify_prevalence_design_policy_v3(
            repo_root=ROOT,
            loaded_policy=tampered,
        )
