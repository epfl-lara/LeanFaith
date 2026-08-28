"""Prepare bounded Sol/Fable AB+BA weak batches from the frozen v4 inventory."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.claude_fable_judge_v1 import (
    FABLE_FAMILY,
    FABLE_PROVIDER,
    load_claude_fable_judge_config,
)
from leanfaith.generation.lf022_codex_audit import (
    LF022CodexAuditFinding,
    LF022CodexAuditInput,
    LF022CodexAuditSummary,
)
from leanfaith.generation.lf022_judge_design_rebind_v4 import (
    LF022JudgeDesignRecordV4,
    verify_lf022_judge_design_v4,
)
from leanfaith.generation.lf022_production import LF022ProductionFamilyMatrix
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
    LF022SupervisionCandidateRecord,
    _judge_visible_payload_hash,
    _render_summary,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    JudgeEndpointPin,
    LF022WeakBatchError,
    LF022WeakBatchSpec,
    LF022WeakDispatchManifest,
    LF022WeakDispatchRecord,
    LF022WeakExecutionManifest,
    LF022WeakFinalizationManifest,
    _load_canonical_jsonl,
    _load_canonical_model,
    _validate_candidate_inventory_records,
    _validate_family_pins,
    _validate_weak_config,
    prepare_lf022_weak_batch,
)
from leanfaith.generation.providers import DecodingValue
from leanfaith.schemas.ids import HEX64_PATTERN, id_pattern, make_id
from leanfaith.schemas.weak_supervision import WeakConsensusCandidateRecord

SOL_FABLE_BATCH_METHOD_VERSION: Literal["lf022_sol_fable_batch_v4"] = "lf022_sol_fable_batch_v4"
_MAX_PAIRS = 64
_SOL_PROVIDER: Literal["openai_codex_exec"] = "openai_codex_exec"
_SOL_FAMILY: Literal["openai_codex_sol"] = "openai_codex_sol"
_SOL_MODEL: Literal["openai/gpt-5.6-sol"] = "openai/gpt-5.6-sol"
_SOL_EFFORT: Literal["xhigh"] = "xhigh"
_HELDOUT_FAMILY: Literal["deepseek_v4"] = "deepseek_v4"
_FAMILY_MATRIX = Path("configs/generation/lf022_sol_fable_family_matrix_v1.json")
_WEAK_CONFIG = Path("configs/judges/weak_supervision.yaml")
_FABLE_CONFIG = Path("configs/generation/lf022_claude_fable_judge_v1.yaml")
_HISTORICAL_SOL_XHIGH_REGISTRY = Path(
    "configs/generation/lf022_historical_sol_xhigh_registry_v1.json"
)
_REQUIRED_HISTORICAL_SOL_XHIGH_REGISTRY_ID = (
    "lf022_sol_history_registry:4a9c11e1a9636233677044d8c1aecd0392db1216883ac19de456d1e00ba05a5e"
)
_COMPLETED_SOL_FABLE_RESERVED_DIRECTORIES = frozenset({"keys"})


class LF022SolFableBatchError(RuntimeError):
    """A v4 selection, family, immutable artifact, or preparation invariant failed."""


class HistoricalSolXhighCorpusPin(StrictModel):
    """Reviewed identity and expected shape of one complete historical corpus."""

    corpus_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    canonical_summary_path: str = Field(min_length=1)
    summary_sha256: str = Field(pattern=HEX64_PATTERN)
    findings_sha256: str = Field(pattern=HEX64_PATTERN)
    finding_count: int = Field(ge=1, strict=True)
    unique_pair_count: int = Field(ge=1, strict=True)


class HistoricalSolXhighRegistry(StrictModel):
    """Versioned authority for every Sol/xhigh corpus excluded at authoring."""

    schema_version: Literal[2] = 2
    method_version: Literal["lf022_historical_sol_xhigh_registry_v2"] = (
        "lf022_historical_sol_xhigh_registry_v2"
    )
    registry_id: str = Field(pattern=id_pattern("lf022_sol_history_registry"))
    model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    reasoning_effort: Literal["xhigh"] = "xhigh"
    expected_union_pair_count: int = Field(ge=1, strict=True)
    expected_union_judge_visible_payload_count: int = Field(ge=1, strict=True)
    completed_sol_fable_root: str = Field(min_length=1)
    completed_sol_fable_scan_policy: Literal["recursive_finalized_batches_fail_on_partial_v1"] = (
        "recursive_finalized_batches_fail_on_partial_v1"
    )
    corpora: tuple[HistoricalSolXhighCorpusPin, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete_and_content_addressed(self) -> Self:
        if tuple(sorted(self.corpora, key=lambda item: item.corpus_id)) != self.corpora:
            raise ValueError("historical Sol/xhigh registry corpora must be sorted")
        if len({item.corpus_id for item in self.corpora}) != len(self.corpora):
            raise ValueError("historical Sol/xhigh registry corpus IDs must be unique")
        if len({item.summary_sha256 for item in self.corpora}) != len(self.corpora):
            raise ValueError("historical Sol/xhigh registry summary hashes must be unique")
        identity_corpora = [
            item.model_dump(mode="json", exclude={"canonical_summary_path"})
            for item in self.corpora
        ]
        expected = make_id(
            "lf022_sol_history_registry",
            {
                "schema_version": self.schema_version,
                "method_version": self.method_version,
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
                "expected_union_pair_count": self.expected_union_pair_count,
                "expected_union_judge_visible_payload_count": (
                    self.expected_union_judge_visible_payload_count
                ),
                "completed_sol_fable_scan_policy": self.completed_sol_fable_scan_policy,
                "corpora": identity_corpora,
            },
        )
        if self.registry_id != expected:
            raise ValueError("historical Sol/xhigh registry ID differs from content")
        return self


class HistoricalSolXhighCorpusBinding(StrictModel):
    """One complete, immutable historical Sol/xhigh findings corpus."""

    registry_corpus_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    registered_summary_path: str = Field(min_length=1)
    summary_id: str = Field(pattern=id_pattern("lf022_codex_audit_summary"))
    summary_sha256: str = Field(pattern=HEX64_PATTERN)
    findings_sha256: str = Field(pattern=HEX64_PATTERN)
    finding_count: int = Field(ge=1, strict=True)
    unique_pair_count: int = Field(ge=1, strict=True)
    pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    unique_theorem_lineage_count: int = Field(ge=1, strict=True)
    theorem_lineage_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    unique_judge_visible_payload_count: int = Field(ge=1, strict=True)
    judge_visible_payload_sha256s_sha256: str = Field(pattern=HEX64_PATTERN)
    model: Literal["gpt-5.6-sol"] = "gpt-5.6-sol"
    reasoning_effort: Literal["xhigh"] = "xhigh"
    copied_summary_artifact: str = Field(min_length=1)
    copied_findings_artifact: str = Field(min_length=1)


class CompletedSolFableBatchBinding(StrictModel):
    """One hash-verified finalized batch discovered beneath the canonical root."""

    relative_finalization_artifact: str = Field(min_length=1)
    finalization_id: str = Field(pattern=id_pattern("lf022_weak_finalization"))
    finalization_sha256: str = Field(pattern=HEX64_PATTERN)
    batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    candidates_sha256: str = Field(pattern=HEX64_PATTERN)
    source_candidate_records_sha256: str = Field(pattern=HEX64_PATTERN)
    pair_count: int = Field(ge=1, strict=True)
    pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    theorem_lineage_count: int = Field(ge=1, strict=True)
    theorem_lineage_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    judge_visible_payload_count: int = Field(ge=1, strict=True)
    judge_visible_payload_sha256s_sha256: str = Field(pattern=HEX64_PATTERN)


class CompletedSolFableExclusionLedger(StrictModel):
    """Dynamic, exact scan of every completed Sol/Fable batch at authoring time."""

    schema_version: Literal[1] = 1
    method_version: Literal["lf022_completed_sol_fable_exclusion_v1"] = (
        "lf022_completed_sol_fable_exclusion_v1"
    )
    ledger_id: str = Field(pattern=id_pattern("lf022_sol_fable_exclusion"))
    scanned_root: str = Field(min_length=1)
    scan_policy: Literal["recursive_finalized_batches_fail_on_partial_v1"] = (
        "recursive_finalized_batches_fail_on_partial_v1"
    )
    completed_batches: tuple[CompletedSolFableBatchBinding, ...]
    excluded_pair_ids: tuple[str, ...]
    excluded_pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    excluded_theorem_lineage_ids: tuple[str, ...]
    excluded_theorem_lineage_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    excluded_judge_visible_payload_sha256s: tuple[str, ...]
    excluded_judge_visible_payload_sha256s_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _complete_and_content_addressed(self) -> Self:
        paths = [item.relative_finalization_artifact for item in self.completed_batches]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("completed Sol/Fable bindings must be path-sorted and unique")
        if self.excluded_pair_ids != tuple(sorted(set(self.excluded_pair_ids))):
            raise ValueError("completed Sol/Fable exclusions must be sorted and unique")
        if self.excluded_pair_ids_sha256 != hash_canonical(list(self.excluded_pair_ids)):
            raise ValueError("completed Sol/Fable exclusion hash differs")
        if self.excluded_theorem_lineage_ids != tuple(
            sorted(set(self.excluded_theorem_lineage_ids))
        ):
            raise ValueError("completed Sol/Fable theorem exclusions must be sorted and unique")
        if self.excluded_theorem_lineage_ids_sha256 != hash_canonical(
            list(self.excluded_theorem_lineage_ids)
        ):
            raise ValueError("completed Sol/Fable theorem exclusion hash differs")
        if self.excluded_judge_visible_payload_sha256s != tuple(
            sorted(set(self.excluded_judge_visible_payload_sha256s))
        ):
            raise ValueError("completed Sol/Fable payload exclusions must be sorted and unique")
        if self.excluded_judge_visible_payload_sha256s_sha256 != hash_canonical(
            list(self.excluded_judge_visible_payload_sha256s)
        ):
            raise ValueError("completed Sol/Fable payload exclusion hash differs")
        expected = make_id(
            "lf022_sol_fable_exclusion",
            self.model_dump(mode="json", exclude={"ledger_id", "scanned_root"}),
        )
        if self.ledger_id != expected:
            raise ValueError("completed Sol/Fable exclusion ledger ID differs")
        return self


def _authoring_identity_payload(values: dict[str, object]) -> dict[str, object]:
    """Remove machine-local discovery paths while retaining their content pins."""

    identity = dict(values)
    identity.pop("authoring_id", None)
    identity.pop("source_v4_artifact_path", None)
    corpora = identity.get("historical_sol_xhigh_corpora")
    if isinstance(corpora, list | tuple):
        normalized: list[object] = []
        for corpus in corpora:
            if isinstance(corpus, HistoricalSolXhighCorpusBinding):
                corpus_values = corpus.model_dump(mode="json")
            elif isinstance(corpus, dict):
                corpus_values = dict(corpus)
            else:
                normalized.append(corpus)
                continue
            corpus_values.pop("registered_summary_path", None)
            normalized.append(corpus_values)
        identity["historical_sol_xhigh_corpora"] = normalized
    return identity


class SolFableBatchAuthoringManifest(StrictModel):
    schema_version: Literal[4] = 4
    method_version: Literal["lf022_sol_fable_batch_v4"] = SOL_FABLE_BATCH_METHOD_VERSION
    authoring_id: str = Field(pattern=id_pattern("lf022_sol_fable_authoring"))
    source_v4_artifact_path: str = Field(min_length=1)
    source_v4_inventory_id: str = Field(pattern=id_pattern("lf022_judge_design_inventory"))
    source_v4_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    source_v4_records_sha256: str = Field(pattern=HEX64_PATTERN)
    source_partition_id: str
    proposer_family_id: Literal["moonshot_kimi_k2", "qwen3"]
    selection_method: Literal[
        "deterministic_theorem_lineage_hash_with_registered_sol_xhigh_exclusions_v4"
    ] = "deterministic_theorem_lineage_hash_with_registered_sol_xhigh_exclusions_v4"
    lineage_diversity_status: Literal[
        "distinct_source_theorem_lineages_not_full_ancestry_certified"
    ] = "distinct_source_theorem_lineages_not_full_ancestry_certified"
    offset_pairs: int = Field(ge=0, strict=True)
    excluded_historical_sol_pair_ids: tuple[str, ...]
    excluded_historical_sol_pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    excluded_historical_sol_theorem_lineage_ids: tuple[str, ...]
    excluded_historical_sol_theorem_lineage_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    excluded_historical_sol_judge_visible_payload_sha256s: tuple[str, ...]
    excluded_historical_sol_judge_visible_payload_sha256s_sha256: str = Field(pattern=HEX64_PATTERN)
    historical_sol_xhigh_registry_id: str = Field(pattern=id_pattern("lf022_sol_history_registry"))
    historical_sol_xhigh_registry_sha256: str = Field(pattern=HEX64_PATTERN)
    historical_sol_xhigh_registry_artifact: Literal["historical_sol_xhigh/registry.json"] = (
        "historical_sol_xhigh/registry.json"
    )
    historical_sol_xhigh_corpora: tuple[HistoricalSolXhighCorpusBinding, ...] = Field(min_length=1)
    historical_sol_xhigh_corpora_sha256: str = Field(pattern=HEX64_PATTERN)
    historical_sol_xhigh_pair_count: int = Field(ge=1, strict=True)
    historical_sol_xhigh_theorem_lineage_count: int = Field(ge=1, strict=True)
    historical_sol_xhigh_judge_visible_payload_count: int = Field(ge=1, strict=True)
    historical_sol_xhigh_exclusion_complete: Literal[True] = True
    selected_pairs_absent_from_historical_sol_xhigh: Literal[True] = True
    selected_theorem_lineages_absent_from_historical_sol_xhigh: Literal[True] = True
    selected_payloads_absent_from_historical_sol_xhigh: Literal[True] = True
    completed_sol_fable_ledger_id: str = Field(pattern=id_pattern("lf022_sol_fable_exclusion"))
    completed_sol_fable_ledger_sha256: str = Field(pattern=HEX64_PATTERN)
    completed_sol_fable_ledger_artifact: Literal["completed_sol_fable/ledger.json"] = (
        "completed_sol_fable/ledger.json"
    )
    completed_sol_fable_pair_ids: tuple[str, ...]
    completed_sol_fable_pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    completed_sol_fable_theorem_lineage_ids: tuple[str, ...]
    completed_sol_fable_theorem_lineage_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    completed_sol_fable_judge_visible_payload_sha256s: tuple[str, ...]
    completed_sol_fable_judge_visible_payload_sha256s_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_pairs_absent_from_completed_sol_fable: Literal[True] = True
    selected_theorem_lineages_absent_from_completed_sol_fable: Literal[True] = True
    selected_payloads_absent_from_completed_sol_fable: Literal[True] = True
    selected_pair_count: int = Field(ge=1, le=64, strict=True)
    unique_source_theorem_lineage_count: int = Field(ge=1, le=64, strict=True)
    selected_pair_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_source_record_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_source_theorem_lineage_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_source_line_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_judge_visible_payload_sha256s: tuple[str, ...] = Field(min_length=1, max_length=64)
    selected_source_theorem_lineage_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_source_record_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_source_line_sha256s_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_judge_visible_payload_sha256s_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_pair_ids_sha256: str = Field(pattern=HEX64_PATTERN)
    selected_source_bytes_preserved: Literal[True] = True
    candidate_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    candidate_records_sha256: str = Field(pattern=HEX64_PATTERN)
    weak_config_sha256: str = Field(pattern=HEX64_PATTERN)
    family_matrix_sha256: str = Field(pattern=HEX64_PATTERN)
    candidate_manifest_selection_spec_seed_sha256: str = Field(pattern=HEX64_PATTERN)
    randomization_key_sha256: str = Field(pattern=HEX64_PATTERN)
    randomization_key_persisted_in_bundle: Literal[False] = False
    randomization_key_reconstruction_prerequisite: Literal[
        "external_secret_bytes_matching_persisted_sha256_required"
    ] = "external_secret_bytes_matching_persisted_sha256_required"
    regeneration_completeness: Literal[
        "requires_bound_v4_artifact_and_external_randomization_key_bytes"
    ] = "requires_bound_v4_artifact_and_external_randomization_key_bytes"
    weak_batch_spec_sha256: str = Field(pattern=HEX64_PATTERN)
    dispatch_manifest_sha256: str = Field(pattern=HEX64_PATTERN)
    weak_batch_id: str = Field(pattern=id_pattern("lf022_weak_batch"))
    dispatch_cell_count: int = Field(ge=4, le=256, strict=True)
    judge_a_family_id: Literal["openai_codex_sol"] = _SOL_FAMILY
    judge_b_family_id: Literal["anthropic_fable"] = FABLE_FAMILY
    primary_eval_family_id: Literal["deepseek_v4"] = _HELDOUT_FAMILY
    execution_authorization: Literal[
        "offline_fixture_or_replay_only",
        "live_provider_calls_explicitly_authorized",
    ]
    live_provider_calls_authorized: bool = Field(strict=True)
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit_claimed: Literal[False] = False

    @model_validator(mode="after")
    def _counts_and_id(self) -> Self:
        if self.historical_sol_xhigh_registry_id != (_REQUIRED_HISTORICAL_SOL_XHIGH_REGISTRY_ID):
            raise ValueError("authoring manifest does not bind the reviewed history registry")
        if self.dispatch_cell_count != 4 * self.selected_pair_count:
            raise ValueError("authoring must produce four cells per selected pair")
        if self.unique_source_theorem_lineage_count != self.selected_pair_count:
            raise ValueError("every selected pair must have a distinct theorem lineage")
        explicit_lists = (
            self.selected_pair_ids,
            self.selected_source_record_ids,
            self.selected_source_theorem_lineage_ids,
            self.selected_source_line_sha256s,
            self.selected_judge_visible_payload_sha256s,
        )
        if any(len(values) != self.selected_pair_count for values in explicit_lists):
            raise ValueError("explicit selected identities must cover every selected pair")
        if len(set(self.selected_pair_ids)) != self.selected_pair_count:
            raise ValueError("selected pair IDs must be unique")
        if len(set(self.selected_source_theorem_lineage_ids)) != self.selected_pair_count:
            raise ValueError("selected theorem lineage IDs must be unique")
        if len(set(self.selected_judge_visible_payload_sha256s)) != self.selected_pair_count:
            raise ValueError("selected judge-visible payloads must be unique")
        if tuple(sorted(set(self.excluded_historical_sol_pair_ids))) != (
            self.excluded_historical_sol_pair_ids
        ):
            raise ValueError("historical Sol pair exclusions must be sorted and unique")
        if self.historical_sol_xhigh_pair_count != len(self.excluded_historical_sol_pair_ids):
            raise ValueError("historical Sol pair count differs from exhaustive exclusion list")
        if self.excluded_historical_sol_theorem_lineage_ids != tuple(
            sorted(set(self.excluded_historical_sol_theorem_lineage_ids))
        ):
            raise ValueError("historical Sol theorem exclusions must be sorted and unique")
        if self.historical_sol_xhigh_theorem_lineage_count != len(
            self.excluded_historical_sol_theorem_lineage_ids
        ):
            raise ValueError("historical Sol theorem count differs from exhaustive exclusion list")
        if self.excluded_historical_sol_judge_visible_payload_sha256s != tuple(
            sorted(set(self.excluded_historical_sol_judge_visible_payload_sha256s))
        ):
            raise ValueError("historical Sol payload exclusions must be sorted and unique")
        if self.historical_sol_xhigh_judge_visible_payload_count != len(
            self.excluded_historical_sol_judge_visible_payload_sha256s
        ):
            raise ValueError("historical Sol payload count differs from exhaustive exclusion list")
        if self.completed_sol_fable_pair_ids != tuple(
            sorted(set(self.completed_sol_fable_pair_ids))
        ):
            raise ValueError("completed Sol/Fable pair exclusions must be sorted and unique")
        if self.completed_sol_fable_pair_ids_sha256 != hash_canonical(
            list(self.completed_sol_fable_pair_ids)
        ):
            raise ValueError("completed Sol/Fable pair exclusion hash differs")
        if self.completed_sol_fable_theorem_lineage_ids != tuple(
            sorted(set(self.completed_sol_fable_theorem_lineage_ids))
        ):
            raise ValueError("completed Sol/Fable theorem exclusions must be sorted and unique")
        if self.completed_sol_fable_theorem_lineage_ids_sha256 != hash_canonical(
            list(self.completed_sol_fable_theorem_lineage_ids)
        ):
            raise ValueError("completed Sol/Fable theorem exclusion hash differs")
        if self.completed_sol_fable_judge_visible_payload_sha256s != tuple(
            sorted(set(self.completed_sol_fable_judge_visible_payload_sha256s))
        ):
            raise ValueError("completed Sol/Fable payload exclusions must be sorted and unique")
        if self.completed_sol_fable_judge_visible_payload_sha256s_sha256 != hash_canonical(
            list(self.completed_sol_fable_judge_visible_payload_sha256s)
        ):
            raise ValueError("completed Sol/Fable payload exclusion hash differs")
        if len({item.summary_sha256 for item in self.historical_sol_xhigh_corpora}) != len(
            self.historical_sol_xhigh_corpora
        ):
            raise ValueError("historical Sol/xhigh corpora must be unique")
        expected_hashes = (
            (
                self.excluded_historical_sol_pair_ids_sha256,
                hash_canonical(list(self.excluded_historical_sol_pair_ids)),
            ),
            (
                self.historical_sol_xhigh_corpora_sha256,
                hash_canonical(
                    [item.model_dump(mode="json") for item in self.historical_sol_xhigh_corpora]
                ),
            ),
            (
                self.excluded_historical_sol_theorem_lineage_ids_sha256,
                hash_canonical(list(self.excluded_historical_sol_theorem_lineage_ids)),
            ),
            (
                self.excluded_historical_sol_judge_visible_payload_sha256s_sha256,
                hash_canonical(list(self.excluded_historical_sol_judge_visible_payload_sha256s)),
            ),
            (self.selected_pair_ids_sha256, hash_canonical(list(self.selected_pair_ids))),
            (
                self.selected_source_record_ids_sha256,
                hash_canonical(list(self.selected_source_record_ids)),
            ),
            (
                self.selected_source_theorem_lineage_ids_sha256,
                hash_canonical(list(self.selected_source_theorem_lineage_ids)),
            ),
            (
                self.selected_source_line_sha256s_sha256,
                hash_canonical(list(self.selected_source_line_sha256s)),
            ),
            (
                self.selected_judge_visible_payload_sha256s_sha256,
                hash_canonical(list(self.selected_judge_visible_payload_sha256s)),
            ),
        )
        if any(observed != expected for observed, expected in expected_hashes):
            raise ValueError("explicit selected or excluded identity hash differs")
        if set(self.selected_pair_ids) & set(self.excluded_historical_sol_pair_ids):
            raise ValueError("selected pairs overlap historical Sol exclusions")
        if set(self.selected_source_theorem_lineage_ids) & set(
            self.excluded_historical_sol_theorem_lineage_ids
        ):
            raise ValueError("selected theorem lineages overlap historical Sol exclusions")
        if set(self.selected_pair_ids) & set(self.completed_sol_fable_pair_ids):
            raise ValueError("selected pairs overlap completed Sol/Fable exclusions")
        if set(self.selected_source_theorem_lineage_ids) & set(
            self.completed_sol_fable_theorem_lineage_ids
        ):
            raise ValueError("selected theorem lineages overlap completed Sol/Fable exclusions")
        if set(self.selected_judge_visible_payload_sha256s) & set(
            self.excluded_historical_sol_judge_visible_payload_sha256s
        ):
            raise ValueError("selected payloads overlap historical Sol exclusions")
        if set(self.selected_judge_visible_payload_sha256s) & set(
            self.completed_sol_fable_judge_visible_payload_sha256s
        ):
            raise ValueError("selected payloads overlap completed Sol/Fable exclusions")
        expected = make_id(
            "lf022_sol_fable_authoring",
            _authoring_identity_payload(self.model_dump(mode="json")),
        )
        if self.authoring_id != expected:
            raise ValueError("authoring_id differs from content")
        return self


@dataclass(frozen=True, slots=True)
class SolFablePreparedBatch:
    authoring: SolFableBatchAuthoringManifest
    authoring_path: Path
    spec: LF022WeakBatchSpec
    spec_path: Path
    dispatches: tuple[LF022WeakDispatchRecord, ...]
    dispatch_manifest: LF022WeakDispatchManifest
    batch_root: Path


def _safe(path: Path, *, label: str, allow_missing: bool) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise LF022SolFableBatchError(f"{label} is missing: {current}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise LF022SolFableBatchError(f"{label} contains a symlink: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise LF022SolFableBatchError(f"{label} parent is not a directory: {current}")
    return absolute


def _immutable(path: Path, payload: bytes, *, label: str) -> str:
    path = _safe(path, label=label, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path, label=label, allow_missing=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise LF022SolFableBatchError(f"immutable {label} conflicts at {path}")
        return hash_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise LF022SolFableBatchError(
                    f"concurrent immutable {label} conflict at {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _matrix(repo_root: Path) -> tuple[Path, LF022ProductionFamilyMatrix]:
    path = _safe(repo_root / _FAMILY_MATRIX, label="Sol/Fable family matrix", allow_missing=False)
    matrix_model = _load_canonical_model(path, LF022ProductionFamilyMatrix)
    assert isinstance(matrix_model, LF022ProductionFamilyMatrix)
    matrix = matrix_model
    if (
        matrix.matrix_id
        != "lf022_family_matrix:377b624fddfc76cf4714d77a238479fa03b48cfb4a2a7820035cb1fc19247e09"
        or matrix.judge_family_ids != (_SOL_FAMILY, FABLE_FAMILY)
        or matrix.heldout_eval_family_id != _HELDOUT_FAMILY
    ):
        raise LF022SolFableBatchError("Sol/Fable family matrix differs from reviewed roles")
    for pin in matrix.family_registry:
        binding = pin.provider_catalog_artifact
        if binding is None:
            continue
        artifact = _safe(
            repo_root / binding.path,
            label=f"{pin.family_id} catalog",
            allow_missing=False,
        )
        if not artifact.is_file() or hash_file(artifact) != binding.sha256:
            raise LF022SolFableBatchError(f"provider catalog differs for {pin.family_id}")
    return path, matrix


def _historical_sol_xhigh_registry(
    repo_root: Path,
) -> tuple[Path, HistoricalSolXhighRegistry]:
    """Load the single reviewed registry and verify every canonical summary pin."""

    path = _safe(
        repo_root / _HISTORICAL_SOL_XHIGH_REGISTRY,
        label="historical Sol/xhigh registry",
        allow_missing=False,
    )
    try:
        model = _load_canonical_model(path, HistoricalSolXhighRegistry)
    except (LF022WeakBatchError, ValueError) as exc:
        raise LF022SolFableBatchError(f"invalid historical Sol/xhigh registry: {exc}") from exc
    assert isinstance(model, HistoricalSolXhighRegistry)
    if model.registry_id != _REQUIRED_HISTORICAL_SOL_XHIGH_REGISTRY_ID:
        raise LF022SolFableBatchError(
            "historical Sol/xhigh registry differs from the reviewed registry"
        )
    for corpus in model.corpora:
        configured = Path(corpus.canonical_summary_path)
        if not configured.is_absolute():
            configured = repo_root / configured
        canonical_summary = _safe(
            configured,
            label=f"registered historical Sol/xhigh summary {corpus.corpus_id}",
            allow_missing=False,
        )
        if not canonical_summary.is_file() or hash_file(canonical_summary) != corpus.summary_sha256:
            raise LF022SolFableBatchError(
                f"registered historical Sol/xhigh summary differs for {corpus.corpus_id}"
            )
    return path, model


def _historical_audit_roots(summary: LF022CodexAuditSummary) -> tuple[Path, ...]:
    """Return every hash-verified audit root that may own a finding input."""

    audit_manifest = _safe(
        Path(summary.audit_manifest),
        label="historical Sol/xhigh audit manifest",
        allow_missing=False,
    )
    if not audit_manifest.is_file() or hash_file(audit_manifest) != summary.audit_manifest_sha256:
        raise LF022SolFableBatchError("historical audit manifest differs from summary")
    roots = [audit_manifest.parent]
    for parent in summary.parent_audit_bindings:
        root = _safe(
            Path(parent.audit_root),
            label="historical parent Sol/xhigh audit root",
            allow_missing=False,
        )
        manifest = _safe(
            root / "manifest.json",
            label="historical parent Sol/xhigh audit manifest",
            allow_missing=False,
        )
        if not manifest.is_file() or hash_file(manifest) != parent.manifest_sha256:
            raise LF022SolFableBatchError("historical parent audit manifest differs")
        roots.append(root)
    return tuple(roots)


def _historical_finding_payload_hash(
    *,
    finding: LF022CodexAuditFinding,
    audit_roots: tuple[Path, ...],
) -> str:
    """Replay one content-addressed audit input and derive its semantic payload hash."""

    digest = finding.audit_item_id.split(":", maxsplit=1)[1]
    candidates = tuple(
        root / "items" / digest[:2] / digest / "input.json"
        for root in audit_roots
        if (root / "items" / digest[:2] / digest / "input.json").is_file()
    )
    if not candidates:
        raise LF022SolFableBatchError(
            f"historical audit input is missing for {finding.audit_item_id}"
        )
    inputs: list[LF022CodexAuditInput] = []
    canonical_inputs: set[bytes] = set()
    for candidate in candidates:
        try:
            model = _load_canonical_model(candidate, LF022CodexAuditInput)
        except LF022WeakBatchError as exc:
            raise LF022SolFableBatchError(
                f"invalid historical audit input {candidate}: {exc}"
            ) from exc
        assert isinstance(model, LF022CodexAuditInput)
        inputs.append(model)
        canonical_inputs.add(canonical_json_bytes(model.model_dump(mode="json")))
    if len(canonical_inputs) != 1:
        raise LF022SolFableBatchError(
            f"historical audit roots disagree for {finding.audit_item_id}"
        )
    audit_input = inputs[0]
    if (
        audit_input.audit_item_id != finding.audit_item_id
        or audit_input.lean_check_id != finding.lean_check_id
        or audit_input.variant_id != finding.variant_id
        or audit_input.pair.pair_id != finding.pair_id
        or audit_input.pair.source_record_ids != finding.source_record_ids
    ):
        raise LF022SolFableBatchError(
            f"historical audit input differs from finding {finding.finding_id}"
        )
    return _judge_visible_payload_hash(audit_input.pair)


def _historical_sol_xhigh_corpora(
    *,
    summary_paths: tuple[Path, ...],
    input_dir: Path,
    registry: HistoricalSolXhighRegistry,
) -> tuple[
    tuple[HistoricalSolXhighCorpusBinding, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Verify and copy the complete reviewed historical Sol/xhigh finding inputs."""

    safe_summaries = tuple(
        _safe(path, label="historical Sol/xhigh summary", allow_missing=False)
        for path in summary_paths
    )
    observed_summary_hashes = [hash_file(path) for path in safe_summaries]
    required_by_hash = {item.summary_sha256: item for item in registry.corpora}
    if (
        len(observed_summary_hashes) != len(required_by_hash)
        or len(set(observed_summary_hashes)) != len(observed_summary_hashes)
        or set(observed_summary_hashes) != set(required_by_hash)
    ):
        raise LF022SolFableBatchError(
            "historical Sol/xhigh summaries are not the complete registered corpus set"
        )

    bindings: list[HistoricalSolXhighCorpusBinding] = []
    all_pair_ids: set[str] = set()
    all_theorem_lineage_ids: set[str] = set()
    all_payload_hashes: set[str] = set()
    for summary_sha256, summary_path in sorted(
        zip(observed_summary_hashes, safe_summaries, strict=True)
    ):
        corpus_pin = required_by_hash[summary_sha256]
        raw_summary = summary_path.read_bytes()

        def duplicate_free(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise ValueError(f"duplicate JSON key {key!r}")
                result[key] = value
            return result

        def reject_nonfinite(value: str) -> float:
            raise ValueError(f"non-finite number {value!r}")

        try:
            summary_payload = json.loads(
                raw_summary.decode("utf-8"),
                object_pairs_hook=duplicate_free,
                parse_constant=reject_nonfinite,
            )
            if raw_summary != canonical_json_bytes(summary_payload) + b"\n":
                raise ValueError("summary is not canonical under its original schema")
            summary = LF022CodexAuditSummary.model_validate(summary_payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise LF022SolFableBatchError(
                f"invalid historical Sol/xhigh summary {summary_path}: {exc}"
            ) from exc
        if summary.model != "gpt-5.6-sol" or summary.reasoning_effort != "xhigh":
            raise LF022SolFableBatchError("historical finding corpus is not Sol/xhigh")
        if (
            summary.findings_sha256 != corpus_pin.findings_sha256
            or summary.completed_judgment_count != corpus_pin.finding_count
        ):
            raise LF022SolFableBatchError(
                f"historical summary differs from registry for {corpus_pin.corpus_id}"
            )
        findings_path = _safe(
            Path(summary.findings_artifact),
            label="historical Sol/xhigh findings",
            allow_missing=False,
        )
        if hash_file(findings_path) != summary.findings_sha256:
            raise LF022SolFableBatchError("historical findings hash differs from its summary")
        finding_models = _load_canonical_jsonl(findings_path, LF022CodexAuditFinding)
        findings = tuple(
            item for item in finding_models if isinstance(item, LF022CodexAuditFinding)
        )
        if len(findings) != summary.completed_judgment_count:
            raise LF022SolFableBatchError("historical findings count differs from its summary")
        audit_roots = _historical_audit_roots(summary)
        pair_ids = tuple(sorted(item.pair_id for item in findings))
        if len(set(pair_ids)) != len(pair_ids):
            raise LF022SolFableBatchError("historical Sol/xhigh findings repeat one pair")
        if len(pair_ids) != corpus_pin.unique_pair_count:
            raise LF022SolFableBatchError(
                f"historical unique-pair count differs for {corpus_pin.corpus_id}"
            )
        theorem_lineage_ids: list[str] = []
        for finding in findings:
            theorem_ids = tuple(
                source_id for source_id in finding.source_record_ids if source_id.startswith("thm:")
            )
            if len(theorem_ids) != 1:
                raise LF022SolFableBatchError(
                    "historical Sol/xhigh finding must bind exactly one theorem lineage"
                )
            theorem_lineage_ids.append(theorem_ids[0])
        unique_theorem_lineage_ids = tuple(sorted(set(theorem_lineage_ids)))
        payload_hashes = tuple(
            sorted(
                {
                    _historical_finding_payload_hash(
                        finding=finding,
                        audit_roots=audit_roots,
                    )
                    for finding in findings
                }
            )
        )
        if len(payload_hashes) != len(findings):
            raise LF022SolFableBatchError(
                f"historical corpus repeats judge-visible content for {corpus_pin.corpus_id}"
            )
        all_pair_ids.update(pair_ids)
        all_theorem_lineage_ids.update(unique_theorem_lineage_ids)
        all_payload_hashes.update(payload_hashes)
        stem = summary_sha256[:16]
        copied_summary = f"historical_sol_xhigh/{stem}.summary.json"
        copied_findings = f"historical_sol_xhigh/{stem}.findings.jsonl"
        _immutable(
            input_dir / copied_summary,
            summary_path.read_bytes(),
            label="historical Sol/xhigh summary copy",
        )
        _immutable(
            input_dir / copied_findings,
            findings_path.read_bytes(),
            label="historical Sol/xhigh findings copy",
        )
        bindings.append(
            HistoricalSolXhighCorpusBinding(
                registry_corpus_id=corpus_pin.corpus_id,
                registered_summary_path=corpus_pin.canonical_summary_path,
                summary_id=summary.summary_id,
                summary_sha256=summary_sha256,
                findings_sha256=summary.findings_sha256,
                finding_count=len(findings),
                unique_pair_count=len(pair_ids),
                pair_ids_sha256=hash_canonical(list(pair_ids)),
                unique_theorem_lineage_count=len(unique_theorem_lineage_ids),
                theorem_lineage_ids_sha256=hash_canonical(list(unique_theorem_lineage_ids)),
                unique_judge_visible_payload_count=len(payload_hashes),
                judge_visible_payload_sha256s_sha256=hash_canonical(list(payload_hashes)),
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                copied_summary_artifact=copied_summary,
                copied_findings_artifact=copied_findings,
            )
        )
    if len(all_pair_ids) != registry.expected_union_pair_count:
        raise LF022SolFableBatchError(
            "historical Sol/xhigh union count differs from the reviewed registry"
        )
    if len(all_payload_hashes) != registry.expected_union_judge_visible_payload_count:
        raise LF022SolFableBatchError(
            "historical Sol/xhigh payload union count differs from the reviewed registry"
        )
    return (
        tuple(bindings),
        tuple(sorted(all_pair_ids)),
        tuple(sorted(all_theorem_lineage_ids)),
        tuple(sorted(all_payload_hashes)),
    )


def _completed_sol_fable_exclusion_ledger(
    *,
    registry: HistoricalSolXhighRegistry,
    input_dir: Path,
) -> CompletedSolFableExclusionLedger:
    """Scan the canonical completed-batch root and freeze an exact exclusion ledger."""

    root = _safe(
        Path(registry.completed_sol_fable_root),
        label="completed Sol/Fable batch root",
        allow_missing=False,
    )
    if not root.is_dir():
        raise LF022SolFableBatchError("completed Sol/Fable batch root is not a directory")
    bindings: list[CompletedSolFableBatchBinding] = []
    all_pair_ids: set[str] = set()
    all_theorem_lineage_ids: set[str] = set()
    all_payload_hashes: set[str] = set()
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        safe_child = _safe(child, label="completed Sol/Fable run", allow_missing=False)
        if not safe_child.is_dir():
            raise LF022SolFableBatchError(
                f"unexpected non-directory in completed Sol/Fable root: {safe_child}"
            )
        if safe_child.name in _COMPLETED_SOL_FABLE_RESERVED_DIRECTORIES:
            continue
        batch_root = safe_child / "batch"
        if not batch_root.exists():
            raise LF022SolFableBatchError(
                f"completed Sol/Fable run lacks batch directory: {safe_child}"
            )
        batch_root = _safe(batch_root, label="completed Sol/Fable batch", allow_missing=False)
        finalization_path = batch_root / "final" / "finalization_manifest.json"
        execution_started = any(
            path.exists()
            for path in (
                batch_root / "execution_started.json",
                batch_root / "execution_manifest.json",
                batch_root / "terminal_records.jsonl",
                batch_root / "raw",
            )
        )
        if not finalization_path.exists():
            if execution_started:
                raise LF022SolFableBatchError(
                    f"executed Sol/Fable batch is not finalized: {batch_root}"
                )
            continue
        try:
            final_model = _load_canonical_model(finalization_path, LF022WeakFinalizationManifest)
        except LF022WeakBatchError as exc:
            raise LF022SolFableBatchError(
                f"invalid completed Sol/Fable finalization {finalization_path}: {exc}"
            ) from exc
        assert isinstance(final_model, LF022WeakFinalizationManifest)
        execution_path = batch_root / "execution_manifest.json"
        if (
            not execution_path.is_file()
            or hash_file(execution_path) != final_model.execution_manifest_sha256
        ):
            raise LF022SolFableBatchError(
                f"completed Sol/Fable execution differs from finalization: {batch_root}"
            )
        try:
            execution_model = _load_canonical_model(execution_path, LF022WeakExecutionManifest)
        except LF022WeakBatchError as exc:
            raise LF022SolFableBatchError(
                f"invalid completed Sol/Fable execution {execution_path}: {exc}"
            ) from exc
        assert isinstance(execution_model, LF022WeakExecutionManifest)
        if execution_model.batch_id != final_model.batch_id:
            raise LF022SolFableBatchError(
                f"completed Sol/Fable execution batch differs from finalization: {batch_root}"
            )
        dispatch_path = batch_root / "dispatch_manifest.json"
        if (
            not dispatch_path.is_file()
            or hash_file(dispatch_path) != execution_model.dispatch_manifest_sha256
        ):
            raise LF022SolFableBatchError(
                f"completed Sol/Fable dispatch differs from execution: {batch_root}"
            )
        try:
            dispatch_model = _load_canonical_model(dispatch_path, LF022WeakDispatchManifest)
        except LF022WeakBatchError as exc:
            raise LF022SolFableBatchError(
                f"invalid completed Sol/Fable dispatch {dispatch_path}: {exc}"
            ) from exc
        assert isinstance(dispatch_model, LF022WeakDispatchManifest)
        if dispatch_model.batch_id != execution_model.batch_id:
            raise LF022SolFableBatchError(
                f"completed Sol/Fable dispatch batch differs from execution: {batch_root}"
            )
        candidates_path = finalization_path.parent / final_model.candidates_artifact
        if (
            not candidates_path.is_file()
            or hash_file(candidates_path) != final_model.candidates_sha256
        ):
            raise LF022SolFableBatchError(
                f"completed Sol/Fable candidates differ from finalization: {batch_root}"
            )
        try:
            candidate_models = _load_canonical_jsonl(candidates_path, WeakConsensusCandidateRecord)
        except LF022WeakBatchError as exc:
            raise LF022SolFableBatchError(
                f"invalid completed Sol/Fable candidates {candidates_path}: {exc}"
            ) from exc
        candidates = tuple(
            item for item in candidate_models if isinstance(item, WeakConsensusCandidateRecord)
        )
        pair_ids = tuple(sorted(item.pair_id for item in candidates))
        if len(pair_ids) != final_model.pair_count or len(set(pair_ids)) != len(pair_ids):
            raise LF022SolFableBatchError(
                f"completed Sol/Fable pair count differs from finalization: {batch_root}"
            )
        source_candidates_path = batch_root / dispatch_model.candidate_records_artifact
        if (
            not source_candidates_path.is_file()
            or hash_file(source_candidates_path) != dispatch_model.candidate_records_sha256
        ):
            raise LF022SolFableBatchError(
                f"completed Sol/Fable source candidates differ from dispatch: {batch_root}"
            )
        try:
            source_candidate_models = _load_canonical_jsonl(
                source_candidates_path, LF022SupervisionCandidateRecord
            )
        except LF022WeakBatchError as exc:
            raise LF022SolFableBatchError(
                f"invalid completed Sol/Fable source candidates {batch_root}: {exc}"
            ) from exc
        source_candidates = tuple(
            item
            for item in source_candidate_models
            if isinstance(item, LF022SupervisionCandidateRecord)
        )
        source_pair_ids = tuple(sorted(item.pair_id for item in source_candidates))
        if source_pair_ids != pair_ids or len(pair_ids) != dispatch_model.dispatch_pair_count:
            raise LF022SolFableBatchError(
                f"completed Sol/Fable dispatch, source, and finalized pair IDs differ: {batch_root}"
            )
        theorem_lineage_ids: list[str] = []
        for source_candidate in source_candidates:
            theorem_ids = tuple(
                source_id
                for source_id in source_candidate.pair.source_record_ids
                if source_id.startswith("thm:")
            )
            if len(theorem_ids) != 1:
                raise LF022SolFableBatchError(
                    "completed Sol/Fable source candidate must bind exactly one theorem lineage"
                )
            theorem_lineage_ids.append(theorem_ids[0])
        unique_theorem_lineage_ids = tuple(sorted(set(theorem_lineage_ids)))
        if len(unique_theorem_lineage_ids) != len(pair_ids):
            raise LF022SolFableBatchError(
                f"completed Sol/Fable batch repeats a theorem lineage: {batch_root}"
            )
        payload_hashes = tuple(
            sorted({item.judge_visible_payload_sha256 for item in source_candidates})
        )
        if len(payload_hashes) != len(pair_ids):
            raise LF022SolFableBatchError(
                f"completed Sol/Fable batch repeats judge-visible content: {batch_root}"
            )
        all_pair_ids.update(pair_ids)
        all_theorem_lineage_ids.update(unique_theorem_lineage_ids)
        all_payload_hashes.update(payload_hashes)
        bindings.append(
            CompletedSolFableBatchBinding(
                relative_finalization_artifact=str(finalization_path.relative_to(root)),
                finalization_id=final_model.finalization_id,
                finalization_sha256=hash_file(finalization_path),
                batch_id=final_model.batch_id,
                candidates_sha256=final_model.candidates_sha256,
                source_candidate_records_sha256=hash_file(source_candidates_path),
                pair_count=len(pair_ids),
                pair_ids_sha256=hash_canonical(list(pair_ids)),
                theorem_lineage_count=len(unique_theorem_lineage_ids),
                theorem_lineage_ids_sha256=hash_canonical(list(unique_theorem_lineage_ids)),
                judge_visible_payload_count=len(payload_hashes),
                judge_visible_payload_sha256s_sha256=hash_canonical(list(payload_hashes)),
            )
        )
    excluded = tuple(sorted(all_pair_ids))
    excluded_theorems = tuple(sorted(all_theorem_lineage_ids))
    excluded_payloads = tuple(sorted(all_payload_hashes))
    values: dict[str, object] = {
        "schema_version": 1,
        "method_version": "lf022_completed_sol_fable_exclusion_v1",
        "scanned_root": str(root),
        "scan_policy": registry.completed_sol_fable_scan_policy,
        "completed_batches": [item.model_dump(mode="json") for item in bindings],
        "excluded_pair_ids": excluded,
        "excluded_pair_ids_sha256": hash_canonical(list(excluded)),
        "excluded_theorem_lineage_ids": excluded_theorems,
        "excluded_theorem_lineage_ids_sha256": hash_canonical(list(excluded_theorems)),
        "excluded_judge_visible_payload_sha256s": excluded_payloads,
        "excluded_judge_visible_payload_sha256s_sha256": hash_canonical(list(excluded_payloads)),
    }
    ledger = CompletedSolFableExclusionLedger.model_validate(
        {
            **values,
            "ledger_id": make_id(
                "lf022_sol_fable_exclusion",
                {key: value for key, value in values.items() if key != "scanned_root"},
            ),
        }
    )
    _immutable(
        input_dir / "completed_sol_fable" / "ledger.json",
        canonical_json_bytes(ledger.model_dump(mode="json")) + b"\n",
        label="completed Sol/Fable exclusion ledger",
    )
    return ledger


def _validate_selected_fresh(
    selected: tuple[LF022JudgeDesignRecordV4, ...],
    historical_pair_ids: tuple[str, ...],
    historical_theorem_lineage_ids: tuple[str, ...] = (),
    historical_judge_visible_payload_sha256s: tuple[str, ...] = (),
) -> None:
    overlap = sorted({item.pair_id for item in selected} & set(historical_pair_ids))
    if overlap:
        raise LF022SolFableBatchError(
            "selected pair already has a historical Sol/xhigh finding: " + ", ".join(overlap)
        )
    selected_theorems = {_theorem_lineage(item) for item in selected}
    theorem_overlap = sorted(selected_theorems & set(historical_theorem_lineage_ids))
    if theorem_overlap:
        raise LF022SolFableBatchError(
            "selected theorem lineage already has a historical Sol/xhigh finding: "
            + ", ".join(theorem_overlap)
        )
    payload_overlap = sorted(
        {item.source_record.judge_visible_payload_sha256 for item in selected}
        & set(historical_judge_visible_payload_sha256s)
    )
    if payload_overlap:
        raise LF022SolFableBatchError(
            "selected judge-visible content already has a historical Sol/xhigh finding: "
            + ", ".join(payload_overlap)
        )


def _selected(
    records: tuple[LF022JudgeDesignRecordV4, ...],
    *,
    partition_id: str,
    offset_pairs: int,
    limit_pairs: int,
    excluded_pair_ids: tuple[str, ...],
    excluded_theorem_lineage_ids: tuple[str, ...],
    excluded_judge_visible_payload_sha256s: tuple[str, ...],
) -> tuple[LF022JudgeDesignRecordV4, ...]:
    if offset_pairs < 0:
        raise LF022SolFableBatchError("offset_pairs must be nonnegative")
    if limit_pairs < 1 or limit_pairs > _MAX_PAIRS:
        raise LF022SolFableBatchError(f"limit_pairs must be within 1..{_MAX_PAIRS}")
    partition = tuple(item for item in records if item.source_partition_id == partition_id)
    if not partition:
        raise LF022SolFableBatchError(f"unknown or empty v4 partition: {partition_id}")
    excluded = tuple(sorted(set(excluded_pair_ids)))
    if excluded != excluded_pair_ids:
        raise LF022SolFableBatchError("excluded_pair_ids must be sorted and unique")
    excluded_theorems = tuple(sorted(set(excluded_theorem_lineage_ids)))
    if excluded_theorems != excluded_theorem_lineage_ids:
        raise LF022SolFableBatchError("excluded_theorem_lineage_ids must be sorted and unique")
    excluded_pair_set = set(excluded)
    excluded_theorem_set = set(excluded_theorems)
    excluded_payloads = tuple(sorted(set(excluded_judge_visible_payload_sha256s)))
    if excluded_payloads != excluded_judge_visible_payload_sha256s:
        raise LF022SolFableBatchError(
            "excluded_judge_visible_payload_sha256s must be sorted and unique"
        )
    excluded_payload_set = set(excluded_payloads)
    admitted_partition = tuple(
        item
        for item in partition
        if item.pair_id not in excluded_pair_set
        and _theorem_lineage(item) not in excluded_theorem_set
        and item.source_record.judge_visible_payload_sha256 not in excluded_payload_set
    )
    by_theorem: dict[str, LF022JudgeDesignRecordV4] = {}
    for item in admitted_partition:
        theorem_ids = tuple(
            lineage for lineage in item.source_lineage_ids if lineage.startswith("thm:")
        )
        if len(theorem_ids) != 1:
            raise LF022SolFableBatchError(
                "every v4 record must carry exactly one source theorem lineage"
            )
        theorem_id = theorem_ids[0]
        previous = by_theorem.get(theorem_id)
        if previous is None or hash_canonical(
            {
                "schema": "lf022_lineage_selection_order_v1",
                "partition_id": partition_id,
                "theorem_id": theorem_id,
                "pair_id": item.pair_id,
            }
        ) < hash_canonical(
            {
                "schema": "lf022_lineage_selection_order_v1",
                "partition_id": partition_id,
                "theorem_id": theorem_id,
                "pair_id": previous.pair_id,
            }
        ):
            by_theorem[theorem_id] = item
    ordered = tuple(
        item
        for _, item in sorted(
            by_theorem.items(),
            key=lambda pair: hash_canonical(
                {
                    "schema": "lf022_lineage_selection_order_v1",
                    "partition_id": partition_id,
                    "theorem_id": pair[0],
                }
            ),
        )
    )
    selected = ordered[offset_pairs : offset_pairs + limit_pairs]
    if len(selected) != limit_pairs:
        raise LF022SolFableBatchError(
            "requested bounded selection exceeds distinct theorem-lineage count"
        )
    if len({item.proposer_family_id for item in selected}) != 1:
        raise LF022SolFableBatchError("one prepared batch must have one proposer family")
    selected_theorems = {
        lineage
        for item in selected
        for lineage in item.source_lineage_ids
        if lineage.startswith("thm:")
    }
    if len(selected_theorems) != len(selected):
        raise LF022SolFableBatchError("selected pairs do not have distinct theorem lineages")
    _validate_selected_fresh(selected, excluded, excluded_theorems, excluded_payloads)
    return selected


def _theorem_lineage(item: LF022JudgeDesignRecordV4) -> str:
    theorem_ids = tuple(
        lineage for lineage in item.source_lineage_ids if lineage.startswith("thm:")
    )
    if len(theorem_ids) != 1:
        raise LF022SolFableBatchError(
            "every selected record must carry exactly one source theorem lineage"
        )
    return theorem_ids[0]


def _derived_candidate_manifest(
    *,
    source_manifest: LF022SupervisionCandidateManifest,
    selected: tuple[LF022JudgeDesignRecordV4, ...],
    records_bytes: bytes,
    selection_spec_seed_sha256: str,
) -> tuple[LF022SupervisionCandidateManifest, bytes, bytes]:
    records = tuple(item.source_record for item in selected)
    sample = records[:10]
    sample_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in sample
    )
    status_counts = dict(sorted(Counter(item.dispatch_status for item in records).items()))
    values: dict[str, object] = {
        "schema_version": 4,
        "method_version": "lf022_supervision_candidate_inventory_v4",
        "collection_id": source_manifest.collection_id,
        "selection_spec_seed_sha256": selection_spec_seed_sha256,
        "checks_sha256": source_manifest.checks_sha256,
        "lean_check_manifest_sha256": source_manifest.lean_check_manifest_sha256,
        "codex_audit_manifest_sha256": None,
        "logical_input_binding_sha256": hash_canonical(
            {
                "schema": "lf022_sol_fable_candidate_selection_v1",
                "source_inventory_id": source_manifest.inventory_id,
                "selected_source_record_ids": [
                    item.source_candidate_inventory_record_id for item in selected
                ],
                "selected_line_sha256s": [item.source_record_line_sha256 for item in selected],
                "judge_a_family_id": _SOL_FAMILY,
                "judge_b_family_id": FABLE_FAMILY,
                "primary_eval_judge_family_id": _HELDOUT_FAMILY,
            }
        ),
        "codex_response_artifact_set_sha256": None,
        "proposer_family_id": records[0].proposer_family_id,
        "proposer_model": records[0].proposer_model,
        "judge_a_family_id": _SOL_FAMILY,
        "judge_b_family_id": FABLE_FAMILY,
        "primary_eval_judge_family_id": _HELDOUT_FAMILY,
        "records_artifact": "candidates.jsonl",
        "records_sha256": sha256_hex(records_bytes),
        "public_sample_artifact": "public_sample.jsonl",
        "public_sample_sha256": sha256_hex(sample_bytes),
        "public_sample_count": len(sample),
        "summary_artifact": "summary.md",
        "summary_sha256": "0" * 64,
        "record_count": len(records),
        "unique_judge_visible_payload_count": len(records),
        "exact_duplicate_record_count": 0,
        "dispatch_eligible_count": len(records),
        "required_future_judge_call_count": 4 * len(records),
        "codex_diagnostic_status": "absent",
        "codex_diagnostic_record_count": 0,
        "codex_same_claim_counts": {},
        "dispatch_status_counts": status_counts,
        "codex_is_diagnostic_only": True,
        "two_family_judgments_completed": False,
        "human_pilot_bound": False,
        "semantic_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    summary_bytes = _render_summary(values)
    values["summary_sha256"] = sha256_hex(summary_bytes)
    identity = {
        key: value
        for key, value in values.items()
        if key
        not in {
            "inventory_id",
            "records_artifact",
            "public_sample_artifact",
            "summary_artifact",
            "spec_sha256",
            "selection_spec_seed_sha256",
        }
    }
    manifest = LF022SupervisionCandidateManifest.model_validate(
        {
            **values,
            "inventory_id": make_id("lf022_supervision_inventory", identity),
        }
    )
    _validate_candidate_inventory_records(manifest=manifest, candidates=records)
    if _render_summary(manifest.model_dump(mode="json")) != summary_bytes:
        raise LF022SolFableBatchError("derived candidate summary does not replay")
    return manifest, sample_bytes, summary_bytes


def _revision(matrix: LF022ProductionFamilyMatrix, family_id: str) -> str:
    pin = matrix.pins_by_id[family_id]
    assert pin.provider_catalog_artifact is not None
    return f"provider-deployment-snapshot:{pin.provider_catalog_artifact.sha256}"


def prepare_lf022_sol_fable_batch_v1(
    *,
    repo_root: Path,
    v4_root: Path,
    source_partition_id: str,
    offset_pairs: int,
    limit_pairs: int,
    randomization_key: bytes,
    output_dir: Path,
    historical_sol_xhigh_summary_paths: tuple[Path, ...],
    authorize_live_provider_calls: bool = False,
) -> SolFablePreparedBatch:
    """Prepare one immutable, bounded, four-cell-per-pair batch offline."""

    if len(randomization_key) < 32:
        raise LF022SolFableBatchError("randomization key must contain at least 32 bytes")
    repo = _safe(repo_root, label="repository root", allow_missing=False)
    output = _safe(output_dir, label="Sol/Fable output", allow_missing=True)
    source_v4_artifact = _safe(
        v4_root, label="source v4 judge-design artifact", allow_missing=False
    )
    verified = verify_lf022_judge_design_v4(source_v4_artifact)
    input_dir = output / "authoring" / "inputs"
    registry_path, historical_registry = _historical_sol_xhigh_registry(repo)
    # Scan the canonical completed-batch root before this authoring attempt
    # writes anything below ``output``.  Otherwise a brand-new output
    # directory can be mistaken for a pre-existing, partially executed batch
    # during its own freshness scan.  A genuinely pre-existing partial output
    # still fails closed because it is present when this scan begins.
    completed_ledger = _completed_sol_fable_exclusion_ledger(
        registry=historical_registry,
        input_dir=input_dir,
    )
    registry_copy = input_dir / "historical_sol_xhigh" / "registry.json"
    _immutable(
        registry_copy,
        registry_path.read_bytes(),
        label="historical Sol/xhigh registry copy",
    )
    (
        historical_corpora,
        exclusions,
        historical_theorem_exclusions,
        historical_payload_exclusions,
    ) = _historical_sol_xhigh_corpora(
        summary_paths=historical_sol_xhigh_summary_paths,
        input_dir=input_dir,
        registry=historical_registry,
    )
    all_exclusions = tuple(sorted(set(exclusions) | set(completed_ledger.excluded_pair_ids)))
    all_theorem_exclusions = tuple(
        sorted(
            set(historical_theorem_exclusions) | set(completed_ledger.excluded_theorem_lineage_ids)
        )
    )
    all_payload_exclusions = tuple(
        sorted(
            set(historical_payload_exclusions)
            | set(completed_ledger.excluded_judge_visible_payload_sha256s)
        )
    )
    selected = _selected(
        verified.records,
        partition_id=source_partition_id,
        offset_pairs=offset_pairs,
        limit_pairs=limit_pairs,
        excluded_pair_ids=all_exclusions,
        excluded_theorem_lineage_ids=all_theorem_exclusions,
        excluded_judge_visible_payload_sha256s=all_payload_exclusions,
    )
    source_binding = next(
        item
        for item in verified.manifest.source_partitions
        if item.partition_id == source_partition_id
    )
    source_manifest_path = verified.output_dir / source_binding.source_manifest_artifact
    source_manifest_model = _load_canonical_model(
        source_manifest_path, LF022SupervisionCandidateManifest
    )
    assert isinstance(source_manifest_model, LF022SupervisionCandidateManifest)
    source_manifest = source_manifest_model
    matrix_path, matrix = _matrix(repo)
    weak_config_path = _safe(repo / _WEAK_CONFIG, label="weak config", allow_missing=False)
    _validate_weak_config(weak_config_path)
    fable_config = load_claude_fable_judge_config(repo / _FABLE_CONFIG).config

    selected_lines: list[bytes] = []
    for item in selected:
        line = canonical_json_bytes(item.source_record.model_dump(mode="json")) + b"\n"
        if sha256_hex(line) != item.source_record_line_sha256:
            raise LF022SolFableBatchError("selected v3 source bytes do not replay")
        selected_lines.append(line)
    records_bytes = b"".join(selected_lines)
    spec_seed = hash_canonical(
        {
            "source_v4_inventory_id": verified.manifest.inventory_id,
            "source_partition_id": source_partition_id,
            "offset_pairs": offset_pairs,
            "historical_sol_xhigh_registry_id": historical_registry.registry_id,
            "historical_sol_xhigh_registry_sha256": hash_file(registry_path),
            "excluded_historical_sol_pair_ids": list(exclusions),
            "excluded_historical_sol_theorem_lineage_ids": list(historical_theorem_exclusions),
            "excluded_historical_sol_judge_visible_payload_sha256s": list(
                historical_payload_exclusions
            ),
            "completed_sol_fable_ledger_id": completed_ledger.ledger_id,
            "completed_sol_fable_pair_ids": list(completed_ledger.excluded_pair_ids),
            "completed_sol_fable_theorem_lineage_ids": list(
                completed_ledger.excluded_theorem_lineage_ids
            ),
            "completed_sol_fable_judge_visible_payload_sha256s": list(
                completed_ledger.excluded_judge_visible_payload_sha256s
            ),
            "historical_sol_xhigh_corpora": [
                item.model_dump(mode="json") for item in historical_corpora
            ],
            "selected_pair_ids": [item.pair_id for item in selected],
            "family_matrix_sha256": hash_file(matrix_path),
            "weak_config_sha256": hash_file(weak_config_path),
        }
    )
    candidate_manifest, sample_bytes, summary_bytes = _derived_candidate_manifest(
        source_manifest=source_manifest,
        selected=selected,
        records_bytes=records_bytes,
        selection_spec_seed_sha256=spec_seed,
    )
    candidate_manifest_path = input_dir / "candidate_manifest.json"
    candidate_records_path = input_dir / "candidates.jsonl"
    _immutable(
        candidate_manifest_path,
        canonical_json_bytes(candidate_manifest.model_dump(mode="json")) + b"\n",
        label="selected candidate manifest",
    )
    _immutable(candidate_records_path, records_bytes, label="selected candidate records")
    _immutable(input_dir / "public_sample.jsonl", sample_bytes, label="candidate sample")
    _immutable(input_dir / "summary.md", summary_bytes, label="candidate summary")
    matrix_copy = input_dir / "family_matrix.json"
    weak_copy = input_dir / "weak_supervision.yaml"
    _immutable(matrix_copy, matrix_path.read_bytes(), label="family matrix")
    _immutable(weak_copy, weak_config_path.read_bytes(), label="weak config")

    def bound(path: Path) -> BoundArtifact:
        return BoundArtifact(path=str(path), sha256=hash_file(path))

    sol_decoding = cast(
        dict[str, DecodingValue],
        {
            "reasoning_effort": _SOL_EFFORT,
            "structured_output": True,
            "system_prompt_sha256": (
                "4c1f6de9fb14818e7432a0b9bdac2ca9ef784cccfd2f73ebc8774a4d0d6588bf"
            ),
            "output_schema_sha256": (
                "9de1b73c98a5df344ac158f77ead4b1b6e118b4c2f5585335fd5a3bcf0dea4d4"
            ),
            "codex_cli_version": "codex-cli 0.144.1",
            "codex_binary_sha256": (
                "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
            ),
            "shell_tool_disabled": True,
            "sandbox": "read-only",
            "web_search": "disabled",
        },
    )
    fable_decoding = cast(
        dict[str, DecodingValue],
        {
            "effort": fable_config.effort,
            "system_prompt_sha256": fable_config.system_prompt_sha256,
            "output_schema_sha256": fable_config.output_schema_sha256,
            "claude_cli_version": fable_config.claude_cli_version,
            "claude_binary_sha256": fable_config.claude_binary_sha256,
            "structured_output": True,
            "safe_mode": True,
            "tools_disabled": True,
            "session_persistence": False,
        },
    )
    spec = LF022WeakBatchSpec(
        batch_name=(f"sol_fable_{source_partition_id}_offset{offset_pairs}_n{limit_pairs}_v4"),
        candidate_manifest=bound(candidate_manifest_path),
        candidate_records=bound(candidate_records_path),
        weak_supervision_config=bound(weak_copy),
        production_family_matrix=bound(matrix_copy),
        randomization_key_sha256=sha256_hex(randomization_key),
        judge_a=JudgeEndpointPin(
            provider_slot="judge_A",
            provider=_SOL_PROVIDER,
            model=_SOL_MODEL,
            family_id=_SOL_FAMILY,
            revision=_revision(matrix, _SOL_FAMILY),
            decoding=sol_decoding,
        ),
        judge_b=JudgeEndpointPin(
            provider_slot="judge_B",
            provider=FABLE_PROVIDER,
            model=fable_config.registry_model_id,
            family_id=FABLE_FAMILY,
            revision=_revision(matrix, FABLE_FAMILY),
            decoding=fable_decoding,
        ),
        primary_eval_family_id=_HELDOUT_FAMILY,
        execution_authorization=(
            "live_provider_calls_explicitly_authorized"
            if authorize_live_provider_calls
            else "offline_fixture_or_replay_only"
        ),
        live_provider_calls_authorized=authorize_live_provider_calls,
    )
    _validate_family_pins(spec, matrix_copy)
    spec_path = output / "authoring" / "weak_batch_spec.json"
    spec_sha = _immutable(
        spec_path,
        canonical_json_bytes(spec.model_dump(mode="json")) + b"\n",
        label="weak batch spec",
    )
    batch_root = output / "batch"
    dispatches, dispatch_manifest = prepare_lf022_weak_batch(
        repo_root=repo,
        spec_path=spec_path,
        expected_spec_sha256=spec_sha,
        randomization_key=randomization_key,
        output_dir=batch_root,
    )
    authoring_values: dict[str, object] = {
        "schema_version": 4,
        "method_version": SOL_FABLE_BATCH_METHOD_VERSION,
        "source_v4_artifact_path": str(source_v4_artifact),
        "source_v4_inventory_id": verified.manifest.inventory_id,
        "source_v4_manifest_sha256": hash_file(verified.manifest_path),
        "source_v4_records_sha256": verified.manifest.records_sha256,
        "source_partition_id": source_partition_id,
        "proposer_family_id": selected[0].proposer_family_id,
        "selection_method": (
            "deterministic_theorem_lineage_hash_with_registered_sol_xhigh_exclusions_v4"
        ),
        "lineage_diversity_status": (
            "distinct_source_theorem_lineages_not_full_ancestry_certified"
        ),
        "offset_pairs": offset_pairs,
        "excluded_historical_sol_pair_ids": exclusions,
        "excluded_historical_sol_pair_ids_sha256": hash_canonical(list(exclusions)),
        "excluded_historical_sol_theorem_lineage_ids": historical_theorem_exclusions,
        "excluded_historical_sol_theorem_lineage_ids_sha256": hash_canonical(
            list(historical_theorem_exclusions)
        ),
        "excluded_historical_sol_judge_visible_payload_sha256s": (historical_payload_exclusions),
        "excluded_historical_sol_judge_visible_payload_sha256s_sha256": hash_canonical(
            list(historical_payload_exclusions)
        ),
        "historical_sol_xhigh_registry_id": historical_registry.registry_id,
        "historical_sol_xhigh_registry_sha256": hash_file(registry_path),
        "historical_sol_xhigh_registry_artifact": "historical_sol_xhigh/registry.json",
        "historical_sol_xhigh_corpora": [
            item.model_dump(mode="json") for item in historical_corpora
        ],
        "historical_sol_xhigh_corpora_sha256": hash_canonical(
            [item.model_dump(mode="json") for item in historical_corpora]
        ),
        "historical_sol_xhigh_pair_count": len(exclusions),
        "historical_sol_xhigh_theorem_lineage_count": len(historical_theorem_exclusions),
        "historical_sol_xhigh_judge_visible_payload_count": len(historical_payload_exclusions),
        "historical_sol_xhigh_exclusion_complete": True,
        "selected_pairs_absent_from_historical_sol_xhigh": True,
        "selected_theorem_lineages_absent_from_historical_sol_xhigh": True,
        "selected_payloads_absent_from_historical_sol_xhigh": True,
        "completed_sol_fable_ledger_id": completed_ledger.ledger_id,
        "completed_sol_fable_ledger_sha256": hash_file(
            input_dir / "completed_sol_fable" / "ledger.json"
        ),
        "completed_sol_fable_ledger_artifact": "completed_sol_fable/ledger.json",
        "completed_sol_fable_pair_ids": completed_ledger.excluded_pair_ids,
        "completed_sol_fable_pair_ids_sha256": completed_ledger.excluded_pair_ids_sha256,
        "completed_sol_fable_theorem_lineage_ids": (completed_ledger.excluded_theorem_lineage_ids),
        "completed_sol_fable_theorem_lineage_ids_sha256": (
            completed_ledger.excluded_theorem_lineage_ids_sha256
        ),
        "completed_sol_fable_judge_visible_payload_sha256s": (
            completed_ledger.excluded_judge_visible_payload_sha256s
        ),
        "completed_sol_fable_judge_visible_payload_sha256s_sha256": (
            completed_ledger.excluded_judge_visible_payload_sha256s_sha256
        ),
        "selected_pairs_absent_from_completed_sol_fable": True,
        "selected_theorem_lineages_absent_from_completed_sol_fable": True,
        "selected_payloads_absent_from_completed_sol_fable": True,
        "selected_pair_count": len(selected),
        "unique_source_theorem_lineage_count": len({_theorem_lineage(item) for item in selected}),
        "selected_pair_ids": tuple(item.pair_id for item in selected),
        "selected_source_record_ids": tuple(
            item.source_candidate_inventory_record_id for item in selected
        ),
        "selected_source_theorem_lineage_ids": tuple(_theorem_lineage(item) for item in selected),
        "selected_source_line_sha256s": tuple(item.source_record_line_sha256 for item in selected),
        "selected_judge_visible_payload_sha256s": tuple(
            item.source_record.judge_visible_payload_sha256 for item in selected
        ),
        "selected_source_theorem_lineage_ids_sha256": hash_canonical(
            [_theorem_lineage(item) for item in selected]
        ),
        "selected_source_record_ids_sha256": hash_canonical(
            [item.source_candidate_inventory_record_id for item in selected]
        ),
        "selected_source_line_sha256s_sha256": hash_canonical(
            [item.source_record_line_sha256 for item in selected]
        ),
        "selected_judge_visible_payload_sha256s_sha256": hash_canonical(
            [item.source_record.judge_visible_payload_sha256 for item in selected]
        ),
        "selected_pair_ids_sha256": hash_canonical([item.pair_id for item in selected]),
        "selected_source_bytes_preserved": True,
        "candidate_manifest_sha256": hash_file(candidate_manifest_path),
        "candidate_records_sha256": hash_file(candidate_records_path),
        "weak_config_sha256": hash_file(weak_copy),
        "family_matrix_sha256": hash_file(matrix_copy),
        "candidate_manifest_selection_spec_seed_sha256": spec_seed,
        "randomization_key_sha256": sha256_hex(randomization_key),
        "randomization_key_persisted_in_bundle": False,
        "randomization_key_reconstruction_prerequisite": (
            "external_secret_bytes_matching_persisted_sha256_required"
        ),
        "regeneration_completeness": (
            "requires_bound_v4_artifact_and_external_randomization_key_bytes"
        ),
        "weak_batch_spec_sha256": spec_sha,
        "dispatch_manifest_sha256": hash_file(batch_root / "dispatch_manifest.json"),
        "weak_batch_id": dispatch_manifest.batch_id,
        "dispatch_cell_count": len(dispatches),
        "judge_a_family_id": _SOL_FAMILY,
        "judge_b_family_id": FABLE_FAMILY,
        "primary_eval_family_id": _HELDOUT_FAMILY,
        "execution_authorization": spec.execution_authorization,
        "live_provider_calls_authorized": spec.live_provider_calls_authorized,
        "semantic_labels_created": False,
        "human_labels_created": False,
        "silver_records_created": False,
        "training_eligible": False,
        "evaluation_eligible": False,
        "gate_credit_claimed": False,
    }
    authoring = SolFableBatchAuthoringManifest.model_validate(
        {
            **authoring_values,
            "authoring_id": make_id(
                "lf022_sol_fable_authoring",
                _authoring_identity_payload(authoring_values),
            ),
        }
    )
    authoring_path = output / "authoring_manifest.json"
    _immutable(
        authoring_path,
        canonical_json_bytes(authoring.model_dump(mode="json")) + b"\n",
        label="authoring manifest",
    )
    return SolFablePreparedBatch(
        authoring=authoring,
        authoring_path=authoring_path,
        spec=spec,
        spec_path=spec_path,
        dispatches=dispatches,
        dispatch_manifest=dispatch_manifest,
        batch_root=batch_root,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--v4-root", type=Path, required=True)
    parser.add_argument("--source-partition", required=True)
    parser.add_argument("--offset-pairs", type=int, default=0)
    parser.add_argument("--limit-pairs", type=int, required=True)
    parser.add_argument("--randomization-key-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--historical-sol-xhigh-summary",
        type=Path,
        action="append",
        required=True,
        help="Complete reviewed Sol/xhigh summary; repeat for every required corpus.",
    )
    parser.add_argument("--authorize-live-provider-calls", action="store_true")
    arguments = parser.parse_args()
    key_path = _safe(
        arguments.randomization_key_file,
        label="randomization key file",
        allow_missing=False,
    )
    if not key_path.is_file():
        raise LF022SolFableBatchError("randomization key is not a regular file")
    result = prepare_lf022_sol_fable_batch_v1(
        repo_root=arguments.repo_root,
        v4_root=arguments.v4_root,
        source_partition_id=arguments.source_partition,
        offset_pairs=arguments.offset_pairs,
        limit_pairs=arguments.limit_pairs,
        randomization_key=key_path.read_bytes(),
        output_dir=arguments.output_dir,
        historical_sol_xhigh_summary_paths=tuple(arguments.historical_sol_xhigh_summary),
        authorize_live_provider_calls=arguments.authorize_live_provider_calls,
    )
    print(result.authoring.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "SOL_FABLE_BATCH_METHOD_VERSION",
    "HistoricalSolXhighCorpusBinding",
    "LF022SolFableBatchError",
    "SolFableBatchAuthoringManifest",
    "SolFablePreparedBatch",
    "prepare_lf022_sol_fable_batch_v1",
]
