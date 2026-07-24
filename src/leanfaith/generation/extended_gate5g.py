"""Fail-closed Gate-5G integration for the truthful post-exhaustion frame.

This module is intentionally versioned apart from ``generation.gate5g``.  It
consumes the strict post-exhaustion verifier and never coerces the extended
stop, population, frame, or entropy records into their older v2/v3 schemas.
Dry runs write only a content-addressed validation report.  The canonical gate
path is reachable only through an explicit, dated finalization.
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.evaluation import prevalence_design_v3
from leanfaith.generation import gate5g as gate5g_v1
from leanfaith.generation import post_exhaustion_frame_v1 as extended_frame
from leanfaith.generation import post_exhaustion_gate5g_lineage_v1 as post_exhaustion_lineage
from leanfaith.generation import post_exhaustion_postprocess_v7 as postprocess_v7
from leanfaith.schemas.gate5g import (
    Gate5GArtifactBinding,
    Gate5GLineageManifestV1,
    Gate5GObservationBinding,
    Gate5GReFormApplicability,
    Gate5GScopeLimitations,
    Gate5GStratumAccounting,
)
from leanfaith.schemas.gate5g_v2 import (
    ExtendedGate5GAuthorizationBindingV2,
    ExtendedGate5GInputBindingsV2,
    ExtendedGate5GLineageBindingsV2,
    ExtendedGate5GReportV2,
    ExtendedGate5GValidationReportV2,
)

_HEX64 = r"^[0-9a-f]{64}$"
_DEFAULT_POLICY = Path("configs/generation/lf021_gate5g_finalizer_v2.yaml")
_DEFAULT_PREVALENCE_DESIGN = Path("policies/lf021_prevalence_design_v3.yaml")
_DRY_ROOT = Path("reports/generation/lf021_extended_gate5g_finalization_v2")
_GATE_PATH = Path("reports/gates/gate_5g.json")
_EXPECTED_FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)

EXTENDED_GATE5G_CHECKS = (
    "strict_extended_frame_replay",
    "preferred_extended_stop",
    "exact_original_plus_extension_observation_prefix",
    "reviewed_extension_authorizations",
    "complete_lineage_manifest_replay",
    "local_collection_transport_only",
    "prevalence_design_v3_and_v2_v1_lineage_bound",
    "population_manifest_and_artifact_bound",
    "production_csprng_seed_and_provenance_bound",
    "frame_hash_count_and_sampling_bound",
    "selected_artifacts_hash_bound",
    "problem_aware_unique_items",
    "family_pool_source_proxy_reconciled",
    "stratum_propensities_reconciled",
    "zero_semantic_labels",
    "zero_supervision",
    "zero_remote_provider_content",
    "coverage_and_phase_artifacts_finalized",
    "three_family_reduced_scope_explicit",
    "gate_5_remains_open",
)


class ExtendedGate5GFinalizationError(ValueError):
    """The truthful post-exhaustion Gate-5G path failed closed."""


class ExtendedGate5GFinalizerPolicyV2(StrictModel):
    """Frozen Gate-5G adapter policy for the extended frame lineage."""

    schema_version: Literal[2] = 2
    policy_id: Literal["lf021_extended_gate5g_finalizer_v2"]
    status: Literal["frozen_prelabel"]
    frame_materializer_policy: Gate5GArtifactBinding
    frame_materializer_implementation: Gate5GArtifactBinding
    prevalence_design_v3: Gate5GArtifactBinding
    prevalence_design_v3_implementation: Gate5GArtifactBinding
    lineage_builder_implementation: Gate5GArtifactBinding
    required_prevalence_design_policy_id: Literal["lf021_prevalence_design_v3"]
    required_frame_policy_id: Literal["lf021_post_exhaustion_frame_materializer_v1"]
    required_source_stop_action: Literal["preferred_eligible_stop"]
    required_action: Literal["freeze_preferred_frame"]
    required_original_observation_count: Literal[12]
    minimum_extension_observation_count: Literal[1]
    maximum_extension_observation_count: Literal[4]
    required_frame_size: Literal[240]
    required_sampling_method: Literal["problem_aware_stratified_csprng_srs_without_replacement_v2"]
    required_sampling_rank_algorithm: Literal["hmac_sha256_keyed_rank_v1"]
    allowed_production_seed_sources: tuple[
        Literal["external_randomness_beacon_256"],
        Literal["os_csprng_secrets_token_bytes_256"],
    ]
    required_scalable_family_ids: tuple[str, str, str]
    required_scope_flags: dict[str, bool]
    required_reduced_data_reasons: tuple[str, str, str]
    dry_run_output_root: Literal["reports/generation/lf021_extended_gate5g_finalization_v2"]
    canonical_gate_report: Literal["reports/gates/gate_5g.json"]
    semantic_labels_inspected_when_frozen: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    remote_provider_content_used: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.allowed_production_seed_sources != (
            "external_randomness_beacon_256",
            "os_csprng_secrets_token_bytes_256",
        ):
            raise ValueError("extended Gate-5G production seed inventory differs")
        if self.required_scalable_family_ids != _EXPECTED_FAMILIES:
            raise ValueError("extended Gate-5G family inventory differs")
        expected_flags = {
            "three_family_collection_only": True,
            "reduced_data_ablation": True,
            "confirmatory_d4_d5_eligible": False,
            "heldout_generator_claim_eligible": False,
            "supplemental_qualifications_count_for_gate_credit": False,
        }
        if self.required_scope_flags != expected_flags:
            raise ValueError("extended Gate-5G scope flags differ")
        expected_reasons = (
            "confirmatory_d4_d5_unavailable",
            "heldout_generator_claim_unavailable",
            "three_family_collection_only",
        )
        if self.required_reduced_data_reasons != expected_reasons:
            raise ValueError("extended Gate-5G reduced-data reasons differ")
        return self


class ExtendedGate5GFinalizationResult(StrictModel):
    validation_report_path: str
    validation_report_sha256: str = Field(pattern=_HEX64)
    validation_report: ExtendedGate5GValidationReportV2
    gate_report_path: str | None
    gate_report_sha256: str | None = Field(default=None, pattern=_HEX64)
    gate_report: ExtendedGate5GReportV2 | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        has_gate = self.gate_report is not None
        if has_gate != (self.gate_report_path is not None) or has_gate != (
            self.gate_report_sha256 is not None
        ):
            raise ValueError("extended Gate-5G result has a partial gate report")
        return self


def load_extended_gate5g_policy(
    path: Path,
) -> LoadedConfig[ExtendedGate5GFinalizerPolicyV2]:
    return load_config(path, ExtendedGate5GFinalizerPolicyV2)


def _binding(
    paths: RepoPaths,
    path: Path | str,
    *,
    label: str,
) -> tuple[Path, Gate5GArtifactBinding]:
    try:
        return gate5g_v1._binding(paths, path, label=label)
    except gate5g_v1.Gate5GFinalizationError as exc:
        raise ExtendedGate5GFinalizationError(str(exc)) from exc


def _verify_binding(
    paths: RepoPaths,
    binding: Any,
    *,
    label: str,
) -> Path:
    try:
        return gate5g_v1._verify_binding(
            paths,
            gate5g_v1._gate_binding(binding),
            label=label,
        )
    except (gate5g_v1.Gate5GFinalizationError, ValueError) as exc:
        raise ExtendedGate5GFinalizationError(str(exc)) from exc


def _scope(policy: ExtendedGate5GFinalizerPolicyV2) -> Gate5GScopeLimitations:
    return Gate5GScopeLimitations(
        scalable_family_ids=policy.required_scalable_family_ids,
        three_family_collection_only=True,
        reduced_data_ablation=True,
        confirmatory_d4_d5_eligible=False,
        heldout_generator_claim_eligible=False,
        supplemental_qualifications_count_for_gate_credit=False,
        reduced_data_reasons=(
            "confirmatory_d4_d5_unavailable",
            "heldout_generator_claim_unavailable",
            "three_family_collection_only",
        ),
    )


def _require_no_remote_provider_content(value: Any, *, label: str) -> None:
    """Reject non-local generation transports and external-transmission claims."""

    if isinstance(value, dict):
        for key, child in value.items():
            if key == "transport" and child != "local":
                raise ExtendedGate5GFinalizationError(
                    f"{label} contains non-local collection transport"
                )
            if (
                key
                in {
                    "external_transmission",
                    "remote_provider_content_used",
                    "sent_to_external_provider",
                }
                and child is not False
            ):
                raise ExtendedGate5GFinalizationError(
                    f"{label} claims external provider transmission"
                )
            _require_no_remote_provider_content(child, label=label)
    elif isinstance(value, list):
        for child in value:
            _require_no_remote_provider_content(child, label=label)


def _label_and_remote_scan(value: Any, *, label: str) -> None:
    try:
        gate5g_v1._require_label_blind(value, label=label)
    except gate5g_v1.Gate5GFinalizationError as exc:
        raise ExtendedGate5GFinalizationError(str(exc)) from exc
    _require_no_remote_provider_content(value, label=label)


def _verify_policy_lineage(
    *,
    paths: RepoPaths,
    policy: ExtendedGate5GFinalizerPolicyV2,
) -> None:
    frame_policy_path = _verify_binding(
        paths,
        policy.frame_materializer_policy,
        label="post-exhaustion frame policy",
    )
    frame_impl_path = _verify_binding(
        paths,
        policy.frame_materializer_implementation,
        label="post-exhaustion frame implementation",
    )
    prevalence_path = _verify_binding(
        paths,
        policy.prevalence_design_v3,
        label="prevalence design v3",
    )
    prevalence_impl_path = _verify_binding(
        paths,
        policy.prevalence_design_v3_implementation,
        label="prevalence design v3 implementation",
    )
    lineage_builder_path = _verify_binding(
        paths,
        policy.lineage_builder_implementation,
        label="post-exhaustion Gate-5G lineage builder",
    )
    if (
        frame_impl_path.resolve() != Path(extended_frame.__file__).resolve()
        or prevalence_impl_path.resolve() != Path(prevalence_design_v3.__file__).resolve()
        or lineage_builder_path.resolve() != Path(post_exhaustion_lineage.__file__).resolve()
    ):
        raise ExtendedGate5GFinalizationError(
            "extended Gate-5G bound implementation is not executing code"
        )
    loaded_frame_policy = extended_frame.load_post_exhaustion_frame_policy_v1(frame_policy_path)
    loaded_prevalence = prevalence_design_v3.load_prevalence_design_policy_v3(prevalence_path)
    base_v2 = prevalence_design_v3.verify_prevalence_design_policy_v3(
        repo_root=paths.root,
        loaded_policy=loaded_prevalence,
    )
    if (
        loaded_frame_policy.config.policy_id != policy.required_frame_policy_id
        or loaded_prevalence.config.policy_id != policy.required_prevalence_design_policy_id
        or gate5g_v1._gate_binding(loaded_frame_policy.config.prevalence_design_v3)
        != policy.prevalence_design_v3
        or gate5g_v1._gate_binding(loaded_frame_policy.config.prevalence_design_v3_implementation)
        != policy.prevalence_design_v3_implementation
        or loaded_frame_policy.config.target_frame_size != policy.required_frame_size
        or loaded_frame_policy.config.sampling_method != policy.required_sampling_method
        or loaded_frame_policy.config.sampling_rank_algorithm
        != policy.required_sampling_rank_algorithm
        or loaded_frame_policy.config.required_scalable_family_ids
        != policy.required_scalable_family_ids
        or loaded_prevalence.config.scope.required_scalable_families
        != policy.required_scalable_family_ids
        or base_v2.config.scope != loaded_prevalence.config.scope
    ):
        raise ExtendedGate5GFinalizationError("extended Gate-5G policy lineage differs")


def _verify_complete_lineage(
    *,
    paths: RepoPaths,
    lineage_manifest_path: Path,
    verified: extended_frame.VerifiedExtendedFrameV1,
    required_families: tuple[str, str, str],
) -> tuple[Gate5GLineageManifestV1, Gate5GArtifactBinding]:
    lineage_path, lineage_binding = _binding(
        paths,
        lineage_manifest_path,
        label="extended Gate-5G lineage manifest",
    )
    try:
        lineage = Gate5GLineageManifestV1.model_validate(gate5g_v1._load_json(lineage_path))
        post_exhaustion_lineage.verify_mixed_gate5g_lineage_v1(
            repo_root=paths.root,
            lineage=lineage,
            decision=cast(Any, verified.decision),
            required_families=required_families,
        )
    except (
        post_exhaustion_lineage.PostExhaustionGate5GLineageError,
        ValueError,
    ) as exc:
        raise ExtendedGate5GFinalizationError(
            f"extended Gate-5G lineage replay failed: {exc}"
        ) from exc
    if len(lineage.tranches) != len(verified.decision.observations):
        raise ExtendedGate5GFinalizationError("extended Gate-5G lineage tranche count differs")
    return lineage, lineage_binding


def _verify_selected_frame_artifacts(
    *,
    paths: RepoPaths,
    verified: extended_frame.VerifiedExtendedFrameV1,
    lineage: Gate5GLineageManifestV1,
) -> tuple[
    dict[str, int],
    dict[str, int],
    dict[str, int],
    tuple[Gate5GStratumAccounting, ...],
]:
    decision = verified.decision
    if len(verified.frame_items) != decision.frame.item_count:
        raise ExtendedGate5GFinalizationError("extended frame row count differs")
    lineage_manifests = {item.postprocess_manifest.manifest_id for item in lineage.tranches}
    seen_units: set[tuple[str, str]] = set()
    family_counts: Counter[str] = Counter()
    pool_counts: Counter[str] = Counter()
    proxy_counts: Counter[str] = Counter()
    strata: dict[str, list[extended_frame.ExtendedFrameItemV1]] = defaultdict(list)
    scanned_artifacts: set[tuple[str, str]] = set()

    for row in verified.frame_items:
        raw = row.model_dump(mode="json")
        _label_and_remote_scan(raw, label="extended Gate-5G frame item")
        item = row.population_item
        if (
            row.population_manifest_id != decision.population_id
            or row.population_manifest != decision.population_manifest
            or row.sampling_method != decision.sampling_method
            or row.sampling_rank_algorithm != decision.sampling_rank_algorithm
            or row.sampling_seed_sha256 != decision.sampling_seed_sha256
            or row.sampling_seed_provenance != decision.sampling_seed_provenance
            or row.test_replay_only
            or not set(item.postprocess_manifest_ids).issubset(lineage_manifests)
        ):
            raise ExtendedGate5GFinalizationError(
                "extended frame item differs from verified decision/lineage"
            )
        unit = (item.problem_group, item.alpha_identity_fingerprint)
        if unit in seen_units:
            raise ExtendedGate5GFinalizationError(
                "extended frame repeats a problem-aware claim unit"
            )
        seen_units.add(unit)

        terminal_path = _verify_binding(
            paths,
            item.terminal_artifact,
            label="selected extended postprocess terminal",
        )
        screening_path = _verify_binding(
            paths,
            item.screening_artifact,
            label="selected extended benchmark screening",
        )
        representation_path = _verify_binding(
            paths,
            item.representation_artifact,
            label="selected extended representation",
        )
        terminal_raw = gate5g_v1._load_json(terminal_path)
        screening_raw = gate5g_v1._load_json(screening_path)
        representation_raw = gate5g_v1._load_json(representation_path)
        try:
            terminal = gate5g_v1._PostprocessTerminalProjection.model_validate(terminal_raw)
            screening = gate5g_v1._ScreeningProjection.model_validate(screening_raw)
            representation = gate5g_v1._RepresentationProjection.model_validate(representation_raw)
        except ValueError as exc:
            raise ExtendedGate5GFinalizationError(
                "selected extended artifact projection is invalid"
            ) from exc
        if (
            terminal.invocation_id != item.representative_invocation_id
            or terminal.family_id != item.representative_family_id
            or terminal.problem_record_id != item.representative_problem_record_id
            or screening.problem_record_id != item.representative_problem_record_id
            or screening.candidate_theorem_id != terminal.candidate_theorem_id
            or representation.theorem_id != terminal.candidate_theorem_id
            or screening.alpha_identity_fingerprint != item.alpha_identity_fingerprint
            or representation.alpha_identity_fingerprint != item.alpha_identity_fingerprint
            or terminal.output_artifact_hashes.get(item.screening_artifact.artifact)
            != item.screening_artifact.sha256
            or terminal.output_artifact_hashes.get(item.representation_artifact.artifact)
            != item.representation_artifact.sha256
        ):
            raise ExtendedGate5GFinalizationError("selected extended artifact projections disagree")
        for artifact_path, artifact_raw, label in (
            (terminal_path, terminal_raw, "selected extended terminal"),
            (screening_path, screening_raw, "selected extended screening"),
            (
                representation_path,
                representation_raw,
                "selected extended representation",
            ),
        ):
            key = (str(artifact_path), hash_file(artifact_path))
            if key not in scanned_artifacts:
                _label_and_remote_scan(artifact_raw, label=label)
                try:
                    gate5g_v1._verify_embedded_path_hashes(
                        paths,
                        artifact_raw,
                        label=label,
                    )
                except gate5g_v1.Gate5GFinalizationError as exc:
                    raise ExtendedGate5GFinalizationError(str(exc)) from exc
                scanned_artifacts.add(key)

        for member in item.members:
            for binding, label in (
                (member.terminal_artifact, "extended member terminal"),
                (member.screening_artifact, "extended member screening"),
                (member.representation_artifact, "extended member representation"),
            ):
                member_path = _verify_binding(paths, binding, label=label)
                key = (str(member_path), binding.sha256)
                if key not in scanned_artifacts:
                    member_raw = gate5g_v1._load_json(member_path)
                    _label_and_remote_scan(member_raw, label=label)
                    scanned_artifacts.add(key)

        family_counts[item.representative_family_id] += 1
        pool_counts[item.representative_pool_id] += 1
        proxy_counts[item.representative_source_proxy] += 1
        strata[row.sampling_stratum].append(row)

    accounting: list[Gate5GStratumAccounting] = []
    for stratum, rows in sorted(strata.items()):
        n_h = rows[0].stratum_sample_size
        population = rows[0].stratum_population_size
        if len(rows) != n_h or any(
            item.stratum_sample_size != n_h
            or item.stratum_population_size != population
            or item.inclusion_probability_numerator != n_h
            or item.inclusion_probability_denominator != population
            for item in rows
        ):
            raise ExtendedGate5GFinalizationError(
                f"extended stratum {stratum} propensity accounting differs"
            )
        accounting.append(
            Gate5GStratumAccounting(
                stratum=stratum,
                population_size=population,
                sample_size=n_h,
            )
        )
    if (
        dict(sorted((key, len(value)) for key, value in strata.items()))
        != decision.stratum_sample_sizes
        or {key: value[0].stratum_population_size for key, value in sorted(strata.items())}
        != decision.stratum_population_sizes
    ):
        raise ExtendedGate5GFinalizationError(
            "extended frame stratum accounting differs from decision"
        )
    return (
        dict(sorted(family_counts.items())),
        dict(sorted(pool_counts.items())),
        dict(sorted(proxy_counts.items())),
        tuple(accounting),
    )


def _authorization_bindings(
    verified: extended_frame.VerifiedExtendedFrameV1,
) -> tuple[ExtendedGate5GAuthorizationBindingV2, ...]:
    return tuple(
        ExtendedGate5GAuthorizationBindingV2(
            authorization_id=record.authorization_id,
            authorization=gate5g_v1._gate_binding(binding),
            extension_decision_id=record.extension_decision_id,
            extension_decision=gate5g_v1._gate_binding(record.extension_decision),
            authorized_tranche_id=record.authorized_tranche.tranche_id,
            authorized_tranche_order=record.authorized_tranche.order,
        )
        for record, binding in zip(
            verified.collection_authorizations.records,
            verified.collection_authorizations.bindings,
            strict=True,
        )
    )


def _verify_authorizations_are_local(
    paths: RepoPaths,
    verified: extended_frame.VerifiedExtendedFrameV1,
) -> None:
    records = verified.collection_authorizations.records
    bindings = verified.collection_authorizations.bindings
    observations = verified.verified_stop.decision.extension_observations
    if not (len(records) == len(bindings) == len(observations)):
        raise ExtendedGate5GFinalizationError(
            "reviewed extension authorization and observation counts differ"
        )
    for record, binding, observation in zip(
        records,
        bindings,
        observations,
        strict=True,
    ):
        raw = record.model_dump(mode="json")
        _label_and_remote_scan(raw, label="reviewed extension authorization")
        if not record.scientific_tranche_authorized or any(
            item.transport != "local" for item in record.family_pins
        ):
            raise ExtendedGate5GFinalizationError(
                "reviewed extension authorization is not a local scientific authorization"
            )
        observation_binding = Gate5GArtifactBinding(
            artifact=observation.postprocess_manifest.artifact,
            sha256=observation.postprocess_manifest.sha256,
        )
        try:
            observation_path = _verify_binding(
                paths,
                observation_binding,
                label="collector-v6/postprocess-v7 extension observation",
            )
            manifest = postprocess_v7.PostExhaustionPostprocessManifestV7.model_validate(
                gate5g_v1._load_json(observation_path)
            )
            execution_config_path = _verify_binding(
                paths,
                Gate5GArtifactBinding(
                    artifact=manifest.input_binding.execution_config.artifact,
                    sha256=manifest.input_binding.execution_config.sha256,
                ),
                label="post-exhaustion execution config",
            )
            collection_manifest_path = _verify_binding(
                paths,
                Gate5GArtifactBinding(
                    artifact=manifest.input_binding.collection_manifest.artifact,
                    sha256=manifest.input_binding.collection_manifest.sha256,
                ),
                label="collector-v6 manifest",
            )
            loaded = postprocess_v7.load_post_exhaustion_postprocess_v7(
                repo_root=paths.root,
                collection_root=collection_manifest_path.parent,
                collection_config_path=execution_config_path,
                output_root=observation_path.parent,
            )
            replayed = postprocess_v7.verify_post_exhaustion_postprocess_v7(loaded)
        except (
            OSError,
            ValueError,
            postprocess_v7.PostExhaustionPostprocessV7Error,
        ) as exc:
            raise ExtendedGate5GFinalizationError(
                "reviewed collector-v6/postprocess-v7 execution evidence is unavailable"
            ) from exc
        if (
            replayed != manifest
            or manifest.manifest_id != observation.manifest_id
            or manifest.schema_version != observation.postprocess_schema_version
            or manifest.tranche_id != record.authorized_tranche.tranche_id
            or manifest.tranche_order != record.authorized_tranche.order
            or manifest.input_binding.extension_authorization_id != record.authorization_id
            or (
                manifest.input_binding.extension_authorization.artifact,
                manifest.input_binding.extension_authorization.sha256,
            )
            != (binding.artifact, binding.sha256)
            or manifest.input_binding.extension_decision_id != record.extension_decision_id
            or (
                manifest.input_binding.extension_decision.artifact,
                manifest.input_binding.extension_decision.sha256,
            )
            != (
                record.extension_decision.artifact,
                record.extension_decision.sha256,
            )
            or manifest.input_binding.family_ids != _EXPECTED_FAMILIES
            or manifest.semantic_labels_inspected
            or manifest.semantic_labels_created
            or manifest.supervision_eligible
            or manifest.gate_5g_credit_claimed
            or manifest.gate_5_closed
        ):
            raise ExtendedGate5GFinalizationError(
                "collector-v6/postprocess-v7 evidence differs from reviewed authorization"
            )


def _text_bindings(
    *,
    path: Path,
    label: str,
    required_literals: tuple[str, ...],
) -> None:
    try:
        gate5g_v1._require_text_bindings(
            path,
            label=label,
            required_literals=required_literals,
        )
    except gate5g_v1.Gate5GFinalizationError as exc:
        raise ExtendedGate5GFinalizationError(str(exc)) from exc


def validate_or_finalize_extended_gate5g(
    *,
    paths: RepoPaths,
    frame_freeze_decision_path: Path,
    collection_authorization_paths: tuple[Path, ...],
    lineage_manifest_path: Path,
    coverage_report_path: Path,
    phase_milestone_path: Path,
    prevalence_design_policy_path: Path | None = None,
    policy_path: Path | None = None,
    finalize: bool = False,
    finalized_date: datetime.date | None = None,
) -> ExtendedGate5GFinalizationResult:
    """Verify the complete extended frame and optionally close Gate 5G."""

    if finalize != (finalized_date is not None):
        raise ExtendedGate5GFinalizationError(
            "explicit finalize mode requires a date, and dry-run forbids it"
        )
    policy_path = policy_path or paths.root / _DEFAULT_POLICY
    loaded_policy = load_extended_gate5g_policy(policy_path)
    policy = loaded_policy.config
    policy_file, policy_binding = _binding(
        paths,
        loaded_policy.path,
        label="extended Gate-5G finalizer policy",
    )
    implementation_path = paths.root / "src/leanfaith/generation/extended_gate5g.py"
    implementation_file, implementation_binding = _binding(
        paths,
        implementation_path,
        label="extended Gate-5G finalizer implementation",
    )
    if (
        hash_file(implementation_file) != hash_file(Path(__file__).resolve())
        or implementation_file.resolve() != Path(__file__).resolve()
    ):
        raise ExtendedGate5GFinalizationError(
            "repository extended Gate-5G implementation differs from executing code"
        )
    _verify_policy_lineage(paths=paths, policy=policy)

    prevalence_path = prevalence_design_policy_path or paths.root / _DEFAULT_PREVALENCE_DESIGN
    loaded_prevalence = prevalence_design_v3.load_prevalence_design_policy_v3(prevalence_path)
    try:
        base_v2 = prevalence_design_v3.verify_prevalence_design_policy_v3(
            repo_root=paths.root,
            loaded_policy=loaded_prevalence,
        )
    except (OSError, ValueError) as exc:
        raise ExtendedGate5GFinalizationError(f"invalid prevalence design v3: {exc}") from exc
    _, prevalence_binding = _binding(
        paths,
        loaded_prevalence.path,
        label="prevalence design v3",
    )
    if prevalence_binding != policy.prevalence_design_v3:
        raise ExtendedGate5GFinalizationError(
            "runtime prevalence design differs from finalizer policy"
        )
    _, prevalence_v2_binding = _binding(
        paths,
        base_v2.path,
        label="prevalence design v2",
    )
    prevalence_v1_binding = gate5g_v1._gate_binding(base_v2.config.base_v1_design)
    _verify_binding(paths, prevalence_v1_binding, label="prevalence design v1")

    frame_policy_path = _verify_binding(
        paths,
        policy.frame_materializer_policy,
        label="post-exhaustion frame policy",
    )
    try:
        verified = extended_frame.verify_extended_frame_freeze_v1(
            repo_root=paths.root,
            policy_path=frame_policy_path,
            decision_path=frame_freeze_decision_path,
        )
    except (extended_frame.PostExhaustionFrameError, OSError, ValueError) as exc:
        raise ExtendedGate5GFinalizationError(
            f"strict extended-frame replay failed: {exc}"
        ) from exc
    supplied_authorizations = tuple(path.resolve() for path in collection_authorization_paths)
    verified_authorizations = tuple(
        path.resolve() for path in verified.collection_authorizations.paths
    )
    if supplied_authorizations != verified_authorizations:
        raise ExtendedGate5GFinalizationError(
            "supplied authorization paths differ from strict extended-frame lineage"
        )
    decision = verified.decision
    if (
        decision.policy_id != policy.required_frame_policy_id
        or decision.source_stop_action != policy.required_source_stop_action
        or decision.action != policy.required_action
        or decision.next_tranche is not None
        or decision.coverage_deficits
        or decision.original_observation_count != policy.required_original_observation_count
        or not (
            policy.minimum_extension_observation_count
            <= decision.extension_observation_count
            <= policy.maximum_extension_observation_count
        )
        or decision.frame.item_count != policy.required_frame_size
        or decision.sampling_method != policy.required_sampling_method
        or decision.sampling_rank_algorithm != policy.required_sampling_rank_algorithm
        or decision.representative_family_ids != policy.required_scalable_family_ids
    ):
        raise ExtendedGate5GFinalizationError(
            "extended frame decision differs from finalizer policy"
        )
    if (
        decision.test_replay_only
        or decision.frame.test_replay_only
        or verified.seed_provenance.test_replay_only
        or verified.seed_provenance.source not in policy.allowed_production_seed_sources
    ):
        raise ExtendedGate5GFinalizationError(
            "test/replay entropy cannot receive extended Gate-5G credit"
        )
    _verify_authorizations_are_local(paths, verified)
    _label_and_remote_scan(
        decision.model_dump(mode="json"),
        label="extended frame decision",
    )
    _label_and_remote_scan(
        verified.population.manifest.model_dump(mode="json"),
        label="extended population manifest",
    )
    for item in verified.population.items:
        _label_and_remote_scan(
            item.model_dump(mode="json"),
            label="extended population item",
        )

    lineage, lineage_binding = _verify_complete_lineage(
        paths=paths,
        lineage_manifest_path=lineage_manifest_path,
        verified=verified,
        required_families=policy.required_scalable_family_ids,
    )
    family_counts, pool_counts, proxy_counts, strata = _verify_selected_frame_artifacts(
        paths=paths,
        verified=verified,
        lineage=lineage,
    )
    if (
        tuple(family_counts) != policy.required_scalable_family_ids
        or sum(family_counts.values()) != policy.required_frame_size
        or sum(pool_counts.values()) != policy.required_frame_size
        or sum(proxy_counts.values()) != policy.required_frame_size
    ):
        raise ExtendedGate5GFinalizationError("extended frame family/pool/source counts differ")

    seed_path = _verify_binding(
        paths,
        verified.seed_provenance.sampling_seed,
        label="extended archived sampling seed",
    )
    if (
        len(verified.seed_bytes) != 32
        or verified.seed_bytes != seed_path.read_bytes()
        or sha256_hex(verified.seed_bytes) != verified.seed_provenance.sampling_seed_sha256
        or verified.seed_provenance.sampling_seed_sha256 != decision.sampling_seed_sha256
    ):
        raise ExtendedGate5GFinalizationError("extended production sampling seed differs")
    _, seed_lock_binding = _binding(
        paths,
        verified.seed_lock_path,
        label="extended population-bound seed lock",
    )
    beacon_binding: Gate5GArtifactBinding | None = None
    if verified.seed_provenance.external_beacon_provenance is not None:
        _verify_binding(
            paths,
            verified.seed_provenance.external_beacon_provenance,
            label="extended external randomness beacon",
        )
        beacon_binding = gate5g_v1._gate_binding(
            verified.seed_provenance.external_beacon_provenance
        )

    decision_file, decision_binding = _binding(
        paths,
        verified.decision_path,
        label="extended frame-freeze decision",
    )
    if decision_file.resolve() != frame_freeze_decision_path.resolve():
        raise ExtendedGate5GFinalizationError(
            "strict verifier returned a different extended decision"
        )
    population_manifest_binding = gate5g_v1._gate_binding(decision.population_manifest)
    population_binding = gate5g_v1._gate_binding(decision.population_artifact)
    frame_binding = Gate5GArtifactBinding(
        artifact=decision.frame.artifact,
        sha256=decision.frame.sha256,
    )
    seed_provenance_binding = gate5g_v1._gate_binding(decision.sampling_seed_provenance)
    for binding, label in (
        (population_manifest_binding, "extended population manifest"),
        (population_binding, "extended population artifact"),
        (frame_binding, "extended prevalence frame"),
        (seed_provenance_binding, "extended sampling-seed provenance"),
    ):
        _verify_binding(paths, binding, label=label)

    authorizations = _authorization_bindings(verified)
    original = verified.verified_stop.verified_original_exhaustion
    activation_binding = gate5g_v1._gate_binding(decision.activation_v2_decision)
    extension_binding = gate5g_v1._gate_binding(decision.extension_stop_decision)
    if (
        original.decision_binding != decision.activation_v2_decision
        or verified.verified_stop.decision_binding != decision.extension_stop_decision
        or verified.collection_authorizations.bindings != decision.collection_authorizations
        or tuple(item.authorization_id for item in authorizations)
        != decision.collection_authorization_ids
    ):
        raise ExtendedGate5GFinalizationError(
            "extended decision lineage bindings differ from strict replay"
        )
    frame_policy = extended_frame.load_post_exhaustion_frame_policy_v1(frame_policy_path).config
    lineage_bindings = ExtendedGate5GLineageBindingsV2(
        activation_v2_decision_id=decision.activation_v2_decision_id,
        activation_v2_decision=activation_binding,
        extension_stop_decision_id=decision.extension_stop_decision_id,
        extension_stop_decision=extension_binding,
        extension_policy=gate5g_v1._gate_binding(decision.extension_policy),
        extension_implementation=gate5g_v1._gate_binding(decision.extension_implementation),
        collection_authorization_policy=gate5g_v1._gate_binding(
            frame_policy.collection_authorization_policy
        ),
        collection_authorization_implementation=gate5g_v1._gate_binding(
            frame_policy.collection_authorization_implementation
        ),
        original_observation_count=decision.original_observation_count,
        extension_observation_count=decision.extension_observation_count,
        observations=tuple(
            Gate5GObservationBinding(
                artifact=item.postprocess_manifest.artifact,
                sha256=item.postprocess_manifest.sha256,
                manifest_id=item.manifest_id,
                tranche_id=item.tranche_id,
            )
            for item in decision.observations
        ),
        authorizations=authorizations,
        lineage_manifest_id=lineage.manifest_id,
        lineage_manifest=lineage_binding,
    )

    coverage_file, coverage_binding = _binding(
        paths,
        coverage_report_path,
        label="extended generation coverage report",
    )
    phase_file, phase_binding = _binding(
        paths,
        phase_milestone_path,
        label="extended Phase-5 milestone",
    )
    coverage_literals = (
        loaded_prevalence.config.policy_id,
        prevalence_binding.sha256,
        prevalence_v2_binding.sha256,
        prevalence_v1_binding.sha256,
        decision.decision_id,
        decision_binding.sha256,
        decision.extension_stop_decision_id,
        extension_binding.sha256,
        decision.activation_v2_decision_id,
        activation_binding.sha256,
        decision.population_id,
        population_manifest_binding.sha256,
        population_binding.sha256,
        decision.frame.frame_id,
        frame_binding.sha256,
        decision.sampling_seed_sha256,
        seed_provenance_binding.sha256,
        verified.seed_provenance.sampling_seed.sha256,
        seed_lock_binding.sha256,
        lineage.manifest_id,
        lineage_binding.sha256,
        *(item.authorization_id for item in authorizations),
        *(item.authorization.sha256 for item in authorizations),
        *policy.required_scalable_family_ids,
        "three_family_collection_only",
        "Gate 5 remains open",
    ) + ((beacon_binding.sha256,) if beacon_binding is not None else ())
    _text_bindings(
        path=coverage_file,
        label="extended generation coverage report",
        required_literals=coverage_literals,
    )
    _text_bindings(
        path=phase_file,
        label="extended Phase-5 milestone",
        required_literals=(
            *coverage_literals,
            coverage_binding.sha256,
            "Gate 5G",
            "ready to finalize",
        ),
    )

    checks: dict[str, Literal[True]] = dict.fromkeys(
        EXTENDED_GATE5G_CHECKS,
        True,
    )
    scope = _scope(policy)
    reform = Gate5GReFormApplicability(
        applicable=False,
        status="not_applicable",
        reason="none of the three scalable extended Gate-5G families is ReForm",
        overlap_report=None,
    )
    inputs = ExtendedGate5GInputBindingsV2(
        policy=policy_binding,
        implementation=implementation_binding,
        prevalence_design_v3=prevalence_binding,
        prevalence_design_v2=prevalence_v2_binding,
        prevalence_design_v1=prevalence_v1_binding,
        prevalence_design_v3_implementation=policy.prevalence_design_v3_implementation,
        frame_freeze_decision=decision_binding,
        frame_materializer_policy=policy.frame_materializer_policy,
        frame_materializer_implementation=policy.frame_materializer_implementation,
        lineage=lineage_bindings,
        population_manifest=population_manifest_binding,
        population_artifact=population_binding,
        frame=frame_binding,
        sampling_seed_provenance=seed_provenance_binding,
        sampling_seed=gate5g_v1._gate_binding(verified.seed_provenance.sampling_seed),
        sampling_seed_lock=seed_lock_binding,
        external_beacon_provenance=beacon_binding,
        coverage_report=coverage_binding,
        phase_milestone=phase_binding,
    )
    payload: dict[str, Any] = {
        "schema_version": 2,
        "validation_status": "ready_to_finalize",
        "input_bindings": inputs.model_dump(mode="json"),
        "prevalence_design_policy_id": loaded_prevalence.config.policy_id,
        "frame_freeze_decision_id": decision.decision_id,
        "frame_id": decision.frame.frame_id,
        "frame_item_count": policy.required_frame_size,
        "sampling_method": decision.sampling_method,
        "sampling_seed_sha256": decision.sampling_seed_sha256,
        "sampling_seed_source": verified.seed_provenance.source,
        "original_observation_count": decision.original_observation_count,
        "extension_observation_count": decision.extension_observation_count,
        "observed_tranche_count": len(decision.observations),
        "scalable_family_ids": policy.required_scalable_family_ids,
        "pool_ids": lineage.pool_ids,
        "source_proxies": lineage.source_proxies,
        "family_item_counts": family_counts,
        "pool_item_counts": pool_counts,
        "source_proxy_item_counts": proxy_counts,
        "strata": tuple(item.model_dump(mode="json") for item in strata),
        "scope_limitations": scope.model_dump(mode="json"),
        "reform_applicability": reform.model_dump(mode="json"),
        "completed_checks": checks,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "remote_provider_content_used": False,
        "gate_5g_closed": False,
        "gate_5_closed": False,
    }
    validation_id = "lf021_extended_gate5g_validation_v2:" + hash_canonical(
        {"schema": "lf021_extended_gate5g_validation_v2", **payload}
    )
    validation = ExtendedGate5GValidationReportV2.model_validate(
        {"validation_id": validation_id, **payload}
    )
    output_root = paths.root / policy.dry_run_output_root
    if output_root.resolve() != (paths.root / _DRY_ROOT).resolve():
        raise ExtendedGate5GFinalizationError(
            "extended Gate-5G dry-run output root is noncanonical"
        )
    validation_path = output_root / f"{validation_id.rsplit(':', 1)[-1]}.json"
    validation_bytes = canonical_json_bytes(validation.model_dump(mode="json")) + b"\n"
    try:
        gate5g_v1._write_immutable(
            validation_path,
            validation_bytes,
            repo_root=paths.root,
            label="extended Gate-5G validation report",
        )
    except gate5g_v1.Gate5GFinalizationError as exc:
        raise ExtendedGate5GFinalizationError(str(exc)) from exc
    validation_binding = Gate5GArtifactBinding(
        artifact=str(validation_path.relative_to(paths.root)),
        sha256=sha256_hex(validation_bytes),
    )

    gate_report: ExtendedGate5GReportV2 | None = None
    gate_path: Path | None = None
    gate_hash: str | None = None
    if finalize:
        assert finalized_date is not None
        gate_payload: dict[str, Any] = {
            "schema_version": 2,
            "gate": "gate_5g",
            "decision": "pass",
            "lineage_kind": "post_exhaustion_extended_frame_v1",
            "finalized_date": finalized_date.isoformat(),
            "validation_report": validation_binding.model_dump(mode="json"),
            "validation_id": validation.validation_id,
            "prevalence_design_policy_id": loaded_prevalence.config.policy_id,
            "frame_freeze_decision_id": decision.decision_id,
            "frame_id": decision.frame.frame_id,
            "frame_item_count": policy.required_frame_size,
            "scope_limitations": scope.model_dump(mode="json"),
            "completed_checks": checks,
            "blocking_checks": (),
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "remote_provider_content_used": False,
            "gate_5g_closed": True,
            "gate_5_closed": False,
        }
        report_id = "lf021_extended_gate5g_report_v2:" + hash_canonical(
            {"schema": "lf021_extended_gate5g_report_v2", **gate_payload}
        )
        gate_report = ExtendedGate5GReportV2.model_validate(
            {"report_id": report_id, **gate_payload}
        )
        gate_path = paths.root / policy.canonical_gate_report
        if gate_path.resolve() != (paths.root / _GATE_PATH).resolve():
            raise ExtendedGate5GFinalizationError("extended Gate-5G report path is noncanonical")
        gate_bytes = canonical_json_bytes(gate_report.model_dump(mode="json")) + b"\n"
        try:
            gate5g_v1._write_immutable(
                gate_path,
                gate_bytes,
                repo_root=paths.root,
                label="canonical extended Gate-5G report",
            )
        except gate5g_v1.Gate5GFinalizationError as exc:
            raise ExtendedGate5GFinalizationError(str(exc)) from exc
        gate_hash = sha256_hex(gate_bytes)

    assert policy_file.is_file() and implementation_file.is_file()
    return ExtendedGate5GFinalizationResult(
        validation_report_path=str(validation_path.relative_to(paths.root)),
        validation_report_sha256=validation_binding.sha256,
        validation_report=validation,
        gate_report_path=(
            str(gate_path.relative_to(paths.root)) if gate_path is not None else None
        ),
        gate_report_sha256=gate_hash,
        gate_report=gate_report,
    )


__all__ = [
    "EXTENDED_GATE5G_CHECKS",
    "ExtendedGate5GFinalizationError",
    "ExtendedGate5GFinalizationResult",
    "ExtendedGate5GFinalizerPolicyV2",
    "load_extended_gate5g_policy",
    "validate_or_finalize_extended_gate5g",
]
