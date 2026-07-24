"""Strict, registry-bound LF-019 positive smoke fixtures.

The fixture profile is deliberately small: it contains exactly one
known-applicable source for each available positive rule in the active
transformation profile.  It is a smoke input, not scientific data, and its
schema makes release/model-selection use impossible.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import find_repo_root
from leanfaith.transforms.factory import build_positive_rule_runtime
from leanfaith.transforms.registry import load_transformation_registry

NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
LeanFixtureName = Annotated[
    str,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_']*$", min_length=1, strict=True),
]
CaseId = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, strict=True),
]

PositiveFixtureRuleId = Literal[
    "p01_alpha",
    "p02_binders",
    "p04_notation_lite",
]
PositiveFixtureTraceOperation = Literal[
    "alpha_rename",
    "replace_exact_span",
    "replace_notation_token_exact",
]

_EXPECTED_RULE_IDS = (
    "p01_alpha",
    "p02_binders",
    "p04_notation_lite",
)
_EXPECTED_CASE_IDS = {
    "p01_alpha": "p01_alpha_fixture",
    "p02_binders": "p02_binders_fixture",
    "p04_notation_lite": "p04_notation_lite_fixture",
}
_EXPECTED_TRACE_OPERATIONS = {
    "p01_alpha": "alpha_rename",
    "p02_binders": "replace_exact_span",
    "p04_notation_lite": "replace_notation_token_exact",
}
_DEFAULT_PATH = Path("configs/transformations/lf019_positive_fixtures_v1.yaml")


class PositiveFixtureProfileError(ValueError):
    """The LF-019 fixture profile is malformed or differs from the registry."""


class PositiveFixtureCase(StrictModel):
    """One deterministic, known-applicable positive transformation fixture."""

    case_id: CaseId
    rule_id: PositiveFixtureRuleId
    source_name: LeanFixtureName
    source_code: NonEmptyStr
    seed: int = Field(ge=0, strict=True)
    expected_candidate_fragment: NonEmptyStr
    expected_trace_operation: PositiveFixtureTraceOperation

    @model_validator(mode="after")
    def _closed_rule_shape(self) -> PositiveFixtureCase:
        expected_case_id = _EXPECTED_CASE_IDS[self.rule_id]
        if self.case_id != expected_case_id:
            raise ValueError(f"{self.rule_id} case_id must be exactly {expected_case_id!r}")
        expected_operation = _EXPECTED_TRACE_OPERATIONS[self.rule_id]
        if self.expected_trace_operation != expected_operation:
            raise ValueError(
                f"{self.rule_id} expected_trace_operation must be {expected_operation!r}"
            )
        declaration_prefix = f"theorem {self.source_name} "
        if not self.source_code.startswith(declaration_prefix):
            raise ValueError("source_code must begin with the exact configured theorem name")
        if not self.source_code.endswith(":= by sorry"):
            raise ValueError("source_code must end with the fixture proof placeholder")
        if self.expected_candidate_fragment in self.source_code:
            raise ValueError("expected_candidate_fragment must witness a generated change")
        return self


class PositiveFixtureProfile(StrictModel):
    """Versioned fixture inventory consumed by the LF-019 smoke slice."""

    schema_version: Literal[1] = 1
    fixture_profile_id: Literal["lf019_positive_fixtures_v1"] = "lf019_positive_fixtures_v1"
    fixture_profile_version: Literal["1.0.0"] = "1.0.0"
    artifact_class: Literal["smoke"] = "smoke"
    release_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    resolution_policy: Literal["provisional_only"] = "provisional_only"
    project_dir: Literal["tests/lean_fixtures"] = "tests/lean_fixtures"
    imports: Literal["import LeanFaithFixtures"] = "import LeanFaithFixtures"
    record_timestamp_utc: NonEmptyStr
    cases: tuple[PositiveFixtureCase, ...]

    @model_validator(mode="after")
    def _exact_inventory(self) -> PositiveFixtureProfile:
        rule_ids = tuple(case.rule_id for case in self.cases)
        if rule_ids != _EXPECTED_RULE_IDS:
            raise ValueError(
                "cases must contain exactly one fixture for each scoped "
                f"positive rule in canonical order: {list(_EXPECTED_RULE_IDS)}"
            )
        if len({case.source_name for case in self.cases}) != len(self.cases):
            raise ValueError("source_name values must be unique")
        parsed = datetime.datetime.fromisoformat(self.record_timestamp_utc)
        if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
            raise ValueError("record_timestamp_utc must be timezone-aware UTC")
        return self

    @property
    def record_timestamp(self) -> datetime.datetime:
        """Return the validated deterministic record timestamp."""

        return datetime.datetime.fromisoformat(self.record_timestamp_utc)

    @property
    def rule_ids(self) -> tuple[str, ...]:
        """Return the canonical fixture inventory."""

        return tuple(case.rule_id for case in self.cases)


@dataclass(frozen=True, slots=True)
class LoadedPositiveFixtureProfile:
    """The fixture config bound to the effective transformation registry."""

    loaded_config: LoadedConfig[PositiveFixtureProfile]
    registry_hash: str
    active_rule_ids: tuple[str, ...]

    @property
    def config(self) -> PositiveFixtureProfile:
        return self.loaded_config.config

    @property
    def config_hash(self) -> str:
        return self.loaded_config.config_hash

    @property
    def path(self) -> Path:
        return self.loaded_config.path


def load_lf019_positive_fixture_profile(
    repo_root: Path | None = None,
    *,
    path: Path | None = None,
) -> LoadedPositiveFixtureProfile:
    """Load the smoke fixtures and fail if registry inventory has drifted."""

    root = find_repo_root(repo_root)
    resolved_root = root.resolve()
    resolved = (path or root / _DEFAULT_PATH).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise PositiveFixtureProfileError("LF-019 positive fixture path escapes the repository")
    loaded = load_config(resolved, PositiveFixtureProfile)
    registry = load_transformation_registry(root)
    registration = build_positive_rule_runtime(registry)
    active_rule_ids = registration.registered_rule_ids
    if loaded.config.rule_ids != active_rule_ids:
        raise PositiveFixtureProfileError(
            "LF-019 positive fixture inventory differs from the code-owned "
            f"active positive registry: fixtures={loaded.config.rule_ids}, "
            f"active={active_rule_ids}"
        )
    return LoadedPositiveFixtureProfile(
        loaded_config=loaded,
        registry_hash=registration.registry_hash,
        active_rule_ids=active_rule_ids,
    )


__all__ = [
    "LoadedPositiveFixtureProfile",
    "PositiveFixtureCase",
    "PositiveFixtureProfile",
    "PositiveFixtureProfileError",
    "load_lf019_positive_fixture_profile",
]
