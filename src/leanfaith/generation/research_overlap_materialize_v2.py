"""Deterministically materialize overlap-v2 records for the 40-problem pool."""

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
from leanfaith.generation.research_collection_v2 import (
    _load_canonical_mapping,
    _load_problem_records_v2,
    _manifest_binding,
    _resolve_pool_binding,
    scalable_pool_source_evidence_sha256,
)
from leanfaith.generation.research_overlap import PublicSourceIntroduction
from leanfaith.generation.research_overlap_v2 import ResearchFamilyOverlapRecordV2
from leanfaith.schemas.nl_lean import ProblemPoolRecord

_HEX64 = r"^[0-9a-f]{64}$"
_BUNDLE_ID = r"^research_overlap_bundle_v2:[0-9a-f]{64}$"


class ResearchOverlapMaterializationError(RuntimeError):
    """The exact scalable overlap evidence cannot be reproduced."""


class ResearchOverlapBundleManifestV2(StrictModel):
    """Immutable index of the three exact overlap-v2 records."""

    schema_version: Literal[2] = 2
    record_kind: Literal["lf021_research_overlap_bundle_v2"] = "lf021_research_overlap_bundle_v2"
    bundle_id: str = Field(pattern=_BUNDLE_ID)
    problem_count: int = Field(ge=1)
    family_count: Literal[3] = 3
    problem_pool_records_sha256: str = Field(pattern=_HEX64)
    problem_pool_manifest_sha256: str = Field(pattern=_HEX64)
    curation_admitted_sha256: str = Field(pattern=_HEX64)
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
        expected = "research_overlap_bundle_v2:" + hash_canonical(
            {"schema": "lf021_research_overlap_bundle_v2", **self.id_payload()}
        )
        if self.bundle_id != expected:
            raise ValueError("overlap bundle ID differs from immutable payload")
        return self


@dataclass(frozen=True, slots=True)
class MaterializedResearchOverlapV2:
    output_directory: Path
    manifest_path: Path
    manifest: ResearchOverlapBundleManifestV2
    records: tuple[ResearchFamilyOverlapRecordV2, ...]


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
        source_candidate = document.get("source_candidate")
        if not isinstance(source_candidate, dict):
            raise ResearchOverlapMaterializationError("curation record lacks source_candidate")
        candidate_id = source_candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in records:
            raise ResearchOverlapMaterializationError(
                "curation candidate IDs must be nonempty and unique"
            )
        records[candidate_id] = cast(dict[str, object], document)
    return records


def _source_introductions(
    *,
    problems: tuple[ProblemPoolRecord, ...],
    curation_by_candidate: dict[str, dict[str, object]],
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
                "problem has no exact curation-admitted record"
            )
        source_candidate = curation.get("source_candidate")
        assert isinstance(source_candidate, dict)
        temporal = source_candidate.get("temporal_introduction")
        if not isinstance(temporal, dict):
            raise ResearchOverlapMaterializationError(
                "curation record lacks temporal introduction evidence"
            )
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
            or source_candidate.get("declaration_full_name")
            != problem.metadata.get("source_declaration_full_name")
            or source_candidate.get("problem_pool_admitted") is not False
            or curation.get("semantic_labels_created") is not False
            or curation.get("gate_claimed") is not False
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


def materialize_research_overlap_v2(
    *,
    repo_root: Path,
    qualification_collection_config: Path,
    problem_pool_records: Path,
    problem_pool_manifest: Path,
    output_directory: Path,
) -> MaterializedResearchOverlapV2:
    """Build or exactly replay three v2 overlap records without model execution."""

    root = repo_root.resolve()
    prior = _v1.load_research_collection(
        qualification_collection_config,
        repo_root=root,
    )
    problems = _load_problem_records_v2(problem_pool_records)
    manifest_document = _load_canonical_mapping(problem_pool_manifest)
    if manifest_document.get("problem_count") != len(problems) or manifest_document.get(
        "problem_record_ids"
    ) != [problem.problem_record_id for problem in problems]:
        raise ResearchOverlapMaterializationError("pool records differ from their exact manifest")
    admitted_binding = _manifest_binding(
        manifest_document,
        "curation_admitted_artifact",
    )
    admitted_path = _resolve_pool_binding(root, admitted_binding)
    curation_by_candidate = _load_curation_by_candidate(admitted_path)
    source_revision = manifest_document.get("source_revision")
    if not isinstance(source_revision, str):
        raise ResearchOverlapMaterializationError("pool source revision is missing")
    introductions = _source_introductions(
        problems=problems,
        curation_by_candidate=curation_by_candidate,
        source_revision=source_revision,
    )
    benchmark_binding = _manifest_binding(
        manifest_document,
        "active_benchmark_manifest_artifact",
    )
    active_registry_sha = manifest_document.get("active_benchmark_registry_sha256")
    if not isinstance(active_registry_sha, str):
        raise ResearchOverlapMaterializationError("pool active benchmark registry hash is missing")
    public_source_hash = scalable_pool_source_evidence_sha256(manifest_document)
    pool_records_hash = hash_file(problem_pool_records)
    pool_manifest_hash = hash_file(problem_pool_manifest)

    records: list[ResearchFamilyOverlapRecordV2] = []
    artifact_bindings: dict[str, _v1.ResearchArtifactBinding] = {}
    for family_id in sorted(prior.qualifications):
        evidence = prior.activation_evidence[family_id]
        baseline = evidence.overlap_record
        if baseline is None:
            raise ResearchOverlapMaterializationError(
                f"v1 activation lacks overlap evidence: {family_id}"
            )
        record = ResearchFamilyOverlapRecordV2.create(
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
        "schema_version": 2,
        "record_kind": "lf021_research_overlap_bundle_v2",
        "problem_count": len(problems),
        "family_count": 3,
        "problem_pool_records_sha256": pool_records_hash,
        "problem_pool_manifest_sha256": pool_manifest_hash,
        "curation_admitted_sha256": admitted_binding.sha256,
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
    bundle_id = "research_overlap_bundle_v2:" + hash_canonical(
        {"schema": "lf021_research_overlap_bundle_v2", **manifest_payload}
    )
    bundle = ResearchOverlapBundleManifestV2.model_validate(
        {"bundle_id": bundle_id, **manifest_payload}
    )
    manifest_path = output_directory / "bundle_manifest.json"
    _v1._write_immutable(
        manifest_path,
        _v1._canonical_record_bytes(bundle),
    )
    return MaterializedResearchOverlapV2(
        output_directory=output_directory,
        manifest_path=manifest_path,
        manifest=bundle,
        records=tuple(records),
    )


__all__ = [
    "MaterializedResearchOverlapV2",
    "ResearchOverlapBundleManifestV2",
    "ResearchOverlapMaterializationError",
    "materialize_research_overlap_v2",
]
