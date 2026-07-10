"""ContextRecord, TheoremRecord, RepresentationRecord (PLAN.md §11.2-§11.4).

These are source/declaration identity records; normalized views live only in
``RepresentationRecord``. Proof bodies are never admitted as representation
fields or model inputs (§11.3).
"""

from __future__ import annotations

import datetime
import re

from pydantic import Field, model_validator

from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import NLTrust, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import (
    ANCESTRY_PREFIX,
    CONTEXT_PREFIX,
    HEX64_PATTERN,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
)
from leanfaith.schemas.manifest import require_utc

#: §13.2 canonical view names, verbatim; used as RepresentationRecord fields.
CANONICAL_VIEW_NAMES: tuple[str, ...] = (
    "raw_proof_stripped",
    "headless",
    "signature_pp",
    "signature_explicit",
    "alpha_structural",
    "notation_light",
    "semantic_atoms",
    "operator_tree",
)

#: Views that must derive successfully in v0 (§13.2).
REQUIRED_V0_VIEWS: tuple[str, ...] = ("raw_proof_stripped", "headless", "signature_pp")

MetadataValue = str | int | float | bool | None


class ContextRecord(StrictModel):
    """Pinned Lean environment identity (§11.2, §12.5)."""

    schema_version: int = 1
    environment_schema_version: int
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    context_fingerprint: str = Field(pattern=HEX64_PATTERN)
    project_kind: str
    project_uri: str
    project_revision: str
    project_registry_key: str
    lean_version: str
    lean_interact_version: str
    repl_revision: str
    imports: tuple[str, ...] = ()
    namespace_context: tuple[str, ...] = ()
    open_context: tuple[str, ...] = ()
    scoped_context: tuple[str, ...] = ()
    options: dict[str, MetadataValue] = Field(default_factory=dict)
    notation_context: tuple[str, ...] = ()
    header_text: str = ""
    header_hash: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _id_matches_fingerprint(self) -> ContextRecord:
        expected = f"{CONTEXT_PREFIX}:{self.context_fingerprint}"
        if self.context_id != expected:
            raise ValueError(
                f"context_id must equal '{CONTEXT_PREFIX}:' + context_fingerprint "
                f"(§8.11); got {self.context_id}"
            )
        return self


class TheoremRecord(StrictModel):
    """Source/declaration identity for one proposition statement (§11.3)."""

    schema_version: int = 1
    theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    ancestry_id: str = Field(pattern=id_pattern(ANCESTRY_PREFIX))
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    parent_theorem_ids: tuple[str, ...] = ()
    source: str
    source_revision: str
    source_split: str | None = None
    source_record: str | None = None
    source_file: str | None = None
    source_range: tuple[int, int] | None = None
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    declaration_kind: str
    declaration_name: str | None = None
    declaration_full_name: str | None = None
    raw_declaration_artifact: str | None = None
    raw_declaration_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    proof_stripped_declaration: str
    declaration_info_artifact: str | None = None
    lean_result_id: str | None = None
    is_proposition: bool
    elaboration_status: ValidationStatus
    elaboration_diagnostics: tuple[str, ...] = ()
    statement_content_hash: str = Field(pattern=HEX64_PATTERN)
    nl_source_link: str | None = None
    nl_trust: NLTrust | None = None
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ancestry_and_parents(self) -> TheoremRecord:
        anc = id_pattern(ANCESTRY_PREFIX)
        thm = id_pattern(THEOREM_PREFIX)
        for root in self.root_ancestry_ids:
            if not re.match(anc, root):
                raise ValueError(f"root ancestry ID {root!r} is not an '{ANCESTRY_PREFIX}:' ID")
        if list(self.root_ancestry_ids) != sorted(set(self.root_ancestry_ids)):
            raise ValueError("root_ancestry_ids must be sorted and unique (§12.6)")
        for parent in self.parent_theorem_ids:
            if not re.match(thm, parent):
                raise ValueError(f"parent theorem ID {parent!r} is not a '{THEOREM_PREFIX}:' ID")
        if not self.parent_theorem_ids and len(self.root_ancestry_ids) != 1:
            raise ValueError("a source theorem (no parents) has exactly one root ancestry (§12.6)")
        return self


class RepresentationRecord(StrictModel):
    """Versioned multi-view representation, keyed (theorem_id, normalization_version) (§11.4)."""

    schema_version: int = 1
    representation_id: str = Field(pattern=id_pattern(REPRESENTATION_PREFIX))
    theorem_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    normalization_version: str
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    raw_proof_stripped: str | None = None
    headless: str | None = None
    signature_pp: str | None = None
    signature_explicit: str | None = None
    alpha_structural: str | None = None
    notation_light: str | None = None
    semantic_atoms: tuple[str, ...] | None = None
    operator_tree: dict[str, object] | None = None
    view_status: dict[str, ViewStatus]
    option_profile: dict[str, MetadataValue] = Field(default_factory=dict)
    content_hash: str = Field(pattern=HEX64_PATTERN)
    created_at: datetime.datetime

    @model_validator(mode="after")
    def _views_consistent(self) -> RepresentationRecord:
        require_utc(self.created_at)
        unknown = set(self.view_status) - set(CANONICAL_VIEW_NAMES)
        if unknown:
            raise ValueError(f"unknown view names in view_status: {sorted(unknown)} (§13.2)")
        missing = set(CANONICAL_VIEW_NAMES) - set(self.view_status)
        if missing:
            raise ValueError(f"view_status must cover every §13.2 view; missing {sorted(missing)}")
        for view in CANONICAL_VIEW_NAMES:
            value = getattr(self, view)
            status = self.view_status[view]
            if value is None and status == ViewStatus.OK:
                raise ValueError(f"view {view!r} is null but marked ok (§11.4)")
            if value is not None and status != ViewStatus.OK:
                raise ValueError(f"view {view!r} is present but marked {status} (§11.4)")
        return self
