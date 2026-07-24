"""Problem-aware, pre-label LF-021 tranche expansion amendment.

Revision 1 grouped compiling candidates by Lean alpha identity alone.  That is
useful as a code-diversity diagnostic, but it is not a valid sampling unit for
NL-to-Lean faithfulness: the same Lean proposition may be correct for one
problem and wrong for another.  This module leaves revision 1 immutable and
reuses its verified artifact loaders, tranche schedule, and coverage rules,
while changing the scientific unit to
``(problem_group, alpha_identity_fingerprint)``.

The module has no model execution, label resolution, proof search, supervision
admission, or Gate-closing capability.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation import tranche_expansion as v1
from leanfaith.schemas.nl_lean import ProblemPoolRecord

_HEX64 = r"^[0-9a-f]{64}$"
_DECISION_ID = r"^lf021_expansion_decision_v2:[0-9a-f]{64}$"
_FRAME_ID = r"^lf021_prevalence_frame_v2:[0-9a-f]{64}$"
_FRAME_RECORD_ID = r"^lf021_prevalence_item_v2:[0-9a-f]{64}$"


class TrancheExpansionV2Error(RuntimeError):
    """The amendment, problem identity, or deterministic output failed closed."""


class TrancheExpansionAmendmentV2(StrictModel):
    """Narrow amendment binding the complete frozen revision-1 policy."""

    schema_version: Literal[2] = 2
    policy_id: Literal["lf021_problem_aware_tranche_expansion_v2"]
    status: Literal["frozen"]
    base_v1_policy: v1.ArtifactBinding
    base_v1_implementation: v1.ArtifactBinding
    scientific_cluster_key: tuple[str, str]
    representative_hash_salt: str = Field(min_length=1)
    sampling_hash_salt: str = Field(min_length=1)
    decision_inputs: tuple[str, ...]
    forbidden_inputs: tuple[str, ...]
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.scientific_cluster_key != (
            "problem_group",
            "alpha_identity_fingerprint",
        ):
            raise ValueError("revision 2 cluster key must be problem_group x alpha")
        if self.decision_inputs != tuple(sorted(set(self.decision_inputs))):
            raise ValueError("decision inputs must be sorted and unique")
        if self.forbidden_inputs != tuple(sorted(set(self.forbidden_inputs))):
            raise ValueError("forbidden inputs must be sorted and unique")
        if set(self.decision_inputs) & set(self.forbidden_inputs):
            raise ValueError("decision and forbidden inputs overlap")
        return self


class FrameItemV2(StrictModel):
    """One unresolved problem-aware claim selected for later human review."""

    schema_version: Literal[2] = 2
    frame_record_id: str = Field(pattern=_FRAME_RECORD_ID)
    cluster_id: str
    problem_group: str = Field(min_length=1)
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    representative_invocation_id: str
    representative_family_id: str
    representative_pool_id: str
    representative_source_proxy: str
    representative_problem_record_id: str
    contributing_invocation_ids: tuple[str, ...] = Field(min_length=1)
    contributing_problem_record_ids: tuple[str, ...] = Field(min_length=1)
    contributing_family_ids: tuple[str, ...] = Field(min_length=1)
    contributing_pool_ids: tuple[str, ...] = Field(min_length=1)
    contributing_source_proxies: tuple[str, ...] = Field(min_length=1)
    postprocess_manifest_ids: tuple[str, ...] = Field(min_length=1)
    member_count: int = Field(ge=1)
    member_count_by_family: dict[str, int] = Field(min_length=1)
    member_count_by_source_proxy: dict[str, int] = Field(min_length=1)
    terminal_artifact: v1.ArtifactBinding
    screening_artifact: v1.ArtifactBinding
    representation_artifact: v1.ArtifactBinding
    sampling_stratum: str
    sampling_rank_hash: str = Field(pattern=_HEX64)
    stratum_population_size: int = Field(ge=1)
    stratum_sample_size: int = Field(ge=1)
    inclusion_probability_numerator: int = Field(ge=1)
    inclusion_probability_denominator: int = Field(ge=1)
    same_claim: None = None
    relation: None = None
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for field_name in (
            "contributing_invocation_ids",
            "contributing_problem_record_ids",
            "contributing_family_ids",
            "contributing_pool_ids",
            "contributing_source_proxies",
            "postprocess_manifest_ids",
        ):
            values = getattr(self, field_name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{field_name} must be sorted and unique")
        if self.member_count != len(self.contributing_invocation_ids):
            raise ValueError("member count differs from invocation multiplicity")
        if sum(self.member_count_by_family.values()) != self.member_count:
            raise ValueError("family multiplicities do not reconcile")
        if sum(self.member_count_by_source_proxy.values()) != self.member_count:
            raise ValueError("source-proxy multiplicities do not reconcile")
        if self.stratum_sample_size > self.stratum_population_size:
            raise ValueError("stratum sample exceeds population")
        if (
            self.inclusion_probability_numerator != self.stratum_sample_size
            or self.inclusion_probability_denominator != self.stratum_population_size
        ):
            raise ValueError("inclusion probability must equal n_h/N_h")
        expected = "lf021_prevalence_item_v2:" + hash_canonical(
            {
                "schema": "lf021_prevalence_frame_item_v2",
                **self.model_dump(mode="json", exclude={"frame_record_id"}),
            }
        )
        if self.frame_record_id != expected:
            raise ValueError("frame record ID differs from content")
        return self


class FrameBindingV2(StrictModel):
    """Content-addressed revision-2 prevalence frame."""

    frame_id: str = Field(pattern=_FRAME_ID)
    artifact: str
    sha256: str = Field(pattern=_HEX64)
    item_count: int = Field(ge=1)
    sampling_method: Literal["problem_aware_stratified_hash_srs_without_replacement_v2"]
    propensity_definition: Literal["stratum_sample_size/stratum_population_size"]


class ExpansionDecisionV2(StrictModel):
    """Immutable problem-aware decision; never a faithfulness judgment."""

    schema_version: Literal[2] = 2
    decision_id: str = Field(pattern=_DECISION_ID)
    policy_id: Literal["lf021_problem_aware_tranche_expansion_v2"]
    policy_artifact: v1.ArtifactBinding
    base_v1_policy: v1.ArtifactBinding
    base_v1_implementation: v1.ArtifactBinding
    implementation_artifact: v1.ArtifactBinding
    observations: tuple[v1.ObservationBinding, ...]
    counts: v1.OperationalCounts
    coverage_deficits: tuple[str, ...]
    action: v1.DecisionAction
    next_tranche: v1.TrancheSpec | None
    frame: FrameBindingV2 | None
    reduced_data_ablation: bool
    reduced_data_flags: tuple[str, ...]
    decision_inputs_used: tuple[str, ...]
    forbidden_inputs_used: tuple[()] = ()
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.action is v1.DecisionAction.COLLECT_NEXT_TRANCHE:
            if self.next_tranche is None or self.frame is not None:
                raise ValueError("collection decision requires a next tranche")
        elif self.action in {
            v1.DecisionAction.FREEZE_PREFERRED_FRAME,
            v1.DecisionAction.FREEZE_REDUCED_FRAME,
        }:
            if self.next_tranche is not None or self.frame is None:
                raise ValueError("frame decision requires a frame and no next tranche")
        elif self.next_tranche is not None or self.frame is not None:
            raise ValueError("exhausted decision has neither frame nor next tranche")
        if self.forbidden_inputs_used:
            raise ValueError("semantic inputs are forbidden")
        expected = "lf021_expansion_decision_v2:" + hash_canonical(
            {
                "schema": "lf021_expansion_decision_v2",
                **self.model_dump(mode="json", exclude={"decision_id"}),
            }
        )
        if self.decision_id != expected:
            raise ValueError("decision ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class _ProblemAwareCluster:
    cluster_id: str
    problem_group: str
    alpha_identity_fingerprint: str
    representative: v1._CandidateMember
    members: tuple[v1._CandidateMember, ...]


@dataclass(frozen=True, slots=True)
class ExpansionRunV2:
    """Written decision plus optional frame and report."""

    decision: ExpansionDecisionV2
    decision_path: Path
    report_path: Path
    frame_path: Path | None


def load_amendment_v2(
    path: Path,
) -> LoadedConfig[TrancheExpansionAmendmentV2]:
    """Load the frozen narrow amendment."""

    return load_config(path, TrancheExpansionAmendmentV2)


def _resolve(repo_root: Path, artifact: str) -> Path:
    path = Path(artifact)
    resolved = (path if path.is_absolute() else repo_root / path).resolve()
    if not path.is_absolute() and not resolved.is_relative_to(repo_root.resolve()):
        raise TrancheExpansionV2Error(f"artifact escapes repository: {artifact}")
    return resolved


def _verify(repo_root: Path, binding: v1.ArtifactBinding) -> Path:
    path = _resolve(repo_root, binding.artifact)
    if not path.is_file() or hash_file(path) != binding.sha256:
        raise TrancheExpansionV2Error(f"bound artifact differs: {binding.artifact}")
    return path


def _load_problem_groups(
    *,
    repo_root: Path,
    policy: v1.TrancheExpansionPolicy,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for pool in policy.pools:
        path = _verify(repo_root, pool.records)
        count = 0
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            record = ProblemPoolRecord.model_validate_json(raw_line)
            if record.problem_record_id in result:
                raise TrancheExpansionV2Error("duplicate problem record across bound pools")
            result[record.problem_record_id] = record.problem_group
            count += 1
        if count != pool.problem_count:
            raise TrancheExpansionV2Error("problem-group mapping count differs from pool")
    return result


def _cluster_candidates(
    observations: tuple[v1.LoadedObservation, ...],
    *,
    problem_groups: dict[str, str],
    representative_hash_salt: str,
) -> tuple[_ProblemAwareCluster, ...]:
    grouped: dict[tuple[str, str], list[v1._CandidateMember]] = defaultdict(list)
    seen: set[str] = set()
    for observation in observations:
        for candidate in observation.candidates:
            if candidate.invocation_id in seen:
                raise TrancheExpansionV2Error("invocation appears in multiple tranches")
            seen.add(candidate.invocation_id)
            try:
                problem_group = problem_groups[candidate.problem_record_id]
            except KeyError as exc:
                raise TrancheExpansionV2Error(
                    "candidate problem lacks a bound problem group"
                ) from exc
            grouped[(problem_group, candidate.alpha_identity_fingerprint)].append(candidate)

    result: list[_ProblemAwareCluster] = []
    for (problem_group, alpha), raw_members in sorted(grouped.items()):
        members = tuple(sorted(raw_members, key=lambda item: item.invocation_id))
        representative = min(
            members,
            key=lambda item: (
                hash_canonical(
                    {
                        "schema": "lf021_problem_aware_representative_rank_v2",
                        "salt": representative_hash_salt,
                        "problem_group": problem_group,
                        "alpha_identity_fingerprint": alpha,
                        "invocation_id": item.invocation_id,
                    }
                ),
                item.invocation_id,
            ),
        )
        cluster_id = "candidate_cluster_v2:" + hash_canonical(
            {
                "schema": "lf021_problem_group_alpha_cluster_v2",
                "problem_group": problem_group,
                "alpha_identity_fingerprint": alpha,
            }
        )
        result.append(
            _ProblemAwareCluster(
                cluster_id=cluster_id,
                problem_group=problem_group,
                alpha_identity_fingerprint=alpha,
                representative=representative,
                members=members,
            )
        )
    return tuple(result)


def _build_frame_items(
    clusters: tuple[_ProblemAwareCluster, ...],
    *,
    target: int,
    base_policy: v1.TrancheExpansionPolicy,
    amendment: TrancheExpansionAmendmentV2,
) -> tuple[FrameItemV2, ...]:
    by_stratum: dict[str, list[_ProblemAwareCluster]] = defaultdict(list)
    for cluster in clusters:
        representative = cluster.representative
        stratum = (
            f"{representative.family_id}|{representative.pool_id}|{representative.source_proxy}"
        )
        by_stratum[stratum].append(cluster)
    sizes = dict(sorted((key, len(value)) for key, value in by_stratum.items()))
    allocation = (
        sizes
        if target == len(clusters)
        else v1._allocate_strata(
            sizes,
            target=target,
            minimum_per_stratum=base_policy.frame.minimum_per_nonempty_stratum,
        )
    )

    selected: list[FrameItemV2] = []
    for stratum in sorted(by_stratum):
        ranked = sorted(
            by_stratum[stratum],
            key=lambda cluster: (
                hash_canonical(
                    {
                        "schema": "lf021_prevalence_sampling_rank_v2",
                        "salt": amendment.sampling_hash_salt,
                        "cluster_id": cluster.cluster_id,
                    }
                ),
                cluster.cluster_id,
            ),
        )
        n_h = allocation[stratum]
        n_population = sizes[stratum]
        for cluster in ranked[:n_h]:
            representative = cluster.representative
            rank_hash = hash_canonical(
                {
                    "schema": "lf021_prevalence_sampling_rank_v2",
                    "salt": amendment.sampling_hash_salt,
                    "cluster_id": cluster.cluster_id,
                }
            )
            family_counts = Counter(member.family_id for member in cluster.members)
            proxy_counts = Counter(member.source_proxy for member in cluster.members)
            payload: dict[str, Any] = {
                "schema_version": 2,
                "cluster_id": cluster.cluster_id,
                "problem_group": cluster.problem_group,
                "alpha_identity_fingerprint": cluster.alpha_identity_fingerprint,
                "representative_invocation_id": representative.invocation_id,
                "representative_family_id": representative.family_id,
                "representative_pool_id": representative.pool_id,
                "representative_source_proxy": representative.source_proxy,
                "representative_problem_record_id": representative.problem_record_id,
                "contributing_invocation_ids": tuple(
                    sorted(member.invocation_id for member in cluster.members)
                ),
                "contributing_problem_record_ids": tuple(
                    sorted({member.problem_record_id for member in cluster.members})
                ),
                "contributing_family_ids": tuple(
                    sorted({member.family_id for member in cluster.members})
                ),
                "contributing_pool_ids": tuple(
                    sorted({member.pool_id for member in cluster.members})
                ),
                "contributing_source_proxies": tuple(
                    sorted({member.source_proxy for member in cluster.members})
                ),
                "postprocess_manifest_ids": tuple(
                    sorted({member.postprocess_manifest_id for member in cluster.members})
                ),
                "member_count": len(cluster.members),
                "member_count_by_family": dict(sorted(family_counts.items())),
                "member_count_by_source_proxy": dict(sorted(proxy_counts.items())),
                "terminal_artifact": representative.terminal_artifact.model_dump(mode="json"),
                "screening_artifact": representative.screening_artifact.model_dump(mode="json"),
                "representation_artifact": representative.representation_artifact.model_dump(
                    mode="json"
                ),
                "sampling_stratum": stratum,
                "sampling_rank_hash": rank_hash,
                "stratum_population_size": n_population,
                "stratum_sample_size": n_h,
                "inclusion_probability_numerator": n_h,
                "inclusion_probability_denominator": n_population,
                "same_claim": None,
                "relation": None,
                "semantic_labels_created": False,
                "supervision_eligible": False,
                "gate_5g_credit_claimed": False,
                "gate_5_closed": False,
            }
            record_id = "lf021_prevalence_item_v2:" + hash_canonical(
                {"schema": "lf021_prevalence_frame_item_v2", **payload}
            )
            selected.append(FrameItemV2.model_validate({"frame_record_id": record_id, **payload}))
    result = tuple(sorted(selected, key=lambda item: item.frame_record_id))
    if len(result) != target:
        raise TrancheExpansionV2Error("selected frame size differs from target")
    return result


def _jsonl_bytes(records: tuple[FrameItemV2, ...]) -> bytes:
    return b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )


def evaluate_tranche_expansion_v2(
    *,
    repo_root: Path,
    loaded_amendment: LoadedConfig[TrancheExpansionAmendmentV2],
    observed_manifests: tuple[Path, ...],
) -> tuple[ExpansionDecisionV2, bytes | None]:
    """Evaluate a complete immutable prefix under problem-aware identity."""

    amendment = loaded_amendment.config
    base_policy_path = _verify(repo_root, amendment.base_v1_policy)
    base_implementation_path = _verify(repo_root, amendment.base_v1_implementation)
    if base_implementation_path.resolve() != Path(v1.__file__).resolve():
        raise TrancheExpansionV2Error("bound v1 implementation is not the imported module")
    loaded_base = v1.load_tranche_expansion_policy(base_policy_path)
    base_policy = loaded_base.config
    if len(observed_manifests) > len(base_policy.tranches):
        raise TrancheExpansionV2Error("observation prefix exceeds frozen sequence")

    observations = tuple(
        v1.load_postprocess_observation(
            repo_root=repo_root,
            policy=base_policy,
            tranche=base_policy.tranches[index],
            manifest_path=path,
        )
        for index, path in enumerate(observed_manifests)
    )
    if len({item.binding.manifest_id for item in observations}) != len(observations):
        raise TrancheExpansionV2Error("postprocess manifest reused across tranches")
    problem_groups = _load_problem_groups(repo_root=repo_root, policy=base_policy)
    clusters = _cluster_candidates(
        observations,
        problem_groups=problem_groups,
        representative_hash_salt=amendment.representative_hash_salt,
    )
    counts = v1._operational_counts(
        policy=base_policy,
        observations=observations,
        clusters=cast(Any, clusters),
    )
    deficits = v1._coverage_deficits(base_policy, counts)
    mandatory_count = sum(item.mandatory_before_stopping for item in base_policy.tranches)
    preferred_ready = (
        len(observations) >= mandatory_count
        and counts.unique_compiling_count >= base_policy.frame.preferred_size
        and not deficits
    )

    frame_bytes: bytes | None = None
    frame_binding: FrameBindingV2 | None = None
    next_tranche: v1.TrancheSpec | None = None
    flags: list[str] = []
    reduced = False
    if preferred_ready:
        action = v1.DecisionAction.FREEZE_PREFERRED_FRAME
        frame_items = _build_frame_items(
            clusters,
            target=base_policy.frame.preferred_size,
            base_policy=base_policy,
            amendment=amendment,
        )
        frame_bytes = _jsonl_bytes(frame_items)
    elif len(observations) < len(base_policy.tranches):
        action = v1.DecisionAction.COLLECT_NEXT_TRANCHE
        next_tranche = base_policy.tranches[len(observations)]
    elif counts.unique_compiling_count >= base_policy.frame.minimum_size:
        action = v1.DecisionAction.FREEZE_REDUCED_FRAME
        reduced = True
        target = min(base_policy.frame.preferred_size, counts.unique_compiling_count)
        if counts.unique_compiling_count < base_policy.frame.preferred_size:
            flags.append(
                "preferred_frame_shortfall:"
                f"{counts.unique_compiling_count}<{base_policy.frame.preferred_size}"
            )
        flags.extend(f"coverage_deficit:{item}" for item in deficits)
        flags.append("preregistered_tranches_exhausted")
        frame_items = _build_frame_items(
            clusters,
            target=target,
            base_policy=base_policy,
            amendment=amendment,
        )
        frame_bytes = _jsonl_bytes(frame_items)
    else:
        action = v1.DecisionAction.EXHAUSTED_WITHOUT_FRAME
        reduced = True
        flags.extend(f"coverage_deficit:{item}" for item in deficits)
        flags.extend(
            (
                "minimum_frame_shortfall:"
                f"{counts.unique_compiling_count}<{base_policy.frame.minimum_size}",
                "preregistered_tranches_exhausted",
            )
        )

    implementation_path = Path(__file__).resolve()
    if frame_bytes is not None:
        frame_sha = sha256_hex(frame_bytes)
        frame_id = "lf021_prevalence_frame_v2:" + hash_canonical(
            {
                "schema": "lf021_prevalence_frame_v2",
                "amendment_config_hash": loaded_amendment.config_hash,
                "implementation_sha256": hash_file(implementation_path),
                "observation_manifest_ids": [item.binding.manifest_id for item in observations],
                "frame_sha256": frame_sha,
                "item_count": len(frame_bytes.splitlines()),
            }
        )
        frame_binding = FrameBindingV2(
            frame_id=frame_id,
            artifact=f"frames/{frame_id.rsplit(':', 1)[-1]}.jsonl",
            sha256=frame_sha,
            item_count=len(frame_bytes.splitlines()),
            sampling_method="problem_aware_stratified_hash_srs_without_replacement_v2",
            propensity_definition="stratum_sample_size/stratum_population_size",
        )

    policy_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, loaded_amendment.path),
        sha256=hash_file(loaded_amendment.path),
    )
    implementation_binding = v1.ArtifactBinding(
        artifact=v1._relative_or_absolute(repo_root, implementation_path),
        sha256=hash_file(implementation_path),
    )
    payload: dict[str, Any] = {
        "schema_version": 2,
        "policy_id": amendment.policy_id,
        "policy_artifact": policy_binding.model_dump(mode="json"),
        "base_v1_policy": amendment.base_v1_policy.model_dump(mode="json"),
        "base_v1_implementation": amendment.base_v1_implementation.model_dump(mode="json"),
        "implementation_artifact": implementation_binding.model_dump(mode="json"),
        "observations": tuple(item.binding.model_dump(mode="json") for item in observations),
        "counts": counts.model_dump(mode="json"),
        "coverage_deficits": deficits,
        "action": action.value,
        "next_tranche": (
            next_tranche.model_dump(mode="json") if next_tranche is not None else None
        ),
        "frame": frame_binding.model_dump(mode="json") if frame_binding is not None else None,
        "reduced_data_ablation": reduced,
        "reduced_data_flags": tuple(sorted(set(flags))),
        "decision_inputs_used": amendment.decision_inputs,
        "forbidden_inputs_used": (),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    decision_id = "lf021_expansion_decision_v2:" + hash_canonical(
        {"schema": "lf021_expansion_decision_v2", **payload}
    )
    return (
        ExpansionDecisionV2.model_validate({"decision_id": decision_id, **payload}),
        frame_bytes,
    )


def _render_report(decision: ExpansionDecisionV2) -> str:
    next_id = decision.next_tranche.tranche_id if decision.next_tranche else "none"
    frame_id = decision.frame.frame_id if decision.frame else "none"
    lines = [
        "# LF-021 problem-aware tranche decision v2",
        "",
        f"- Decision: `{decision.decision_id}`",
        f"- Action: `{decision.action.value}`",
        f"- Observed tranches: {decision.counts.observed_tranche_count}",
        f"- Next tranche: `{next_id}`",
        f"- Benchmark-clear candidate members: {decision.counts.benchmark_clear_compile_count}",
        f"- Problem-group x alpha units: `{decision.counts.unique_compiling_count}`",
        f"- Duplicate members retained as multiplicity: `{decision.counts.duplicate_member_count}`",
        f"- Frozen frame: `{frame_id}`",
        "- Semantic labels inspected: `false`",
        "- Semantic labels created: `false`",
        "- Gate 5G credit claimed: `false`",
        "- Gate 5 closed: `false`",
        "",
        "This is a pre-label operational decision. Compilation is not faithfulness.",
        "",
    ]
    return "\n".join(lines)


def run_tranche_expansion_v2(
    *,
    repo_root: Path,
    policy_path: Path,
    observed_manifests: tuple[Path, ...],
    output_root: Path,
) -> ExpansionRunV2:
    """Evaluate and persist immutable revision-2 decision artifacts."""

    loaded = load_amendment_v2(policy_path)
    decision, frame_bytes = evaluate_tranche_expansion_v2(
        repo_root=repo_root,
        loaded_amendment=loaded,
        observed_manifests=observed_manifests,
    )
    suffix = decision.decision_id.rsplit(":", 1)[-1]
    decision_path = output_root / "decisions" / f"{suffix}.json"
    report_path = output_root / "decisions" / f"{suffix}.md"
    frame_path: Path | None = None
    if decision.frame is not None:
        if frame_bytes is None:
            raise TrancheExpansionV2Error("frame binding exists without bytes")
        frame_path = output_root / decision.frame.artifact
        if sha256_hex(frame_bytes) != decision.frame.sha256:
            raise TrancheExpansionV2Error("frame bytes changed before persistence")
        v1._write_immutable(frame_path, frame_bytes)
    v1._write_immutable(
        decision_path,
        canonical_json_bytes(decision.model_dump(mode="json")),
    )
    v1._write_immutable(report_path, _render_report(decision).encode("utf-8"))
    return ExpansionRunV2(
        decision=decision,
        decision_path=decision_path,
        report_path=report_path,
        frame_path=frame_path,
    )


__all__ = [
    "ExpansionDecisionV2",
    "ExpansionRunV2",
    "FrameBindingV2",
    "FrameItemV2",
    "TrancheExpansionAmendmentV2",
    "TrancheExpansionV2Error",
    "evaluate_tranche_expansion_v2",
    "load_amendment_v2",
    "run_tranche_expansion_v2",
]
