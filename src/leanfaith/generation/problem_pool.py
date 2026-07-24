"""Deterministic LF-021 problem-pool construction.

The builder is deliberately policy-only.  It performs no source reads, no
network calls, and no near-duplicate inference.  Callers provide typed source
candidates and an already-preflighted active benchmark ``DenylistIndex``.
Every input candidate produces exactly one terminal ``ProblemPoolRecord``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import (
    ActiveBenchmarkRegistry,
    DenylistIndex,
    nl_hash,
    normalize_nl,
)
from leanfaith.generation.config import ProblemPoolConfig, ProblemPoolSourceConfig
from leanfaith.generation.prompts import PublicTrustedProblem
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.ids import (
    HEX64_PATTERN,
    THEOREM_PREFIX,
    id_pattern,
)
from leanfaith.schemas.nl_lean import (
    MetadataValue,
    ProblemPoolRecord,
    make_problem_record_id,
)


class ProblemPoolBuildError(ValueError):
    """The requested pool cannot be built under the frozen policy."""


class ProblemPoolExclusionReason(StrEnum):
    """Stable LF-021 terminal exclusion reason codes."""

    SOURCE_NOT_CONFIGURED = "source_not_configured"
    SOURCE_DISABLED = "source_disabled"
    NL_TRUST_NOT_ALLOWED = "nl_trust_not_allowed"
    MISSING_REFERENCE_THEOREM = "missing_reference_theorem"
    DENYLIST_HIT = "denylist_hit"
    PROTECTED_EXACT_DUPLICATE = "protected_exact_duplicate"
    EXACT_NORMALIZED_NL_DUPLICATE = "exact_normalized_nl_duplicate"
    SUPPLIED_NEAR_DUPLICATE = "supplied_near_duplicate"


class ProblemPoolCandidate(StrictModel):
    """One typed, pre-extracted input to LF-021 pool selection.

    ``near_duplicate_group_ids`` are upstream/frozen group assignments.  This
    module never infers new groups from textual similarity.
    """

    schema_version: Literal[1] = 1
    problem_id: str = Field(min_length=1)
    problem_group: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    source_split: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_record_content_hash: str = Field(pattern=HEX64_PATTERN)
    nl_statement: str = Field(min_length=1)
    nl_trust: NLTrust
    nl_source_link: str = Field(min_length=1)
    context_id: str = Field(pattern=id_pattern("ctx"))
    import_header_artifact: str = Field(min_length=1)
    import_header_hash: str = Field(pattern=HEX64_PATTERN)
    reference_theorem_ids: tuple[str, ...] = ()
    source_license: str = Field(min_length=1)
    private_source_content: bool
    release_eligible: bool
    denylist_row_ids: tuple[str, ...] = ()
    near_duplicate_group_ids: tuple[str, ...] = ()
    overlap_tags: tuple[str, ...] = ()
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> ProblemPoolCandidate:
        for field_name in (
            "reference_theorem_ids",
            "denylist_row_ids",
            "near_duplicate_group_ids",
            "overlap_tags",
        ):
            values = getattr(self, field_name)
            if list(values) != sorted(set(values)):
                raise ValueError(f"{field_name} must be sorted and unique")
        theorem_pattern = id_pattern(THEOREM_PREFIX)
        for theorem_id in self.reference_theorem_ids:
            if re.fullmatch(theorem_pattern, theorem_id) is None:
                raise ValueError(f"reference theorem ID {theorem_id!r} is not a 'thm:' ID")
        for field_name in (
            "problem_id",
            "problem_group",
            "source",
            "source_revision",
            "source_split",
            "source_record_id",
            "nl_statement",
            "nl_source_link",
            "import_header_artifact",
            "source_license",
        ):
            value = getattr(self, field_name)
            if not value.strip() or "\x00" in value:
                raise ValueError(f"{field_name} must contain non-whitespace text without NUL")
        return self

    @property
    def problem_record_id(self) -> str:
        return make_problem_record_id(
            source=self.source,
            source_revision=self.source_revision,
            source_split=self.source_split,
            source_record_id=self.source_record_id,
            problem_id=self.problem_id,
        )


@dataclass(frozen=True, slots=True)
class ProblemPoolBuildResult:
    """Deterministically ordered terminal records and prompt-safe projection."""

    records: tuple[ProblemPoolRecord, ...]
    public_trusted_problems: tuple[PublicTrustedProblem, ...]


@dataclass(frozen=True, slots=True)
class ProblemPoolDenylistBinding:
    """Cryptographic provenance for the exact denylist index used by a pool."""

    index: DenylistIndex
    manifest_path: str
    manifest_sha256: str
    active_registry_sha256: str
    registry_content_hash: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.manifest_path)
        if not self.manifest_path.strip() or path.is_absolute() or ".." in path.parts:
            raise ValueError("denylist manifest_path must be a repository-relative path")
        for field_name in (
            "manifest_sha256",
            "active_registry_sha256",
            "registry_content_hash",
        ):
            value = getattr(self, field_name)
            if re.fullmatch(HEX64_PATTERN, value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256")
        if self.registry_content_hash != self.index.registry_content_hash:
            raise ValueError("denylist registry_content_hash does not match the index")

    @classmethod
    def from_active_registry(
        cls,
        registry: ActiveBenchmarkRegistry,
        *,
        repo_root: Path,
    ) -> ProblemPoolDenylistBinding:
        """Bind a fully preflighted active benchmark registry for pool use."""

        root = repo_root.resolve()
        try:
            manifest_path = str(registry.manifest_path.resolve().relative_to(root))
        except ValueError as exc:
            raise ProblemPoolBuildError(
                "active benchmark manifest must reside under the repository root"
            ) from exc
        manifest_sha256 = hash_file(registry.manifest_path)
        active_registry_sha256 = hash_file(registry.active_registry_path)
        if manifest_sha256 != hash_file(registry.manifest_path):
            raise AssertionError("benchmark manifest changed during binding")
        if active_registry_sha256 != registry.manifest.active_registry.sha256:
            raise ProblemPoolBuildError(
                "active benchmark registry hash differs from its pointer manifest"
            )
        return cls(
            index=registry.index,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            active_registry_sha256=active_registry_sha256,
            registry_content_hash=registry.index.registry_content_hash,
        )


@dataclass(frozen=True, slots=True)
class _CandidateState:
    candidate: ProblemPoolCandidate
    source_policy: ProblemPoolSourceConfig | None
    private_source_content: bool
    denylist_hits: tuple[str, ...]
    reasons: tuple[str, ...]


def _denylist_hits(
    candidate: ProblemPoolCandidate,
    denylist_index: DenylistIndex,
) -> tuple[str, ...]:
    hits: set[str] = set()
    identity_keys = set(candidate.denylist_row_ids)
    identity_keys.add(candidate.source_record_id)
    identity_keys.add(candidate.problem_id)
    for row_id in sorted(identity_keys):
        if denylist_index.contains_row_id(row_id):
            hits.add(f"row_id:{row_id}")
    if denylist_index.contains_nl(candidate.nl_statement):
        hits.add(f"normalized_nl:{nl_hash(candidate.nl_statement)}")
    return tuple(sorted(hits))


def _base_state(
    candidate: ProblemPoolCandidate,
    *,
    source_policies: dict[str, ProblemPoolSourceConfig],
    denylist_index: DenylistIndex,
) -> _CandidateState:
    policy = source_policies.get(candidate.source)
    reasons: set[str] = set()
    if policy is None:
        reasons.add(ProblemPoolExclusionReason.SOURCE_NOT_CONFIGURED.value)
    else:
        if not policy.enabled:
            reasons.add(ProblemPoolExclusionReason.SOURCE_DISABLED.value)
        if candidate.nl_trust not in policy.allowed_trust:
            reasons.add(ProblemPoolExclusionReason.NL_TRUST_NOT_ALLOWED.value)

    # ProblemPoolRecord itself requires a reference for every eligible real-
    # output problem, even if a future source policy relaxes its local flag.
    if not candidate.reference_theorem_ids:
        reasons.add(ProblemPoolExclusionReason.MISSING_REFERENCE_THEOREM.value)

    hits = _denylist_hits(candidate, denylist_index)
    if hits:
        reasons.add(ProblemPoolExclusionReason.DENYLIST_HIT.value)

    # Privacy is fail-closed: either the source policy or the row provenance
    # can make content private; neither can downgrade the other.
    private_source_content = candidate.private_source_content or bool(
        policy is not None and policy.private_source
    )
    return _CandidateState(
        candidate=candidate,
        source_policy=policy,
        private_source_content=private_source_content,
        denylist_hits=hits,
        reasons=tuple(sorted(reasons)),
    )


def _apply_exact_dedup(
    states: tuple[_CandidateState, ...],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    reasons_by_id = {state.candidate.problem_record_id: set(state.reasons) for state in states}
    exact_duplicate_of: dict[str, str] = {}
    states_by_normalized_nl: dict[str, list[_CandidateState]] = {}
    for state in states:
        states_by_normalized_nl.setdefault(
            normalize_nl(state.candidate.nl_statement),
            [],
        ).append(state)

    for normalized_group in states_by_normalized_nl.values():
        protected_ids = sorted(
            state.candidate.problem_record_id for state in normalized_group if state.denylist_hits
        )
        if protected_ids:
            # Exact copies inherit protection even when the frozen registry
            # knows only another copy's row identity.  Link every copy to a
            # deterministic protected record so its denylist provenance stays
            # inspectable.
            protected_canonical = protected_ids[0]
            for state in normalized_group:
                record_id = state.candidate.problem_record_id
                if record_id == protected_canonical:
                    continue
                reasons_by_id[record_id].add(
                    ProblemPoolExclusionReason.PROTECTED_EXACT_DUPLICATE.value
                )
                exact_duplicate_of[record_id] = protected_canonical
            continue

        eligible_ids = sorted(
            state.candidate.problem_record_id
            for state in normalized_group
            if not reasons_by_id[state.candidate.problem_record_id]
        )
        if not eligible_ids:
            continue
        canonical = eligible_ids[0]
        for record_id in eligible_ids[1:]:
            reasons_by_id[record_id].add(
                ProblemPoolExclusionReason.EXACT_NORMALIZED_NL_DUPLICATE.value
            )
            exact_duplicate_of[record_id] = canonical
    return reasons_by_id, exact_duplicate_of


def _apply_supplied_near_duplicate_groups(
    states: tuple[_CandidateState, ...],
    reasons_by_id: dict[str, set[str]],
) -> dict[str, str]:
    """Deduplicate connected supplied groups without deriving fuzzy matches."""

    eligible_states = [
        state
        for state in states
        if not reasons_by_id[state.candidate.problem_record_id]
        and state.candidate.near_duplicate_group_ids
    ]
    if not eligible_states:
        return {}

    parent = {
        state.candidate.problem_record_id: state.candidate.problem_record_id
        for state in eligible_states
    }

    def find(record_id: str) -> str:
        while parent[record_id] != record_id:
            parent[record_id] = parent[parent[record_id]]
            record_id = parent[record_id]
        return record_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        canonical, other = sorted((left_root, right_root))
        parent[other] = canonical

    first_by_group: dict[str, str] = {}
    for state in eligible_states:
        record_id = state.candidate.problem_record_id
        for group_id in state.candidate.near_duplicate_group_ids:
            prior = first_by_group.setdefault(group_id, record_id)
            union(prior, record_id)

    components: dict[str, list[str]] = {}
    for record_id in sorted(parent):
        components.setdefault(find(record_id), []).append(record_id)

    canonical_by_duplicate: dict[str, str] = {}
    for members in components.values():
        if len(members) < 2:
            continue
        canonical = min(members)
        for record_id in sorted(members):
            if record_id == canonical:
                continue
            reasons_by_id[record_id].add(ProblemPoolExclusionReason.SUPPLIED_NEAR_DUPLICATE.value)
            canonical_by_duplicate[record_id] = canonical
    return canonical_by_duplicate


def to_public_trusted_problem(
    record: ProblemPoolRecord,
) -> PublicTrustedProblem:
    """Project one eligible public trusted record into the prompt contract."""

    if (
        record.eligibility != "eligible"
        or record.nl_trust is not NLTrust.TRUSTED
        or record.private_source_content
        or not record.external_provider_eligible
        or not record.denylist_checked
        or record.denylist_hits
    ):
        raise ProblemPoolBuildError(
            "PublicTrustedProblem requires an eligible, public, trusted, "
            "externally transmissible, denylist-cleared record"
        )
    if (
        record.source_license is None
        or not record.source_license.strip()
        or "\x00" in record.source_license
    ):
        raise ProblemPoolBuildError("public prompt projection requires a bound source license")
    return PublicTrustedProblem(
        problem_record_id=record.problem_record_id,
        problem_id=record.problem_id,
        problem_group=record.problem_group,
        nl_statement=record.nl_statement,
        nl_source_link=record.nl_source_link,
        nl_trust=record.nl_trust,
        source_id=record.source,
        source_revision=record.source_revision,
        source_license=record.source_license,
        source_is_public=True,
        external_transmission_allowed=True,
        denylist_checked=True,
        denylist_hits=(),
    )


def build_problem_pool(
    *,
    config: ProblemPoolConfig,
    denylist: ProblemPoolDenylistBinding,
    candidates: tuple[ProblemPoolCandidate, ...],
) -> ProblemPoolBuildResult:
    """Build one deterministic terminal record per typed source candidate."""

    if config.status != "ready":
        raise ProblemPoolBuildError(
            "problem-pool construction requires status=ready; checked-in "
            "disabled_until_phase_5_adr configs fail closed"
        )
    if config.active_benchmark_registry_manifest_sha256 is None:
        raise ProblemPoolBuildError("ready problem pool lacks a pinned benchmark manifest hash")
    if (
        denylist.manifest_path != config.active_benchmark_registry_manifest
        or denylist.manifest_sha256 != config.active_benchmark_registry_manifest_sha256
    ):
        raise ProblemPoolBuildError(
            "denylist binding does not match the configured benchmark manifest"
        )

    ordered_candidates = tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.problem_record_id,
                candidate.source_record_content_hash,
            ),
        )
    )
    record_ids = [candidate.problem_record_id for candidate in ordered_candidates]
    if len(record_ids) != len(set(record_ids)):
        raise ProblemPoolBuildError(
            "candidate immutable identities must be unique; duplicate problem_record_id"
        )

    source_policies = {source.source: source for source in config.sources}
    for candidate in ordered_candidates:
        policy = source_policies.get(candidate.source)
        if policy is None or not policy.enabled:
            continue
        if policy.authorization is None or policy.source_config_sha256 is None:
            raise ProblemPoolBuildError(
                f"enabled source {candidate.source!r} lacks bound authorization"
            )
        if candidate.source_revision != policy.authorization.source_revision:
            raise ProblemPoolBuildError(
                f"candidate source revision does not match authorization for {candidate.source!r}"
            )
        if candidate.source_license != policy.authorization.license_id:
            raise ProblemPoolBuildError(
                f"candidate source license does not match authorization for {candidate.source!r}"
            )
    states = tuple(
        _base_state(
            candidate,
            source_policies=source_policies,
            denylist_index=denylist.index,
        )
        for candidate in ordered_candidates
    )
    reasons_by_id, exact_duplicate_of = _apply_exact_dedup(states)
    near_duplicate_of = _apply_supplied_near_duplicate_groups(states, reasons_by_id)

    records: list[ProblemPoolRecord] = []
    public_trusted: list[PublicTrustedProblem] = []
    for state in states:
        candidate = state.candidate
        record_id = candidate.problem_record_id
        reasons = tuple(sorted(reasons_by_id[record_id]))
        eligible = not reasons
        policy = state.source_policy
        authorization = policy.authorization if policy is not None else None
        source_config_sha256 = policy.source_config_sha256 if policy is not None else None
        if eligible and (policy is None or authorization is None or source_config_sha256 is None):
            raise ProblemPoolBuildError(
                f"eligible source {candidate.source!r} lacks bound authorization"
            )
        external_eligible = bool(
            eligible
            and policy is not None
            and policy.external_provider_eligible
            and authorization is not None
            and authorization.external_transmission
            and not state.private_source_content
        )
        release_eligible = bool(
            eligible
            and candidate.release_eligible
            and authorization is not None
            and authorization.release_eligible
            and not state.private_source_content
        )
        metadata = dict(candidate.metadata)
        if record_id in near_duplicate_of:
            metadata["near_duplicate_canonical_id"] = near_duplicate_of[record_id]
        record = ProblemPoolRecord(
            schema_version=2,
            problem_record_id=record_id,
            problem_id=candidate.problem_id,
            problem_group=candidate.problem_group,
            source=candidate.source,
            source_revision=candidate.source_revision,
            source_split=candidate.source_split,
            source_record_id=candidate.source_record_id,
            source_record_content_hash=candidate.source_record_content_hash,
            source_config_sha256=source_config_sha256,
            source_authorization_hash=(
                hash_canonical(authorization.model_dump(mode="json"))
                if authorization is not None
                else None
            ),
            source_license=(authorization.license_id if authorization is not None else None),
            nl_statement=candidate.nl_statement,
            nl_trust=candidate.nl_trust,
            nl_source_link=candidate.nl_source_link,
            context_id=candidate.context_id,
            import_header_artifact=candidate.import_header_artifact,
            import_header_hash=candidate.import_header_hash,
            reference_theorem_ids=candidate.reference_theorem_ids,
            private_source_content=state.private_source_content,
            external_provider_eligible=external_eligible,
            release_eligible=release_eligible,
            eligibility="eligible" if eligible else "excluded",
            exclusion_reasons=reasons,
            denylist_checked=True,
            denylist_hits=state.denylist_hits,
            denylist_manifest_path=denylist.manifest_path,
            denylist_manifest_sha256=denylist.manifest_sha256,
            denylist_active_registry_sha256=(denylist.active_registry_sha256),
            denylist_registry_content_hash=denylist.registry_content_hash,
            exact_duplicate_of=exact_duplicate_of.get(record_id),
            near_duplicate_group_ids=candidate.near_duplicate_group_ids,
            overlap_tags=candidate.overlap_tags,
            metadata=metadata,
        )
        records.append(record)
        if (
            record.eligibility == "eligible"
            and record.nl_trust is NLTrust.TRUSTED
            and not record.private_source_content
            and record.external_provider_eligible
        ):
            public_trusted.append(to_public_trusted_problem(record))

    if len(records) != len(candidates):
        raise AssertionError("problem-pool accounting lost an input candidate")
    return ProblemPoolBuildResult(
        records=tuple(records),
        public_trusted_problems=tuple(public_trusted),
    )
