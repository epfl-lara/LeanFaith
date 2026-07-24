"""Production-only Gate-5G lineage sealing for a post-exhaustion frame.

This module is deliberately versioned apart from the frozen Gate-5G v1
finalizer.  It derives, rather than accepts, tranche denominators from a
strictly replayed extended frame and writes the replay certificates and mixed
original/extension lineage that the existing Gate verifier consumes.

The builder performs no model, Lean, provider, annotation, or gate execution.
It can only seal an already complete, production (non-test) frame lineage.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation import gate5g as gate5g_v1
from leanfaith.generation import post_exhaustion_collection_v6 as collection_v6
from leanfaith.generation import post_exhaustion_extension as extension_v1
from leanfaith.generation import post_exhaustion_frame_v1 as frame_v1
from leanfaith.generation import post_exhaustion_postprocess_v7 as postprocess_v7
from leanfaith.generation import research_collection as collection_v1
from leanfaith.generation import research_collection_v2 as collection_v2
from leanfaith.generation import research_collection_v3 as collection_v3
from leanfaith.generation import research_collection_v4 as collection_v4
from leanfaith.generation import research_collection_v5 as collection_v5
from leanfaith.generation import research_postprocess_v3 as postprocess_v3
from leanfaith.generation import research_postprocess_v4 as postprocess_v4
from leanfaith.generation import research_postprocess_v5 as postprocess_v5
from leanfaith.generation import research_postprocess_v6 as postprocess_v6
from leanfaith.generation import tranche_expansion as tranche_v1
from leanfaith.schemas.gate5g import (
    Gate5GArtifactBinding,
    Gate5GFamilyRevisionBinding,
    Gate5GLineageManifestV1,
    Gate5GObservationBinding,
    Gate5GReplayCertificateV1,
    Gate5GTrancheBindingV1,
)

_HEX64 = r"^[0-9a-f]{64}$"
_SPEC_ID = r"^lf021_post_exhaustion_gate5g_lineage_spec_v1:[0-9a-f]{64}$"
_SPEC_OUTPUT_ROOT = Path("reports/generation/lf021_post_exhaustion_gate5g_lineage_specs_v1")
_OUTPUT_ROOT = Path("reports/generation/lf021_post_exhaustion_gate5g_lineage_v1")
_CANONICAL_FRAME_POLICY = Path("configs/generation/lf021_post_exhaustion_frame_v1.yaml")
_CANONICAL_BUILDER = Path("src/leanfaith/generation/post_exhaustion_gate5g_lineage_v1.py")
_CANONICAL_POOL_OVERLAPS = (
    (
        "algebra_gate3_docstrings_v1",
        Path("reports/generation/overlap_v2/gate3_docstrings_operational_v1/bundle_manifest.json"),
    ),
    (
        "cross_domain_docstrings_v1",
        Path(
            "reports/generation/overlap_v3/"
            "cross_domain_docstrings_operational_v1/bundle_manifest.json"
        ),
    ),
)
_EXPECTED_FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)
_COLLECTION_PREFIX_BY_SCHEMA = {
    1: "research_collection_manifest",
    2: "research_collection_manifest_v2",
    3: "research_collection_manifest_v3",
    4: "research_collection_manifest_v4",
    5: "research_collection_manifest_v5",
    6: "lf021_post_exhaustion_collection_manifest_v6",
}
_POSTPROCESS_PREFIX_BY_SCHEMA = {
    3: "research_postprocess_v3_manifest",
    4: "research_postprocess_v4_manifest",
    5: "research_postprocess_v5_manifest",
    6: "research_postprocess_v6_manifest",
    7: "research_postprocess_v7_manifest",
}
_COLLECTION_MODEL_BY_SCHEMA: dict[int, type[BaseModel]] = {
    1: collection_v1.ResearchCollectionManifest,
    2: collection_v2.ResearchCollectionManifestV2,
    3: collection_v3.ResearchCollectionManifestV3,
    4: collection_v4.ResearchCollectionManifestV4,
    5: collection_v5.ResearchCollectionManifestV5,
    6: collection_v6.PostExhaustionCollectionManifestV6,
}
_POSTPROCESS_MODEL_BY_SCHEMA: dict[int, type[BaseModel]] = {
    3: postprocess_v3.ResearchPostprocessV3Manifest,
    4: postprocess_v4.ResearchPostprocessV4Manifest,
    5: postprocess_v5.ResearchPostprocessV5Manifest,
    6: postprocess_v6.ResearchPostprocessV6Manifest,
    7: postprocess_v7.PostExhaustionPostprocessManifestV7,
}
_PATH_HASH_MAP_KEYS = frozenset(
    {
        "collection_family_session_artifacts",
        "collection_terminal_artifacts",
        "family_report_artifacts",
        "family_session_artifact_hashes",
        "output_artifact_hashes",
        "terminal_artifact_hashes",
        "terminal_artifacts",
    }
)


class PostExhaustionGate5GLineageError(RuntimeError):
    """Exact lineage sealing failed before an authoritative lineage was written."""


class PoolOverlapBindingV1(StrictModel):
    """One exact overlap bundle selected by the operational pool ID."""

    pool_id: str = Field(min_length=1)
    overlap_manifest: Gate5GArtifactBinding


class PostExhaustionGate5GLineageSpecV1(StrictModel):
    """Immutable production request for a mixed Gate-5G lineage."""

    schema_version: Literal[1] = 1
    spec_id: str = Field(pattern=_SPEC_ID)
    report_kind: Literal["lf021_post_exhaustion_gate5g_lineage_spec_v1"]
    builder_implementation: Gate5GArtifactBinding
    frame_policy: Gate5GArtifactBinding
    frame_decision: Gate5GArtifactBinding
    pool_overlaps: tuple[PoolOverlapBindingV1, ...] = Field(min_length=1)
    required_original_observation_count: Literal[12] = 12
    minimum_extension_observation_count: Literal[1] = 1
    maximum_extension_observation_count: Literal[4] = 4
    required_family_ids: tuple[str, str, str] = _EXPECTED_FAMILIES
    production_only: Literal[True] = True
    semantic_labels_inspected: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    gate_5g_credit_claimed: Literal[False] = False
    gate_5_closed: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        pool_ids = tuple(item.pool_id for item in self.pool_overlaps)
        if pool_ids != tuple(sorted(set(pool_ids))):
            raise ValueError("lineage-spec pool overlaps must be sorted and unique")
        if self.required_family_ids != _EXPECTED_FAMILIES:
            raise ValueError("lineage-spec family inventory differs")
        expected = "lf021_post_exhaustion_gate5g_lineage_spec_v1:" + hash_canonical(
            {
                "schema": "lf021_post_exhaustion_gate5g_lineage_spec_v1",
                **self.model_dump(mode="json", exclude={"spec_id"}),
            }
        )
        if self.spec_id != expected:
            raise ValueError("lineage-spec ID differs from content")
        return self


class ExactArtifactTreeEntryV1(StrictModel):
    """One no-follow file observation in an exact artifact closure."""

    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class PostExhaustionGate5GLineageRunV1:
    """Content-addressed artifacts written by one successful seal."""

    spec: PostExhaustionGate5GLineageSpecV1
    lineage: Gate5GLineageManifestV1
    lineage_path: Path
    collection_replay_paths: tuple[Path, ...]
    postprocess_replay_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class PostExhaustionGate5GLineageSpecRunV1:
    """One securely published canonical production lineage request."""

    spec: PostExhaustionGate5GLineageSpecV1
    spec_path: Path
    spec_binding: Gate5GArtifactBinding


def _json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PostExhaustionGate5GLineageError(
                    f"{label} contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PostExhaustionGate5GLineageError(
                    f"{label} contains non-finite JSON value {token!r}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PostExhaustionGate5GLineageError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PostExhaustionGate5GLineageError(f"{label} JSON root is not an object")
    return value


def _strict_json(payload: bytes, *, label: str) -> dict[str, Any]:
    value = _json_object(payload, label=label)
    if canonical_json_bytes(value) != payload:
        raise PostExhaustionGate5GLineageError(f"{label} is not canonical JSON")
    return value


def _legacy_json_with_terminal_lf(payload: bytes, *, label: str) -> dict[str, Any]:
    """Load the exact historical writer encoding: canonical JSON plus one LF.

    Research collection, postprocess, and family-session artifacts predate the
    canonical-byte publication rule used by this lineage builder.  Their
    versioned writers emitted exactly one terminal LF.  Accepting arbitrary
    JSON whitespace here would make a re-encoded artifact look historical, so
    compatibility is deliberately limited to those exact bytes.
    """

    value = _json_object(payload, label=label)
    if canonical_json_bytes(value) + b"\n" != payload:
        raise PostExhaustionGate5GLineageError(
            f"{label} is not exact legacy canonical-JSON-plus-LF"
        )
    return value


def _validate_registered_manifest_schema(
    raw: dict[str, Any],
    *,
    kind: Literal["collection", "postprocess"],
    label: str,
) -> None:
    """Validate a legacy manifest against its exact registered version model."""

    schema_version = raw.get("schema_version")
    if type(schema_version) is not int:
        raise PostExhaustionGate5GLineageError(f"{label} schema version is invalid")
    models = _COLLECTION_MODEL_BY_SCHEMA if kind == "collection" else _POSTPROCESS_MODEL_BY_SCHEMA
    prefixes = (
        _COLLECTION_PREFIX_BY_SCHEMA if kind == "collection" else _POSTPROCESS_PREFIX_BY_SCHEMA
    )
    model = models.get(schema_version)
    expected_prefix = prefixes.get(schema_version)
    manifest_id = raw.get("manifest_id")
    if model is None or expected_prefix is None:
        raise PostExhaustionGate5GLineageError(f"{label} schema version is not registered")
    if not isinstance(manifest_id, str) or manifest_id.split(":", 1)[0] != expected_prefix:
        raise PostExhaustionGate5GLineageError(
            f"{label} ID prefix differs from its registered schema"
        )
    try:
        model.model_validate(raw)
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError(
            f"{label} does not validate under its registered schema"
        ) from exc


def _load_bound_registered_manifest_json(
    *,
    repo_root: Path,
    binding: Gate5GArtifactBinding,
    label: str,
    kind: Literal["collection", "postprocess"],
) -> dict[str, Any]:
    """Read one hash-bound historical manifest through its versioned loader."""

    _, _, payload = _read_repo_file_no_follow(
        repo_root=repo_root,
        artifact=binding.artifact,
        expected_sha256=binding.sha256,
        label=label,
    )
    raw = _legacy_json_with_terminal_lf(payload, label=label)
    _validate_registered_manifest_schema(raw, kind=kind, label=label)
    return raw


def _project_registered_postprocess_manifest(
    raw: dict[str, Any],
    *,
    expected_tranche_id: str,
    label: str,
) -> gate5g_v1._PostprocessManifestProjection:
    """Project versioned manifests without weakening their historical schema."""

    schema_version = raw.get("schema_version")
    recorded_tranche_id = raw.get("tranche_id")
    if schema_version == 3:
        if recorded_tranche_id is not None:
            raise PostExhaustionGate5GLineageError(f"{label} v3 unexpectedly records a tranche ID")
    elif recorded_tranche_id != expected_tranche_id:
        raise PostExhaustionGate5GLineageError(f"{label} tranche ID differs")

    input_binding = raw.get("input_binding")
    if not isinstance(input_binding, dict):
        raise PostExhaustionGate5GLineageError(f"{label} input binding is invalid")
    collection_manifest = input_binding.get("collection_manifest")
    if not isinstance(collection_manifest, dict):
        raise PostExhaustionGate5GLineageError(f"{label} collection binding is invalid")
    if collection_manifest.get("location_kind") != "repo_relative":
        raise PostExhaustionGate5GLineageError(
            f"{label} collection binding location is not registered"
        )
    normalized_input = {
        **input_binding,
        "collection_manifest": {
            "artifact": collection_manifest.get("artifact"),
            "sha256": collection_manifest.get("sha256"),
        },
    }
    try:
        return gate5g_v1._PostprocessManifestProjection.model_validate(
            {
                **raw,
                "tranche_id": expected_tranche_id,
                "input_binding": normalized_input,
            }
        )
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError(f"{label} Gate-5G projection is invalid") from exc


def _project_registered_collection_manifest(
    raw: dict[str, Any],
    *,
    expected_tranche_id: str,
    label: str,
) -> gate5g_v1._CollectionManifestProjection:
    """Project a versioned collection manifest into the Gate-5G denominator."""

    schema_version = raw.get("schema_version")
    recorded_tranche_id = raw.get("tranche_id")
    if schema_version in {1, 2}:
        if recorded_tranche_id is not None:
            raise PostExhaustionGate5GLineageError(
                f"{label} legacy schema unexpectedly records a tranche ID"
            )
    elif recorded_tranche_id != expected_tranche_id:
        raise PostExhaustionGate5GLineageError(f"{label} tranche ID differs")
    normalized = {**raw, "tranche_id": expected_tranche_id}
    if schema_version == 1 and "family_count" not in normalized:
        session_artifacts = raw.get("family_session_artifact_hashes")
        if not isinstance(session_artifacts, dict) or len(session_artifacts) % 2:
            raise PostExhaustionGate5GLineageError(f"{label} family-session denominator is invalid")
        normalized["family_count"] = len(session_artifacts) // 2
    try:
        return gate5g_v1._CollectionManifestProjection.model_validate(normalized)
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError(f"{label} Gate-5G projection is invalid") from exc


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _read_repo_file_no_follow(
    *,
    repo_root: Path,
    artifact: str | Path,
    expected_sha256: str | None,
    label: str,
) -> tuple[Path, str, bytes]:
    """Read one repository file through an all-component no-follow walk."""

    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise PostExhaustionGate5GLineageError(f"repository root is unavailable: {exc}") from exc
    raw = Path(artifact)
    lexical = Path(os.path.abspath(os.fspath(raw if raw.is_absolute() else root / raw)))
    if not lexical.is_relative_to(root) or lexical == root:
        raise PostExhaustionGate5GLineageError(f"{label} escapes the repository")
    opened: list[int] = []
    current_fd = os.open(root, _directory_flags())
    opened.append(current_fd)
    try:
        relative = lexical.relative_to(root)
        for component in relative.parts[:-1]:
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise PostExhaustionGate5GLineageError(
                    f"{label} traverses a missing or symlinked component"
                ) from exc
            opened.append(next_fd)
            current_fd = next_fd
        try:
            descriptor = os.open(
                relative.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current_fd,
            )
        except OSError as exc:
            raise PostExhaustionGate5GLineageError(
                f"{label} is missing, symlinked, or unreadable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise PostExhaustionGate5GLineageError(f"{label} is not a regular file")
            payload = gate5g_v1._read_publication_fd(descriptor)
            after = os.fstat(descriptor)
            identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            if tuple(getattr(before, key) for key in identity) != tuple(
                getattr(after, key) for key in identity
            ):
                raise PostExhaustionGate5GLineageError(f"{label} changed while read")
        finally:
            os.close(descriptor)
        try:
            gate5g_v1._verify_publication_path(
                repo_root=root,
                path=lexical,
                expected=after,
                label=label,
            )
        except gate5g_v1.Gate5GFinalizationError as exc:
            raise PostExhaustionGate5GLineageError(str(exc)) from exc
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    digest = sha256_hex(payload)
    if expected_sha256 is not None and digest != expected_sha256:
        raise PostExhaustionGate5GLineageError(f"{label} hash differs")
    return lexical, str(lexical.relative_to(root)), payload


def _stat_absolute_no_follow(*, lexical: Path, label: str) -> os.stat_result:
    """Reopen an absolute path from `/` without following any component."""

    opened: list[int] = []
    current_fd = os.open("/", _directory_flags())
    opened.append(current_fd)
    try:
        relative = lexical.relative_to("/")
        for component in relative.parts[:-1]:
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise PostExhaustionGate5GLineageError(
                    f"{label} traverses a missing or symlinked component"
                ) from exc
            opened.append(next_fd)
            current_fd = next_fd
        try:
            descriptor = os.open(
                relative.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current_fd,
            )
        except OSError as exc:
            raise PostExhaustionGate5GLineageError(
                f"{label} is missing, symlinked, or unreadable"
            ) from exc
        try:
            observed = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise PostExhaustionGate5GLineageError(f"{label} is not a regular file")
        return observed
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _read_absolute_content_addressed_no_follow(
    *,
    artifact: str,
    expected_sha256: str,
    label: str,
) -> tuple[Path, str, bytes]:
    """Read one explicitly absolute, hash-bound artifact without following links."""

    raw = Path(artifact)
    if not raw.is_absolute() or ".." in raw.parts or raw == Path("/"):
        raise PostExhaustionGate5GLineageError(f"{label} is not an absolute content-addressed path")
    lexical = Path(os.path.abspath(os.fspath(raw)))
    opened: list[int] = []
    current_fd = os.open("/", _directory_flags())
    opened.append(current_fd)
    try:
        relative = lexical.relative_to("/")
        for component in relative.parts[:-1]:
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise PostExhaustionGate5GLineageError(
                    f"{label} traverses a missing or symlinked component"
                ) from exc
            opened.append(next_fd)
            current_fd = next_fd
        try:
            descriptor = os.open(
                relative.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current_fd,
            )
        except OSError as exc:
            raise PostExhaustionGate5GLineageError(
                f"{label} is missing, symlinked, or unreadable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise PostExhaustionGate5GLineageError(f"{label} is not a regular file")
            payload = gate5g_v1._read_publication_fd(descriptor)
            after = os.fstat(descriptor)
            identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            if tuple(getattr(before, key) for key in identity) != tuple(
                getattr(after, key) for key in identity
            ):
                raise PostExhaustionGate5GLineageError(f"{label} changed while read")
        finally:
            os.close(descriptor)
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    current = _stat_absolute_no_follow(lexical=lexical, label=label)
    identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if tuple(getattr(current, key) for key in identity) != tuple(
        getattr(after, key) for key in identity
    ):
        raise PostExhaustionGate5GLineageError(f"{label} changed after read")
    if sha256_hex(payload) != expected_sha256:
        raise PostExhaustionGate5GLineageError(f"{label} hash differs")
    return lexical, str(lexical), payload


def _binding_from_path(
    *,
    repo_root: Path,
    path: Path,
    label: str,
) -> Gate5GArtifactBinding:
    _, artifact, payload = _read_repo_file_no_follow(
        repo_root=repo_root,
        artifact=path,
        expected_sha256=None,
        label=label,
    )
    return Gate5GArtifactBinding(artifact=artifact, sha256=sha256_hex(payload))


def load_post_exhaustion_gate5g_lineage_spec_v1(
    *,
    repo_root: Path,
    spec_path: Path,
) -> PostExhaustionGate5GLineageSpecV1:
    _, _, payload = _read_repo_file_no_follow(
        repo_root=repo_root,
        artifact=spec_path,
        expected_sha256=None,
        label="post-exhaustion Gate-5G lineage spec",
    )
    try:
        return PostExhaustionGate5GLineageSpecV1.model_validate(
            _strict_json(payload, label="post-exhaustion Gate-5G lineage spec")
        )
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError("lineage spec is invalid") from exc


def publish_post_exhaustion_gate5g_lineage_spec_v1(
    *,
    repo_root: Path,
    frame_decision_path: Path,
) -> PostExhaustionGate5GLineageSpecRunV1:
    """Replay a production frame and publish its sole canonical lineage spec.

    The frame policy, builder implementation, pool-overlap inventory, and
    publication namespace are intentionally not caller-configurable.  This
    keeps the operator command from turning into a generic artifact signer.
    """

    root = repo_root.resolve()
    frame_policy_path = root / _CANONICAL_FRAME_POLICY
    builder_path = root / _CANONICAL_BUILDER
    _, _, repository_builder = _read_repo_file_no_follow(
        repo_root=root,
        artifact=builder_path,
        expected_sha256=None,
        label="repository post-exhaustion Gate-5G lineage builder",
    )
    try:
        executing_builder = Path(__file__).resolve().read_bytes()
    except OSError as exc:
        raise PostExhaustionGate5GLineageError(
            "executing post-exhaustion Gate-5G lineage builder is unreadable"
        ) from exc
    if repository_builder != executing_builder:
        raise PostExhaustionGate5GLineageError(
            "repository lineage builder differs from executing code"
        )
    try:
        verified = frame_v1.verify_extended_frame_freeze_v1(
            repo_root=root,
            policy_path=frame_policy_path,
            decision_path=frame_decision_path,
        )
    except (frame_v1.PostExhaustionFrameError, OSError, ValueError) as exc:
        raise PostExhaustionGate5GLineageError(
            "extended production frame does not strictly replay"
        ) from exc
    decision = verified.decision
    if verified.decision_path.resolve() != frame_decision_path.resolve():
        raise PostExhaustionGate5GLineageError(
            "strict frame verifier returned a different decision"
        )
    if (
        decision.test_replay_only
        or decision.frame.test_replay_only
        or decision.source_stop_action != "preferred_eligible_stop"
        or decision.action != "freeze_preferred_frame"
        or decision.next_tranche is not None
        or decision.coverage_deficits
        or decision.original_observation_count != 12
        or not 1 <= decision.extension_observation_count <= 4
        or len(decision.observations)
        != decision.original_observation_count + decision.extension_observation_count
        or decision.frame.item_count != 240
    ):
        raise PostExhaustionGate5GLineageError(
            "extended decision is not a production preferred frame"
        )

    overlaps: list[PoolOverlapBindingV1] = []
    for pool_id, relative_path in _CANONICAL_POOL_OVERLAPS:
        overlap_binding = _binding_from_path(
            repo_root=root,
            path=root / relative_path,
            label=f"{pool_id} overlap manifest",
        )
        _, _, overlap_payload = _read_repo_file_no_follow(
            repo_root=root,
            artifact=overlap_binding.artifact,
            expected_sha256=overlap_binding.sha256,
            label=f"{pool_id} overlap manifest",
        )
        try:
            overlap_raw = _json_object(
                overlap_payload,
                label=f"{pool_id} overlap manifest",
            )
            overlap = gate5g_v1._OverlapManifestProjection.model_validate(overlap_raw)
            gate5g_v1._require_label_blind(
                overlap_raw,
                label=f"{pool_id} overlap manifest",
            )
        except (ValueError, gate5g_v1.Gate5GFinalizationError) as exc:
            raise PostExhaustionGate5GLineageError(
                f"{pool_id} overlap manifest is invalid"
            ) from exc
        if (
            overlap.family_count != len(_EXPECTED_FAMILIES)
            or tuple(sorted(overlap.family_artifacts)) != _EXPECTED_FAMILIES
        ):
            raise PostExhaustionGate5GLineageError(f"{pool_id} overlap family inventory differs")
        _snapshot_artifact_tree(
            repo_root=root,
            root_manifest=overlap_binding,
            label=f"{pool_id} overlap",
        )
        overlaps.append(
            PoolOverlapBindingV1(
                pool_id=pool_id,
                overlap_manifest=overlap_binding,
            )
        )

    payload: dict[str, Any] = {
        "schema_version": 1,
        "report_kind": "lf021_post_exhaustion_gate5g_lineage_spec_v1",
        "builder_implementation": _binding_from_path(
            repo_root=root,
            path=builder_path,
            label="post-exhaustion Gate-5G lineage builder",
        ).model_dump(mode="json"),
        "frame_policy": _binding_from_path(
            repo_root=root,
            path=frame_policy_path,
            label="post-exhaustion frame policy",
        ).model_dump(mode="json"),
        "frame_decision": _binding_from_path(
            repo_root=root,
            path=frame_decision_path,
            label="post-exhaustion frame decision",
        ).model_dump(mode="json"),
        "pool_overlaps": tuple(item.model_dump(mode="json") for item in overlaps),
        "required_original_observation_count": 12,
        "minimum_extension_observation_count": 1,
        "maximum_extension_observation_count": 4,
        "required_family_ids": _EXPECTED_FAMILIES,
        "production_only": True,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    spec_id = "lf021_post_exhaustion_gate5g_lineage_spec_v1:" + hash_canonical(
        {
            "schema": "lf021_post_exhaustion_gate5g_lineage_spec_v1",
            **payload,
        }
    )
    try:
        spec = PostExhaustionGate5GLineageSpecV1.model_validate({"spec_id": spec_id, **payload})
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError("generated lineage spec is invalid") from exc
    spec_bytes = canonical_json_bytes(spec.model_dump(mode="json"))
    spec_path = root / _SPEC_OUTPUT_ROOT / f"{spec_id.rsplit(':', 1)[-1]}.json"
    try:
        gate5g_v1._write_immutable(
            spec_path,
            spec_bytes,
            repo_root=root,
            label="post-exhaustion Gate-5G lineage spec",
        )
    except gate5g_v1.Gate5GFinalizationError as exc:
        raise PostExhaustionGate5GLineageError(str(exc)) from exc
    reloaded = load_post_exhaustion_gate5g_lineage_spec_v1(
        repo_root=root,
        spec_path=spec_path,
    )
    _, _, persisted = _read_repo_file_no_follow(
        repo_root=root,
        artifact=spec_path,
        expected_sha256=sha256_hex(spec_bytes),
        label="persisted post-exhaustion Gate-5G lineage spec",
    )
    if reloaded != spec or persisted != spec_bytes:
        raise PostExhaustionGate5GLineageError("persisted lineage spec differs")
    return PostExhaustionGate5GLineageSpecRunV1(
        spec=spec,
        spec_path=spec_path,
        spec_binding=Gate5GArtifactBinding(
            artifact=str(spec_path.relative_to(root)),
            sha256=sha256_hex(spec_bytes),
        ),
    )


def _looks_like_artifact_path(value: str) -> bool:
    suffix = Path(value).suffix.lower()
    return "/" in value or suffix in {
        ".bin",
        ".json",
        ".jsonl",
        ".lean",
        ".md",
        ".txt",
        ".yaml",
        ".yml",
    }


def _extract_bindings(value: Any) -> tuple[Gate5GArtifactBinding, ...]:
    found: dict[str, str] = {}

    def add(artifact: str, digest: str) -> None:
        previous = found.get(artifact)
        if previous is not None and previous != digest:
            raise PostExhaustionGate5GLineageError(f"artifact {artifact!r} is bound to two hashes")
        found[artifact] = digest

    def walk(node: Any, parent_key: str | None = None) -> None:
        if isinstance(node, dict):
            artifact = node.get("artifact")
            digest = node.get("sha256")
            if (
                isinstance(artifact, str)
                and isinstance(digest, str)
                and re.fullmatch(_HEX64, digest) is not None
            ):
                location_kind = node.get("location_kind")
                artifact_path = Path(artifact)
                if location_kind is None:
                    if artifact_path.is_absolute():
                        raise PostExhaustionGate5GLineageError(
                            "absolute artifact binding lacks an explicit location kind"
                        )
                elif location_kind == "repo_relative":
                    if artifact_path.is_absolute() or ".." in artifact_path.parts:
                        raise PostExhaustionGate5GLineageError(
                            "repo-relative artifact binding is not repository relative"
                        )
                elif location_kind == "absolute_content_addressed":
                    if not artifact_path.is_absolute() or ".." in artifact_path.parts:
                        raise PostExhaustionGate5GLineageError(
                            "absolute content-addressed artifact binding is invalid"
                        )
                else:
                    raise PostExhaustionGate5GLineageError(
                        "artifact binding has an unregistered location kind"
                    )
                add(artifact, digest)
            is_path_hash_map = all(
                isinstance(key, str)
                and isinstance(item, str)
                and re.fullmatch(_HEX64, item) is not None
                for key, item in node.items()
            ) and (
                parent_key in _PATH_HASH_MAP_KEYS
                or (bool(node) and all(_looks_like_artifact_path(key) for key in node))
            )
            if is_path_hash_map:
                for key, item in node.items():
                    path_key = Path(key)
                    if not key or path_key.is_absolute() or ".." in path_key.parts:
                        raise PostExhaustionGate5GLineageError(
                            "path-hash map contains a non-repository artifact path"
                        )
                    add(key, item)
            for key, item in node.items():
                walk(item, str(key))
        elif isinstance(node, list):
            for item in node:
                walk(item, parent_key)

    walk(value)
    return tuple(
        Gate5GArtifactBinding(artifact=artifact, sha256=digest)
        for artifact, digest in sorted(found.items())
    )


def _snapshot_artifact_tree(
    *,
    repo_root: Path,
    root_manifest: Gate5GArtifactBinding,
    label: str,
) -> tuple[ExactArtifactTreeEntryV1, ...]:
    if Path(root_manifest.artifact).is_absolute():
        raise PostExhaustionGate5GLineageError(f"{label} root manifest must be repository relative")
    pending = [root_manifest]
    expected: dict[str, str] = {}
    entries: dict[str, ExactArtifactTreeEntryV1] = {}
    while pending:
        binding = pending.pop()
        prior = expected.get(binding.artifact)
        if prior is not None:
            if prior != binding.sha256:
                raise PostExhaustionGate5GLineageError(f"{label} artifact has inconsistent hashes")
            continue
        expected[binding.artifact] = binding.sha256
        if Path(binding.artifact).is_absolute():
            path, artifact, payload = _read_absolute_content_addressed_no_follow(
                artifact=binding.artifact,
                expected_sha256=binding.sha256,
                label=f"{label} artifact",
            )
        else:
            path, artifact, payload = _read_repo_file_no_follow(
                repo_root=repo_root,
                artifact=binding.artifact,
                expected_sha256=binding.sha256,
                label=f"{label} artifact",
            )
        entries[artifact] = ExactArtifactTreeEntryV1(
            artifact=artifact,
            sha256=binding.sha256,
            byte_count=len(payload),
        )
        if path.suffix == ".json":
            raw = _json_object(payload, label=f"{label} artifact {artifact}")
            try:
                gate5g_v1._require_label_blind(raw, label=f"{label} artifact {artifact}")
            except gate5g_v1.Gate5GFinalizationError as exc:
                raise PostExhaustionGate5GLineageError(str(exc)) from exc
            pending.extend(_extract_bindings(raw))
    return tuple(entries[key] for key in sorted(entries))


def _tree_sha256(
    *,
    tranche_id: str,
    kind: Literal["collection", "postprocess"],
    expected_record_count: int,
    root_manifest: Gate5GArtifactBinding,
    entries: tuple[ExactArtifactTreeEntryV1, ...],
) -> str:
    return hash_canonical(
        {
            "schema": "lf021_gate5g_exact_artifact_tree_v1",
            "tranche_id": tranche_id,
            "kind": kind,
            "expected_record_count": expected_record_count,
            "root_manifest": root_manifest.model_dump(mode="json"),
            "artifacts": tuple(item.model_dump(mode="json") for item in entries),
        }
    )


def seal_gate5g_replay_certificate_v1(
    *,
    repo_root: Path,
    manifest: Gate5GArtifactBinding,
    tranche_id: str,
    kind: Literal["collection", "postprocess"],
    expected_record_count: int,
    output_root: Path,
) -> tuple[Gate5GReplayCertificateV1, Path, Gate5GArtifactBinding]:
    """Seal two independent no-follow scans of one exact artifact closure."""

    if expected_record_count < 1:
        raise PostExhaustionGate5GLineageError("replay denominator must be positive")
    first = _snapshot_artifact_tree(
        repo_root=repo_root,
        root_manifest=manifest,
        label=f"{tranche_id} {kind} first replay",
    )
    replay = _snapshot_artifact_tree(
        repo_root=repo_root,
        root_manifest=manifest,
        label=f"{tranche_id} {kind} second replay",
    )
    first_sha = _tree_sha256(
        tranche_id=tranche_id,
        kind=kind,
        expected_record_count=expected_record_count,
        root_manifest=manifest,
        entries=first,
    )
    replay_sha = _tree_sha256(
        tranche_id=tranche_id,
        kind=kind,
        expected_record_count=expected_record_count,
        root_manifest=manifest,
        entries=replay,
    )
    if first != replay or first_sha != replay_sha:
        raise PostExhaustionGate5GLineageError(
            f"{tranche_id} {kind} artifact tree changed during replay"
        )
    report_kind: Literal[
        "lf021_collection_replay_certificate_v1",
        "lf021_postprocess_replay_certificate_v1",
    ] = (
        "lf021_collection_replay_certificate_v1"
        if kind == "collection"
        else "lf021_postprocess_replay_certificate_v1"
    )
    certificate = Gate5GReplayCertificateV1(
        report_kind=report_kind,
        tranche_id=tranche_id,
        manifest=manifest,
        replayed=True,
        byte_identical=True,
        first_tree_sha256=first_sha,
        replay_tree_sha256=replay_sha,
        expected_record_count=expected_record_count,
        replay_record_count=expected_record_count,
        semantic_labels_inspected=False,
        semantic_labels_created=False,
        supervision_eligible=False,
        gate_5g_credit_claimed=False,
        gate_5_closed=False,
    )
    payload = canonical_json_bytes(certificate.model_dump(mode="json"))
    digest = sha256_hex(payload)
    path = output_root / "replay" / kind / f"{digest}.json"
    try:
        gate5g_v1._write_immutable(
            path,
            payload,
            repo_root=repo_root,
            label=f"{tranche_id} {kind} replay certificate",
        )
    except gate5g_v1.Gate5GFinalizationError as exc:
        raise PostExhaustionGate5GLineageError(str(exc)) from exc
    binding = _binding_from_path(
        repo_root=repo_root,
        path=path,
        label=f"{tranche_id} {kind} replay certificate",
    )
    if binding.sha256 != digest:
        raise PostExhaustionGate5GLineageError("persisted replay certificate differs")
    return certificate, path, binding


def verify_gate5g_replay_certificate_v1_exact(
    *,
    repo_root: Path,
    certificate_binding: Gate5GArtifactBinding,
    manifest: Gate5GArtifactBinding,
    tranche_id: str,
    kind: Literal["collection", "postprocess"],
    expected_record_count: int,
) -> Gate5GReplayCertificateV1:
    """Independently recompute a V1 replay certificate's exact artifact tree.

    The frozen Gate-5G V1 reader validates only the certificate assertions.
    Extended Gate-5G V2 calls this verifier so a hand-authored equal-hash
    assertion cannot receive credit without the exact, stable artifact tree.
    """

    raw = _load_bound_json(
        repo_root=repo_root,
        binding=certificate_binding,
        label=f"{tranche_id} {kind} replay certificate",
    )
    try:
        certificate = Gate5GReplayCertificateV1.model_validate(raw)
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError(
            f"{tranche_id} {kind} replay certificate is invalid"
        ) from exc
    expected_kind = f"lf021_{kind}_replay_certificate_v1"
    if (
        certificate.report_kind != expected_kind
        or certificate.tranche_id != tranche_id
        or certificate.manifest != manifest
        or certificate.expected_record_count != expected_record_count
        or certificate.replay_record_count != expected_record_count
    ):
        raise PostExhaustionGate5GLineageError(
            f"{tranche_id} {kind} replay certificate binding differs"
        )
    first = _snapshot_artifact_tree(
        repo_root=repo_root,
        root_manifest=manifest,
        label=f"{tranche_id} {kind} verification replay one",
    )
    replay = _snapshot_artifact_tree(
        repo_root=repo_root,
        root_manifest=manifest,
        label=f"{tranche_id} {kind} verification replay two",
    )
    first_sha = _tree_sha256(
        tranche_id=tranche_id,
        kind=kind,
        expected_record_count=expected_record_count,
        root_manifest=manifest,
        entries=first,
    )
    replay_sha = _tree_sha256(
        tranche_id=tranche_id,
        kind=kind,
        expected_record_count=expected_record_count,
        root_manifest=manifest,
        entries=replay,
    )
    if (
        first != replay
        or first_sha != replay_sha
        or certificate.first_tree_sha256 != first_sha
        or certificate.replay_tree_sha256 != replay_sha
    ):
        raise PostExhaustionGate5GLineageError(
            f"{tranche_id} {kind} replay certificate was not independently reproduced"
        )
    return certificate


def _family_revisions(
    *,
    repo_root: Path,
    collection: gate5g_v1._CollectionManifestProjection,
) -> tuple[Gate5GFamilyRevisionBinding, ...]:
    starts: dict[str, tuple[gate5g_v1._FamilySessionStartProjection, Gate5GArtifactBinding]] = {}
    ends: dict[str, tuple[gate5g_v1._FamilySessionEndProjection, Gate5GArtifactBinding]] = {}
    for artifact, digest in collection.family_session_artifact_hashes.items():
        binding = Gate5GArtifactBinding(artifact=artifact, sha256=digest)
        _, _, payload = _read_repo_file_no_follow(
            repo_root=repo_root,
            artifact=artifact,
            expected_sha256=digest,
            label="family session artifact",
        )
        raw = _legacy_json_with_terminal_lf(
            payload,
            label="family session artifact",
        )
        if raw.get("schema_version") != 1:
            raise PostExhaustionGate5GLineageError(
                "family session artifact schema version is not registered"
            )
        try:
            if artifact.endswith("/family_session_start.json"):
                start = gate5g_v1._FamilySessionStartProjection.model_validate(raw)
                if start.family_id in starts:
                    raise PostExhaustionGate5GLineageError("duplicate family-session start")
                starts[start.family_id] = (start, binding)
            elif artifact.endswith("/family_session_end.json"):
                end = gate5g_v1._FamilySessionEndProjection.model_validate(raw)
                if end.family_id in ends:
                    raise PostExhaustionGate5GLineageError("duplicate family-session end")
                ends[end.family_id] = (end, binding)
            else:
                raise PostExhaustionGate5GLineageError("unexpected family-session artifact name")
        except ValueError as exc:
            raise PostExhaustionGate5GLineageError("invalid family-session artifact") from exc
    if tuple(sorted(starts)) != _EXPECTED_FAMILIES or set(ends) != set(starts):
        raise PostExhaustionGate5GLineageError("family-session inventory differs")
    result: list[Gate5GFamilyRevisionBinding] = []
    for family_id in sorted(starts):
        start, start_binding = starts[family_id]
        end, end_binding = ends[family_id]
        if start.family_session_id != end.family_session_id:
            raise PostExhaustionGate5GLineageError("family-session endpoints differ")
        result.append(
            Gate5GFamilyRevisionBinding(
                family_id=family_id,
                model_repo_id=start.model_repo_id,
                model_revision=start.model_revision,
                session_start=start_binding,
                session_end=end_binding,
            )
        )
    return tuple(result)


def _load_bound_json(
    *,
    repo_root: Path,
    binding: Gate5GArtifactBinding,
    label: str,
) -> dict[str, Any]:
    _, _, payload = _read_repo_file_no_follow(
        repo_root=repo_root,
        artifact=binding.artifact,
        expected_sha256=binding.sha256,
        label=label,
    )
    return _strict_json(payload, label=label)


def _verify_extension_v7(
    *,
    repo_root: Path,
    observation: tranche_v1.ObservationBinding,
    expected_order: int,
) -> None:
    raw = _load_bound_registered_manifest_json(
        repo_root=repo_root,
        binding=Gate5GArtifactBinding.model_validate(
            observation.postprocess_manifest.model_dump(mode="json")
        ),
        label="extension postprocess-v7 manifest",
        kind="postprocess",
    )
    try:
        manifest = postprocess_v7.PostExhaustionPostprocessManifestV7.model_validate(raw)
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError(
            "extension observation is not a valid postprocess-v7 manifest"
        ) from exc
    if (
        observation.postprocess_schema_version != 7
        or manifest.manifest_id != observation.manifest_id
        or manifest.tranche_id != observation.tranche_id
        or manifest.tranche_order != expected_order
    ):
        raise PostExhaustionGate5GLineageError(
            "extension observation differs from postprocess-v7 lineage"
        )
    collection_manifest = manifest.input_binding.collection_manifest
    collection_path = Path(collection_manifest.artifact)
    collection_root = (
        collection_path if collection_path.is_absolute() else repo_root / collection_path
    ).parent
    config_path = Path(manifest.input_binding.execution_config.artifact)
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    postprocess_path = Path(observation.postprocess_manifest.artifact)
    if not postprocess_path.is_absolute():
        postprocess_path = repo_root / postprocess_path
    try:
        loaded = postprocess_v7.load_post_exhaustion_postprocess_v7(
            repo_root=repo_root,
            collection_root=collection_root,
            collection_config_path=config_path,
            output_root=postprocess_path.parent,
        )
        replayed = postprocess_v7.verify_post_exhaustion_postprocess_v7(loaded)
    except postprocess_v7.PostExhaustionPostprocessV7Error as exc:
        raise PostExhaustionGate5GLineageError(
            "extension postprocess-v7 execution does not replay"
        ) from exc
    if replayed != manifest:
        raise PostExhaustionGate5GLineageError("extension postprocess-v7 replay differs")


def _write_lineage(
    *,
    repo_root: Path,
    lineage: Gate5GLineageManifestV1,
    output_root: Path,
) -> Path:
    payload = canonical_json_bytes(lineage.model_dump(mode="json"))
    suffix = lineage.manifest_id.rsplit(":", 1)[-1]
    path = output_root / "lineage" / f"{suffix}.json"
    try:
        gate5g_v1._write_immutable(
            path,
            payload,
            repo_root=repo_root,
            label="post-exhaustion Gate-5G lineage",
        )
    except gate5g_v1.Gate5GFinalizationError as exc:
        raise PostExhaustionGate5GLineageError(str(exc)) from exc
    _, _, persisted = _read_repo_file_no_follow(
        repo_root=repo_root,
        artifact=path,
        expected_sha256=sha256_hex(payload),
        label="persisted post-exhaustion Gate-5G lineage",
    )
    try:
        reloaded = Gate5GLineageManifestV1.model_validate(
            _strict_json(persisted, label="persisted post-exhaustion Gate-5G lineage")
        )
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError(
            "persisted post-exhaustion Gate-5G lineage is invalid"
        ) from exc
    if reloaded != lineage:
        raise PostExhaustionGate5GLineageError("persisted post-exhaustion Gate-5G lineage differs")
    return path


def verify_mixed_gate5g_lineage_v1(
    *,
    repo_root: Path,
    lineage: Gate5GLineageManifestV1,
    decision: Any,
    required_families: tuple[str, str, str],
) -> None:
    """Verify a mixed historical/extension lineage without rewriting history.

    The original Gate-5G verifier predates collection schemas 1/2 and
    postprocess schema 3, whose writers did not persist ``tranche_id``.  This
    verifier applies only the registered projections above, with tranche
    identity supplied by the immutable frame observation, and retains the
    exact two-scan replay-certificate verification for every tranche.
    """

    root = repo_root.resolve()
    if required_families != _EXPECTED_FAMILIES:
        raise PostExhaustionGate5GLineageError("mixed lineage family inventory is not registered")
    if lineage.scalable_family_ids != required_families:
        raise PostExhaustionGate5GLineageError(
            "mixed lineage has the wrong scalable family inventory"
        )
    observations = tuple(
        (
            item.tranche_id,
            item.manifest_id,
            item.postprocess_manifest.artifact,
            item.postprocess_manifest.sha256,
        )
        for item in decision.observations
    )
    expected_observations = tuple(
        (
            item.tranche_id,
            item.postprocess_manifest.manifest_id,
            item.postprocess_manifest.artifact,
            item.postprocess_manifest.sha256,
        )
        for item in lineage.tranches
    )
    if observations != expected_observations:
        raise PostExhaustionGate5GLineageError(
            "frame observations differ from the complete mixed lineage"
        )

    for tranche in lineage.tranches:
        collection_raw = _load_bound_registered_manifest_json(
            repo_root=root,
            binding=tranche.collection_manifest,
            label=f"{tranche.tranche_id} collection manifest",
            kind="collection",
        )
        collection = _project_registered_collection_manifest(
            collection_raw,
            expected_tranche_id=tranche.tranche_id,
            label=f"{tranche.tranche_id} collection manifest",
        )
        if (
            collection.expected_candidate_count != tranche.expected_invocations
            or collection.terminal_candidate_count != tranche.collection_terminal_count
            or collection.family_count != len(tranche.family_ids)
            or len(collection.terminal_artifact_hashes) != tranche.collection_terminal_count
        ):
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} collection denominator differs"
            )
        observed_revisions = _family_revisions(
            repo_root=root,
            collection=collection,
        )
        if (
            observed_revisions != tranche.family_revisions
            or tuple(item.family_id for item in observed_revisions) != tranche.family_ids
        ):
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} family revision lineage differs"
            )

        postprocess_raw = _load_bound_registered_manifest_json(
            repo_root=root,
            binding=Gate5GArtifactBinding(
                artifact=tranche.postprocess_manifest.artifact,
                sha256=tranche.postprocess_manifest.sha256,
            ),
            label=f"{tranche.tranche_id} postprocess manifest",
            kind="postprocess",
        )
        postprocess = _project_registered_postprocess_manifest(
            postprocess_raw,
            expected_tranche_id=tranche.tranche_id,
            label=f"{tranche.tranche_id} postprocess manifest",
        )
        if (
            postprocess.manifest_id != tranche.postprocess_manifest.manifest_id
            or postprocess.expected_invocations != tranche.expected_invocations
            or postprocess.terminal_invocations != tranche.postprocess_terminal_count
            or postprocess.family_count != len(tranche.family_ids)
            or len(postprocess.terminal_artifacts) != tranche.postprocess_terminal_count
            or sum(postprocess.status_counts.values()) != tranche.postprocess_terminal_count
            or postprocess.status_counts.get("admitted_unresolved", 0)
            + postprocess.status_counts.get("screen_rejected", 0)
            != tranche.benchmark_clear_compiling_count
            or postprocess.input_binding.collection_manifest != tranche.collection_manifest
            or postprocess.input_binding.collection_manifest_id != collection.manifest_id
        ):
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} postprocess denominator/lineage differs"
            )

        _, _, overlap_payload = _read_repo_file_no_follow(
            repo_root=root,
            artifact=tranche.overlap_manifest.artifact,
            expected_sha256=tranche.overlap_manifest.sha256,
            label=f"{tranche.tranche_id} overlap manifest",
        )
        overlap_raw = _json_object(
            overlap_payload,
            label=f"{tranche.tranche_id} overlap manifest",
        )
        try:
            overlap = gate5g_v1._OverlapManifestProjection.model_validate(overlap_raw)
            gate5g_v1._require_label_blind(
                overlap_raw,
                label=f"{tranche.tranche_id} overlap manifest",
            )
        except (ValueError, gate5g_v1.Gate5GFinalizationError) as exc:
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} overlap manifest is invalid"
            ) from exc
        if (
            overlap.family_count != len(tranche.family_ids)
            or tuple(sorted(overlap.family_artifacts)) != tranche.family_ids
        ):
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} overlap family inventory differs"
            )
        _snapshot_artifact_tree(
            repo_root=root,
            root_manifest=tranche.overlap_manifest,
            label=f"{tranche.tranche_id} overlap verification",
        )

        verify_gate5g_replay_certificate_v1_exact(
            repo_root=root,
            certificate_binding=tranche.collection_replay,
            manifest=tranche.collection_manifest,
            tranche_id=tranche.tranche_id,
            kind="collection",
            expected_record_count=tranche.collection_terminal_count,
        )
        verify_gate5g_replay_certificate_v1_exact(
            repo_root=root,
            certificate_binding=tranche.postprocess_replay,
            manifest=Gate5GArtifactBinding(
                artifact=tranche.postprocess_manifest.artifact,
                sha256=tranche.postprocess_manifest.sha256,
            ),
            tranche_id=tranche.tranche_id,
            kind="postprocess",
            expected_record_count=tranche.postprocess_terminal_count,
        )


def publish_mixed_gate5g_lineage_v1(
    *,
    repo_root: Path,
    decision: Any,
    tranches: tuple[Gate5GTrancheBindingV1, ...],
    required_families: tuple[str, str, str],
    output_root: Path,
) -> tuple[Gate5GLineageManifestV1, Path]:
    """Independently replay and atomically publish one complete mixed prefix."""

    if required_families != _EXPECTED_FAMILIES:
        raise PostExhaustionGate5GLineageError("mixed lineage family inventory differs")
    pool_ids = tuple(sorted({pool for tranche in tranches for pool in tranche.pool_ids}))
    source_proxies = tuple(
        sorted({proxy for tranche in tranches for proxy in tranche.source_proxies})
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "tranches": tuple(item.model_dump(mode="json") for item in tranches),
        "scalable_family_ids": required_families,
        "pool_ids": pool_ids,
        "source_proxies": source_proxies,
        "total_expected_invocations": sum(item.expected_invocations for item in tranches),
        "total_collection_terminals": sum(item.collection_terminal_count for item in tranches),
        "total_postprocess_terminals": sum(item.postprocess_terminal_count for item in tranches),
        "total_benchmark_clear_compiling": sum(
            item.benchmark_clear_compiling_count for item in tranches
        ),
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    lineage_id = "lf021_gate5g_lineage:" + hash_canonical(
        {"schema": "lf021_gate5g_lineage_v1", **payload}
    )
    try:
        lineage = Gate5GLineageManifestV1.model_validate({"manifest_id": lineage_id, **payload})
        verify_mixed_gate5g_lineage_v1(
            repo_root=repo_root,
            lineage=lineage,
            decision=cast(Any, decision),
            required_families=required_families,
        )
    except ValueError as exc:
        raise PostExhaustionGate5GLineageError(
            "mixed original/extension lineage does not pass Gate-5G replay"
        ) from exc
    return lineage, _write_lineage(
        repo_root=repo_root,
        lineage=lineage,
        output_root=output_root,
    )


def build_post_exhaustion_gate5g_lineage_v1(
    *,
    repo_root: Path,
    spec_path: Path,
    output_root: Path | None = None,
) -> PostExhaustionGate5GLineageRunV1:
    """Strictly replay and seal a real original-plus-extension lineage."""

    root = repo_root.resolve()
    spec = load_post_exhaustion_gate5g_lineage_spec_v1(
        repo_root=root,
        spec_path=spec_path,
    )
    executing_builder = _binding_from_path(
        repo_root=root,
        path=Path(__file__).resolve(),
        label="executing post-exhaustion Gate-5G lineage builder",
    )
    if spec.builder_implementation != executing_builder:
        raise PostExhaustionGate5GLineageError("lineage spec does not bind the executing builder")
    frame_policy_path = _read_repo_file_no_follow(
        repo_root=root,
        artifact=spec.frame_policy.artifact,
        expected_sha256=spec.frame_policy.sha256,
        label="extended frame policy",
    )[0]
    frame_decision_path = _read_repo_file_no_follow(
        repo_root=root,
        artifact=spec.frame_decision.artifact,
        expected_sha256=spec.frame_decision.sha256,
        label="extended frame decision",
    )[0]
    try:
        verified = frame_v1.verify_extended_frame_freeze_v1(
            repo_root=root,
            policy_path=frame_policy_path,
            decision_path=frame_decision_path,
        )
    except frame_v1.PostExhaustionFrameError as exc:
        raise PostExhaustionGate5GLineageError(
            "extended production frame does not strictly replay"
        ) from exc
    decision = verified.decision
    if decision.test_replay_only:
        raise PostExhaustionGate5GLineageError(
            "test/replay frame cannot produce production Gate-5G lineage"
        )
    if (
        decision.original_observation_count != spec.required_original_observation_count
        or not (
            spec.minimum_extension_observation_count
            <= decision.extension_observation_count
            <= spec.maximum_extension_observation_count
        )
        or len(decision.observations)
        != decision.original_observation_count + decision.extension_observation_count
    ):
        raise PostExhaustionGate5GLineageError("extended frame observation denominator differs")

    loaded_frame_policy = frame_v1.load_post_exhaustion_frame_policy_v1(frame_policy_path)
    extension_policy_path = _read_repo_file_no_follow(
        repo_root=root,
        artifact=loaded_frame_policy.config.extension_policy.artifact,
        expected_sha256=loaded_frame_policy.config.extension_policy.sha256,
        label="post-exhaustion extension policy",
    )[0]
    loaded_extension = extension_v1.load_post_exhaustion_extension_policy(extension_policy_path)
    base_policy_path = _read_repo_file_no_follow(
        repo_root=root,
        artifact=loaded_extension.config.base_v1_policy.artifact,
        expected_sha256=loaded_extension.config.base_v1_policy.sha256,
        label="base tranche policy",
    )[0]
    base_policy = tranche_v1.load_tranche_expansion_policy(base_policy_path).config
    tranche_specs = (
        base_policy.tranches
        + loaded_extension.config.extension_tranches[: decision.extension_observation_count]
    )
    if (
        len(base_policy.tranches) != spec.required_original_observation_count
        or len(tranche_specs) != len(decision.observations)
        or tuple(item.tranche_id for item in tranche_specs)
        != tuple(item.tranche_id for item in decision.observations)
    ):
        raise PostExhaustionGate5GLineageError(
            "frame observations differ from the frozen tranche prefix"
        )
    pools = {item.pool_id: item for item in base_policy.pools}
    observed_pool_ids = tuple(sorted({item.pool_id for item in tranche_specs}))
    overlap_by_pool = {item.pool_id: item.overlap_manifest for item in spec.pool_overlaps}
    if tuple(sorted(overlap_by_pool)) != observed_pool_ids:
        raise PostExhaustionGate5GLineageError(
            "lineage-spec overlap inventory differs from observed pools"
        )
    destination = output_root if output_root is not None else root / _OUTPUT_ROOT
    if not destination.is_absolute():
        destination = root / destination

    tranche_bindings: list[Gate5GTrancheBindingV1] = []
    collection_replay_paths: list[Path] = []
    postprocess_replay_paths: list[Path] = []
    for index, (tranche, observation) in enumerate(
        zip(tranche_specs, decision.observations, strict=True)
    ):
        postprocess_binding = Gate5GObservationBinding(
            artifact=observation.postprocess_manifest.artifact,
            sha256=observation.postprocess_manifest.sha256,
            manifest_id=observation.manifest_id,
            tranche_id=observation.tranche_id,
        )
        postprocess_raw = _load_bound_registered_manifest_json(
            repo_root=root,
            binding=postprocess_binding,
            label=f"{tranche.tranche_id} postprocess manifest",
            kind="postprocess",
        )
        postprocess = _project_registered_postprocess_manifest(
            postprocess_raw,
            expected_tranche_id=tranche.tranche_id,
            label=f"{tranche.tranche_id} postprocess manifest",
        )
        if (
            postprocess_raw.get("schema_version") != observation.postprocess_schema_version
            or _POSTPROCESS_PREFIX_BY_SCHEMA.get(observation.postprocess_schema_version)
            != observation.manifest_id.split(":", 1)[0]
            or postprocess.manifest_id != observation.manifest_id
            or postprocess.tranche_id != tranche.tranche_id
        ):
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} observation binding differs"
            )
        collection_binding = postprocess.input_binding.collection_manifest
        collection_raw = _load_bound_registered_manifest_json(
            repo_root=root,
            binding=collection_binding,
            label=f"{tranche.tranche_id} collection manifest",
            kind="collection",
        )
        collection = _project_registered_collection_manifest(
            collection_raw,
            expected_tranche_id=tranche.tranche_id,
            label=f"{tranche.tranche_id} collection manifest",
        )
        collection_schema = collection_raw.get("schema_version")
        if (
            not isinstance(collection_schema, int)
            or _COLLECTION_PREFIX_BY_SCHEMA.get(collection_schema)
            != collection.manifest_id.split(":", 1)[0]
        ):
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} collection version is not registered"
            )
        if (
            collection.manifest_id != postprocess.input_binding.collection_manifest_id
            or collection.tranche_id != tranche.tranche_id
            or collection.expected_candidate_count != tranche.expected_invocations
            or postprocess.expected_invocations != tranche.expected_invocations
            or collection.family_count != len(spec.required_family_ids)
            or postprocess.family_count != len(spec.required_family_ids)
        ):
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} collection/postprocess denominator differs"
            )
        if index >= spec.required_original_observation_count:
            _verify_extension_v7(
                repo_root=root,
                observation=observation,
                expected_order=index,
            )
        family_revisions = _family_revisions(
            repo_root=root,
            collection=collection,
        )
        if tuple(item.family_id for item in family_revisions) != spec.required_family_ids:
            raise PostExhaustionGate5GLineageError(
                f"{tranche.tranche_id} family revision inventory differs"
            )
        _, collection_replay_path, collection_replay = seal_gate5g_replay_certificate_v1(
            repo_root=root,
            manifest=collection_binding,
            tranche_id=tranche.tranche_id,
            kind="collection",
            expected_record_count=collection.terminal_candidate_count,
            output_root=destination,
        )
        _, postprocess_replay_path, postprocess_replay = seal_gate5g_replay_certificate_v1(
            repo_root=root,
            manifest=Gate5GArtifactBinding(
                artifact=postprocess_binding.artifact,
                sha256=postprocess_binding.sha256,
            ),
            tranche_id=tranche.tranche_id,
            kind="postprocess",
            expected_record_count=postprocess.terminal_invocations,
            output_root=destination,
        )
        collection_replay_paths.append(collection_replay_path)
        postprocess_replay_paths.append(postprocess_replay_path)
        pool = pools[tranche.pool_id]
        benchmark_clear = postprocess.status_counts.get(
            "admitted_unresolved", 0
        ) + postprocess.status_counts.get("screen_rejected", 0)
        tranche_bindings.append(
            Gate5GTrancheBindingV1(
                tranche_id=tranche.tranche_id,
                collection_manifest=collection_binding,
                postprocess_manifest=postprocess_binding,
                collection_replay=collection_replay,
                postprocess_replay=postprocess_replay,
                family_ids=spec.required_family_ids,
                family_revisions=family_revisions,
                overlap_manifest=overlap_by_pool[tranche.pool_id],
                pool_ids=(tranche.pool_id,),
                source_proxies=pool.declared_source_proxies,
                expected_invocations=tranche.expected_invocations,
                collection_terminal_count=collection.terminal_candidate_count,
                postprocess_terminal_count=postprocess.terminal_invocations,
                benchmark_clear_compiling_count=benchmark_clear,
            )
        )

    lineage, lineage_path = publish_mixed_gate5g_lineage_v1(
        repo_root=root,
        decision=decision,
        tranches=tuple(tranche_bindings),
        required_families=spec.required_family_ids,
        output_root=destination,
    )
    return PostExhaustionGate5GLineageRunV1(
        spec=spec,
        lineage=lineage,
        lineage_path=lineage_path,
        collection_replay_paths=tuple(collection_replay_paths),
        postprocess_replay_paths=tuple(postprocess_replay_paths),
    )


__all__ = [
    "ExactArtifactTreeEntryV1",
    "PoolOverlapBindingV1",
    "PostExhaustionGate5GLineageError",
    "PostExhaustionGate5GLineageRunV1",
    "PostExhaustionGate5GLineageSpecRunV1",
    "PostExhaustionGate5GLineageSpecV1",
    "build_post_exhaustion_gate5g_lineage_v1",
    "load_post_exhaustion_gate5g_lineage_spec_v1",
    "publish_mixed_gate5g_lineage_v1",
    "publish_post_exhaustion_gate5g_lineage_spec_v1",
    "seal_gate5g_replay_certificate_v1",
    "verify_gate5g_replay_certificate_v1_exact",
]
