"""Materialize overlap-v3 records for the exact 20-problem cross-domain pool."""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.generation import research_collection as _v1
from leanfaith.generation.research_collection_v3 import (
    _load_canonical_mapping,
    _load_problem_records_v3,
    _nested_manifest_binding,
    _resolve_pool_binding,
    cross_domain_pool_source_evidence_sha256,
)
from leanfaith.generation.research_overlap import PublicSourceIntroduction
from leanfaith.generation.research_overlap_v3 import ResearchFamilyOverlapRecordV3
from leanfaith.schemas.nl_lean import ProblemPoolRecord

_HEX64 = r"^[0-9a-f]{64}$"
_BUNDLE_ID = r"^research_overlap_bundle_v3:[0-9a-f]{64}$"


class ResearchOverlapMaterializationError(RuntimeError):
    """The exact scalable overlap evidence cannot be reproduced."""


class ResearchOverlapBundleManifestV3(StrictModel):
    """Immutable index of the three exact overlap-v3 records."""

    schema_version: Literal[3] = 3
    record_kind: Literal["lf021_research_overlap_bundle_v3"] = "lf021_research_overlap_bundle_v3"
    bundle_id: str = Field(pattern=_BUNDLE_ID)
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    problem_pool_records_sha256: str = Field(pattern=_HEX64)
    problem_pool_manifest_sha256: str = Field(pattern=_HEX64)
    curation_decisions_sha256: str = Field(pattern=_HEX64)
    selected_candidates_sha256: str = Field(pattern=_HEX64)
    public_source_evidence_sha256: str = Field(pattern=_HEX64)
    qualification_collection_config_sha256: str = Field(pattern=_HEX64)
    family_artifacts: dict[str, _v1.ResearchArtifactBinding]
    problem_record_ids: tuple[str, ...] = Field(min_length=1)
    semantic_labels_created: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False
    heldout_claim_allowed: Literal[False] = False
    unseen_claim_allowed: Literal[False] = False
    evaluation_claim_allowed: Literal[False] = False

    def id_payload(self) -> dict[str, object]:
        return {
            key: value for key, value in self.model_dump(mode="json").items() if key != "bundle_id"
        }

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if len(self.problem_record_ids) != self.problem_count:
            raise ValueError("overlap bundle problem IDs do not reconcile")
        if self.problem_record_ids != tuple(sorted(set(self.problem_record_ids))):
            raise ValueError("overlap bundle problem IDs must be sorted and unique")
        if len(self.family_artifacts) != self.family_count:
            raise ValueError("overlap bundle requires exactly three family artifacts")
        if list(self.family_artifacts) != sorted(self.family_artifacts):
            raise ValueError("overlap family artifacts must be sorted")
        expected = "research_overlap_bundle_v3:" + hash_canonical(
            {"schema": "lf021_research_overlap_bundle_v3", **self.id_payload()}
        )
        if self.bundle_id != expected:
            raise ValueError("overlap bundle ID differs from immutable payload")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedResearchOverlapV3:
    output_directory: Path
    manifest_path: Path
    manifest: ResearchOverlapBundleManifestV3
    records: tuple[ResearchFamilyOverlapRecordV3, ...]


def _parse_utc(value: object, *, field: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise ResearchOverlapMaterializationError(f"{field} is not an ISO timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchOverlapMaterializationError(f"{field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ResearchOverlapMaterializationError(f"{field} lacks a timezone")
    return parsed


def _load_curation_by_candidate(path: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise ResearchOverlapMaterializationError("cannot read curation-admitted artifact") from exc
    for line in lines:
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResearchOverlapMaterializationError("curation-admitted JSONL is invalid") from exc
        if not isinstance(document, dict) or line != canonical_json_bytes(document):
            raise ResearchOverlapMaterializationError("curation-admitted JSONL is not canonical")
        candidate_id = document.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in records:
            raise ResearchOverlapMaterializationError(
                "curation candidate IDs must be nonempty and unique"
            )
        records[candidate_id] = cast(dict[str, object], document)
    return records


def _load_selected_by_candidate(path: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise ResearchOverlapMaterializationError(
            "cannot read selected-candidates artifact"
        ) from exc
    for line in lines:
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResearchOverlapMaterializationError(
                "selected-candidates JSONL is invalid"
            ) from exc
        if not isinstance(document, dict) or line != canonical_json_bytes(document):
            raise ResearchOverlapMaterializationError("selected-candidates JSONL is not canonical")
        candidate_id = document.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in records:
            raise ResearchOverlapMaterializationError(
                "selected-candidate IDs must be nonempty and unique"
            )
        records[candidate_id] = cast(dict[str, object], document)
    return records


def _source_introductions(
    *,
    problems: tuple[ProblemPoolRecord, ...],
    curation_by_candidate: dict[str, dict[str, object]],
    selected_by_candidate: dict[str, dict[str, object]],
    source_revision: str,
) -> tuple[PublicSourceIntroduction, ...]:
    introductions: list[PublicSourceIntroduction] = []
    for problem in problems:
        candidate_id = problem.metadata.get("candidate_id")
        if not isinstance(candidate_id, str):
            raise ResearchOverlapMaterializationError(
                "problem record lacks its curation candidate ID"
            )
        curation = curation_by_candidate.get(candidate_id)
        if curation is None:
            raise ResearchOverlapMaterializationError(
                "problem has no exact cross-domain curation decision"
            )
        selected = selected_by_candidate.get(candidate_id)
        if selected is None:
            raise ResearchOverlapMaterializationError(
                "problem has no exact selected-candidate source record"
            )
        temporal = selected.get("exact_pair_introduction")
        if not isinstance(temporal, dict):
            raise ResearchOverlapMaterializationError(
                "selected candidate lacks exact-pair temporal evidence"
            )
        theorem = selected.get("theorem")
        if not isinstance(theorem, dict):
            raise ResearchOverlapMaterializationError("selected candidate lacks theorem provenance")
        commit = temporal.get("introduction_commit")
        if (
            not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", commit) is None
            or commit != problem.metadata.get("temporal_introduction_commit")
            or temporal.get("search_revision") != source_revision
            or temporal.get("exact_pair_present_in_introduction_blob") is not True
            or temporal.get("first_pickaxe_occurrence_with_exact_pair") is not True
            or temporal.get("introduction_commit_is_search_revision_ancestor") is not True
            or temporal.get("strictly_postdates_latest_checkpoint") is not True
            or theorem.get("declaration_full_name")
            != problem.metadata.get("source_declaration_full_name")
            or curation.get("declaration_full_name")
            != problem.metadata.get("source_declaration_full_name")
            or curation.get("decision") != "standalone_sufficient"
            or curation.get("model_collection_authorized") is not True
            or curation.get("authorization_scope") != "local_models_only"
            or curation.get("reference_visible_to_generator") is not False
            or curation.get("semantic_labels_created") is not False
            or curation.get("gate_claimed") is not False
            or selected.get("problem_pool_admitted") is not False
            or selected.get("semantic_labels_created") is not False
            or selected.get("gate_claimed") is not False
            or selected.get("model_execution_performed") is not False
        ):
            raise ResearchOverlapMaterializationError(
                "curation temporal/policy evidence differs from the problem record"
            )
        introductions.append(
            PublicSourceIntroduction(
                problem_record_id=problem.problem_record_id,
                problem_id=problem.problem_id,
                introduction_commit=commit,
                introduction_created_at=_parse_utc(
                    temporal.get("introduction_created_at"),
                    field="introduction_created_at",
                ),
            )
        )
    ordered = tuple(sorted(introductions, key=lambda item: item.problem_record_id))
    if tuple(introductions) != ordered:
        raise ResearchOverlapMaterializationError(
            "problem records must produce sorted source introductions"
        )
    return ordered


def materialize_research_overlap_v3(
    *,
    repo_root: Path,
    qualification_collection_config: Path,
    problem_pool_records: Path,
    problem_pool_manifest: Path,
    output_directory: Path,
) -> MaterializedResearchOverlapV3:
    """Build or exactly replay three v3 overlap records without model execution."""

    root = repo_root.resolve()
    prior = _v1.load_research_collection(
        qualification_collection_config,
        repo_root=root,
    )
    problems = _load_problem_records_v3(problem_pool_records)
    manifest_document = _load_canonical_mapping(problem_pool_manifest)
    if manifest_document.get("problem_count") != len(problems) or manifest_document.get(
        "problem_record_ids"
    ) != [problem.problem_record_id for problem in problems]:
        raise ResearchOverlapMaterializationError("pool records differ from their exact manifest")
    curation_binding = _nested_manifest_binding(
        manifest_document,
        "output_artifacts",
        "curation_decisions",
    )
    selected_binding = _nested_manifest_binding(
        manifest_document,
        "input_artifacts",
        "selected_candidates",
    )
    curation_by_candidate = _load_curation_by_candidate(
        _resolve_pool_binding(root, curation_binding)
    )
    selected_by_candidate = _load_selected_by_candidate(
        _resolve_pool_binding(root, selected_binding)
    )
    source_revision = manifest_document.get("source_revision")
    if not isinstance(source_revision, str):
        raise ResearchOverlapMaterializationError("pool source revision is missing")
    introductions = _source_introductions(
        problems=problems,
        curation_by_candidate=curation_by_candidate,
        selected_by_candidate=selected_by_candidate,
        source_revision=source_revision,
    )
    benchmark_binding = _nested_manifest_binding(
        manifest_document,
        "input_artifacts",
        "active_benchmark_manifest",
    )
    active_registry_sha = manifest_document.get("active_benchmark_registry_sha256")
    if not isinstance(active_registry_sha, str):
        raise ResearchOverlapMaterializationError("pool active benchmark registry hash is missing")
    public_source_hash = cross_domain_pool_source_evidence_sha256(manifest_document)
    pool_records_hash = hash_file(problem_pool_records)
    pool_manifest_hash = hash_file(problem_pool_manifest)

    records: list[ResearchFamilyOverlapRecordV3] = []
    artifact_bindings: dict[str, _v1.ResearchArtifactBinding] = {}
    for family_id in sorted(prior.qualifications):
        evidence = prior.activation_evidence[family_id]
        baseline = evidence.overlap_record
        if baseline is None:
            raise ResearchOverlapMaterializationError(
                f"v1 activation lacks overlap evidence: {family_id}"
            )
        record = ResearchFamilyOverlapRecordV3.create(
            family_id=family_id,
            model_repo_id=baseline.model_repo_id,
            model_revision=baseline.model_revision,
            checkpoint_probe=baseline.checkpoint_probe,
            pinned_readme_sha256=baseline.pinned_readme_sha256,
            training_lineage_disclosure=baseline.training_lineage_disclosure,
            problem_pool_records_sha256=pool_records_hash,
            problem_pool_manifest_sha256=pool_manifest_hash,
            active_benchmark_manifest_sha256=benchmark_binding.sha256,
            active_benchmark_registry_sha256=active_registry_sha,
            public_source_evidence_sha256=public_source_hash,
            problem_count=len(problems),
            source_introductions=introductions,
            interpretation=(
                "temporal_non_overlap_only_semantic_and_pretraining_contamination_unknown"
            ),
        )
        filename = re.sub(r"[^a-z0-9]+", "_", family_id.lower()).strip("_")
        path = output_directory / f"{filename}.json"
        digest = _v1._write_immutable(
            path,
            _v1._canonical_record_bytes(record),
        )
        try:
            relative = str(path.resolve().relative_to(root))
        except ValueError as exc:
            raise ResearchOverlapMaterializationError(
                "overlap output directory must be inside the repository"
            ) from exc
        artifact_bindings[family_id] = _v1.ResearchArtifactBinding(
            artifact=relative,
            sha256=digest,
        )
        records.append(record)

    manifest_payload: dict[str, object] = {
        "schema_version": 3,
        "record_kind": "lf021_research_overlap_bundle_v3",
        "problem_count": len(problems),
        "family_count": 3,
        "problem_pool_records_sha256": pool_records_hash,
        "problem_pool_manifest_sha256": pool_manifest_hash,
        "curation_decisions_sha256": curation_binding.sha256,
        "selected_candidates_sha256": selected_binding.sha256,
        "public_source_evidence_sha256": public_source_hash,
        "qualification_collection_config_sha256": hash_file(qualification_collection_config),
        "family_artifacts": {
            family_id: binding.model_dump(mode="json")
            for family_id, binding in sorted(artifact_bindings.items())
        },
        "problem_record_ids": [problem.problem_record_id for problem in problems],
        "semantic_labels_created": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "heldout_claim_allowed": False,
        "unseen_claim_allowed": False,
        "evaluation_claim_allowed": False,
    }
    bundle_id = "research_overlap_bundle_v3:" + hash_canonical(
        {"schema": "lf021_research_overlap_bundle_v3", **manifest_payload}
    )
    bundle = ResearchOverlapBundleManifestV3.model_validate(
        {"bundle_id": bundle_id, **manifest_payload}
    )
    manifest_path = output_directory / "bundle_manifest.json"
    _v1._write_immutable(
        manifest_path,
        _v1._canonical_record_bytes(bundle),
    )
    return MaterializedResearchOverlapV3(
        output_directory=output_directory,
        manifest_path=manifest_path,
        manifest=bundle,
        records=tuple(records),
    )


__all__ = [
    "MaterializedResearchOverlapV3",
    "ResearchOverlapBundleManifestV3",
    "ResearchOverlapMaterializationError",
    "materialize_research_overlap_v3",
]
