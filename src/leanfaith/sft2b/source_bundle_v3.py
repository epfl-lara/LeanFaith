"""Fail-closed staging and verification for the additive SFT2B v3 source bundle.

The command can stage a complete local release only after externally supplied,
hash-pinned human review and attestation evidence passes.  It never uploads,
generates model output, or substitutes automatic dispositions for human review.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

from pydantic import model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.sft2b import meta_instruction_filter as meta_instruction_filter_module
from leanfaith.sft2b import source_conservation_v3 as source_conservation_module
from leanfaith.sft2b import source_review_v3 as source_review_module
from leanfaith.sft2b.meta_instruction_filter import (
    verify_v2_active_impact_fixture,
    verify_v2_impact_fixture,
)
from leanfaith.sft2b.schemas import NonEmpty, Sha256, SourceRecord, StableId
from leanfaith.sft2b.source_bundle_schemas import (
    SourceIdViewV2,
    SourceSelectionAuditV2,
    WorkbookQuarantineV2,
)
from leanfaith.sft2b.source_conservation_v3 import (
    ConservationAction,
    DeltaReasonCode,
    ExplicitDeltaReasonV3,
    SourceConservationEventV3,
    SourceConservationReceiptV3,
    build_conservation_events,
    summarize_conservation,
)
from leanfaith.sft2b.source_review_v3 import (
    AutomaticDispositionV3,
    HumanReviewVerificationReceiptV3,
    HumanSourceReviewV3,
    ReviewPacketManifestV3,
    SourceReviewContractError,
    SourceReviewPacketEntryV3,
    expected_packet_entries,
    verify_completed_human_reviews,
    verify_review_packet,
)
from leanfaith.sft2b.source_review_v3 import (
    load_config as load_review_config,
)

CONFIG_SCHEMA = "sft2b_reform_diverse_full_sources_v3"
BASELINE_META_ROWS = 326
BASELINE_META_VIEW_COUNTS = {"legacy_tail": 64, "matched_core": 262}
ACTIVE_META_ROWS = 469
ACTIVE_META_VIEW_COUNTS = {"legacy_tail": 75, "matched_core": 394}
V2_ACTIVE_ROWS = 54_621
V2_CORE_ROWS = 50_000
V2_TAIL_ROWS = 4_621
V2_WORKBOOK_QUARANTINE_ROWS = 285
V2_UNIVERSE_ROWS = 54_906
REQUIRED_HUMAN_REVIEWS = 992
REQUIRED_WORKBOOK_HITS = 293
REQUIRED_DETERMINISTIC_PER_CLASS = 100
CORE_SELECTION_RULE = "release_class_blocks_retained_v2_order_then_sorted_readmissions_v1"
V2_HF_REPOSITORY = "Lemmy00/leanfaith-sft2-autoformalizer-v1"
V2_HF_REVISION = "d0b961d2112d186009984242db674f2ad59905c7"
V2_REMOTE_PREFIX = "source_inputs/reform_diverse_full_v2"
V3_REMOTE_PREFIX = "source_inputs/reform_diverse_full_v3"
V2_FROZEN_FILE_NAMES = frozenset(
    {
        "SHA256SUMS",
        "legacy_tail_source_ids.json",
        "library_docstring_corrections.jsonl",
        "matched_50000_source_ids.json",
        "prompt_token_counts.json",
        "semantic_alignment_audit.jsonl",
        "source_audit.jsonl",
        "source_manifest.json",
        "sources.jsonl",
        "workbook_discourse_audit.jsonl",
        "workbook_quarantine.jsonl",
    }
)
FROZEN_V2_COPY_NAMES = {
    "source_audit.jsonl": "frozen_v2_source_audit.jsonl",
    "library_docstring_corrections.jsonl": ("frozen_v2_library_docstring_corrections.jsonl"),
    "source_manifest.json": "frozen_v2_source_manifest.json",
}
V2_RICH_MANIFEST_KEYS = (
    "source_use_policy",
    "schemas",
    "prompt",
    "tokenizer",
    "repr",
    "benchmark_exclusions",
    "contamination_and_dedup_exclusions",
    "source_mix",
    "domain_mix",
    "trust_tier_mix",
    "source_catalogs",
    "source_audits",
    "audited_but_not_admitted_catalogs",
    "existing_301_replay",
    "library_docstring_correction",
)
OUTPUT_NAMES = frozenset(
    {
        "SHA256SUMS",
        "automatic_dispositions.jsonl",
        "external_human_attestation.json",
        "frozen_v2_library_docstring_corrections.jsonl",
        "frozen_v2_source_audit.jsonl",
        "frozen_v2_source_manifest.json",
        "human_review_verification_receipt.json",
        "human_reviews.jsonl",
        "legacy_tail_source_ids.json",
        "matched_50000_source_ids.json",
        "prompt_token_counts.json",
        "review_packet.jsonl",
        "review_packet_SHA256SUMS",
        "review_packet_manifest.json",
        "source_conservation_events.jsonl",
        "source_conservation_receipt.json",
        "source_manifest.json",
        "source_mechanical_evidence.jsonl",
        "source_quarantine.jsonl",
        "sources.jsonl",
    }
)

FinalReviewVerdict = Literal[
    "admit_standalone_aligned",
    "quarantine_solution_or_proof_fragment",
    "quarantine_incomplete_or_nonstandalone",
    "quarantine_misaligned",
    "quarantine_other_quality_failure",
]


class SourceBundleV3Error(RuntimeError):
    """Base error for v3 source preflight and planning."""


class SourceBundleV3Blocked(SourceBundleV3Error):
    """The release cannot be emitted because externally supplied evidence is absent."""


class ExternalHumanAttestationV3(StrictModel):
    """Accountable out-of-band evidence; this is not cryptographic authentication."""

    schema_version: Literal["sft2b_external_human_review_attestation_v3"]
    completed_reviews_sha256: Sha256
    reviewer_identities: tuple[NonEmpty, ...]
    attestor_identity: NonEmpty
    attested_at_utc: datetime.datetime
    attestation_scope: Literal["out_of_band_accountable_not_cryptographic_authentication"]
    statement: Literal[
        "The named human reviewers personally reviewed the exact hash-bound source fields; "
        "this is an accountable out-of-band attestation, not cryptographic authentication."
    ]

    @model_validator(mode="after")
    def validate_identities(self) -> ExternalHumanAttestationV3:
        if not self.reviewer_identities:
            raise ValueError("external attestation requires at least one reviewer identity")
        if tuple(sorted(set(self.reviewer_identities))) != self.reviewer_identities:
            raise ValueError("external reviewer identities must be unique and sorted")
        if (
            self.attested_at_utc.tzinfo is None
            or self.attested_at_utc.utcoffset() != datetime.timedelta(0)
        ):
            raise ValueError("external attestation timestamp must be timezone-aware UTC")
        return self


class QuarantinedSourceV3(StrictModel):
    schema_version: Literal["sft2b_quarantined_source_v3"] = "sft2b_quarantined_source_v3"
    source: SourceRecord
    source_record_sha256: Sha256
    v2_view: Literal["core", "tail", "quarantine"]
    terminal_basis: Literal["active_meta_instruction_filter_v2", "final_human_review_v3"]
    evidence_sha256: Sha256
    human_verdict: FinalReviewVerdict | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> QuarantinedSourceV3:
        observed = hash_canonical(self.source.model_dump(mode="json"))
        if observed != self.source_record_sha256:
            raise ValueError("quarantined SourceRecord hash mismatch")
        if self.terminal_basis == "final_human_review_v3" and self.human_verdict is None:
            raise ValueError("human quarantine requires its final verdict")
        if self.terminal_basis == "active_meta_instruction_filter_v2" and self.human_verdict:
            raise ValueError("meta quarantine cannot be represented as a human disposition")
        return self


class MechanicalSourceEvidenceV3(StrictModel):
    """Mechanical provenance only; this is not a semantic or human audit."""

    schema_version: Literal["sft2b_mechanical_source_evidence_v3"] = (
        "sft2b_mechanical_source_evidence_v3"
    )
    source_id: StableId
    release_class: NonEmpty
    source_record_sha256: Sha256
    v2_view: Literal["core", "tail", "quarantine"]
    v3_view: Literal["core", "tail", "quarantine"]
    v2_evidence_kind: Literal[
        "v2_source_selection_audit",
        "v2_workbook_automatic_disposition",
    ]
    v2_evidence_sha256: Sha256
    semantic_or_human_review: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ReleasePreflightV3:
    v2_file_count: int
    baseline_meta_rows: int
    active_meta_rows: int
    review_count: int
    reviewer_identities: tuple[str, ...]
    release_gate_passed: bool


@dataclass(frozen=True, slots=True)
class ReleasePlanV3:
    ordered_active_ids: tuple[str, ...]
    core_ids: tuple[str, ...]
    tail_ids: tuple[str, ...]
    quarantine_ids: tuple[str, ...]
    source_bytes: bytes
    events: tuple[SourceConservationEventV3, ...]
    event_stream: bytes
    action_counts: dict[ConservationAction, int]
    reason_counts: dict[DeltaReasonCode, int]


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SourceBundleV3Error(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _repo_path(repo_root: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else repo_root / path


def _section(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name)
    if not isinstance(value, Mapping):
        raise SourceBundleV3Error(f"v3 config section is missing or not an object: {name}")
    return cast(Mapping[str, Any], value)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SourceBundleV3Error(f"{label} key set drifted")


def _secure_repo_pin(
    repo_root: Path,
    configured_path: object,
    *,
    expected_relative_path: str,
    imported_path: Path,
    label: str,
) -> Path:
    """Resolve a frozen repository-owned pin without permitting path substitution."""

    if not isinstance(configured_path, str) or configured_path != expected_relative_path:
        raise SourceBundleV3Error(f"{label} must equal {expected_relative_path!r}")
    relative = Path(configured_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise SourceBundleV3Error(f"{label} must be a traversal-free repository-relative path")
    if repo_root.is_symlink():
        raise SourceBundleV3Error("repository root for frozen code pins must not be a symlink")
    root = repo_root.resolve(strict=True)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SourceBundleV3Error(f"{label} must not traverse a symlink")
    if not current.is_file():
        raise SourceBundleV3Error(f"missing {label}: {current}")
    if current.resolve(strict=True) != imported_path.resolve(strict=True):
        raise SourceBundleV3Error(f"{label} does not identify the actual imported module/file")
    return current


def _repo_pin_paths(repo_root: Path, config: Mapping[str, Any]) -> dict[str, Path]:
    builder = _section(config, "builder")
    v2_config = _section(config, "v2_source_config")
    meta = _section(config, "meta_instruction_filter")
    review = _section(config, "human_review")
    conservation = _section(config, "conservation")
    specs = (
        (
            "builder implementation",
            builder["implementation_path"],
            "src/leanfaith/sft2b/source_bundle_v3.py",
            Path(__file__),
        ),
        (
            "v2 source config",
            v2_config["path"],
            "configs/sft2b/reform_diverse_full_sources_v2.json",
            repo_root / "configs/sft2b/reform_diverse_full_sources_v2.json",
        ),
        (
            "meta implementation",
            meta["implementation_path"],
            "src/leanfaith/sft2b/meta_instruction_filter.py",
            Path(meta_instruction_filter_module.__file__),
        ),
        (
            "baseline meta fixture",
            meta["baseline_fixture_path"],
            "configs/sft2b/source_meta_instruction_impact_v1.json",
            repo_root / "configs/sft2b/source_meta_instruction_impact_v1.json",
        ),
        (
            "active meta fixture",
            meta["active_fixture_path"],
            "configs/sft2b/source_meta_instruction_impact_v2.json",
            repo_root / "configs/sft2b/source_meta_instruction_impact_v2.json",
        ),
        (
            "human review contract",
            review["contract_path"],
            "configs/sft2b/source_review_contract_v3.json",
            repo_root / "configs/sft2b/source_review_contract_v3.json",
        ),
        (
            "human review implementation",
            review["implementation_path"],
            "src/leanfaith/sft2b/source_review_v3.py",
            Path(source_review_module.__file__),
        ),
        (
            "conservation implementation",
            conservation["implementation_path"],
            "src/leanfaith/sft2b/source_conservation_v3.py",
            Path(source_conservation_module.__file__),
        ),
    )
    return {
        label: _secure_repo_pin(
            repo_root,
            configured,
            expected_relative_path=expected,
            imported_path=imported,
            label=label,
        )
        for label, configured, expected, imported in specs
    }


def _validate_identity_allowlist(
    value: object, *, label: str, allow_empty: bool
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise SourceBundleV3Error(f"{label} must be a list of nonempty identities")
    result = tuple(cast(list[str], value))
    if not allow_empty and not result:
        raise SourceBundleV3Error(f"{label} must be nonempty")
    if tuple(sorted(set(result))) != result:
        raise SourceBundleV3Error(f"{label} must be unique and sorted")
    return result


def _validate_config(repo_root: Path, config: Mapping[str, Any]) -> None:
    """Validate the complete frozen v3 contract before interpreting any inputs."""

    _require_exact_keys(
        config,
        {
            "schema_version",
            "output_subdir",
            "matched_view_rows",
            "builder",
            "v2_evidence",
            "v2_source_config",
            "meta_instruction_filter",
            "human_review",
            "conservation",
            "publication",
            "generation_gate",
        },
        "v3 source config",
    )
    if (
        config.get("schema_version") != CONFIG_SCHEMA
        or config.get("output_subdir") != V3_REMOTE_PREFIX
        or config.get("matched_view_rows") != V2_CORE_ROWS
    ):
        raise SourceBundleV3Error("v3 source config identity/count drifted")

    builder = _section(config, "builder")
    _require_exact_keys(builder, {"implementation_path", "implementation_sha256"}, "builder")
    v2 = _section(config, "v2_evidence")
    _require_exact_keys(
        v2,
        {
            "hf_repository",
            "hf_revision",
            "remote_prefix",
            "local_bundle_path",
            "source_count",
            "matched_count",
            "tail_count",
            "workbook_quarantine_count",
            "file_sha256",
        },
        "v2 evidence",
    )
    if (
        v2.get("hf_repository") != V2_HF_REPOSITORY
        or v2.get("hf_revision") != V2_HF_REVISION
        or v2.get("remote_prefix") != V2_REMOTE_PREFIX
        or v2.get("source_count") != V2_ACTIVE_ROWS
        or v2.get("matched_count") != V2_CORE_ROWS
        or v2.get("tail_count") != V2_TAIL_ROWS
        or v2.get("workbook_quarantine_count") != V2_WORKBOOK_QUARANTINE_ROWS
    ):
        raise SourceBundleV3Error("declared frozen v2 identity/counts drifted")
    local_bundle = v2.get("local_bundle_path")
    if (
        not isinstance(local_bundle, str)
        or not local_bundle
        or not Path(local_bundle).is_absolute()
    ):
        raise SourceBundleV3Error("v2 local bundle path must be explicit and absolute")
    file_hashes = v2.get("file_sha256")
    if not isinstance(file_hashes, Mapping) or set(file_hashes) != V2_FROZEN_FILE_NAMES:
        raise SourceBundleV3Error("declared frozen v2 file set drifted")
    if any(not isinstance(value, str) or len(value) != 64 for value in file_hashes.values()):
        raise SourceBundleV3Error("declared frozen v2 file hashes are malformed")

    v2_config = _section(config, "v2_source_config")
    _require_exact_keys(v2_config, {"path", "sha256"}, "v2 source config")
    meta = _section(config, "meta_instruction_filter")
    _require_exact_keys(
        meta,
        {
            "implementation_path",
            "implementation_sha256",
            "baseline_fixture_path",
            "baseline_fixture_sha256",
            "baseline_expected_rows",
            "baseline_expected_view_counts",
            "active_fixture_path",
            "active_fixture_sha256",
            "active_expected_rows",
            "active_expected_view_counts",
        },
        "meta-instruction filter",
    )
    if (
        meta.get("baseline_expected_rows") != BASELINE_META_ROWS
        or meta.get("baseline_expected_view_counts") != BASELINE_META_VIEW_COUNTS
        or meta.get("active_expected_rows") != ACTIVE_META_ROWS
        or meta.get("active_expected_view_counts") != ACTIVE_META_VIEW_COUNTS
    ):
        raise SourceBundleV3Error("meta-instruction expected rows/views drifted")

    review = _section(config, "human_review")
    _require_exact_keys(
        review,
        {
            "contract_path",
            "contract_sha256",
            "implementation_path",
            "implementation_sha256",
            "required_reviews",
            "required_workbook_hits",
            "required_deterministic_per_class",
            "allow_model_substitution",
            "completed_reviews_path",
            "completed_reviews_sha256",
            "allowed_reviewer_identities",
            "external_human_attestation_path",
            "external_human_attestation_sha256",
            "allowed_attestor_identities",
        },
        "human review",
    )
    if (
        review.get("required_reviews") != REQUIRED_HUMAN_REVIEWS
        or review.get("required_workbook_hits") != REQUIRED_WORKBOOK_HITS
        or review.get("required_deterministic_per_class") != REQUIRED_DETERMINISTIC_PER_CLASS
        or review.get("allow_model_substitution") is not False
    ):
        raise SourceBundleV3Error("human-review counts or no-model-substitution contract drifted")
    review_pin_values = (
        review.get("completed_reviews_path"),
        review.get("completed_reviews_sha256"),
        review.get("external_human_attestation_path"),
        review.get("external_human_attestation_sha256"),
    )
    pending = all(value is None for value in review_pin_values)
    reviewers = _validate_identity_allowlist(
        review.get("allowed_reviewer_identities"),
        label="allowed reviewer identities",
        allow_empty=pending,
    )
    attestors = _validate_identity_allowlist(
        review.get("allowed_attestor_identities"),
        label="allowed attestor identities",
        allow_empty=pending,
    )
    if pending:
        if reviewers or attestors:
            raise SourceBundleV3Error("pending human-review state must have empty identity lists")
    elif (
        any(not isinstance(value, str) or not value for value in review_pin_values)
        or not reviewers
        or not attestors
    ):
        raise SourceBundleV3Error(
            "human-review pins must be either wholly pending or wholly frozen"
        )

    conservation = _section(config, "conservation")
    _require_exact_keys(
        conservation,
        {
            "implementation_path",
            "implementation_sha256",
            "v2_universe_rule",
            "expected_v2_universe_count",
            "allow_new_sources",
            "allow_removed_sources",
            "expected_additions",
            "expected_removals",
            "expected_dedup_displacements",
            "expected_dedup_displacement_movements",
            "core_selection_rule",
        },
        "conservation",
    )
    if conservation != {
        "implementation_path": "src/leanfaith/sft2b/source_conservation_v3.py",
        "implementation_sha256": conservation.get("implementation_sha256"),
        "v2_universe_rule": "v2_active_sources_plus_full_workbook_quarantine_v1",
        "expected_v2_universe_count": V2_UNIVERSE_ROWS,
        "allow_new_sources": False,
        "allow_removed_sources": False,
        "expected_additions": 0,
        "expected_removals": 0,
        "expected_dedup_displacements": 0,
        "expected_dedup_displacement_movements": 0,
        "core_selection_rule": CORE_SELECTION_RULE,
    }:
        raise SourceBundleV3Error("conservation universe/action/selection contract drifted")

    publication = _section(config, "publication")
    if publication != {
        "hf_repository": V2_HF_REPOSITORY,
        "remote_prefix": V3_REMOTE_PREFIX,
        "private": True,
        "additive_only": True,
        "requires_authentic_human_review_gate": True,
    }:
        raise SourceBundleV3Error("publication contract drifted")
    generation = _section(config, "generation_gate")
    if generation != {
        "request_scale_authorization": False,
        "allow_core_generation": False,
        "allow_tail_generation": False,
    }:
        raise SourceBundleV3Error("generation gates must all remain false")
    _repo_pin_paths(repo_root, config)


def _require_file_hash(path: Path, expected: object, label: str) -> str:
    if not path.is_file():
        raise SourceBundleV3Error(f"missing {label}: {path}")
    observed = hash_file(path)
    if observed != expected:
        raise SourceBundleV3Error(f"{label} hash mismatch")
    return observed


def _required_external_review_pins(
    config: Mapping[str, Any],
    *,
    repo_root: Path,
) -> tuple[Path, str, tuple[str, ...], Path, str, tuple[str, ...]]:
    review = cast(Mapping[str, Any], config["human_review"])
    reviews_path = review.get("completed_reviews_path")
    reviews_sha256 = review.get("completed_reviews_sha256")
    allowlist_raw = review.get("allowed_reviewer_identities")
    attestation_path = review.get("external_human_attestation_path")
    attestation_sha256 = review.get("external_human_attestation_sha256")
    attestor_allowlist_raw = review.get("allowed_attestor_identities")
    if (
        not isinstance(reviews_path, str)
        or not reviews_path
        or not isinstance(reviews_sha256, str)
        or not reviews_sha256
        or not isinstance(allowlist_raw, list)
        or not allowlist_raw
        or any(not isinstance(value, str) or not value for value in allowlist_raw)
        or not isinstance(attestation_path, str)
        or not attestation_path
        or not isinstance(attestation_sha256, str)
        or not attestation_sha256
        or not isinstance(attestor_allowlist_raw, list)
        or not attestor_allowlist_raw
        or any(not isinstance(value, str) or not value for value in attestor_allowlist_raw)
    ):
        raise SourceBundleV3Blocked(
            "v3 release requires explicit completed-review, reviewer-allowlist, and external "
            "human-attestation pins; automatic records cannot satisfy this gate"
        )
    allowlist = tuple(cast(list[str], allowlist_raw))
    if tuple(sorted(set(allowlist))) != allowlist:
        raise SourceBundleV3Blocked(
            "allowed reviewer identities must be nonempty, unique, and sorted"
        )
    attestor_allowlist = tuple(cast(list[str], attestor_allowlist_raw))
    if tuple(sorted(set(attestor_allowlist))) != attestor_allowlist:
        raise SourceBundleV3Blocked(
            "allowed attestor identities must be nonempty, unique, and sorted"
        )
    return (
        _repo_path(repo_root, reviews_path),
        reviews_sha256,
        allowlist,
        _repo_path(repo_root, attestation_path),
        attestation_sha256,
        attestor_allowlist,
    )


def preflight_release(
    repo_root: Path,
    *,
    config_path: Path,
    v2_bundle_dir: Path,
    review_packet_dir: Path,
    output_dir: Path,
) -> ReleasePreflightV3:
    """Replay all frozen inputs and fail before creating ``output_dir`` when review is absent.

    The low-level review verifier establishes schema, exact source binding, and coverage only.
    Human authenticity comes solely from the separately pinned allowlist and attestation.
    """

    if output_dir.exists():
        raise SourceBundleV3Error("v3 output path must not exist before release preflight")
    config = _object(config_path)
    _validate_config(repo_root, config)

    v2 = cast(Mapping[str, Any], config["v2_evidence"])
    files = cast(Mapping[str, str], v2["file_sha256"])
    if set(files) != {path.name for path in v2_bundle_dir.iterdir() if path.is_file()}:
        raise SourceBundleV3Error("v2 frozen file set drifted")
    for name, digest in sorted(files.items()):
        _require_file_hash(v2_bundle_dir / name, digest, f"v2 {name}")

    pin_paths = _repo_pin_paths(repo_root, config)
    builder = cast(Mapping[str, Any], config["builder"])
    v2_source_config = cast(Mapping[str, Any], config["v2_source_config"])
    meta = cast(Mapping[str, Any], config["meta_instruction_filter"])
    review = cast(Mapping[str, Any], config["human_review"])
    conservation = cast(Mapping[str, Any], config["conservation"])
    pin_hashes = {
        "builder implementation": builder["implementation_sha256"],
        "v2 source config": v2_source_config["sha256"],
        "meta implementation": meta["implementation_sha256"],
        "baseline meta fixture": meta["baseline_fixture_sha256"],
        "active meta fixture": meta["active_fixture_sha256"],
        "human review contract": review["contract_sha256"],
        "human review implementation": review["implementation_sha256"],
        "conservation implementation": conservation["implementation_sha256"],
    }
    for label, path in pin_paths.items():
        _require_file_hash(path, pin_hashes[label], label)

    baseline_path = pin_paths["baseline meta fixture"]
    active_path = pin_paths["active meta fixture"]
    verify_v2_impact_fixture(v2_bundle_dir, baseline_path)
    verify_v2_active_impact_fixture(v2_bundle_dir, baseline_path, active_path)
    baseline_fixture = _object(baseline_path)
    active_fixture = _object(active_path)
    if (
        baseline_fixture.get("expected_rows") != BASELINE_META_ROWS
        or baseline_fixture.get("expected_view_counts") != BASELINE_META_VIEW_COUNTS
        or active_fixture.get("expected_rows") != ACTIVE_META_ROWS
        or active_fixture.get("expected_view_counts") != ACTIVE_META_VIEW_COUNTS
        or active_fixture.get("baseline_receipt", {}).get("expected_rows") != BASELINE_META_ROWS
    ):
        raise SourceBundleV3Error("active meta fixture count/baseline conservation drifted")

    review_config_path = pin_paths["human review contract"]
    verify_review_packet(review_config_path, review_packet_dir)

    # This is intentionally after every immutable-input replay but before any output creation.
    (
        reviews_path,
        reviews_hash,
        allowlist,
        attestation_path,
        attestation_hash,
        attestor_allowlist,
    ) = _required_external_review_pins(config, repo_root=repo_root)
    _require_file_hash(reviews_path, reviews_hash, "completed human reviews")
    _require_file_hash(attestation_path, attestation_hash, "external human attestation")
    try:
        receipt = verify_completed_human_reviews(
            review_config_path,
            review_packet_dir,
            reviews_path,
        )
    except SourceReviewContractError as error:
        raise SourceBundleV3Blocked(str(error)) from error
    if receipt.reviewer_identities != allowlist:
        raise SourceBundleV3Blocked("verified review identities do not equal the frozen allowlist")
    attestation = ExternalHumanAttestationV3.model_validate(_object(attestation_path))
    if (
        attestation.completed_reviews_sha256 != reviews_hash
        or attestation.reviewer_identities != allowlist
        or attestation.attestor_identity not in attestor_allowlist
    ):
        raise SourceBundleV3Blocked(
            "external attestation does not bind the frozen reviews/allowlist"
        )
    if not receipt.schema_coverage_binding_passed:
        raise SourceBundleV3Blocked("completed review receipt did not pass its coverage gate")
    if receipt.review_count != REQUIRED_HUMAN_REVIEWS:
        raise SourceBundleV3Blocked("completed human-review count drifted")
    return ReleasePreflightV3(
        v2_file_count=len(files),
        baseline_meta_rows=BASELINE_META_ROWS,
        active_meta_rows=ACTIVE_META_ROWS,
        review_count=receipt.review_count,
        reviewer_identities=receipt.reviewer_identities,
        release_gate_passed=True,
    )


def canonical_source_line(source: SourceRecord) -> bytes:
    return canonical_json_bytes(source.model_dump(mode="json")) + b"\n"


def _final_review_disposition(
    source_id: str,
    *,
    v2_quarantine: set[str],
    meta_quarantine: set[str],
    review_verdicts: Mapping[str, FinalReviewVerdict],
) -> Literal["active", "quarantine"]:
    if source_id in meta_quarantine:
        return "quarantine"
    verdict = review_verdicts.get(source_id)
    if verdict == "admit_standalone_aligned":
        return "active"
    if verdict is not None:
        return "quarantine"
    return "quarantine" if source_id in v2_quarantine else "active"


def plan_release(
    *,
    rows: Mapping[str, SourceRecord],
    source_line_bytes: Mapping[str, bytes],
    release_class_by_id: Mapping[str, str],
    v2_active_order: Sequence[str],
    v2_core_ids: Sequence[str],
    v2_tail_ids: Sequence[str],
    v2_quarantine_ids: Sequence[str],
    meta_quarantine_ids: Sequence[str],
    review_verdicts: Mapping[str, FinalReviewVerdict],
    review_evidence_sha256: Mapping[str, str],
    meta_evidence_sha256: Mapping[str, str],
    target_core_count: int = 50_000,
) -> ReleasePlanV3:
    """Plan v3 views with class-aware ordering and exhaustive delta reasons."""

    universe = set(rows)
    v2_core = set(v2_core_ids)
    v2_tail = set(v2_tail_ids)
    v2_quarantine = set(v2_quarantine_ids)
    if v2_core & v2_tail or v2_core & v2_quarantine or v2_tail & v2_quarantine:
        raise SourceBundleV3Error("v2 source views overlap")
    if v2_core | v2_tail | v2_quarantine != universe:
        raise SourceBundleV3Error("v2 views do not partition the source universe")
    if tuple(v2_active_order) != tuple(dict.fromkeys(v2_active_order)):
        raise SourceBundleV3Error("v2 active order contains duplicates")
    if set(v2_active_order) != v2_core | v2_tail:
        raise SourceBundleV3Error("v2 active order does not cover active sources")
    if set(source_line_bytes) != universe or set(release_class_by_id) != universe:
        raise SourceBundleV3Error("source bytes/classes do not cover the source universe")
    for source_id, source in rows.items():
        if source.source_id != source_id or source_line_bytes[source_id] != canonical_source_line(
            source
        ):
            raise SourceBundleV3Error("SourceRecord bytes or mapping identity drifted")

    meta = set(meta_quarantine_ids)
    if not meta.issubset(v2_core | v2_tail):
        raise SourceBundleV3Error("meta quarantine must be derived from v2 active sources")
    if set(meta_evidence_sha256) != meta:
        raise SourceBundleV3Error("meta evidence does not cover every meta quarantine")
    if not set(review_verdicts).issubset(universe):
        raise SourceBundleV3Error("review verdict names an unknown source")

    disposition = {
        source_id: _final_review_disposition(
            source_id,
            v2_quarantine=v2_quarantine,
            meta_quarantine=meta,
            review_verdicts=review_verdicts,
        )
        for source_id in universe
    }
    quarantine_ids = tuple(
        sorted(source_id for source_id in universe if disposition[source_id] == "quarantine")
    )

    class_order = tuple(
        dict.fromkeys(release_class_by_id[source_id] for source_id in v2_active_order)
    )
    extra_classes = sorted(set(release_class_by_id.values()) - set(class_order))
    class_order += tuple(extra_classes)
    retained_by_class: dict[str, list[str]] = {name: [] for name in class_order}
    for source_id in v2_active_order:
        if disposition[source_id] == "active":
            retained_by_class[release_class_by_id[source_id]].append(source_id)
    readmitted_by_class: dict[str, list[str]] = {name: [] for name in class_order}
    for source_id in sorted(v2_quarantine):
        if disposition[source_id] == "active":
            readmitted_by_class[release_class_by_id[source_id]].append(source_id)
    ordered_active = tuple(
        source_id
        for release_class in class_order
        for source_id in retained_by_class[release_class] + readmitted_by_class[release_class]
    )
    if len(ordered_active) < target_core_count:
        raise SourceBundleV3Error("v3 active pool is smaller than the configured core target")
    core_ids = ordered_active[:target_core_count]
    tail_ids = ordered_active[target_core_count:]

    v3_core = set(core_ids)
    v3_tail = set(tail_ids)
    selection_evidence = hash_canonical(
        {
            "rule": CORE_SELECTION_RULE,
            "target_core_count": target_core_count,
            "ordered_active_ids_sha256": sha256_hex("\n".join(ordered_active).encode("utf-8")),
        }
    )
    reasons: list[ExplicitDeltaReasonV3] = []
    for source_id in sorted(universe):
        old_view = (
            "core" if source_id in v2_core else "tail" if source_id in v2_tail else "quarantine"
        )
        new_view = (
            "core" if source_id in v3_core else "tail" if source_id in v3_tail else "quarantine"
        )
        if old_view in {"core", "tail"} and new_view == "quarantine":
            if source_id in meta:
                reasons.append(
                    ExplicitDeltaReasonV3(
                        source_id=source_id,
                        direction="quarantined",
                        reason_code="meta_instruction_quarantine",
                        rationale="active v3 fail-closed meta-instruction detector matched this NL",
                        evidence_sha256=meta_evidence_sha256[source_id],
                    )
                )
            else:
                evidence = review_evidence_sha256.get(source_id)
                if evidence is None:
                    raise SourceBundleV3Error("human quarantine lacks review evidence")
                reasons.append(
                    ExplicitDeltaReasonV3(
                        source_id=source_id,
                        direction="quarantined",
                        reason_code="human_review_quarantine",
                        rationale="frozen final human review verdict requires source quarantine",
                        evidence_sha256=evidence,
                    )
                )
        elif old_view == "quarantine" and new_view in {"core", "tail"}:
            evidence = review_evidence_sha256.get(source_id)
            if evidence is None:
                raise SourceBundleV3Error("human readmission lacks review evidence")
            reasons.append(
                ExplicitDeltaReasonV3(
                    source_id=source_id,
                    direction="readmitted",
                    reason_code="human_review_readmission",
                    rationale="frozen final human review found a standalone aligned source",
                    evidence_sha256=evidence,
                )
            )
        elif old_view != new_view:
            reasons.append(
                ExplicitDeltaReasonV3(
                    source_id=source_id,
                    direction="moved",
                    reason_code="core_boundary_reselection",
                    rationale="deterministic class-aware v3 core boundary reselection",
                    evidence_sha256=selection_evidence,
                )
            )

    events = build_conservation_events(
        v2_rows=rows,
        v2_core_ids=v2_core_ids,
        v2_quarantine_ids=v2_quarantine_ids,
        v2_tail_ids=v2_tail_ids,
        v3_rows=rows,
        v3_core_ids=core_ids,
        v3_quarantine_ids=quarantine_ids,
        v3_tail_ids=tail_ids,
        delta_reasons=reasons,
    )
    event_stream = b"".join(
        canonical_json_bytes(event.model_dump(mode="json")) + b"\n" for event in events
    )
    action_counts, reason_counts = summarize_event_stream(event_stream)
    source_bytes = b"".join(source_line_bytes[source_id] for source_id in ordered_active)
    return ReleasePlanV3(
        ordered_active_ids=ordered_active,
        core_ids=core_ids,
        tail_ids=tail_ids,
        quarantine_ids=quarantine_ids,
        source_bytes=source_bytes,
        events=events,
        event_stream=event_stream,
        action_counts=action_counts,
        reason_counts=reason_counts,
    )


def summarize_event_stream(
    event_stream: bytes,
) -> tuple[dict[ConservationAction, int], dict[DeltaReasonCode, int]]:
    """Parse the serialized event stream before deriving release counters."""

    if not event_stream or not event_stream.endswith(b"\n"):
        raise SourceBundleV3Error("conservation event stream must be nonempty canonical JSONL")
    events: list[SourceConservationEventV3] = []
    for line in event_stream.splitlines(keepends=True):
        payload = json.loads(line)
        event = SourceConservationEventV3.model_validate(payload)
        if line != canonical_json_bytes(event.model_dump(mode="json")) + b"\n":
            raise SourceBundleV3Error("conservation event stream is not canonical JSONL")
        events.append(event)
    observed_actions, observed_reasons = summarize_conservation(events)
    actions = {name: observed_actions.get(name, 0) for name in get_args(ConservationAction)}
    reasons = {name: observed_reasons.get(name, 0) for name in get_args(DeltaReasonCode)}
    if sum(actions.values()) != len(events):
        raise SourceBundleV3Error("parsed conservation counters do not cover the event stream")
    reasoned = {
        "added",
        "moved_core_to_tail",
        "moved_tail_to_core",
        "quarantined_from_core",
        "quarantined_from_tail",
        "readmitted_to_core",
        "readmitted_to_tail",
        "removed",
    }
    if sum(reasons.values()) != sum(
        count for action, count in actions.items() if action in reasoned
    ):
        raise SourceBundleV3Error("parsed conservation reasons do not cover every delta")
    return (
        cast(dict[ConservationAction, int], actions),
        cast(dict[DeltaReasonCode, int], reasons),
    )


@dataclass(frozen=True, slots=True)
class _ProductionState:
    rows: dict[str, SourceRecord]
    source_lines: dict[str, bytes]
    release_classes: dict[str, str]
    active_order: tuple[str, ...]
    core_ids: tuple[str, ...]
    tail_ids: tuple[str, ...]
    quarantine_ids: tuple[str, ...]
    meta_ids: tuple[str, ...]
    meta_evidence: dict[str, str]
    review_verdicts: dict[str, FinalReviewVerdict]
    review_evidence: dict[str, str]
    reviews: tuple[HumanSourceReviewV3, ...]
    mechanical_evidence: dict[str, tuple[str, str]]


def _validate_state_against_config(state: _ProductionState, config: Mapping[str, Any]) -> None:
    v2 = cast(Mapping[str, Any], config["v2_evidence"])
    meta = cast(Mapping[str, Any], config["meta_instruction_filter"])
    review = cast(Mapping[str, Any], config["human_review"])
    core, tail, quarantine = set(state.core_ids), set(state.tail_ids), set(state.quarantine_ids)
    universe = set(state.rows)
    if (
        len(state.active_order) != v2["source_count"]
        or len(state.core_ids) != v2["matched_count"]
        or len(state.tail_ids) != v2["tail_count"]
        or len(state.quarantine_ids) != v2["workbook_quarantine_count"]
        or len(state.rows) != V2_UNIVERSE_ROWS
    ):
        raise SourceBundleV3Error("loaded v2 state counts differ from the strict config")
    if (
        core & tail
        or core & quarantine
        or tail & quarantine
        or core | tail | quarantine != universe
        or tuple(dict.fromkeys(state.active_order)) != state.active_order
        or set(state.active_order) != core | tail
    ):
        raise SourceBundleV3Error("loaded v2 views do not form the exact ordered partition")
    meta_ids = set(state.meta_ids)
    observed_meta_views = {
        "matched_core": len(meta_ids & core),
        "legacy_tail": len(meta_ids & tail),
    }
    if (
        len(meta_ids) != meta["active_expected_rows"]
        or observed_meta_views != meta["active_expected_view_counts"]
        or not meta_ids.issubset(core | tail)
    ):
        raise SourceBundleV3Error("loaded active meta impact differs from the strict config")
    if len(state.reviews) != review["required_reviews"]:
        raise SourceBundleV3Error("loaded completed-review count differs from the strict config")


def _validate_plan_against_config(
    state: _ProductionState,
    plan: ReleasePlanV3,
    config: Mapping[str, Any],
) -> None:
    conservation = cast(Mapping[str, Any], config["conservation"])
    active = set(plan.ordered_active_ids)
    core, tail, quarantine = set(plan.core_ids), set(plan.tail_ids), set(plan.quarantine_ids)
    if (
        len(state.rows) != conservation["expected_v2_universe_count"]
        or len(plan.events) != len(state.rows)
        or len(plan.core_ids) != config["matched_view_rows"]
        or len(active) + len(quarantine) != len(state.rows)
        or core & tail
        or core & quarantine
        or tail & quarantine
        or core | tail != active
        or active | quarantine != set(state.rows)
    ):
        raise SourceBundleV3Error("planned v3 views do not conserve the configured universe")
    if (
        plan.action_counts["added"] != conservation["expected_additions"]
        or plan.action_counts["removed"] != conservation["expected_removals"]
        or plan.reason_counts["dedup_displacement_addition"]
        != conservation["expected_dedup_displacements"]
        or plan.reason_counts["dedup_displacement_movement"]
        != conservation["expected_dedup_displacement_movements"]
    ):
        raise SourceBundleV3Error("planned conservation actions differ from the strict config")
    if (
        (not conservation["allow_new_sources"] and plan.action_counts["added"])
        or (not conservation["allow_removed_sources"] and plan.action_counts["removed"])
        or conservation["core_selection_rule"] != CORE_SELECTION_RULE
    ):
        raise SourceBundleV3Error("planned source policy differs from the strict config")


def _canonical_model_jsonl(models: Sequence[StrictModel]) -> bytes:
    return b"".join(canonical_json_bytes(model.model_dump(mode="json")) + b"\n" for model in models)


def _jsonl_objects(path: Path) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_bytes().splitlines(keepends=True), start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise SourceBundleV3Error(f"invalid JSONL at {path}:{line_number}") from error
        if not isinstance(value, dict):
            raise SourceBundleV3Error(f"non-object JSONL at {path}:{line_number}")
        result.append(cast(dict[str, Any], value))
    if not result:
        raise SourceBundleV3Error(f"empty JSONL: {path}")
    return tuple(result)


def _load_production_state(
    repo_root: Path,
    *,
    config: Mapping[str, Any],
    v2_bundle_dir: Path,
    reviews_path: Path | None = None,
) -> _ProductionState:
    rows: dict[str, SourceRecord] = {}
    source_lines: dict[str, bytes] = {}
    active_order: list[str] = []
    for raw_line in (v2_bundle_dir / "sources.jsonl").read_bytes().splitlines(keepends=True):
        source = SourceRecord.model_validate(json.loads(raw_line))
        canonical = canonical_source_line(source)
        if raw_line != canonical or source.source_id in rows:
            raise SourceBundleV3Error("v2 active SourceRecord JSONL is noncanonical or duplicated")
        rows[source.source_id] = source
        source_lines[source.source_id] = canonical
        active_order.append(source.source_id)

    core = SourceIdViewV2.model_validate(_object(v2_bundle_dir / "matched_50000_source_ids.json"))
    tail = SourceIdViewV2.model_validate(_object(v2_bundle_dir / "legacy_tail_source_ids.json"))
    quarantine_rows = tuple(
        WorkbookQuarantineV2.model_validate(value)
        for value in _jsonl_objects(v2_bundle_dir / "workbook_quarantine.jsonl")
    )
    quarantine_ids: list[str] = []
    workbook_evidence: dict[str, tuple[str, str]] = {}
    for wrapper in quarantine_rows:
        source = wrapper.source
        if source.source_id in rows:
            raise SourceBundleV3Error("v2 Workbook quarantine overlaps active sources")
        rows[source.source_id] = source
        source_lines[source.source_id] = canonical_source_line(source)
        quarantine_ids.append(source.source_id)
        workbook_evidence[source.source_id] = (
            "v2_workbook_automatic_disposition",
            hash_canonical(wrapper.discourse_audit.model_dump(mode="json")),
        )

    release_classes: dict[str, str] = {}
    mechanical_evidence: dict[str, tuple[str, str]] = dict(workbook_evidence)
    for value in _jsonl_objects(v2_bundle_dir / "source_audit.jsonl"):
        audit = SourceSelectionAuditV2.model_validate(value)
        if audit.source_id in release_classes:
            raise SourceBundleV3Error("duplicate v2 source-selection evidence")
        release_classes[audit.source_id] = audit.release_class
        mechanical_evidence[audit.source_id] = (
            "v2_source_selection_audit",
            hash_canonical(audit.model_dump(mode="json")),
        )
    for source_id in quarantine_ids:
        release_classes[source_id] = "lean_workbook"
    if set(release_classes) != set(rows) or set(mechanical_evidence) != set(rows):
        raise SourceBundleV3Error("mechanical v2 evidence does not cover the full source universe")

    meta = cast(Mapping[str, Any], config["meta_instruction_filter"])
    active_fixture = _object(_repo_path(repo_root, meta["active_fixture_path"]))
    meta_evidence: dict[str, str] = {}
    for value in cast(list[dict[str, Any]], active_fixture["rows"]):
        source_id = str(value["source_id"])
        if source_id in meta_evidence:
            raise SourceBundleV3Error("active meta fixture contains duplicate IDs")
        meta_evidence[source_id] = hash_canonical(value)
    if len(meta_evidence) != ACTIVE_META_ROWS:
        raise SourceBundleV3Error("active meta fixture does not contain exactly 469 rows")

    review = cast(Mapping[str, Any], config["human_review"])
    if reviews_path is None:
        reviews_path = _repo_path(repo_root, review["completed_reviews_path"])
    reviews = tuple(
        HumanSourceReviewV3.model_validate(value) for value in _jsonl_objects(reviews_path)
    )
    review_verdicts: dict[str, FinalReviewVerdict] = {}
    review_evidence: dict[str, str] = {}
    for item in reviews:
        if item.verdict == "needs_escalation":
            raise SourceBundleV3Blocked("unresolved human-review escalation cannot enter v3")
        if item.source_id in review_verdicts:
            raise SourceBundleV3Error("duplicate completed review source ID")
        review_verdicts[item.source_id] = item.verdict
        review_evidence[item.source_id] = hash_canonical(item.model_dump(mode="json"))
    state = _ProductionState(
        rows=rows,
        source_lines=source_lines,
        release_classes=release_classes,
        active_order=tuple(active_order),
        core_ids=core.source_ids,
        tail_ids=tail.source_ids,
        quarantine_ids=tuple(sorted(quarantine_ids)),
        meta_ids=tuple(sorted(meta_evidence)),
        meta_evidence=meta_evidence,
        review_verdicts=review_verdicts,
        review_evidence=review_evidence,
        reviews=tuple(sorted(reviews, key=lambda item: item.source_id)),
        mechanical_evidence=mechanical_evidence,
    )
    _validate_state_against_config(state, config)
    return state


def _plan_from_state(
    state: _ProductionState,
    *,
    target_core_count: int,
    config: Mapping[str, Any],
) -> ReleasePlanV3:
    plan = plan_release(
        rows=state.rows,
        source_line_bytes=state.source_lines,
        release_class_by_id=state.release_classes,
        v2_active_order=state.active_order,
        v2_core_ids=state.core_ids,
        v2_tail_ids=state.tail_ids,
        v2_quarantine_ids=state.quarantine_ids,
        meta_quarantine_ids=state.meta_ids,
        review_verdicts=state.review_verdicts,
        review_evidence_sha256=state.review_evidence,
        meta_evidence_sha256=state.meta_evidence,
        target_core_count=target_core_count,
    )
    _validate_plan_against_config(state, plan, config)
    return plan


def _prompt_counts(
    repo_root: Path,
    *,
    v2_config_path: Path,
    sources: Sequence[SourceRecord],
) -> tuple[bytes, int, int]:
    """Re-render and tokenize every admitted v3 source with the pinned v2 contract."""

    from leanfaith.sft2b.full_source_freeze import _render_source_prompt, _resolved_source_config
    from leanfaith.sft2b.pilot_source_freeze import _tokenizer

    _, effective, _ = _resolved_source_config(repo_root, v2_config_path)
    prompt = cast(Mapping[str, Any], effective["prompt"])
    prompt_path = _repo_path(repo_root, prompt["path"])
    template = prompt_path.read_text(encoding="utf-8")
    tokenizer = _tokenizer(effective)
    placement = cast(Mapping[str, Any], effective["placement"])
    tokenizer_config = cast(Mapping[str, Any], effective["tokenizer"])
    rows: list[dict[str, object]] = []
    maximum = 0
    for source in sources:
        rendered = _render_source_prompt(template, source)
        count = len(tokenizer.encode(rendered, add_special_tokens=True))
        maximum = max(maximum, count)
        rows.append(
            {
                "source_id": source.source_id,
                "prompt_sha256": sha256_hex(rendered.encode("utf-8")),
                "prompt_tokens": count,
            }
        )
    max_new_tokens = int(placement["max_new_tokens"])
    required = maximum + max_new_tokens
    payload = {
        "schema_version": "sft2b_prompt_token_counts_v3",
        "source_count": len(sources),
        "model_id": placement["model_id"],
        "model_revision": placement["model_revision"],
        "prompt_sha256": prompt["sha256"],
        "tokenizer_revision": tokenizer_config["revision"],
        "tokenizer_sha256": tokenizer_config["primary_sha256"],
        "maximum_prompt_tokens": maximum,
        "max_new_tokens": max_new_tokens,
        "required_max_model_len": required,
        "rows": rows,
    }
    return canonical_json_bytes(payload) + b"\n", maximum, required


def _expected_mechanical_evidence(
    state: _ProductionState, plan: ReleasePlanV3
) -> tuple[MechanicalSourceEvidenceV3, ...]:
    v2_core, v2_tail = set(state.core_ids), set(state.tail_ids)
    v3_core, v3_tail = set(plan.core_ids), set(plan.tail_ids)
    result: list[MechanicalSourceEvidenceV3] = []
    for source_id in sorted(state.rows):
        kind, evidence_hash = state.mechanical_evidence[source_id]
        result.append(
            MechanicalSourceEvidenceV3.model_validate(
                {
                    "source_id": source_id,
                    "release_class": state.release_classes[source_id],
                    "source_record_sha256": hash_canonical(
                        state.rows[source_id].model_dump(mode="json")
                    ),
                    "v2_view": (
                        "core"
                        if source_id in v2_core
                        else "tail"
                        if source_id in v2_tail
                        else "quarantine"
                    ),
                    "v3_view": (
                        "core"
                        if source_id in v3_core
                        else "tail"
                        if source_id in v3_tail
                        else "quarantine"
                    ),
                    "v2_evidence_kind": kind,
                    "v2_evidence_sha256": evidence_hash,
                    "semantic_or_human_review": False,
                }
            )
        )
    return tuple(result)


def _frozen_v2_manifest_evidence(
    config: Mapping[str, Any], v2_bundle_dir: Path
) -> dict[str, object]:
    v2 = cast(Mapping[str, Any], config["v2_evidence"])
    file_hashes = cast(Mapping[str, str], v2["file_sha256"])
    manifest = _object(v2_bundle_dir / "source_manifest.json")
    if any(key not in manifest for key in V2_RICH_MANIFEST_KEYS):
        raise SourceBundleV3Error("frozen v2 source manifest lacks required rich evidence")
    copied_files = {
        target_name: {
            "frozen_v2_name": source_name,
            "sha256": file_hashes[source_name],
        }
        for source_name, target_name in sorted(FROZEN_V2_COPY_NAMES.items())
    }
    return {
        "copied_files": copied_files,
        "rich_manifest_fields": {key: manifest[key] for key in V2_RICH_MANIFEST_KEYS},
        "semantic_alignment_audit": {
            "frozen_v2_name": "semantic_alignment_audit.jsonl",
            "sha256": file_hashes["semantic_alignment_audit.jsonl"],
            "v3_disposition": (
                "historical_automatic_selection_evidence_not_copied_or_treated_as_"
                "semantic_or_human_review"
            ),
        },
    }


def _write_release_files(
    repo_root: Path,
    *,
    config_path: Path,
    config: Mapping[str, Any],
    v2_bundle_dir: Path,
    review_packet_dir: Path,
    state: _ProductionState,
    plan: ReleasePlanV3,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=False, exist_ok=False)
    sources_path = output_dir / "sources.jsonl"
    sources_path.write_bytes(plan.source_bytes)
    source_hash = hash_file(sources_path)
    core_view = SourceIdViewV2(
        view_id="corrected_core_50000",
        source_count=len(plan.core_ids),
        selection_rule=CORE_SELECTION_RULE,
        parent_sources_sha256=source_hash,
        source_ids=plan.core_ids,
    )
    tail_view = SourceIdViewV2(
        view_id="legacy_tail",
        source_count=len(plan.tail_ids),
        selection_rule="ordered_v3_sources_minus_corrected_core_50000_v1",
        parent_sources_sha256=source_hash,
        source_ids=plan.tail_ids,
    )
    (output_dir / "matched_50000_source_ids.json").write_bytes(
        canonical_json_bytes(core_view.model_dump(mode="json")) + b"\n"
    )
    (output_dir / "legacy_tail_source_ids.json").write_bytes(
        canonical_json_bytes(tail_view.model_dump(mode="json")) + b"\n"
    )

    quarantines: list[QuarantinedSourceV3] = []
    v2_core, v2_tail = set(state.core_ids), set(state.tail_ids)
    for source_id in plan.quarantine_ids:
        if source_id in state.meta_evidence:
            basis: Literal["active_meta_instruction_filter_v2", "final_human_review_v3"] = (
                "active_meta_instruction_filter_v2"
            )
            evidence = state.meta_evidence[source_id]
            verdict = None
        else:
            basis = "final_human_review_v3"
            evidence = state.review_evidence[source_id]
            verdict = state.review_verdicts[source_id]
        v2_view: Literal["core", "tail", "quarantine"] = (
            "core" if source_id in v2_core else "tail" if source_id in v2_tail else "quarantine"
        )
        quarantines.append(
            QuarantinedSourceV3(
                source=state.rows[source_id],
                source_record_sha256=hash_canonical(state.rows[source_id].model_dump(mode="json")),
                v2_view=v2_view,
                terminal_basis=basis,
                evidence_sha256=evidence,
                human_verdict=verdict,
            )
        )
    (output_dir / "source_quarantine.jsonl").write_bytes(_canonical_model_jsonl(quarantines))

    (output_dir / "source_mechanical_evidence.jsonl").write_bytes(
        _canonical_model_jsonl(_expected_mechanical_evidence(state, plan))
    )
    (output_dir / "source_conservation_events.jsonl").write_bytes(plan.event_stream)
    for source_name, target_name in FROZEN_V2_COPY_NAMES.items():
        shutil.copyfile(v2_bundle_dir / source_name, output_dir / target_name)
    reviews_path, _, _, attestation_path, _, _ = _required_external_review_pins(
        config, repo_root=repo_root
    )
    shutil.copyfile(reviews_path, output_dir / "human_reviews.jsonl")
    shutil.copyfile(attestation_path, output_dir / "external_human_attestation.json")
    attestation_record = ExternalHumanAttestationV3.model_validate(_object(attestation_path))
    for source_name, target_name in (
        ("automatic_dispositions.jsonl", "automatic_dispositions.jsonl"),
        ("review_packet.jsonl", "review_packet.jsonl"),
        ("review_packet_manifest.json", "review_packet_manifest.json"),
        ("SHA256SUMS", "review_packet_SHA256SUMS"),
    ):
        shutil.copyfile(review_packet_dir / source_name, output_dir / target_name)

    review_config_path = _repo_path(
        repo_root, cast(Mapping[str, Any], config["human_review"])["contract_path"]
    )
    review_receipt = verify_completed_human_reviews(
        review_config_path,
        review_packet_dir,
        reviews_path,
    )
    (output_dir / "human_review_verification_receipt.json").write_bytes(
        canonical_json_bytes(review_receipt.model_dump(mode="json")) + b"\n"
    )

    ordered_sources = [state.rows[source_id] for source_id in plan.ordered_active_ids]
    v2_config_path = _repo_path(
        repo_root, cast(Mapping[str, Any], config["v2_source_config"])["path"]
    )
    prompt_bytes, maximum_prompt_tokens, required_max_model_len = _prompt_counts(
        repo_root,
        v2_config_path=v2_config_path,
        sources=ordered_sources,
    )
    (output_dir / "prompt_token_counts.json").write_bytes(prompt_bytes)

    action_counts, reason_counts = summarize_event_stream(plan.event_stream)
    receipt = SourceConservationReceiptV3(
        v2_sources_sha256=hash_file(v2_bundle_dir / "sources.jsonl"),
        v2_core_view_sha256=hash_file(v2_bundle_dir / "matched_50000_source_ids.json"),
        v2_quarantine_view_sha256=hash_file(v2_bundle_dir / "workbook_quarantine.jsonl"),
        v2_tail_view_sha256=hash_file(v2_bundle_dir / "legacy_tail_source_ids.json"),
        v3_sources_sha256=source_hash,
        v3_core_view_sha256=hash_file(output_dir / "matched_50000_source_ids.json"),
        v3_quarantine_view_sha256=hash_file(output_dir / "source_quarantine.jsonl"),
        v3_tail_view_sha256=hash_file(output_dir / "legacy_tail_source_ids.json"),
        event_stream_sha256=hash_file(output_dir / "source_conservation_events.jsonl"),
        event_count=len(plan.events),
        v2_source_count=len(state.rows),
        v3_source_count=len(state.rows),
        action_counts=action_counts,
        reason_counts=reason_counts,
        v2_partition_complete=True,
        v3_partition_complete=True,
        every_delta_explained=True,
    )
    (output_dir / "source_conservation_receipt.json").write_bytes(
        canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n"
    )

    file_names = sorted(path.name for path in output_dir.iterdir() if path.is_file())
    file_hashes = {name: hash_file(output_dir / name) for name in file_names}
    manifest = {
        "schema_version": "sft2b_diverse_full_source_manifest_v3",
        "source_config_path": str(config_path),
        "source_config_sha256": hash_file(config_path),
        "builder_implementation_sha256": hash_file(Path(__file__)),
        "v2_evidence": config["v2_evidence"],
        "frozen_v2_evidence": _frozen_v2_manifest_evidence(config, v2_bundle_dir),
        "source_count": len(plan.ordered_active_ids),
        "core_count": len(plan.core_ids),
        "tail_count": len(plan.tail_ids),
        "quarantine_count": len(plan.quarantine_ids),
        "source_universe_count": len(state.rows),
        "selection_rule": CORE_SELECTION_RULE,
        "meta_instruction": {
            "baseline_rows": BASELINE_META_ROWS,
            "active_rows": ACTIVE_META_ROWS,
            "additive_rows": ACTIVE_META_ROWS - BASELINE_META_ROWS,
        },
        "human_review": {
            "review_count": len(state.reviews),
            "reviewer_identities": list(review_receipt.reviewer_identities),
            "verdict_counts": dict(sorted(Counter(row.verdict for row in state.reviews).items())),
            "external_attestation_sha256": cast(Mapping[str, Any], config["human_review"])[
                "external_human_attestation_sha256"
            ],
            "allowed_attestor_identities": cast(Mapping[str, Any], config["human_review"])[
                "allowed_attestor_identities"
            ],
            "attestor_identity": attestation_record.attestor_identity,
            "attestation_scope": attestation_record.attestation_scope,
        },
        "conservation": {
            "action_counts": action_counts,
            "reason_counts": reason_counts,
            "additions": action_counts["added"],
            "removals": action_counts["removed"],
            "dedup_displacement_additions": reason_counts["dedup_displacement_addition"],
            "dedup_displacement_movements": reason_counts["dedup_displacement_movement"],
        },
        "prompt_tokens": {
            "maximum_prompt_tokens": maximum_prompt_tokens,
            "required_max_model_len": required_max_model_len,
        },
        "data_files": {name: {"sha256": digest} for name, digest in sorted(file_hashes.items())},
    }
    (output_dir / "source_manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    all_hashes = {path.name: hash_file(path) for path in output_dir.iterdir() if path.is_file()}
    (output_dir / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(all_hashes.items())),
        encoding="utf-8",
    )


def _verify_checksums(bundle_dir: Path) -> None:
    names = {path.name for path in bundle_dir.iterdir() if path.is_file()}
    if names != OUTPUT_NAMES:
        raise SourceBundleV3Error("v3 release file set drifted")
    lines = (bundle_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    observed: dict[str, str] = {}
    for line in lines:
        digest, separator, name = line.partition("  ")
        if not separator or name in observed:
            raise SourceBundleV3Error("malformed v3 SHA256SUMS")
        observed[name] = digest
    expected_names = {path.name for path in bundle_dir.iterdir() if path.is_file()} - {"SHA256SUMS"}
    if set(observed) != expected_names:
        raise SourceBundleV3Error("v3 SHA256SUMS file set drifted")
    for name, digest in observed.items():
        if hash_file(bundle_dir / name) != digest:
            raise SourceBundleV3Error(f"v3 checksum mismatch: {name}")


def _verify_static_inputs(
    repo_root: Path,
    *,
    config: Mapping[str, Any],
    v2_bundle_dir: Path,
) -> None:
    _validate_config(repo_root, config)
    v2 = cast(Mapping[str, Any], config["v2_evidence"])
    files = cast(Mapping[str, str], v2["file_sha256"])
    if set(files) != {path.name for path in v2_bundle_dir.iterdir() if path.is_file()}:
        raise SourceBundleV3Error("v2 frozen file set drifted")
    for name, digest in sorted(files.items()):
        _require_file_hash(v2_bundle_dir / name, digest, f"v2 {name}")
    pin_paths = _repo_pin_paths(repo_root, config)
    builder = cast(Mapping[str, Any], config["builder"])
    v2_source_config = cast(Mapping[str, Any], config["v2_source_config"])
    meta = cast(Mapping[str, Any], config["meta_instruction_filter"])
    review = cast(Mapping[str, Any], config["human_review"])
    conservation = cast(Mapping[str, Any], config["conservation"])
    pin_hashes = {
        "builder implementation": builder["implementation_sha256"],
        "v2 source config": v2_source_config["sha256"],
        "meta implementation": meta["implementation_sha256"],
        "baseline meta fixture": meta["baseline_fixture_sha256"],
        "active meta fixture": meta["active_fixture_sha256"],
        "human review contract": review["contract_sha256"],
        "human review implementation": review["implementation_sha256"],
        "conservation implementation": conservation["implementation_sha256"],
    }
    for label, path in pin_paths.items():
        _require_file_hash(path, pin_hashes[label], label)
    baseline = pin_paths["baseline meta fixture"]
    active = pin_paths["active meta fixture"]
    verify_v2_impact_fixture(v2_bundle_dir, baseline)
    verify_v2_active_impact_fixture(v2_bundle_dir, baseline, active)
    baseline_fixture = _object(baseline)
    active_fixture = _object(active)
    if (
        baseline_fixture.get("expected_rows") != BASELINE_META_ROWS
        or baseline_fixture.get("expected_view_counts") != BASELINE_META_VIEW_COUNTS
        or active_fixture.get("expected_rows") != ACTIVE_META_ROWS
        or active_fixture.get("expected_view_counts") != ACTIVE_META_VIEW_COUNTS
        or active_fixture.get("baseline_receipt", {}).get("expected_rows") != BASELINE_META_ROWS
    ):
        raise SourceBundleV3Error("active meta fixture count/baseline conservation drifted")


def _verify_contained_review_evidence(
    repo_root: Path,
    *,
    config: Mapping[str, Any],
    v2_bundle_dir: Path,
    bundle_dir: Path,
) -> tuple[HumanSourceReviewV3, ...]:
    reviews_hash, allowlist = _verify_contained_attestation_pins(config, bundle_dir)
    review = cast(Mapping[str, Any], config["human_review"])

    review_config = load_review_config(_repo_path(repo_root, review["contract_path"])).model_copy(
        update={"source_bundle_path": str(v2_bundle_dir)}
    )
    expected_entries, expected_automatic = expected_packet_entries(review_config)
    entries = tuple(
        SourceReviewPacketEntryV3.model_validate(value)
        for value in _jsonl_objects(bundle_dir / "review_packet.jsonl")
    )
    automatic = tuple(
        AutomaticDispositionV3.model_validate(value)
        for value in _jsonl_objects(bundle_dir / "automatic_dispositions.jsonl")
    )
    if entries != expected_entries or automatic != expected_automatic:
        raise SourceBundleV3Error("contained review packet does not replay from immutable v2")
    packet_manifest = ReviewPacketManifestV3.model_validate(
        _object(bundle_dir / "review_packet_manifest.json")
    )
    reason_counts = Counter(reason for entry in entries for reason in entry.required_reasons)
    release_counts = Counter(entry.release_class for entry in entries)
    overlap_ids = tuple(
        sorted(entry.source_id for entry in entries if len(entry.required_reasons) > 1)
    )
    expected_packet_manifest = ReviewPacketManifestV3.model_validate(
        {
            "source_bundle_revision": review_config.source_bundle_revision,
            "source_bundle_prefix": review_config.source_bundle_prefix,
            "packet_entry_count": len(entries),
            "automatic_disposition_count": len(automatic),
            "deterministic_sample_count": reason_counts["deterministic_100_per_release_class"],
            "workbook_hit_count": reason_counts["workbook_heuristic_hit"],
            "overlap_count": len(overlap_ids),
            "release_class_counts": dict(sorted(release_counts.items())),
            "reason_counts": dict(sorted(reason_counts.items())),
            "overlap_source_ids": overlap_ids,
            "review_status": "awaiting_authentic_human_review",
            "review_contract_schema_sha256": hash_canonical(
                HumanSourceReviewV3.model_json_schema()
            ),
            "packet_sha256": hash_file(bundle_dir / "review_packet.jsonl"),
            "automatic_dispositions_sha256": hash_file(bundle_dir / "automatic_dispositions.jsonl"),
        }
    )
    if packet_manifest != expected_packet_manifest:
        raise SourceBundleV3Error("contained review-packet manifest drifted")
    packet_checksums: dict[str, str] = {}
    for line in (bundle_dir / "review_packet_SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name in packet_checksums:
            raise SourceBundleV3Error("contained review packet checksum ledger is malformed")
        packet_checksums[name] = digest
    expected_packet_checksums = {
        "automatic_dispositions.jsonl": hash_file(bundle_dir / "automatic_dispositions.jsonl"),
        "review_packet.jsonl": hash_file(bundle_dir / "review_packet.jsonl"),
        "review_packet_manifest.json": hash_file(bundle_dir / "review_packet_manifest.json"),
    }
    if packet_checksums != expected_packet_checksums:
        raise SourceBundleV3Error("contained review packet checksum ledger drifted")

    reviews = tuple(
        HumanSourceReviewV3.model_validate(value)
        for value in _jsonl_objects(bundle_dir / "human_reviews.jsonl")
    )
    packet_by_id = {entry.packet_entry_id: entry for entry in entries}
    review_by_packet = {item.packet_entry_id: item for item in reviews}
    if len(review_by_packet) != len(reviews) or set(review_by_packet) != set(packet_by_id):
        raise SourceBundleV3Error("contained human-review coverage is incomplete or duplicated")
    for packet_id, item in review_by_packet.items():
        entry = packet_by_id[packet_id]
        if (
            item.source_id != entry.source_id
            or item.reviewed_fields != entry.reviewed_fields
            or item.reviewed_field_sha256 != entry.reviewed_field_sha256
            or item.reviewed_source_sha256 != entry.reviewed_source_sha256
            or item.verdict == "needs_escalation"
        ):
            raise SourceBundleV3Error("contained human review/source binding failed")
    identities = tuple(sorted({item.reviewer_identity for item in reviews}))
    if identities != allowlist:
        raise SourceBundleV3Blocked("contained review identities differ from frozen allowlist")
    receipt = HumanReviewVerificationReceiptV3.model_validate(
        _object(bundle_dir / "human_review_verification_receipt.json")
    )
    verdict_counts = dict(sorted(Counter(item.verdict for item in reviews).items()))
    if (
        receipt.packet_sha256 != packet_manifest.packet_sha256
        or receipt.reviews_sha256 != reviews_hash
        or receipt.reviewer_identities != allowlist
        or receipt.verdict_counts != verdict_counts
        or receipt.escalation_count != 0
    ):
        raise SourceBundleV3Error("contained low-level review verification receipt drifted")
    return reviews


def _verify_contained_attestation_pins(
    config: Mapping[str, Any], bundle_dir: Path
) -> tuple[str, tuple[str, ...]]:
    review = cast(Mapping[str, Any], config["human_review"])
    reviews_hash = review.get("completed_reviews_sha256")
    attestation_hash = review.get("external_human_attestation_sha256")
    allowlist_raw = review.get("allowed_reviewer_identities")
    attestor_allowlist_raw = review.get("allowed_attestor_identities")
    if not isinstance(reviews_hash, str) or not isinstance(attestation_hash, str):
        raise SourceBundleV3Blocked("v3 config lacks frozen review/attestation hashes")
    if not isinstance(allowlist_raw, list) or not allowlist_raw:
        raise SourceBundleV3Blocked("v3 config lacks a frozen reviewer allowlist")
    allowlist = tuple(cast(list[str], allowlist_raw))
    if tuple(sorted(set(allowlist))) != allowlist:
        raise SourceBundleV3Blocked("frozen reviewer allowlist is not unique and sorted")
    if not isinstance(attestor_allowlist_raw, list) or not attestor_allowlist_raw:
        raise SourceBundleV3Blocked("v3 config lacks a frozen attestor allowlist")
    attestor_allowlist = tuple(cast(list[str], attestor_allowlist_raw))
    if (
        any(not isinstance(value, str) or not value for value in attestor_allowlist)
        or tuple(sorted(set(attestor_allowlist))) != attestor_allowlist
    ):
        raise SourceBundleV3Blocked("frozen attestor allowlist is not unique and sorted")
    _require_file_hash(bundle_dir / "human_reviews.jsonl", reviews_hash, "contained reviews")
    _require_file_hash(
        bundle_dir / "external_human_attestation.json",
        attestation_hash,
        "contained external human attestation",
    )
    attestation = ExternalHumanAttestationV3.model_validate(
        _object(bundle_dir / "external_human_attestation.json")
    )
    if (
        attestation.completed_reviews_sha256 != reviews_hash
        or attestation.reviewer_identities != allowlist
        or attestation.attestor_identity not in attestor_allowlist
    ):
        raise SourceBundleV3Blocked("contained external attestation binding failed")
    return reviews_hash, allowlist


def verify_release(
    repo_root: Path,
    *,
    config_path: Path,
    v2_bundle_dir: Path,
    review_packet_dir: Path,
    bundle_dir: Path,
) -> None:
    """Fresh-directory verifier for a staged, never automatically published v3 bundle."""

    config = _object(config_path)
    _verify_static_inputs(repo_root, config=config, v2_bundle_dir=v2_bundle_dir)
    _verify_checksums(bundle_dir)
    for source_name, target_name in FROZEN_V2_COPY_NAMES.items():
        if (bundle_dir / target_name).read_bytes() != (v2_bundle_dir / source_name).read_bytes():
            raise SourceBundleV3Error(f"frozen v2 evidence copy drifted: {target_name}")
    contained_reviews = _verify_contained_review_evidence(
        repo_root,
        config=config,
        v2_bundle_dir=v2_bundle_dir,
        bundle_dir=bundle_dir,
    )
    state = _load_production_state(
        repo_root,
        config=config,
        v2_bundle_dir=v2_bundle_dir,
        reviews_path=bundle_dir / "human_reviews.jsonl",
    )
    if state.reviews != tuple(sorted(contained_reviews, key=lambda item: item.source_id)):
        raise SourceBundleV3Error("contained review parsing is not deterministic")
    plan = _plan_from_state(
        state,
        target_core_count=int(config["matched_view_rows"]),
        config=config,
    )
    if (bundle_dir / "sources.jsonl").read_bytes() != plan.source_bytes:
        raise SourceBundleV3Error("v3 sources do not replay from immutable v2 bytes")
    core = SourceIdViewV2.model_validate(_object(bundle_dir / "matched_50000_source_ids.json"))
    tail = SourceIdViewV2.model_validate(_object(bundle_dir / "legacy_tail_source_ids.json"))
    if core.source_ids != plan.core_ids or tail.source_ids != plan.tail_ids:
        raise SourceBundleV3Error("v3 deterministic core/tail views do not replay")
    if (bundle_dir / "source_conservation_events.jsonl").read_bytes() != plan.event_stream:
        raise SourceBundleV3Error("v3 conservation event stream does not replay")
    receipt = SourceConservationReceiptV3.model_validate(
        _object(bundle_dir / "source_conservation_receipt.json")
    )
    actions, reasons = summarize_event_stream(plan.event_stream)
    if receipt.action_counts != actions or receipt.reason_counts != reasons:
        raise SourceBundleV3Error("v3 conservation receipt counters do not replay")
    if (
        receipt.v2_sources_sha256 != hash_file(v2_bundle_dir / "sources.jsonl")
        or receipt.v2_core_view_sha256 != hash_file(v2_bundle_dir / "matched_50000_source_ids.json")
        or receipt.v2_quarantine_view_sha256
        != hash_file(v2_bundle_dir / "workbook_quarantine.jsonl")
        or receipt.v2_tail_view_sha256 != hash_file(v2_bundle_dir / "legacy_tail_source_ids.json")
        or receipt.v3_sources_sha256 != hash_file(bundle_dir / "sources.jsonl")
        or receipt.v3_core_view_sha256 != hash_file(bundle_dir / "matched_50000_source_ids.json")
        or receipt.v3_quarantine_view_sha256 != hash_file(bundle_dir / "source_quarantine.jsonl")
        or receipt.v3_tail_view_sha256 != hash_file(bundle_dir / "legacy_tail_source_ids.json")
        or receipt.event_stream_sha256 != hash_file(bundle_dir / "source_conservation_events.jsonl")
    ):
        raise SourceBundleV3Error("v3 conservation receipt file bindings do not replay")
    expected_prompt, _, _ = _prompt_counts(
        repo_root,
        v2_config_path=_repo_path(
            repo_root, cast(Mapping[str, Any], config["v2_source_config"])["path"]
        ),
        sources=[state.rows[source_id] for source_id in plan.ordered_active_ids],
    )
    if (bundle_dir / "prompt_token_counts.json").read_bytes() != expected_prompt:
        raise SourceBundleV3Error("v3 prompt-token artifact does not replay")
    quarantine_rows = tuple(
        QuarantinedSourceV3.model_validate(value)
        for value in _jsonl_objects(bundle_dir / "source_quarantine.jsonl")
    )
    expected_quarantine: list[QuarantinedSourceV3] = []
    old_core, old_tail = set(state.core_ids), set(state.tail_ids)
    for source_id in plan.quarantine_ids:
        if source_id in state.meta_evidence:
            basis: Literal["active_meta_instruction_filter_v2", "final_human_review_v3"] = (
                "active_meta_instruction_filter_v2"
            )
            evidence = state.meta_evidence[source_id]
            verdict = None
        else:
            basis = "final_human_review_v3"
            evidence = state.review_evidence[source_id]
            verdict = state.review_verdicts[source_id]
        expected_quarantine.append(
            QuarantinedSourceV3(
                source=state.rows[source_id],
                source_record_sha256=hash_canonical(state.rows[source_id].model_dump(mode="json")),
                v2_view=(
                    "core"
                    if source_id in old_core
                    else "tail"
                    if source_id in old_tail
                    else "quarantine"
                ),
                terminal_basis=basis,
                evidence_sha256=evidence,
                human_verdict=verdict,
            )
        )
    if quarantine_rows != tuple(expected_quarantine):
        raise SourceBundleV3Error("v3 quarantine keyed view does not replay")
    mechanical = tuple(
        MechanicalSourceEvidenceV3.model_validate(value)
        for value in _jsonl_objects(bundle_dir / "source_mechanical_evidence.jsonl")
    )
    mechanical_ids = tuple(row.source_id for row in mechanical)
    if len(set(mechanical_ids)) != len(mechanical_ids):
        raise SourceBundleV3Error("v3 mechanical source evidence contains duplicate rows")
    expected_mechanical = _expected_mechanical_evidence(state, plan)
    if mechanical != expected_mechanical or len(mechanical) != len(state.rows):
        raise SourceBundleV3Error(
            "v3 mechanical source evidence exact ordered rows/counts/hashes do not replay"
        )
    if (bundle_dir / "source_mechanical_evidence.jsonl").read_bytes() != _canonical_model_jsonl(
        expected_mechanical
    ):
        raise SourceBundleV3Error("v3 mechanical source evidence is not exact canonical JSONL")
    manifest = _object(bundle_dir / "source_manifest.json")
    prompt_payload = cast(dict[str, Any], json.loads(expected_prompt))
    expected_verdict_counts = dict(sorted(Counter(row.verdict for row in state.reviews).items()))
    expected_human = cast(Mapping[str, Any], config["human_review"])
    contained_attestation = ExternalHumanAttestationV3.model_validate(
        _object(bundle_dir / "external_human_attestation.json")
    )
    if (
        manifest.get("schema_version") != "sft2b_diverse_full_source_manifest_v3"
        or manifest.get("source_config_sha256") != hash_file(config_path)
        or manifest.get("builder_implementation_sha256") != hash_file(Path(__file__))
        or manifest.get("v2_evidence") != config["v2_evidence"]
        or manifest.get("frozen_v2_evidence") != _frozen_v2_manifest_evidence(config, v2_bundle_dir)
        or manifest.get("source_count") != len(plan.ordered_active_ids)
        or manifest.get("core_count") != len(plan.core_ids)
        or manifest.get("tail_count") != len(plan.tail_ids)
        or manifest.get("quarantine_count") != len(plan.quarantine_ids)
        or manifest.get("source_universe_count") != len(state.rows)
        or manifest.get("selection_rule") != CORE_SELECTION_RULE
        or manifest.get("meta_instruction")
        != {
            "baseline_rows": BASELINE_META_ROWS,
            "active_rows": ACTIVE_META_ROWS,
            "additive_rows": ACTIVE_META_ROWS - BASELINE_META_ROWS,
        }
        or manifest.get("human_review")
        != {
            "review_count": len(state.reviews),
            "reviewer_identities": expected_human["allowed_reviewer_identities"],
            "verdict_counts": expected_verdict_counts,
            "external_attestation_sha256": expected_human["external_human_attestation_sha256"],
            "allowed_attestor_identities": expected_human["allowed_attestor_identities"],
            "attestor_identity": contained_attestation.attestor_identity,
            "attestation_scope": contained_attestation.attestation_scope,
        }
        or manifest.get("prompt_tokens")
        != {
            "maximum_prompt_tokens": prompt_payload["maximum_prompt_tokens"],
            "required_max_model_len": prompt_payload["required_max_model_len"],
        }
        or manifest.get("conservation", {}).get("action_counts") != actions
        or manifest.get("conservation", {}).get("reason_counts") != reasons
        or manifest.get("conservation", {}).get("additions") != actions["added"]
        or manifest.get("conservation", {}).get("removals") != actions["removed"]
        or manifest.get("conservation", {}).get("dedup_displacement_additions")
        != reasons["dedup_displacement_addition"]
        or manifest.get("conservation", {}).get("dedup_displacement_movements")
        != reasons["dedup_displacement_movement"]
    ):
        raise SourceBundleV3Error("v3 source manifest counts/conservation do not replay")
    manifest_files = cast(Mapping[str, Any], manifest.get("data_files", {}))
    expected_manifest_files = OUTPUT_NAMES - {"SHA256SUMS", "source_manifest.json"}
    if set(manifest_files) != expected_manifest_files:
        raise SourceBundleV3Error("v3 manifest data-file set drifted")
    for name in expected_manifest_files:
        if cast(Mapping[str, Any], manifest_files[name]).get("sha256") != hash_file(
            bundle_dir / name
        ):
            raise SourceBundleV3Error(f"v3 manifest file hash drifted: {name}")


def build_release(
    repo_root: Path,
    *,
    config_path: Path,
    v2_bundle_dir: Path,
    review_packet_dir: Path,
    output_dir: Path,
) -> None:
    """Build atomically only after authentic-review preflight; never upload automatically."""

    preflight_release(
        repo_root,
        config_path=config_path,
        v2_bundle_dir=v2_bundle_dir,
        review_packet_dir=review_packet_dir,
        output_dir=output_dir,
    )
    config = _object(config_path)
    state = _load_production_state(repo_root, config=config, v2_bundle_dir=v2_bundle_dir)
    plan = _plan_from_state(
        state,
        target_core_count=int(config["matched_view_rows"]),
        config=config,
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    # mkdtemp created the directory; the writer requires absent destination.
    temporary.rmdir()
    try:
        _write_release_files(
            repo_root,
            config_path=config_path,
            config=config,
            v2_bundle_dir=v2_bundle_dir,
            review_packet_dir=review_packet_dir,
            state=state,
            plan=plan,
            output_dir=temporary,
        )
        verify_release(
            repo_root,
            config_path=config_path,
            v2_bundle_dir=v2_bundle_dir,
            review_packet_dir=review_packet_dir,
            bundle_dir=temporary,
        )
        with tempfile.TemporaryDirectory(prefix="leanfaith-sft2b-v3-fresh-") as fresh_root:
            fresh = Path(fresh_root) / output_dir.name
            shutil.copytree(temporary, fresh)
            verify_release(
                repo_root,
                config_path=config_path,
                v2_bundle_dir=v2_bundle_dir,
                review_packet_dir=review_packet_dir,
                bundle_dir=fresh,
            )
        os.replace(temporary, output_dir)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "build", "verify"))
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--v2-bundle", type=Path, required=True)
    parser.add_argument("--review-packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "preflight":
        result = preflight_release(
            args.repo_root,
            config_path=args.config,
            v2_bundle_dir=args.v2_bundle,
            review_packet_dir=args.review_packet,
            output_dir=args.output,
        )
        print(json.dumps(asdict(result), sort_keys=True))
    elif args.command == "build":
        build_release(
            args.repo_root,
            config_path=args.config,
            v2_bundle_dir=args.v2_bundle,
            review_packet_dir=args.review_packet,
            output_dir=args.output,
        )
    else:
        verify_release(
            args.repo_root,
            config_path=args.config,
            v2_bundle_dir=args.v2_bundle,
            review_packet_dir=args.review_packet,
            bundle_dir=args.output,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
