"""Truthful prevalence-design amendment for post-exhaustion frames.

Revision 3 binds revision 2 byte-for-byte and changes only the frame-source
contract.  All estimands, ambiguity rules, nonresponse rules, source-proxy
interpretation, and three-family scope remain exactly those of revision 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.evaluation import prevalence as prevalence_v2


class PostExhaustionTargetPopulationPolicyV3(StrictModel):
    frame_schema_version: Literal[1]
    frame_source_kind: Literal["post_exhaustion_extended_frame_v1"]
    primary_unit: Literal["problem_group_x_alpha_identity"]
    eligible_population: Literal[
        "benchmark_clear_compiling_problem_aware_claims_in_verified_extended_population"
    ]
    label_reuse_scope: Literal["same_problem_group_and_alpha_identity_only"]
    sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]


class PrevalenceDesignPolicyV3(StrictModel):
    """Frame-source-only amendment over exact prevalence-design v2 bytes."""

    schema_version: Literal[3] = 3
    policy_id: Literal["lf021_prevalence_design_v3"]
    status: Literal["frozen_prelabel"]
    base_v2_design: prevalence_v2.PolicyArtifactBinding
    target_population: PostExhaustionTargetPopulationPolicyV3
    primary: prevalence_v2.PrimaryEstimandPolicy
    secondary: prevalence_v2.SecondaryEstimandPolicy
    ambiguity: prevalence_v2.AmbiguityPolicy
    nonresponse: prevalence_v2.NonresponsePolicy
    source_proxy: prevalence_v2.SourceProxyPolicy
    scope: prevalence_v2.ThreeFamilyScopePolicy
    semantic_labels_inspected_when_frozen: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    gate_5g_closed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.target_population.frame_schema_version != 1:
            raise ValueError("post-exhaustion frame schema must be version 1")
        return self


def load_prevalence_design_policy_v3(
    path: Path,
) -> LoadedConfig[PrevalenceDesignPolicyV3]:
    return load_config(path, PrevalenceDesignPolicyV3)


def verify_prevalence_design_policy_v3(
    *,
    repo_root: Path,
    loaded_policy: LoadedConfig[PrevalenceDesignPolicyV3],
) -> LoadedConfig[prevalence_v2.PrevalenceDesignPolicyV2]:
    """Verify exact v2 inheritance and the sole prospective frame-source delta."""

    binding = loaded_policy.config.base_v2_design
    base_path = (repo_root / binding.artifact).resolve()
    if (
        not base_path.is_relative_to(repo_root.resolve())
        or not base_path.is_file()
        or hash_file(base_path) != binding.sha256
    ):
        raise prevalence_v2.PrevalenceInputError("prevalence-design v3 base-v2 binding differs")
    loaded_base = prevalence_v2.load_prevalence_design_policy(base_path)
    prevalence_v2.verify_prevalence_design_policy_v2(
        repo_root=repo_root,
        loaded_policy=loaded_base,
    )
    policy = loaded_policy.config
    base = loaded_base.config
    for field_name in (
        "primary",
        "secondary",
        "ambiguity",
        "nonresponse",
        "source_proxy",
        "scope",
        "semantic_labels_inspected_when_frozen",
        "semantic_labels_created",
        "gate_5g_closed",
        "gate_5_closed",
    ):
        if getattr(policy, field_name) != getattr(base, field_name):
            raise prevalence_v2.PrevalenceInputError(
                f"prevalence-design v3 changes frozen v2 field {field_name}"
            )
    if (
        policy.target_population.primary_unit != base.target_population.primary_unit
        or policy.target_population.label_reuse_scope != base.target_population.label_reuse_scope
        or policy.target_population.sampling_method != base.target_population.sampling_method
        or policy.target_population.sampling_rank_algorithm
        != base.target_population.sampling_rank_algorithm
    ):
        raise prevalence_v2.PrevalenceInputError(
            "prevalence-design v3 changes the v2 sampling estimand contract"
        )
    return loaded_base


__all__ = [
    "PostExhaustionTargetPopulationPolicyV3",
    "PrevalenceDesignPolicyV3",
    "load_prevalence_design_policy_v3",
    "verify_prevalence_design_policy_v3",
]
