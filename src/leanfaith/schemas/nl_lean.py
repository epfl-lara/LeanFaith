"""NLPLeanRecord (PLAN.md §11.9)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import NLTrust
from leanfaith.schemas.ids import (
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    LABEL_PREFIX,
    NL_LEAN_PREFIX,
    PAIR_PREFIX,
    PROBLEM_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
    make_id,
)

MetadataValue = str | int | float | bool | None
ProblemEligibility = Literal["eligible", "excluded"]


def make_problem_record_id(
    *,
    source: str,
    source_revision: str,
    source_split: str,
    source_record_id: str,
    problem_id: str,
) -> str:
    """Build a stable Phase-5 problem-pool record ID."""

    return make_id(
        PROBLEM_PREFIX,
        {
            "schema": "problem_pool_record_v1",
            "source": source,
            "source_revision": source_revision,
            "source_split": source_split,
            "source_record_id": source_record_id,
            "problem_id": problem_id,
        },
    )


class ProblemPoolRecord(StrictModel):
    """One immutable NL problem eligible for, or excluded from, generation.

    This record contains no provider output and no semantic label. Excluded
    rows are retained so pool accounting cannot silently lose denylisted,
    duplicate, or provenance-ineligible source records.
    """

    schema_version: Literal[1, 2] = 1
    problem_record_id: str = Field(pattern=id_pattern(PROBLEM_PREFIX))
    problem_id: str = Field(min_length=1)
    problem_group: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    source_split: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    source_record_content_hash: str = Field(pattern=HEX64_PATTERN)
    source_config_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
    )
    source_authorization_hash: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
    )
    source_license: str | None = Field(default=None, min_length=1)
    nl_statement: str = Field(min_length=1)
    nl_trust: NLTrust
    nl_source_link: str = Field(min_length=1)
    context_id: str = Field(pattern=id_pattern("ctx"))
    import_header_artifact: str = Field(min_length=1)
    import_header_hash: str = Field(pattern=HEX64_PATTERN)
    reference_theorem_ids: tuple[str, ...] = ()
    private_source_content: bool
    external_provider_eligible: bool
    release_eligible: bool
    eligibility: ProblemEligibility
    exclusion_reasons: tuple[str, ...] = ()
    denylist_checked: bool
    denylist_hits: tuple[str, ...] = ()
    denylist_manifest_path: str | None = Field(default=None, min_length=1)
    denylist_manifest_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
    )
    denylist_active_registry_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
    )
    denylist_registry_content_hash: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
    )
    exact_duplicate_of: str | None = Field(default=None, pattern=id_pattern(PROBLEM_PREFIX))
    near_duplicate_group_ids: tuple[str, ...] = ()
    overlap_tags: tuple[str, ...] = ()
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> ProblemPoolRecord:
        expected_id = make_problem_record_id(
            source=self.source,
            source_revision=self.source_revision,
            source_split=self.source_split,
            source_record_id=self.source_record_id,
            problem_id=self.problem_id,
        )
        if self.problem_record_id != expected_id:
            raise ValueError("problem_record_id does not match immutable source identity")
        if self.schema_version == 2:
            required_v2 = {
                "denylist_manifest_path": self.denylist_manifest_path,
                "denylist_manifest_sha256": self.denylist_manifest_sha256,
                "denylist_active_registry_sha256": (self.denylist_active_registry_sha256),
                "denylist_registry_content_hash": (self.denylist_registry_content_hash),
            }
            missing = sorted(name for name, value in required_v2.items() if value is None)
            if missing:
                raise ValueError("schema-v2 ProblemPoolRecord requires: " + ", ".join(missing))
            if not self.denylist_checked:
                raise ValueError("schema-v2 ProblemPoolRecord requires a completed denylist check")
            assert self.denylist_manifest_path is not None
            denylist_path = PurePosixPath(self.denylist_manifest_path)
            if (
                denylist_path.is_absolute()
                or ".." in denylist_path.parts
                or not self.denylist_manifest_path.strip()
            ):
                raise ValueError("denylist_manifest_path must be a repository-relative path")
            required_source_binding = {
                "source_config_sha256": self.source_config_sha256,
                "source_authorization_hash": self.source_authorization_hash,
                "source_license": self.source_license,
            }
            bound_source_values = [value is not None for value in required_source_binding.values()]
            if any(bound_source_values) and not all(bound_source_values):
                raise ValueError(
                    "schema-v2 source authorization binding must be complete or absent"
                )
            if self.eligibility == "eligible":
                missing_source = sorted(
                    name for name, value in required_source_binding.items() if value is None
                )
                if missing_source:
                    raise ValueError(
                        "eligible schema-v2 ProblemPoolRecord requires: "
                        + ", ".join(missing_source)
                    )
        if self.source_license is not None and (
            not self.source_license.strip() or "\x00" in self.source_license
        ):
            raise ValueError("source_license must contain non-whitespace text without NUL")
        for field_name in (
            "reference_theorem_ids",
            "exclusion_reasons",
            "denylist_hits",
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
        if self.private_source_content and (
            self.external_provider_eligible or self.release_eligible
        ):
            raise ValueError(
                "private-source problem records cannot be externally transmitted or released"
            )
        if self.external_provider_eligible and self.eligibility != "eligible":
            raise ValueError("excluded problems cannot be external-provider eligible")
        if self.eligibility == "eligible":
            if not self.denylist_checked:
                raise ValueError("eligible problems require a completed denylist check")
            if self.denylist_hits:
                raise ValueError("denylisted problems cannot be eligible")
            if self.exclusion_reasons or self.exact_duplicate_of is not None:
                raise ValueError("eligible problems cannot carry exclusion or duplicate status")
            if not self.reference_theorem_ids:
                raise ValueError("eligible real-output problems require a reference theorem")
        elif not self.exclusion_reasons:
            raise ValueError("excluded problems require at least one exclusion reason")
        return self


class ReferencePairLink(StrictModel):
    """One candidate-vs-reference comparison, stored as its own PairRecord (§11.9)."""

    reference_theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    pair_id: str = Field(pattern=id_pattern(PAIR_PREFIX))


class NLPLeanRecord(StrictModel):
    """One NL problem x one Lean candidate, with optional references (§11.9)."""

    schema_version: Literal[1, 2] = 1
    nl_lean_id: str = Field(pattern=id_pattern(NL_LEAN_PREFIX))
    problem_record_id: str | None = Field(default=None, pattern=id_pattern(PROBLEM_PREFIX))
    problem_id: str
    problem_group: str
    source: str
    source_revision: str
    nl_statement: str = Field(min_length=1)
    nl_trust: NLTrust
    candidate_theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    generator_id: str | None = None
    reference_theorem_ids: tuple[str, ...] = ()
    reference_pairs: tuple[ReferencePairLink, ...] = ()
    resolved_label_id: str | None = Field(default=None, pattern=id_pattern(LABEL_PREFIX))
    evidence_ids: tuple[str, ...] = ()
    split_group_ids: tuple[str, ...] = Field(min_length=1)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> NLPLeanRecord:
        if self.schema_version == 2 and self.problem_record_id is None:
            raise ValueError("schema-v2 NLPLeanRecord requires problem_record_id")
        thm = id_pattern(THEOREM_PREFIX)
        for reference in self.reference_theorem_ids:
            if not re.match(thm, reference):
                raise ValueError(f"reference theorem ID {reference!r} is not a 'thm:' ID")
        declared = set(self.reference_theorem_ids)
        for link in self.reference_pairs:
            if link.reference_theorem_id not in declared:
                raise ValueError(
                    f"reference pair {link.pair_id} names undeclared reference "
                    f"{link.reference_theorem_id} (§11.9)"
                )
        ev = id_pattern(EVIDENCE_PREFIX)
        for evidence_id in self.evidence_ids:
            if not re.match(ev, evidence_id):
                raise ValueError(f"evidence ID {evidence_id!r} is not an 'ev:' ID")
        if list(self.split_group_ids) != sorted(set(self.split_group_ids)):
            raise ValueError("split_group_ids must be sorted and unique (§19.5)")
        if self.problem_group not in self.split_group_ids:
            raise ValueError("split_group_ids must include the NL problem group (§19.5)")
        return self


def check_nl_lean_problem_link(
    record: NLPLeanRecord,
    problem: ProblemPoolRecord,
) -> list[str]:
    """Return cross-record lineage violations for one generated NL-Lean item."""

    violations: list[str] = []
    if record.problem_record_id != problem.problem_record_id:
        violations.append("problem_record_id_mismatch")
    for field_name in (
        "problem_id",
        "problem_group",
        "source",
        "source_revision",
        "nl_statement",
        "nl_trust",
    ):
        if getattr(record, field_name) != getattr(problem, field_name):
            violations.append(f"{field_name}_mismatch")
    if not set(record.reference_theorem_ids).issubset(problem.reference_theorem_ids):
        violations.append("reference_theorem_ids_not_in_problem_record")
    return violations
