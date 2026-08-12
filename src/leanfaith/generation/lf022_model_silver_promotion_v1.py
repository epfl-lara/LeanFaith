"""Replay LF-022 Sol/Fable consensus into training-only model silver.

This module is intentionally downstream of the immutable weak-batch replay.
It never creates ``ResolvedLabel`` or human-gold records and cannot make any
record eligible for selection, calibration, or evaluation.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.generation.claude_fable_judge_v1 import (
    LoadedClaudeFableJudgeConfig,
    load_claude_fable_judge_config,
)
from leanfaith.generation.codex_sol_judge_v1 import (
    LoadedCodexSolJudgeConfig,
    load_codex_sol_judge_config,
)
from leanfaith.generation.lf022_sol_fable_batch_v1 import (
    CompletedSolFableExclusionLedger,
    HistoricalSolXhighRegistry,
    SolFableBatchAuthoringManifest,
    _historical_sol_xhigh_corpora,
    _safe,
)
from leanfaith.generation.lf022_supervision_candidates import LF022SupervisionCandidateRecord
from leanfaith.generation.lf022_weak_batch import (
    LF022WeakBatchSpec,
    LF022WeakDispatchManifest,
    LF022WeakDispatchRecord,
    LF022WeakExecutionManifest,
    LF022WeakFinalizationManifest,
    _load_canonical_jsonl,
    _load_canonical_model,
    _load_prepared_batch,
    _persist_immutable,
    finalize_lf022_weak_batch,
)
from leanfaith.schemas.enums import (
    EvidenceExecutionStatus,
    EvidenceKind,
    LLMCallStatus,
    LLMRole,
    ParseStatus,
    RelationLabel,
)
from leanfaith.schemas.evidence import EvidenceRecord, JudgmentValue
from leanfaith.schemas.ids import HEX64_PATTERN, make_id
from leanfaith.schemas.llm import LLMCallRecord
from leanfaith.schemas.model_silver import (
    ModelAdjudicatedSilverCellV1,
    ModelAdjudicatedSilverPromotionManifestV1,
    ModelAdjudicatedSilverPromotionRecordV1,
    ModelAdjudicatedSilverRejectionV1,
)
from leanfaith.schemas.weak_supervision import WeakConsensusCandidateRecord

PROMOTION_METHOD: Literal["lf022_model_silver_promotion_v1"] = "lf022_model_silver_promotion_v1"
DEFAULT_POLICY = Path("configs/generation/lf022_model_adjudicated_training_silver_v1.yaml")
_EXPECTED_CELL_KEYS = (
    ("judge_A", "AB"),
    ("judge_A", "BA"),
    ("judge_B", "AB"),
    ("judge_B", "BA"),
)


class LF022ModelSilverPromotionError(RuntimeError):
    """A global policy, artifact, replay, or output invariant failed."""


class LF022ModelSilverPromotionPolicyV1(StrictModel):
    """Exact registered boundary for training-only model adjudication."""

    schema_version: Literal[1] = 1
    policy_id: Literal["lf022_model_adjudicated_training_silver_v1"]
    status: Literal["training_only_model_adjudicated_silver_enabled"]
    promotion_profile: Literal["sol_fable_abba_model_adjudicated_training_silver_v1"]
    label_basis: Literal["model_adjudicated_training_silver"]
    resolution_method: Literal["sol_fable_abba_model_consensus_v1"]
    minimum_self_reported_confidence: float = Field(ge=0.0, le=1.0)
    sol_config_path: str = Field(min_length=1)
    sol_config_sha256: str = Field(pattern=HEX64_PATTERN)
    fable_config_path: str = Field(min_length=1)
    fable_config_sha256: str = Field(pattern=HEX64_PATTERN)
    prompt_template_path: str = Field(min_length=1)
    prompt_template_sha256: str = Field(pattern=HEX64_PATTERN)
    parser_implementation_path: str = Field(min_length=1)
    parser_implementation_sha256: str = Field(pattern=HEX64_PATTERN)
    historical_registry_path: str = Field(min_length=1)
    historical_registry_sha256: str = Field(pattern=HEX64_PATTERN)
    prompt_template_id: Literal["lean_pair_blinded"]
    prompt_template_version: Literal["v2"]
    call_schema_version: Literal[2]
    source_evidence_method_version: Literal["lf022_weak_batch_v1"]
    required_cells: tuple[Literal["judge_A:AB", "judge_A:BA", "judge_B:AB", "judge_B:BA"], ...]
    required_source_status: Literal["candidate_consensus"]
    strong_evidence_scope: Literal["bound_batch_evidence_only"]
    non_llm_bound_evidence_allowed: Literal[False]
    source_review_requests_allowed: Literal[False]
    public_sources_only: Literal[True]
    denylist_hits_allowed: Literal[False]
    proposer_judge_family_overlap_allowed: Literal[False]
    heldout_judge_family_overlap_allowed: Literal[False]
    quality_tier: Literal["silver_consensus"]
    train_eligibility: Literal[True]
    eval_eligibility: Literal[False]
    selection_eligibility: Literal[False]
    calibration_eligibility: Literal[False]
    human_gold_eligible: Literal[False]
    resolved_label_created: Literal[False]
    gate_6_human_audit_claimed: Literal[False]

    @model_validator(mode="after")
    def _exact_cells(self) -> Self:
        if self.required_cells != (
            "judge_A:AB",
            "judge_A:BA",
            "judge_B:AB",
            "judge_B:BA",
        ):
            raise ValueError("promotion policy requires the canonical four cells")
        return self


@dataclass(frozen=True, slots=True)
class LoadedModelSilverPromotionPolicyV1:
    policy: LF022ModelSilverPromotionPolicyV1
    path: Path
    sha256: str
    sol_config_path: Path
    fable_config_path: Path
    historical_registry_path: Path


@dataclass(frozen=True, slots=True)
class LF022ModelSilverPromotionResultV1:
    promotions: tuple[ModelAdjudicatedSilverPromotionRecordV1, ...]
    rejections: tuple[ModelAdjudicatedSilverRejectionV1, ...]
    manifest: ModelAdjudicatedSilverPromotionManifestV1
    output_root: Path


@dataclass(frozen=True, slots=True)
class _SelectedFreshnessIdentity:
    source_record_id: str
    source_theorem_lineage_id: str
    source_line_sha256: str
    judge_visible_payload_sha256: str


@dataclass(frozen=True, slots=True)
class _VerifiedAuthoringFreshness:
    authoring: SolFableBatchAuthoringManifest
    authoring_manifest_sha256: str
    historical_registry: HistoricalSolXhighRegistry
    completed_ledger: CompletedSolFableExclusionLedger
    selected_by_pair: dict[str, _SelectedFreshnessIdentity]


def _bound_repo_path(repo_root: Path, value: str, expected_sha256: str, label: str) -> Path:
    root = repo_root.resolve(strict=True)
    candidate = root / value
    if candidate.is_symlink() or not candidate.is_file():
        raise LF022ModelSilverPromotionError(f"registered {label} is missing or unsafe")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise LF022ModelSilverPromotionError(f"registered {label} escapes repository") from exc
    if hash_file(resolved) != expected_sha256:
        raise LF022ModelSilverPromotionError(f"registered {label} hash differs")
    return resolved


def load_model_silver_promotion_policy_v1(
    *, repo_root: Path, policy_path: Path
) -> LoadedModelSilverPromotionPolicyV1:
    """Load the strict policy and verify every registered implementation pin."""

    loaded: LoadedConfig[LF022ModelSilverPromotionPolicyV1] = load_config(
        policy_path, LF022ModelSilverPromotionPolicyV1
    )
    policy = loaded.config
    sol_path = _bound_repo_path(
        repo_root, policy.sol_config_path, policy.sol_config_sha256, "Sol config"
    )
    fable_path = _bound_repo_path(
        repo_root, policy.fable_config_path, policy.fable_config_sha256, "Fable config"
    )
    _bound_repo_path(
        repo_root,
        policy.prompt_template_path,
        policy.prompt_template_sha256,
        "judge prompt",
    )
    _bound_repo_path(
        repo_root,
        policy.parser_implementation_path,
        policy.parser_implementation_sha256,
        "judge parser implementation",
    )
    historical_registry_path = _bound_repo_path(
        repo_root,
        policy.historical_registry_path,
        policy.historical_registry_sha256,
        "historical Sol/xhigh registry",
    )
    return LoadedModelSilverPromotionPolicyV1(
        policy=policy,
        path=policy_path.resolve(strict=True),
        sha256=hash_file(policy_path),
        sol_config_path=sol_path,
        fable_config_path=fable_path,
        historical_registry_path=historical_registry_path,
    )


def _canonical_model_at(path: Path, model_type: type[StrictModel], *, label: str) -> StrictModel:
    try:
        safe = _safe(path, label=label, allow_missing=False)
        model = _load_canonical_model(safe, model_type)
    except (OSError, ValueError, RuntimeError) as exc:
        raise LF022ModelSilverPromotionError(f"invalid {label}: {exc}") from exc
    return model


def _verify_completed_ledger_snapshot(
    *,
    ledger: CompletedSolFableExclusionLedger,
    registry: HistoricalSolXhighRegistry,
) -> None:
    """Replay every batch named by the authoring-time completed-batch snapshot."""

    configured_root = _safe(
        Path(registry.completed_sol_fable_root),
        label="registered completed Sol/Fable root",
        allow_missing=False,
    )
    observed_root = _safe(
        Path(ledger.scanned_root),
        label="completed Sol/Fable ledger root",
        allow_missing=False,
    )
    if (
        observed_root != configured_root
        or ledger.scan_policy != registry.completed_sol_fable_scan_policy
    ):
        raise LF022ModelSilverPromotionError(
            "completed-batch ledger uses an unregistered root or policy"
        )

    pair_ids: set[str] = set()
    theorem_ids: set[str] = set()
    payload_hashes: set[str] = set()
    for binding in ledger.completed_batches:
        finalization_path = _safe(
            observed_root / binding.relative_finalization_artifact,
            label="completed Sol/Fable finalization",
            allow_missing=False,
        )
        try:
            finalization_path.relative_to(observed_root)
        except ValueError as exc:
            raise LF022ModelSilverPromotionError(
                "completed Sol/Fable binding escapes its registered root"
            ) from exc
        if (
            not finalization_path.is_file()
            or hash_file(finalization_path) != binding.finalization_sha256
        ):
            raise LF022ModelSilverPromotionError("completed Sol/Fable finalization differs")
        finalization_model = _canonical_model_at(
            finalization_path,
            LF022WeakFinalizationManifest,
            label="completed Sol/Fable finalization",
        )
        assert isinstance(finalization_model, LF022WeakFinalizationManifest)
        if (
            finalization_model.finalization_id != binding.finalization_id
            or finalization_model.batch_id != binding.batch_id
            or finalization_model.candidates_sha256 != binding.candidates_sha256
        ):
            raise LF022ModelSilverPromotionError("completed Sol/Fable finalization lineage differs")

        batch_root = finalization_path.parent.parent
        source_candidates_path = _safe(
            batch_root / "inputs/candidate_records.jsonl",
            label="completed Sol/Fable candidate records",
            allow_missing=False,
        )
        if hash_file(source_candidates_path) != binding.source_candidate_records_sha256:
            raise LF022ModelSilverPromotionError("completed Sol/Fable candidate records differ")
        candidate_models = _load_canonical_jsonl(
            source_candidates_path, LF022SupervisionCandidateRecord
        )
        source_candidates = tuple(
            item for item in candidate_models if isinstance(item, LF022SupervisionCandidateRecord)
        )
        observed_pairs = tuple(sorted(item.pair_id for item in source_candidates))
        observed_theorems: list[str] = []
        for item in source_candidates:
            item_theorems = tuple(
                source_id
                for source_id in item.pair.source_record_ids
                if source_id.startswith("thm:")
            )
            if len(item_theorems) != 1:
                raise LF022ModelSilverPromotionError(
                    "completed Sol/Fable source record lacks one theorem lineage"
                )
            observed_theorems.append(item_theorems[0])
        observed_theorems_tuple = tuple(sorted(observed_theorems))
        observed_payloads = tuple(
            sorted(item.judge_visible_payload_sha256 for item in source_candidates)
        )
        if (
            len(observed_pairs) != binding.pair_count
            or len(set(observed_pairs)) != len(observed_pairs)
            or hash_canonical(list(observed_pairs)) != binding.pair_ids_sha256
            or len(observed_theorems_tuple) != binding.theorem_lineage_count
            or len(set(observed_theorems_tuple)) != len(observed_theorems_tuple)
            or hash_canonical(list(observed_theorems_tuple)) != binding.theorem_lineage_ids_sha256
            or len(observed_payloads) != binding.judge_visible_payload_count
            or len(set(observed_payloads)) != len(observed_payloads)
            or hash_canonical(list(observed_payloads))
            != binding.judge_visible_payload_sha256s_sha256
        ):
            raise LF022ModelSilverPromotionError("completed Sol/Fable exclusion binding differs")
        pair_ids.update(observed_pairs)
        theorem_ids.update(observed_theorems_tuple)
        payload_hashes.update(observed_payloads)

    if (
        tuple(sorted(pair_ids)) != ledger.excluded_pair_ids
        or tuple(sorted(theorem_ids)) != ledger.excluded_theorem_lineage_ids
        or tuple(sorted(payload_hashes)) != ledger.excluded_judge_visible_payload_sha256s
    ):
        raise LF022ModelSilverPromotionError("completed Sol/Fable exclusion union differs")


def _verify_authoring_freshness(
    *,
    batch_root: Path,
    loaded_policy: LoadedModelSilverPromotionPolicyV1,
    dispatch_manifest: LF022WeakDispatchManifest,
    candidates_by_id: dict[str, LF022SupervisionCandidateRecord],
) -> _VerifiedAuthoringFreshness:
    """Verify and bind the v4 authoring proof that no selected item was judge-seen."""

    if batch_root.name != "batch":
        raise LF022ModelSilverPromotionError(
            "promotion batch must retain its authoring bundle layout"
        )
    bundle_root = batch_root.parent
    authoring_path = bundle_root / "authoring_manifest.json"
    authoring_model = _canonical_model_at(
        authoring_path,
        SolFableBatchAuthoringManifest,
        label="Sol/Fable v4 authoring manifest",
    )
    assert isinstance(authoring_model, SolFableBatchAuthoringManifest)
    authoring = authoring_model
    authoring_sha = hash_file(authoring_path)

    current_registry_model = _canonical_model_at(
        loaded_policy.historical_registry_path,
        HistoricalSolXhighRegistry,
        label="registered historical Sol/xhigh registry",
    )
    assert isinstance(current_registry_model, HistoricalSolXhighRegistry)
    registry = current_registry_model
    authoring_inputs = bundle_root / "authoring/inputs"
    copied_registry_path = authoring_inputs / authoring.historical_sol_xhigh_registry_artifact
    copied_registry_model = _canonical_model_at(
        copied_registry_path,
        HistoricalSolXhighRegistry,
        label="authoring historical Sol/xhigh registry copy",
    )
    assert isinstance(copied_registry_model, HistoricalSolXhighRegistry)
    if (
        registry != copied_registry_model
        or authoring.historical_sol_xhigh_registry_id != registry.registry_id
        or authoring.historical_sol_xhigh_registry_sha256
        != loaded_policy.policy.historical_registry_sha256
        or hash_file(copied_registry_path) != loaded_policy.policy.historical_registry_sha256
    ):
        raise LF022ModelSilverPromotionError("authoring history registry differs from policy")

    registered_corpora = {item.corpus_id: item for item in registry.corpora}
    if {item.registry_corpus_id for item in authoring.historical_sol_xhigh_corpora} != set(
        registered_corpora
    ):
        raise LF022ModelSilverPromotionError("authoring does not bind every registered Sol corpus")
    for binding in authoring.historical_sol_xhigh_corpora:
        pin = registered_corpora[binding.registry_corpus_id]
        if (
            binding.summary_sha256 != pin.summary_sha256
            or binding.findings_sha256 != pin.findings_sha256
            or binding.finding_count != pin.finding_count
            or binding.unique_pair_count != pin.unique_pair_count
        ):
            raise LF022ModelSilverPromotionError(
                "authoring historical corpus differs from registry"
            )
        copied_summary = _safe(
            authoring_inputs / binding.copied_summary_artifact,
            label="copied historical Sol/xhigh summary",
            allow_missing=False,
        )
        copied_findings = _safe(
            authoring_inputs / binding.copied_findings_artifact,
            label="copied historical Sol/xhigh findings",
            allow_missing=False,
        )
        try:
            copied_summary.relative_to(authoring_inputs)
            copied_findings.relative_to(authoring_inputs)
        except ValueError as exc:
            raise LF022ModelSilverPromotionError(
                "copied historical Sol/xhigh evidence escapes authoring bundle"
            ) from exc
        if (
            not copied_summary.is_file()
            or not copied_findings.is_file()
            or hash_file(copied_summary) != binding.summary_sha256
            or hash_file(copied_findings) != binding.findings_sha256
        ):
            raise LF022ModelSilverPromotionError(
                "copied historical Sol/xhigh evidence differs from authoring"
            )
    replayed_corpora, historical_pairs, historical_theorems, historical_payloads = (
        _historical_sol_xhigh_corpora(
            summary_paths=tuple(
                Path(item.registered_summary_path)
                for item in authoring.historical_sol_xhigh_corpora
            ),
            input_dir=authoring_inputs,
            registry=registry,
        )
    )
    if (
        replayed_corpora != authoring.historical_sol_xhigh_corpora
        or historical_pairs != authoring.excluded_historical_sol_pair_ids
        or historical_theorems != authoring.excluded_historical_sol_theorem_lineage_ids
        or historical_payloads != authoring.excluded_historical_sol_judge_visible_payload_sha256s
    ):
        raise LF022ModelSilverPromotionError("authoring historical exclusion replay differs")

    completed_ledger_path = authoring_inputs / authoring.completed_sol_fable_ledger_artifact
    completed_ledger_model = _canonical_model_at(
        completed_ledger_path,
        CompletedSolFableExclusionLedger,
        label="authoring completed Sol/Fable exclusion ledger",
    )
    assert isinstance(completed_ledger_model, CompletedSolFableExclusionLedger)
    completed_ledger = completed_ledger_model
    if (
        hash_file(completed_ledger_path) != authoring.completed_sol_fable_ledger_sha256
        or completed_ledger.ledger_id != authoring.completed_sol_fable_ledger_id
        or completed_ledger.excluded_pair_ids != authoring.completed_sol_fable_pair_ids
        or completed_ledger.excluded_theorem_lineage_ids
        != authoring.completed_sol_fable_theorem_lineage_ids
        or completed_ledger.excluded_judge_visible_payload_sha256s
        != authoring.completed_sol_fable_judge_visible_payload_sha256s
    ):
        raise LF022ModelSilverPromotionError("authoring completed-batch exclusion ledger differs")
    _verify_completed_ledger_snapshot(ledger=completed_ledger, registry=registry)

    if (
        authoring.weak_batch_id != dispatch_manifest.batch_id
        or authoring.dispatch_manifest_sha256 != hash_file(batch_root / "dispatch_manifest.json")
        or authoring.weak_batch_spec_sha256 != dispatch_manifest.spec_sha256
        or authoring.candidate_manifest_sha256 != dispatch_manifest.candidate_manifest_sha256
        or authoring.candidate_records_sha256 != dispatch_manifest.candidate_records_sha256
        or authoring.weak_config_sha256 != dispatch_manifest.weak_supervision_config_sha256
        or authoring.family_matrix_sha256 != dispatch_manifest.production_family_matrix_sha256
        or authoring.randomization_key_sha256 != dispatch_manifest.randomization_key_sha256
        or authoring.proposer_family_id != dispatch_manifest.proposer_family_id
    ):
        raise LF022ModelSilverPromotionError("authoring manifest differs from prepared batch")

    selected_by_pair: dict[str, _SelectedFreshnessIdentity] = {}
    for pair_id, source_record_id, theorem_id, source_line_sha, payload_sha in zip(
        authoring.selected_pair_ids,
        authoring.selected_source_record_ids,
        authoring.selected_source_theorem_lineage_ids,
        authoring.selected_source_line_sha256s,
        authoring.selected_judge_visible_payload_sha256s,
        strict=True,
    ):
        selected_by_pair[pair_id] = _SelectedFreshnessIdentity(
            source_record_id=source_record_id,
            source_theorem_lineage_id=theorem_id,
            source_line_sha256=source_line_sha,
            judge_visible_payload_sha256=payload_sha,
        )
    if set(selected_by_pair) != {
        dispatch.pair_id
        for dispatch in _load_canonical_jsonl(
            batch_root / dispatch_manifest.dispatch_records_artifact,
            LF022WeakDispatchRecord,
        )
        if isinstance(dispatch, LF022WeakDispatchRecord)
    }:
        raise LF022ModelSilverPromotionError(
            "authoring selection differs from dispatch denominator"
        )
    if set(selected_by_pair) & (set(historical_pairs) | set(completed_ledger.excluded_pair_ids)):
        raise LF022ModelSilverPromotionError("selected pair overlaps a judge-seen exclusion")
    if {item.source_theorem_lineage_id for item in selected_by_pair.values()} & (
        set(historical_theorems) | set(completed_ledger.excluded_theorem_lineage_ids)
    ):
        raise LF022ModelSilverPromotionError(
            "selected theorem lineage overlaps a judge-seen exclusion"
        )
    if {item.judge_visible_payload_sha256 for item in selected_by_pair.values()} & (
        set(historical_payloads) | set(completed_ledger.excluded_judge_visible_payload_sha256s)
    ):
        raise LF022ModelSilverPromotionError("selected payload overlaps a judge-seen exclusion")

    for pair_id, identity in selected_by_pair.items():
        try:
            source_candidate = candidates_by_id[identity.source_record_id]
        except KeyError as exc:
            raise LF022ModelSilverPromotionError(
                "authoring source record is absent from the prepared batch"
            ) from exc
        theorem_ids = tuple(
            source_id
            for source_id in source_candidate.pair.source_record_ids
            if source_id.startswith("thm:")
        )
        source_line_sha = sha256_hex(
            canonical_json_bytes(source_candidate.model_dump(mode="json")) + b"\n"
        )
        if (
            source_candidate.pair_id != pair_id
            or theorem_ids != (identity.source_theorem_lineage_id,)
            or source_candidate.judge_visible_payload_sha256
            != identity.judge_visible_payload_sha256
            or source_line_sha != identity.source_line_sha256
        ):
            raise LF022ModelSilverPromotionError(
                "authoring selected identity differs from source record"
            )

    return _VerifiedAuthoringFreshness(
        authoring=authoring,
        authoring_manifest_sha256=authoring_sha,
        historical_registry=registry,
        completed_ledger=completed_ledger,
        selected_by_pair=selected_by_pair,
    )


def _verify_registered_batch_endpoints(
    *, spec: LF022WeakBatchSpec, loaded: LoadedModelSilverPromotionPolicyV1
) -> tuple[LoadedCodexSolJudgeConfig, LoadedClaudeFableJudgeConfig]:
    sol = load_codex_sol_judge_config(loaded.sol_config_path)
    fable = load_claude_fable_judge_config(loaded.fable_config_path)
    if sol.sha256 != loaded.policy.sol_config_sha256:
        raise LF022ModelSilverPromotionError("loaded Sol config hash differs from policy")
    if fable.sha256 != loaded.policy.fable_config_sha256:
        raise LF022ModelSilverPromotionError("loaded Fable config hash differs from policy")
    judge_a = spec.judge_a
    judge_b = spec.judge_b
    expected_a = (
        sol.config.provider,
        sol.config.registry_model_id,
        sol.config.model_family,
        sol.config.endpoint_revision,
        sol.config.endpoint_decoding,
    )
    observed_a = (
        judge_a.provider,
        judge_a.model,
        judge_a.family_id,
        judge_a.revision,
        judge_a.decoding,
    )
    expected_b_decoding = {
        "effort": fable.config.effort,
        "system_prompt_sha256": fable.config.system_prompt_sha256,
        "output_schema_sha256": fable.config.output_schema_sha256,
        "claude_cli_version": fable.config.claude_cli_version,
        "claude_binary_sha256": fable.config.claude_binary_sha256,
        "structured_output": True,
        "safe_mode": True,
        "tools_disabled": True,
        "session_persistence": False,
    }
    expected_b = (
        fable.config.provider,
        fable.config.registry_model_id,
        fable.config.model_family,
        fable.config.endpoint_revision,
        expected_b_decoding,
    )
    observed_b = (
        judge_b.provider,
        judge_b.model,
        judge_b.family_id,
        judge_b.revision,
        judge_b.decoding,
    )
    if observed_a != expected_a:
        raise LF022ModelSilverPromotionError("batch Sol endpoint differs from registered config")
    if observed_b != expected_b:
        raise LF022ModelSilverPromotionError("batch Fable endpoint differs from registered config")
    return sol, fable


def _semantic_projection(value: JudgmentValue) -> tuple[str, str | None, str | None, str | None]:
    return (value.answer, value.relation, value.a_implies_b, value.b_implies_a)


def _canonical_jsonl(records: tuple[StrictModel, ...]) -> bytes:
    return b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )


def _rejection(
    *, pair_id: str, weak_candidate_id: str, reasons: set[str]
) -> ModelAdjudicatedSilverRejectionV1:
    values = {
        "schema_version": 1,
        "pair_id": pair_id,
        "weak_candidate_id": weak_candidate_id,
        "reasons": tuple(sorted(reasons)),
    }
    return ModelAdjudicatedSilverRejectionV1.model_validate(
        {
            **values,
            "rejection_id": make_id("model_silver_rejection", values),
        }
    )


def _cell_key(dispatch: LF022WeakDispatchRecord) -> tuple[str, str]:
    return dispatch.judge_slot, dispatch.orientation


def _evaluate_pair(
    *,
    pair_id: str,
    weak_candidate: WeakConsensusCandidateRecord,
    dispatches: tuple[LF022WeakDispatchRecord, ...],
    calls: tuple[LLMCallRecord, ...],
    evidence: tuple[EvidenceRecord, ...],
    source_candidate: LF022SupervisionCandidateRecord,
    dispatch_manifest: LF022WeakDispatchManifest,
    execution: LF022WeakExecutionManifest,
    finalization: LF022WeakFinalizationManifest,
    verified_authoring: _VerifiedAuthoringFreshness,
    loaded_policy: LoadedModelSilverPromotionPolicyV1,
    sol_config_sha256: str,
    fable_config_sha256: str,
    dispatch_manifest_sha256: str,
    execution_manifest_sha256: str,
    finalization_manifest_sha256: str,
) -> ModelAdjudicatedSilverPromotionRecordV1 | ModelAdjudicatedSilverRejectionV1:
    policy = loaded_policy.policy
    reasons: set[str] = set()
    pair_dispatches = tuple(item for item in dispatches if item.pair_id == pair_id)
    dispatch_by_key = {_cell_key(item): item for item in pair_dispatches}
    if len(pair_dispatches) != 4 or tuple(sorted(dispatch_by_key)) != _EXPECTED_CELL_KEYS:
        reasons.add("exact_four_cells_missing_or_duplicate")

    calls_by_cell: dict[str, list[LLMCallRecord]] = defaultdict(list)
    for call in calls:
        cell_id = call.metadata.get("weak_dispatch_cell_id")
        if isinstance(cell_id, str):
            calls_by_cell[cell_id].append(call)
    evidence_by_call: dict[str, list[EvidenceRecord]] = defaultdict(list)
    pair_non_llm = False
    for item in evidence:
        if item.target_id != pair_id:
            continue
        if item.kind is not EvidenceKind.LLM_JUDGMENT:
            pair_non_llm = True
            continue
        call_id = item.metadata.get("llm_call_id")
        if isinstance(call_id, str):
            evidence_by_call[call_id].append(item)
    if pair_non_llm:
        reasons.add("bound_non_llm_evidence_forbidden")
        reasons.add("strong_evidence_conflict_not_resolved")

    source_pair = source_candidate.pair
    if not source_pair.source_is_public:
        reasons.add("source_not_public")
    if source_pair.private_source_content:
        reasons.add("source_private_content")
    if not source_pair.external_transmission_allowed:
        reasons.add("source_external_transmission_forbidden")
    if not source_pair.denylist_checked or source_pair.denylist_hits:
        reasons.add("source_denylist_not_clean")

    proposer_family = dispatch_manifest.proposer_family_id
    heldout_family = dispatch_manifest.primary_eval_family_id
    judge_families = {
        dispatch_manifest.judge_a_family_id,
        dispatch_manifest.judge_b_family_id,
    }
    if proposer_family != weak_candidate.proposer_family:
        reasons.add("proposer_family_mismatch")
    if proposer_family in judge_families:
        reasons.add("proposer_judge_family_overlap")
    if heldout_family in judge_families:
        reasons.add("heldout_judge_family_overlap")
    if weak_candidate.status != policy.required_source_status:
        reasons.add("weak_candidate_not_consensus")
    if weak_candidate.consensus_value is None:
        reasons.add("weak_candidate_value_missing")

    cells: list[ModelAdjudicatedSilverCellV1] = []
    cell_values: list[JudgmentValue] = []
    pair_evidence_ids: list[str] = []
    pair_call_ids: list[str] = []
    if len(dispatch_by_key) == 4:
        for key in _EXPECTED_CELL_KEYS:
            dispatch = dispatch_by_key[key]
            matching_calls = calls_by_cell.get(dispatch.dispatch_cell_id, [])
            if len(matching_calls) != 1:
                reasons.add("call_missing_or_duplicate")
                continue
            call = matching_calls[0]
            pair_call_ids.append(call.call_id)
            matching_evidence = evidence_by_call.get(call.call_id, [])
            if len(matching_evidence) != 1:
                reasons.add("evidence_missing_or_duplicate")
                continue
            item = matching_evidence[0]
            pair_evidence_ids.append(item.evidence_id)
            if item.status is not EvidenceExecutionStatus.SUCCESS or not isinstance(
                item.value, JudgmentValue
            ):
                reasons.add("judgment_evidence_not_successful")
                continue
            value = item.value
            if (
                call.schema_version != policy.call_schema_version
                or call.role is not LLMRole.JUDGE
                or call.terminal_status is not LLMCallStatus.COMPLETED
                or call.parse_status is not ParseStatus.PARSED
                or call.prompt_template_id != policy.prompt_template_id
                or call.prompt_template_version != policy.prompt_template_version
                or call.prompt_template_hash != policy.prompt_template_sha256
                or item.method_version != policy.source_evidence_method_version
            ):
                reasons.add("registered_parser_prompt_binding_mismatch")
            if (
                call.provider_slot != dispatch.judge_slot
                or call.model_family != dispatch.judge_family_id
                or call.metadata.get("judge_orientation") != dispatch.orientation
                or call.metadata.get("proposer_family") != proposer_family
                or call.metadata.get("weak_batch_id") != dispatch_manifest.batch_id
                or item.target_id != pair_id
                or item.metadata.get("judge_slot") != dispatch.judge_slot
                or item.metadata.get("orientation") != dispatch.orientation
                or item.metadata.get("judge_family") != dispatch.judge_family_id
                or item.metadata.get("proposer_family") != proposer_family
            ):
                reasons.add("call_evidence_lineage_mismatch")
            if not call.supervision_eligible:
                reasons.add("call_not_supervision_eligible")
            if call.private_source_content:
                reasons.add("call_private_source_content")
            if not call.denylist_checked or call.denylist_hits:
                reasons.add("call_denylist_not_clean")
            if value.needs_expert_review is not False:
                reasons.add("source_judgment_requested_review")
            if (
                value.confidence is None
                or value.confidence < policy.minimum_self_reported_confidence
            ):
                reasons.add("judgment_confidence_below_policy")
                if value.confidence is None:
                    continue
            if (
                value.answer not in {"same_claim", "not_same_claim"}
                or value.relation
                not in {
                    "equivalent",
                    "A_stronger",
                    "B_stronger",
                    "incomparable",
                    "unrelated",
                }
                or value.a_implies_b is None
                or value.b_implies_a is None
            ):
                reasons.add("judgment_semantics_not_promotable")
                continue
            expected_slot = "judge_A" if call.model_family == "openai_codex_sol" else "judge_B"
            if dispatch.judge_slot != expected_slot:
                reasons.add("registered_judge_slot_mismatch")
                continue
            expected_model = (
                "openai/gpt-5.6-sol"
                if dispatch.judge_slot == "judge_A"
                else "anthropic/claude-fable-5"
            )
            expected_provider = (
                "openai_codex_exec" if dispatch.judge_slot == "judge_A" else "anthropic_claude_code"
            )
            expected_effort = "xhigh" if dispatch.judge_slot == "judge_A" else "max"
            effort_key = "reasoning_effort" if dispatch.judge_slot == "judge_A" else "effort"
            if dispatch.judge_slot == "judge_A":
                registered_config = load_codex_sol_judge_config(
                    loaded_policy.sol_config_path
                ).config
                expected_output_schema = registered_config.output_schema_sha256
                expected_revision = registered_config.endpoint_revision
                expected_decoding = registered_config.endpoint_decoding
            else:
                registered_config_b = load_claude_fable_judge_config(
                    loaded_policy.fable_config_path
                ).config
                expected_output_schema = registered_config_b.output_schema_sha256
                expected_revision = registered_config_b.endpoint_revision
                expected_decoding = {
                    "effort": registered_config_b.effort,
                    "system_prompt_sha256": registered_config_b.system_prompt_sha256,
                    "output_schema_sha256": registered_config_b.output_schema_sha256,
                    "claude_cli_version": registered_config_b.claude_cli_version,
                    "claude_binary_sha256": registered_config_b.claude_binary_sha256,
                    "structured_output": True,
                    "safe_mode": True,
                    "tools_disabled": True,
                    "session_persistence": False,
                }
            if (
                call.model != expected_model
                or call.provider != expected_provider
                or call.model_revision != expected_revision
                or call.decoding != expected_decoding
                or call.decoding.get(effort_key) != expected_effort
                or call.decoding.get("output_schema_sha256") != expected_output_schema
            ):
                reasons.add("registered_model_effort_schema_mismatch")
                continue
            cell_values.append(value)
            cells.append(
                ModelAdjudicatedSilverCellV1(
                    judge_slot=dispatch.judge_slot,
                    orientation=dispatch.orientation,
                    judge_family=cast(
                        Literal["openai_codex_sol", "anthropic_fable"], call.model_family
                    ),
                    provider=cast(
                        Literal["openai_codex_exec", "anthropic_claude_code"], call.provider
                    ),
                    model=cast(
                        Literal["openai/gpt-5.6-sol", "anthropic/claude-fable-5"],
                        call.model,
                    ),
                    model_revision=call.model_revision or "",
                    effort=cast(Literal["xhigh", "max"], expected_effort),
                    judge_config_sha256=(
                        sol_config_sha256
                        if dispatch.judge_slot == "judge_A"
                        else fable_config_sha256
                    ),
                    prompt_template_sha256=call.prompt_template_hash,
                    output_schema_sha256=expected_output_schema,
                    evidence_id=item.evidence_id,
                    call_id=call.call_id,
                    answer=value.answer,
                    canonical_relation=value.relation,
                    a_implies_b=value.a_implies_b,
                    b_implies_a=value.b_implies_a,
                    confidence=value.confidence,
                    needs_expert_review=False,
                    private_source_content=False,
                    denylist_hits=(),
                )
            )

    if len(cells) == 4 and len({_semantic_projection(value) for value in cell_values}) != 1:
        reasons.add("canonical_semantic_disagreement")
    expected_evidence_ids = tuple(sorted(pair_evidence_ids))
    expected_call_ids = tuple(sorted(pair_call_ids))
    if weak_candidate.judgment_evidence_ids != expected_evidence_ids:
        reasons.add("weak_candidate_evidence_ids_mismatch")
    if weak_candidate.llm_call_ids != expected_call_ids:
        reasons.add("weak_candidate_call_ids_mismatch")
    if weak_candidate.consensus_value is not None and len(cell_values) == 4:
        consensus = weak_candidate.consensus_value
        if _semantic_projection(consensus) != _semantic_projection(cell_values[0]):
            reasons.add("weak_candidate_consensus_mismatch")

    if reasons:
        return _rejection(
            pair_id=pair_id,
            weak_candidate_id=weak_candidate.candidate_id,
            reasons=reasons,
        )
    if len(cells) != 4 or not cell_values:
        raise LF022ModelSilverPromotionError("promotion evaluator lost its exact-cell invariant")
    answer, relation, _, _ = _semantic_projection(cell_values[0])
    assert relation is not None
    freshness = verified_authoring.selected_by_pair[pair_id]
    values: dict[str, object] = {
        "schema_version": 1,
        "promotion_profile": policy.promotion_profile,
        "label_basis": policy.label_basis,
        "resolution_method": policy.resolution_method,
        "pair_id": pair_id,
        "weak_candidate_id": weak_candidate.candidate_id,
        "source_batch_id": dispatch_manifest.batch_id,
        "source_execution_id": execution.execution_id,
        "source_finalization_id": finalization.finalization_id,
        "source_authoring_id": verified_authoring.authoring.authoring_id,
        "source_authoring_manifest_sha256": verified_authoring.authoring_manifest_sha256,
        "historical_sol_xhigh_registry_id": verified_authoring.historical_registry.registry_id,
        "historical_sol_xhigh_registry_sha256": (
            verified_authoring.authoring.historical_sol_xhigh_registry_sha256
        ),
        "completed_sol_fable_ledger_id": verified_authoring.completed_ledger.ledger_id,
        "completed_sol_fable_ledger_sha256": (
            verified_authoring.authoring.completed_sol_fable_ledger_sha256
        ),
        "source_record_id": freshness.source_record_id,
        "source_theorem_lineage_id": freshness.source_theorem_lineage_id,
        "source_line_sha256": freshness.source_line_sha256,
        "judge_visible_payload_sha256": freshness.judge_visible_payload_sha256,
        "freshness_status": "verified_authoring_history_and_completed_ledger_v1",
        "dispatch_manifest_sha256": dispatch_manifest_sha256,
        "execution_manifest_sha256": execution_manifest_sha256,
        "finalization_manifest_sha256": finalization_manifest_sha256,
        "weak_candidates_sha256": finalization.candidates_sha256,
        "judgment_evidence_sha256": finalization.evidence_sha256,
        "calls_sha256": finalization.calls_sha256,
        "promotion_policy_sha256": loaded_policy.sha256,
        "proposer_family": proposer_family,
        "heldout_evaluation_family": heldout_family,
        "cells": tuple(cell.model_dump(mode="json") for cell in cells),
        "evidence_ids": tuple(
            sorted(item.evidence_id for item in evidence if item.target_id == pair_id)
        ),
        "llm_call_ids": tuple(sorted(pair_call_ids)),
        "same_claim": answer == "same_claim",
        "relation": RelationLabel(relation),
        "minimum_self_reported_confidence": min(cell.confidence for cell in cells),
        "quality_tier": "silver_consensus",
        "accepted_strong_evidence_ids": (),
        "strong_evidence_conflict_status": "none_in_bound_evidence",
        "train_eligibility": True,
        "eval_eligibility": False,
        "selection_eligibility": False,
        "calibration_eligibility": False,
        "human_gold_eligible": False,
        "human_adjudication_status": "not_performed",
        "resolved_label_created": False,
        "gate_6_human_audit_claimed": False,
    }
    return ModelAdjudicatedSilverPromotionRecordV1.model_validate(
        {**values, "promotion_id": make_id("model_silver", values)}
    )


def promote_finalized_lf022_batch_to_model_silver_v1(
    *,
    repo_root: Path,
    batch_root: Path,
    output_root: Path,
    policy_path: Path = DEFAULT_POLICY,
) -> LF022ModelSilverPromotionResultV1:
    """Fully replay one finalized batch and materialize a complete decision partition."""

    repo_root = repo_root.resolve(strict=True)
    batch_root = batch_root.resolve(strict=True)
    policy_path = policy_path if policy_path.is_absolute() else repo_root / policy_path
    loaded_policy = load_model_silver_promotion_policy_v1(
        repo_root=repo_root, policy_path=policy_path
    )
    final_root = batch_root / "final"
    required = (
        batch_root / "dispatch_manifest.json",
        batch_root / "execution_manifest.json",
        final_root / "finalization_manifest.json",
        final_root / "calls.jsonl",
        final_root / "judgment_evidence.jsonl",
        final_root / "weak_consensus_candidates.jsonl",
    )
    if any(path.is_symlink() or not path.is_file() for path in required):
        raise LF022ModelSilverPromotionError("promotion requires an already finalized safe batch")

    spec, dispatch_manifest, dispatches, candidates_by_id = _load_prepared_batch(batch_root)
    sol_loaded, fable_loaded = _verify_registered_batch_endpoints(spec=spec, loaded=loaded_policy)
    verified_authoring = _verify_authoring_freshness(
        batch_root=batch_root,
        loaded_policy=loaded_policy,
        dispatch_manifest=dispatch_manifest,
        candidates_by_id=candidates_by_id,
    )
    execution_model = _load_canonical_model(
        batch_root / "execution_manifest.json", LF022WeakExecutionManifest
    )
    assert isinstance(execution_model, LF022WeakExecutionManifest)
    stored_finalization = _load_canonical_model(
        final_root / "finalization_manifest.json", LF022WeakFinalizationManifest
    )
    assert isinstance(stored_finalization, LF022WeakFinalizationManifest)
    dispatch_sha = hash_file(batch_root / "dispatch_manifest.json")
    execution_sha = hash_file(batch_root / "execution_manifest.json")
    finalization_sha = hash_file(final_root / "finalization_manifest.json")
    if (
        execution_model.batch_id != dispatch_manifest.batch_id
        or execution_model.dispatch_manifest_sha256 != dispatch_sha
        or stored_finalization.batch_id != dispatch_manifest.batch_id
        or stored_finalization.execution_manifest_sha256 != execution_sha
    ):
        raise LF022ModelSilverPromotionError("dispatch/execution/finalization lineage differs")

    evidence, weak_candidates, replayed_finalization = finalize_lf022_weak_batch(
        batch_root=batch_root
    )
    if replayed_finalization != stored_finalization:
        raise LF022ModelSilverPromotionError("replayed finalization differs from stored manifest")
    call_models = _load_canonical_jsonl(
        final_root / stored_finalization.calls_artifact, LLMCallRecord
    )
    calls = tuple(item for item in call_models if isinstance(item, LLMCallRecord))
    if (
        hash_file(final_root / stored_finalization.calls_artifact)
        != stored_finalization.calls_sha256
        or len(calls) != stored_finalization.call_count
    ):
        raise LF022ModelSilverPromotionError("final call corpus differs from finalization")
    if len(weak_candidates) != dispatch_manifest.dispatch_pair_count:
        raise LF022ModelSilverPromotionError("weak candidates do not cover every dispatched pair")

    weak_by_pair = {item.pair_id: item for item in weak_candidates}
    if len(weak_by_pair) != len(weak_candidates):
        raise LF022ModelSilverPromotionError("duplicate weak candidate pair")
    dispatch_candidate_ids: dict[str, set[str]] = defaultdict(set)
    for dispatch in dispatches:
        dispatch_candidate_ids[dispatch.pair_id].add(dispatch.candidate_inventory_record_id)
    promotions: list[ModelAdjudicatedSilverPromotionRecordV1] = []
    rejections: list[ModelAdjudicatedSilverRejectionV1] = []
    for pair_id in sorted(weak_by_pair):
        candidate_ids = dispatch_candidate_ids.get(pair_id, set())
        if len(candidate_ids) != 1:
            raise LF022ModelSilverPromotionError("pair does not bind exactly one source candidate")
        source_candidate = candidates_by_id[next(iter(candidate_ids))]
        result = _evaluate_pair(
            pair_id=pair_id,
            weak_candidate=weak_by_pair[pair_id],
            dispatches=dispatches,
            calls=calls,
            evidence=evidence,
            source_candidate=source_candidate,
            dispatch_manifest=dispatch_manifest,
            execution=execution_model,
            finalization=stored_finalization,
            verified_authoring=verified_authoring,
            loaded_policy=loaded_policy,
            sol_config_sha256=sol_loaded.sha256,
            fable_config_sha256=fable_loaded.sha256,
            dispatch_manifest_sha256=dispatch_sha,
            execution_manifest_sha256=execution_sha,
            finalization_manifest_sha256=finalization_sha,
        )
        if isinstance(result, ModelAdjudicatedSilverPromotionRecordV1):
            promotions.append(result)
        else:
            rejections.append(result)
    promotions.sort(key=lambda item: item.pair_id)
    rejections.sort(key=lambda item: item.pair_id)
    typed_promotions = tuple(promotions)
    typed_rejections = tuple(rejections)
    promotion_bytes = _canonical_jsonl(typed_promotions)
    rejection_bytes = _canonical_jsonl(typed_rejections)
    promotion_sha = sha256_hex(promotion_bytes)
    rejection_sha = sha256_hex(rejection_bytes)
    reason_counts = Counter(reason for item in rejections for reason in item.reasons)
    manifest_values: dict[str, object] = {
        "schema_version": 1,
        "promotion_profile": loaded_policy.policy.promotion_profile,
        "source_batch_id": dispatch_manifest.batch_id,
        "source_execution_id": execution_model.execution_id,
        "source_finalization_id": stored_finalization.finalization_id,
        "source_authoring_id": verified_authoring.authoring.authoring_id,
        "source_authoring_manifest_sha256": verified_authoring.authoring_manifest_sha256,
        "historical_sol_xhigh_registry_id": verified_authoring.historical_registry.registry_id,
        "historical_sol_xhigh_registry_sha256": (
            verified_authoring.authoring.historical_sol_xhigh_registry_sha256
        ),
        "completed_sol_fable_ledger_id": verified_authoring.completed_ledger.ledger_id,
        "completed_sol_fable_ledger_sha256": (
            verified_authoring.authoring.completed_sol_fable_ledger_sha256
        ),
        "freshness_verified": True,
        "dispatch_manifest_sha256": dispatch_sha,
        "execution_manifest_sha256": execution_sha,
        "finalization_manifest_sha256": finalization_sha,
        "promotion_policy_sha256": loaded_policy.sha256,
        "promotions_artifact": "promotions.jsonl",
        "promotions_sha256": promotion_sha,
        "rejections_artifact": "rejections.jsonl",
        "rejections_sha256": rejection_sha,
        "input_pair_count": len(weak_candidates),
        "promotion_count": len(promotions),
        "rejection_count": len(rejections),
        "rejection_reason_counts": dict(sorted(reason_counts.items())),
        "complete_pair_partition": True,
        "model_adjudicated_silver_records_created": True,
        "promotion_record_policy_train_eligible": True,
        "contains_train_eligible_records": bool(promotions),
        "eval_eligibility": False,
        "selection_eligibility": False,
        "calibration_eligibility": False,
        "human_gold_eligible": False,
        "resolved_label_created": False,
        "gate_6_human_audit_claimed": False,
    }
    manifest = ModelAdjudicatedSilverPromotionManifestV1.model_validate(
        {
            **manifest_values,
            "manifest_id": make_id("model_silver_manifest", manifest_values),
        }
    )
    output_root = output_root.resolve(strict=False)
    if not output_root.is_absolute():
        raise LF022ModelSilverPromotionError("promotion output must be absolute")
    if output_root.is_symlink():
        raise LF022ModelSilverPromotionError("promotion output cannot be a symlink")
    if output_root.exists() and not output_root.is_dir():
        raise LF022ModelSilverPromotionError("promotion output is not a directory")
    if output_root.exists():
        extras = {path.name for path in output_root.iterdir()} - {
            "promotions.jsonl",
            "rejections.jsonl",
            "manifest.json",
        }
        if extras:
            raise LF022ModelSilverPromotionError("promotion output contains unexpected artifacts")
    _persist_immutable(output_root / "promotions.jsonl", promotion_bytes, label="silver promotions")
    _persist_immutable(output_root / "rejections.jsonl", rejection_bytes, label="silver rejections")
    _persist_immutable(
        output_root / "manifest.json",
        canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
        label="silver promotion manifest",
    )
    return LF022ModelSilverPromotionResultV1(
        promotions=typed_promotions,
        rejections=typed_rejections,
        manifest=manifest,
        output_root=output_root,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    arguments = parser.parse_args()
    result = promote_finalized_lf022_batch_to_model_silver_v1(
        repo_root=arguments.repo_root,
        batch_root=arguments.batch_root,
        output_root=arguments.output_root,
        policy_path=arguments.policy,
    )
    print(
        f"manifest_id={result.manifest.manifest_id} "
        f"promotions={len(result.promotions)} rejections={len(result.rejections)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "DEFAULT_POLICY",
    "LF022ModelSilverPromotionError",
    "LF022ModelSilverPromotionPolicyV1",
    "LF022ModelSilverPromotionResultV1",
    "LoadedModelSilverPromotionPolicyV1",
    "load_model_silver_promotion_policy_v1",
    "promote_finalized_lf022_batch_to_model_silver_v1",
]
