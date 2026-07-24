"""Content-addressed blinded export for the frozen LF-021 prevalence frame.

The public bundles contain only natural-language context and name-free
reference/candidate Lean views.  All source identifiers are held in a
mode-0600 private linkage artifact.  This module creates no semantic labels.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from leanfaith.annotation_support.blinding import (
    assert_blinded_payload,
    assert_name_free_statement,
    blind_item_id,
    independently_randomized,
    validate_entropy,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.generation.post_exhaustion_frame_v1 import ExtendedFrameItemV1
from leanfaith.schemas.nl_lean import NLPLeanRecord
from leanfaith.schemas.pair import PairRecord
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord

EXACT_FRAME_RELATIVE_PATH = Path(
    "reports/generation/lf021_post_exhaustion_frame_v1/frames/"
    "a07b352030a2c51fa51ebcabc00a3c1d1ecf2041318feabfe57a4c70fc365069.jsonl"
)
EXACT_FRAME_SHA256 = "a07b352030a2c51fa51ebcabc00a3c1d1ecf2041318feabfe57a4c70fc365069"
EXACT_FRAME_ITEM_COUNT = 240
ANNOTATOR_SLOTS = ("independent_annotator_1", "independent_annotator_2")
ANNOTATION_TEMPLATE_PATH = Path("annotation/templates/lf021_prevalence_v1.json")
ANNOTATION_TEMPLATE_SHA256 = "33aa5161e61d56ea447eed70188b1663eee881c66ba9672a8400c89ee3dbc8af"
ANNOTATION_CODEBOOK_PATH = Path("annotation/codebook_v1.yaml")
ANNOTATION_CODEBOOK_SHA256 = "b98404daba174c2ff70557fdf1e8bcfd13ba9c4990f992b63d2f5506eaeb7885"
ANNOTATION_GUIDELINE_PATH = Path("annotation/guidelines_v1.md")
ANNOTATION_GUIDELINE_SHA256 = "604eeade46c6328646bd71641f9a3c69cb0588462ac725c5eaf251a89a4b779f"
ANNOTATION_CONFIG_PATH = Path("configs/annotation/lf021_prevalence_v1.yaml")
ANNOTATION_CONFIG_SHA256 = "6146e1774195e8df7b453bed59258d9bc3622b03287c62a309e3ce3fac6df1dd"
MINIMAL_IMPORT_PATH = Path("examples/lf021_public_research_mathlib_header_v1.lean")
MINIMAL_IMPORT_SHA256 = "1015f8f4676a27ea63780b30cc916712232857097b2bdf635dc61cb949c107fb"
_HEX64 = r"^[0-9a-f]{64}$"
_BLIND_ITEM_ID = r"^lf023_blind_item_v1:[0-9a-f]{64}$"
_BUNDLE_ID = r"^lf023_blinded_bundle_v1:[0-9a-f]{64}$"
_MANIFEST_ID = r"^lf023_blinded_bundle_manifest_v1:[0-9a-f]{64}$"
_LINKAGE_ID = r"^lf023_private_linkage_v1:[0-9a-f]{64}$"
_LINKAGE_MANIFEST_ID = r"^lf023_private_linkage_manifest_v1:[0-9a-f]{64}$"


class AnnotationExportError(RuntimeError):
    """Raised when source lineage, blinding, or immutable output fails."""


class ArtifactBinding(StrictModel):
    artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)


class LeanDisplayViewsV1(StrictModel):
    """Exactly the three model views authorized by the frozen codebook."""

    headless: str = Field(min_length=1)
    signature_pp: str = Field(min_length=1)
    signature_explicit: str = Field(min_length=1)

    @model_validator(mode="after")
    def _name_free(self) -> Self:
        for field_name in ("headless", "signature_pp", "signature_explicit"):
            assert_name_free_statement(getattr(self, field_name), field_name=field_name)
        return self


class PermittedContextV1(StrictModel):
    """Neutral context allow-listed by the frozen display schema."""

    minimal_import_text: str
    namespace_text: str
    local_notation_text: str
    required_type_information: str
    view_unavailable_notices: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _notices_are_unique(self) -> Self:
        if list(self.view_unavailable_notices) != sorted(set(self.view_unavailable_notices)):
            raise ValueError("view_unavailable_notices must be sorted and unique")
        return self


class BlindedAnnotationItemV1(StrictModel):
    """Exact ``display_payload_schema`` projection from the frozen template."""

    opaque_item_token: str = Field(pattern=_BLIND_ITEM_ID)
    natural_language_statement: str = Field(min_length=1)
    lean_a: LeanDisplayViewsV1
    lean_b: LeanDisplayViewsV1
    permitted_context: PermittedContextV1

    @model_validator(mode="after")
    def _blinded(self) -> Self:
        assert_blinded_payload(self.model_dump(mode="json"))
        return self


class BlindedBundleManifestV1(StrictModel):
    """Public manifest; intentionally contains no source-frame binding."""

    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_MANIFEST_ID)
    manifest_kind: Literal["lf023_blinded_annotation_bundle_v1"]
    annotator_slot: Literal["independent_annotator_1", "independent_annotator_2"]
    bundle_id: str = Field(pattern=_BUNDLE_ID)
    bundle: ArtifactBinding
    item_count: Literal[240]
    item_schema: Literal["BlindedAnnotationItemV1"]
    blinding_contract: Literal["lf023_reference_aware_minimal_v1"]
    randomized_order: Literal[True] = True

    @model_validator(mode="after")
    def _content_id_and_blinding(self) -> Self:
        payload = self.model_dump(mode="json")
        expected = "lf023_blinded_bundle_manifest_v1:" + hash_canonical(
            {
                "schema": "lf023_blinded_bundle_manifest_v1",
                **{key: item for key, item in payload.items() if key != "manifest_id"},
            }
        )
        if self.manifest_id != expected:
            raise ValueError("blinded bundle manifest ID differs from content")
        assert_blinded_payload(payload)
        return self


class PrivateLinkageRecordV1(StrictModel):
    schema_version: Literal[1] = 1
    source_frame_record_id: str = Field(min_length=1)
    target_pair_id: str = Field(min_length=1)
    target_nl_lean_id: str = Field(min_length=1)
    independent_annotator_1_item_id: str = Field(pattern=_BLIND_ITEM_ID)
    independent_annotator_2_item_id: str = Field(pattern=_BLIND_ITEM_ID)


class PrivateLinkageManifestV1(StrictModel):
    schema_version: Literal[1] = 1
    manifest_id: str = Field(pattern=_LINKAGE_MANIFEST_ID)
    manifest_kind: Literal["lf023_private_annotation_linkage_v1"]
    source_frame: ArtifactBinding
    implementation: ArtifactBinding
    blinding_implementation: ArtifactBinding
    annotation_config: ArtifactBinding
    annotation_template: ArtifactBinding
    annotation_codebook: ArtifactBinding
    annotation_guideline: ArtifactBinding
    minimal_import: ArtifactBinding
    pool_manifests: tuple[ArtifactBinding, ArtifactBinding]
    reference_theorem_collections: tuple[ArtifactBinding, ArtifactBinding]
    reference_representation_collections: tuple[ArtifactBinding, ArtifactBinding]
    public_bundle_manifests: tuple[ArtifactBinding, ArtifactBinding]
    private_linkage_id: str = Field(pattern=_LINKAGE_ID)
    private_linkage: ArtifactBinding
    item_count: Literal[240]
    randomization_method: Literal["independent_hmac_sha256_keys_not_serialized_v1"]
    private: Literal[True] = True
    release_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _content_id(self) -> Self:
        payload = self.model_dump(mode="json")
        expected = "lf023_private_linkage_manifest_v1:" + hash_canonical(
            {
                "schema": "lf023_private_linkage_manifest_v1",
                **{key: item for key, item in payload.items() if key != "manifest_id"},
            }
        )
        if self.manifest_id != expected:
            raise ValueError("private linkage manifest ID differs from content")
        return self


@dataclass(frozen=True, slots=True)
class BlindedBundleRun:
    manifest: BlindedBundleManifestV1
    bundle_path: Path
    manifest_path: Path
    items: tuple[BlindedAnnotationItemV1, ...]


@dataclass(frozen=True, slots=True)
class AnnotationExportRun:
    bundles: tuple[BlindedBundleRun, BlindedBundleRun]
    private_linkage_id: str
    private_linkage_path: Path
    private_manifest: PrivateLinkageManifestV1
    private_manifest_path: Path


@dataclass(frozen=True, slots=True)
class _PoolReferenceIndex:
    manifest_binding: ArtifactBinding
    theorem_collection: ArtifactBinding
    representation_collection: ArtifactBinding
    theorems: dict[str, TheoremRecord]
    representations: dict[str, RepresentationRecord]


@dataclass(frozen=True, slots=True)
class _PreparedItem:
    frame_record_id: str
    pair_id: str
    nl_lean_id: str
    natural_language_statement: str
    lean_a: LeanDisplayViewsV1
    lean_b: LeanDisplayViewsV1


_POOL_RESOURCES: dict[str, tuple[str, str, str, str, str]] = {
    "algebra_gate3_docstrings_v1": (
        "data/parsed/real_outputs/gate3_docstrings_operational_v1/problem_pool_manifest.json",
        "229cf1dfcc7c8eee0de839c62b6beb708678ff2a2bea876803b704033de324d7",
        "data/parsed/real_outputs/gate3_docstrings_operational_v1/reference_theorems.jsonl",
        "75a4c486d88a981e39a18bfa611ed0245ee5684d9b2b4cf6a25023227a4a4cee",
        "data/parsed/real_outputs/gate3_docstrings_operational_v1/reference_representations.jsonl",
    ),
    "cross_domain_docstrings_v1": (
        "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/"
        "problem_pool_manifest.json",
        "f7fdcdb9676148500ba4620bc12a4776192e8b4804ab6fc160571d4d8aef4122",
        "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/reference_theorems.jsonl",
        "3037cf382482f24d00e2923809fcad6c144a00a5e9fec81f43b29842b8214ef2",
        "data/parsed/real_outputs/cross_domain_docstrings_operational_v1/"
        "reference_representations.jsonl",
    ),
}

_REFERENCE_REPRESENTATION_HASHES = {
    "algebra_gate3_docstrings_v1": (
        "c3109ebb282bda4b54a9beec062a45b9c42c94132485db394fd6aad6914b6493"
    ),
    "cross_domain_docstrings_v1": (
        "072c341f52339edc56fa90f548470d22aeea3a3fcb0936ac1a1e3af3255ee39b"
    ),
}


def _artifact_name(repo_root: Path, path: Path) -> str:
    resolved_root = repo_root.resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(resolved_root):
        return resolved.relative_to(resolved_root).as_posix()
    return str(resolved)


def _binding(repo_root: Path, path: Path) -> ArtifactBinding:
    return ArtifactBinding(artifact=_artifact_name(repo_root, path), sha256=hash_file(path))


def _resolve_bound(repo_root: Path, artifact: str, expected_sha256: str) -> Path:
    raw = Path(artifact)
    path = raw if raw.is_absolute() else repo_root / raw
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AnnotationExportError(f"required artifact is unavailable: {artifact}") from exc
    if not raw.is_absolute() and not resolved.is_relative_to(repo_root.resolve()):
        raise AnnotationExportError(f"artifact escapes repository: {artifact}")
    if not resolved.is_file() or hash_file(resolved) != expected_sha256:
        raise AnnotationExportError(f"required artifact hash differs: {artifact}")
    return resolved


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise AnnotationExportError(f"invalid JSON object: {path}") from exc
    if not isinstance(value, dict):
        raise AnnotationExportError(f"JSON artifact must contain an object: {path}")
    return value


def _canonical_jsonl_objects(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AnnotationExportError(f"cannot read JSONL artifact: {path}") from exc
    if payload and not payload.endswith(b"\n"):
        raise AnnotationExportError(f"JSONL artifact lacks terminal LF: {path}")
    rows: list[dict[str, Any]] = []
    for number, raw in enumerate(payload.splitlines(), start=1):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AnnotationExportError(f"invalid JSONL row {path}:{number}") from exc
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise AnnotationExportError(f"non-canonical JSONL row {path}:{number}")
        rows.append(value)
    return tuple(rows)


def _manifest_reference_hash(manifest: dict[str, Any], *, key: str) -> str | None:
    output_artifacts = manifest.get("output_artifacts")
    if isinstance(output_artifacts, dict):
        binding = output_artifacts.get(key)
        if isinstance(binding, dict) and isinstance(binding.get("sha256"), str):
            return str(binding["sha256"])
    output_hashes = manifest.get("output_hashes")
    if isinstance(output_hashes, dict) and isinstance(output_hashes.get(key), str):
        return str(output_hashes[key])
    return None


def _load_reference_index(repo_root: Path, pool_id: str) -> _PoolReferenceIndex:
    try:
        (
            manifest_artifact,
            manifest_sha,
            theorem_artifact,
            theorem_sha,
            representation_artifact,
        ) = _POOL_RESOURCES[pool_id]
    except KeyError as exc:
        raise AnnotationExportError(f"unsupported frozen-frame pool: {pool_id}") from exc
    representation_sha = _REFERENCE_REPRESENTATION_HASHES[pool_id]
    manifest_path = _resolve_bound(repo_root, manifest_artifact, manifest_sha)
    theorem_path = _resolve_bound(repo_root, theorem_artifact, theorem_sha)
    representation_path = _resolve_bound(
        repo_root,
        representation_artifact,
        representation_sha,
    )
    manifest = _json_object(manifest_path)
    if _manifest_reference_hash(manifest, key="reference_theorems") != theorem_sha:
        raise AnnotationExportError(f"pool manifest does not bind reference theorems: {pool_id}")
    if _manifest_reference_hash(manifest, key="reference_representations") != representation_sha:
        raise AnnotationExportError(
            f"pool manifest does not bind reference representations: {pool_id}"
        )
    theorems = tuple(
        TheoremRecord.model_validate(item) for item in _canonical_jsonl_objects(theorem_path)
    )
    representations = tuple(
        RepresentationRecord.model_validate(item)
        for item in _canonical_jsonl_objects(representation_path)
    )
    theorem_by_id = {item.theorem_id: item for item in theorems}
    representation_by_theorem = {item.theorem_id: item for item in representations}
    if len(theorem_by_id) != len(theorems) or len(representation_by_theorem) != len(
        representations
    ):
        raise AnnotationExportError(f"duplicate reference IDs in pool: {pool_id}")
    if set(theorem_by_id) != set(representation_by_theorem):
        raise AnnotationExportError(f"reference theorem/representation mismatch: {pool_id}")
    return _PoolReferenceIndex(
        manifest_binding=_binding(repo_root, manifest_path),
        theorem_collection=_binding(repo_root, theorem_path),
        representation_collection=_binding(repo_root, representation_path),
        theorems=theorem_by_id,
        representations=representation_by_theorem,
    )


def _artifact_by_suffix(
    *,
    repo_root: Path,
    output_hashes: object,
    suffix: str,
) -> Path:
    if not isinstance(output_hashes, dict):
        raise AnnotationExportError("processing terminal lacks output artifact hashes")
    matches = [
        (artifact, digest)
        for artifact, digest in output_hashes.items()
        if isinstance(artifact, str) and isinstance(digest, str) and artifact.endswith(suffix)
    ]
    if len(matches) != 1:
        raise AnnotationExportError(f"expected exactly one terminal artifact ending {suffix}")
    artifact, digest = matches[0]
    return _resolve_bound(repo_root, artifact, digest)


def _required_view(
    representation: RepresentationRecord,
    *,
    field_name: Literal["headless", "signature_pp", "signature_explicit"],
) -> str:
    value = getattr(representation, field_name)
    if not isinstance(value, str) or not value.strip():
        raise AnnotationExportError(
            f"required {field_name} view unavailable for {representation.theorem_id}"
        )
    assert_name_free_statement(value, field_name=field_name)
    return value


def _prepare_item(
    *,
    repo_root: Path,
    frame: ExtendedFrameItemV1,
    references: dict[str, _PoolReferenceIndex],
) -> _PreparedItem:
    population = frame.population_item
    terminal_path = _resolve_bound(
        repo_root,
        population.terminal_artifact.artifact,
        population.terminal_artifact.sha256,
    )
    terminal = _json_object(terminal_path)
    output_hashes = terminal.get("output_artifact_hashes")
    nl_path = _artifact_by_suffix(
        repo_root=repo_root,
        output_hashes=output_hashes,
        suffix="/unresolved_nl_lean.json",
    )
    pair_path = _artifact_by_suffix(
        repo_root=repo_root,
        output_hashes=output_hashes,
        suffix="/unresolved_pairs.jsonl",
    )
    nl_record = NLPLeanRecord.model_validate(_json_object(nl_path))
    pair_rows = _canonical_jsonl_objects(pair_path)
    if len(pair_rows) != 1:
        raise AnnotationExportError(
            f"frame item must bind exactly one reference pair: {frame.frame_record_id}"
        )
    pair = PairRecord.model_validate(pair_rows[0])
    candidate_path = _resolve_bound(
        repo_root,
        population.representation_artifact.artifact,
        population.representation_artifact.sha256,
    )
    candidate = RepresentationRecord.model_validate(_json_object(candidate_path))

    if (
        nl_record.problem_record_id != population.representative_problem_record_id
        or nl_record.problem_group != population.problem_group
        or nl_record.candidate_theorem_id != candidate.theorem_id
        or pair.pair_id not in {link.pair_id for link in nl_record.reference_pairs}
        or pair.theorem_b_id != candidate.theorem_id
        or pair.nl_problem_group != nl_record.problem_group
    ):
        raise AnnotationExportError(
            f"NL, pair, candidate, and frame lineage differ: {frame.frame_record_id}"
        )
    if len(nl_record.reference_theorem_ids) != 1:
        raise AnnotationExportError(
            f"frame item must have one registered reference: {frame.frame_record_id}"
        )
    reference_id = nl_record.reference_theorem_ids[0]
    if pair.theorem_a_id != reference_id:
        raise AnnotationExportError(f"pair side A is not the registered reference: {pair.pair_id}")
    try:
        reference_index = references[population.representative_pool_id]
        reference_theorem = reference_index.theorems[reference_id]
        reference = reference_index.representations[reference_id]
    except KeyError as exc:
        raise AnnotationExportError(
            f"registered reference cannot be resolved: {frame.frame_record_id}"
        ) from exc
    if reference.theorem_id != reference_theorem.theorem_id:
        raise AnnotationExportError(f"reference theorem and representation differ: {reference_id}")

    return _PreparedItem(
        frame_record_id=frame.frame_record_id,
        pair_id=pair.pair_id,
        nl_lean_id=nl_record.nl_lean_id,
        natural_language_statement=nl_record.nl_statement,
        lean_a=LeanDisplayViewsV1(
            headless=_required_view(reference, field_name="headless"),
            signature_pp=_required_view(reference, field_name="signature_pp"),
            signature_explicit=_required_view(reference, field_name="signature_explicit"),
        ),
        lean_b=LeanDisplayViewsV1(
            headless=_required_view(candidate, field_name="headless"),
            signature_pp=_required_view(candidate, field_name="signature_pp"),
            signature_explicit=_required_view(candidate, field_name="signature_explicit"),
        ),
    )


def _load_exact_frame(repo_root: Path, frame_path: Path) -> tuple[ExtendedFrameItemV1, ...]:
    expected = (repo_root / EXACT_FRAME_RELATIVE_PATH).resolve()
    try:
        actual = frame_path.resolve(strict=True)
    except OSError as exc:
        raise AnnotationExportError("frozen annotation frame is unavailable") from exc
    if actual != expected:
        raise AnnotationExportError("annotation export accepts only the exact frozen frame path")
    if hash_file(actual) != EXACT_FRAME_SHA256:
        raise AnnotationExportError("frozen annotation frame hash differs")
    rows = tuple(
        ExtendedFrameItemV1.model_validate(item) for item in _canonical_jsonl_objects(actual)
    )
    if len(rows) != EXACT_FRAME_ITEM_COUNT:
        raise AnnotationExportError("frozen annotation frame must contain exactly 240 items")
    if tuple(item.frame_record_id for item in rows) != tuple(
        sorted(item.frame_record_id for item in rows)
    ):
        raise AnnotationExportError("frozen annotation frame order differs")
    if len({item.frame_record_id for item in rows}) != len(rows):
        raise AnnotationExportError("frozen annotation frame contains duplicate item IDs")
    return rows


def _jsonl_bytes(models: tuple[StrictModel, ...]) -> bytes:
    return b"".join(canonical_json_bytes(model.model_dump(mode="json")) + b"\n" for model in models)


def _safe_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path.parent
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise AnnotationExportError(f"annotation output parent is a symlink: {current}")
        if current == current.parent:
            return
        current = current.parent


def _write_immutable(path: Path, payload: bytes, *, private: bool) -> None:
    _safe_parent(path)
    mode = 0o600 if private else 0o640
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise AnnotationExportError(f"immutable annotation artifact differs: {path}") from None
        return
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode, follow_symlinks=False)


def _public_bundle(
    *,
    repo_root: Path,
    output_root: Path,
    annotator_slot: Literal["independent_annotator_1", "independent_annotator_2"],
    prepared: tuple[_PreparedItem, ...],
    entropy: bytes,
    permitted_context: PermittedContextV1,
) -> BlindedBundleRun:
    randomized = independently_randomized(
        prepared,
        entropy=entropy,
        annotator_slot=annotator_slot,
        stable_key=lambda item: item.frame_record_id,
    )
    items = tuple(
        BlindedAnnotationItemV1(
            opaque_item_token=blind_item_id(
                entropy=entropy,
                annotator_slot=annotator_slot,
                frame_record_id=item.frame_record_id,
            ),
            natural_language_statement=item.natural_language_statement,
            lean_a=item.lean_a,
            lean_b=item.lean_b,
            permitted_context=permitted_context,
        )
        for item in randomized
    )
    if len({item.opaque_item_token for item in items}) != len(items):
        raise AnnotationExportError("opaque annotation item IDs collided")
    item_bytes = _jsonl_bytes(items)
    bundle_sha = sha256_hex(item_bytes)
    bundle_id = f"lf023_blinded_bundle_v1:{bundle_sha}"
    bundle_path = output_root / "annotator_bundles" / f"{bundle_sha}.jsonl"
    _write_immutable(bundle_path, item_bytes, private=False)
    bundle_binding = _binding(repo_root, bundle_path)
    manifest_payload: dict[str, Any] = {
        "schema_version": 1,
        "manifest_kind": "lf023_blinded_annotation_bundle_v1",
        "annotator_slot": annotator_slot,
        "bundle_id": bundle_id,
        "bundle": bundle_binding.model_dump(mode="json"),
        "item_count": EXACT_FRAME_ITEM_COUNT,
        "item_schema": "BlindedAnnotationItemV1",
        "blinding_contract": "lf023_reference_aware_minimal_v1",
        "randomized_order": True,
    }
    manifest_id = "lf023_blinded_bundle_manifest_v1:" + hash_canonical(
        {"schema": "lf023_blinded_bundle_manifest_v1", **manifest_payload}
    )
    manifest = BlindedBundleManifestV1.model_validate(
        {"manifest_id": manifest_id, **manifest_payload}
    )
    manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
    manifest_path = output_root / "annotator_manifests" / f"{manifest_id.rsplit(':', 1)[-1]}.json"
    _write_immutable(manifest_path, manifest_bytes, private=False)
    return BlindedBundleRun(
        manifest=manifest,
        bundle_path=bundle_path,
        manifest_path=manifest_path,
        items=items,
    )


def export_blinded_annotation_bundles(
    *,
    repo_root: Path,
    frame_path: Path,
    output_root: Path,
    entropy_by_slot: tuple[bytes, bytes] | None = None,
) -> AnnotationExportRun:
    """Export two independently randomized reference-aware annotation bundles.

    ``entropy_by_slot`` exists for deterministic tests and audited one-time
    orchestration.  Its values are validated, used, and discarded; they are
    never serialized.  Production callers should omit it to use ``secrets``.
    """

    repo_root = repo_root.resolve(strict=True)
    frame_path = frame_path.resolve(strict=True)
    if entropy_by_slot is None:
        entropy_by_slot = (secrets.token_bytes(32), secrets.token_bytes(32))
    first_entropy, second_entropy = entropy_by_slot
    validate_entropy(first_entropy)
    validate_entropy(second_entropy)
    if hmac_compare(first_entropy, second_entropy):
        raise AnnotationExportError("annotator bundles require independent randomization secrets")

    frame = _load_exact_frame(repo_root, frame_path)
    pool_ids = tuple(sorted({item.population_item.representative_pool_id for item in frame}))
    if pool_ids != tuple(sorted(_POOL_RESOURCES)):
        raise AnnotationExportError("frozen frame pool set differs from the registered resources")
    references = {pool_id: _load_reference_index(repo_root, pool_id) for pool_id in pool_ids}
    annotation_config_path = _resolve_bound(
        repo_root,
        ANNOTATION_CONFIG_PATH.as_posix(),
        ANNOTATION_CONFIG_SHA256,
    )
    annotation_template_path = _resolve_bound(
        repo_root,
        ANNOTATION_TEMPLATE_PATH.as_posix(),
        ANNOTATION_TEMPLATE_SHA256,
    )
    annotation_codebook_path = _resolve_bound(
        repo_root,
        ANNOTATION_CODEBOOK_PATH.as_posix(),
        ANNOTATION_CODEBOOK_SHA256,
    )
    annotation_guideline_path = _resolve_bound(
        repo_root,
        ANNOTATION_GUIDELINE_PATH.as_posix(),
        ANNOTATION_GUIDELINE_SHA256,
    )
    minimal_import_path = _resolve_bound(
        repo_root,
        MINIMAL_IMPORT_PATH.as_posix(),
        MINIMAL_IMPORT_SHA256,
    )
    minimal_import_text = minimal_import_path.read_text(encoding="utf-8").strip()
    if minimal_import_text != "import Mathlib":
        raise AnnotationExportError("frozen minimal import text differs")
    template = _json_object(annotation_template_path)
    display_schema = template.get("display_payload_schema")
    expected_top_level = {
        "opaque_item_token",
        "natural_language_statement",
        "lean_a",
        "lean_b",
        "permitted_context",
    }
    if (
        not isinstance(display_schema, dict)
        or display_schema.get("additionalProperties") is not False
        or set(display_schema.get("required", ())) != expected_top_level
    ):
        raise AnnotationExportError("frozen display payload schema differs")
    permitted_context = PermittedContextV1(
        minimal_import_text=minimal_import_text,
        namespace_text="",
        local_notation_text="",
        required_type_information=(
            "Use each side's signature_pp and signature_explicit for elaborated "
            "binder and constant types."
        ),
    )
    prepared = tuple(
        _prepare_item(repo_root=repo_root, frame=item, references=references) for item in frame
    )
    if len({item.pair_id for item in prepared}) != EXACT_FRAME_ITEM_COUNT:
        raise AnnotationExportError("frozen frame does not map one-to-one to reference pairs")

    first = _public_bundle(
        repo_root=repo_root,
        output_root=output_root,
        annotator_slot="independent_annotator_1",
        prepared=prepared,
        entropy=first_entropy,
        permitted_context=permitted_context,
    )
    second = _public_bundle(
        repo_root=repo_root,
        output_root=output_root,
        annotator_slot="independent_annotator_2",
        prepared=prepared,
        entropy=second_entropy,
        permitted_context=permitted_context,
    )
    if tuple(item.natural_language_statement for item in first.items) == tuple(
        item.natural_language_statement for item in second.items
    ):
        raise AnnotationExportError("independent annotator randomizations produced equal orders")
    if {item.opaque_item_token for item in first.items} & {
        item.opaque_item_token for item in second.items
    }:
        raise AnnotationExportError("annotator bundles share opaque item identifiers")

    first_ids = {
        item.frame_record_id: blind_item_id(
            entropy=first_entropy,
            annotator_slot="independent_annotator_1",
            frame_record_id=item.frame_record_id,
        )
        for item in prepared
    }
    second_ids = {
        item.frame_record_id: blind_item_id(
            entropy=second_entropy,
            annotator_slot="independent_annotator_2",
            frame_record_id=item.frame_record_id,
        )
        for item in prepared
    }
    linkage = tuple(
        PrivateLinkageRecordV1(
            source_frame_record_id=item.frame_record_id,
            target_pair_id=item.pair_id,
            target_nl_lean_id=item.nl_lean_id,
            independent_annotator_1_item_id=first_ids[item.frame_record_id],
            independent_annotator_2_item_id=second_ids[item.frame_record_id],
        )
        for item in prepared
    )
    linkage_bytes = _jsonl_bytes(linkage)
    linkage_sha = sha256_hex(linkage_bytes)
    linkage_id = f"lf023_private_linkage_v1:{linkage_sha}"
    linkage_path = output_root / "private" / "linkage" / f"{linkage_sha}.jsonl"
    _write_immutable(linkage_path, linkage_bytes, private=True)

    manifest_bindings = (
        _binding(repo_root, first.manifest_path),
        _binding(repo_root, second.manifest_path),
    )
    pool_indices = tuple(references[pool_id] for pool_id in pool_ids)
    private_payload: dict[str, Any] = {
        "schema_version": 1,
        "manifest_kind": "lf023_private_annotation_linkage_v1",
        "source_frame": _binding(repo_root, frame_path).model_dump(mode="json"),
        "implementation": _binding(repo_root, Path(__file__)).model_dump(mode="json"),
        "blinding_implementation": _binding(
            repo_root,
            Path(__file__).with_name("blinding.py"),
        ).model_dump(mode="json"),
        "annotation_config": _binding(repo_root, annotation_config_path).model_dump(mode="json"),
        "annotation_template": _binding(repo_root, annotation_template_path).model_dump(
            mode="json"
        ),
        "annotation_codebook": _binding(repo_root, annotation_codebook_path).model_dump(
            mode="json"
        ),
        "annotation_guideline": _binding(repo_root, annotation_guideline_path).model_dump(
            mode="json"
        ),
        "minimal_import": _binding(repo_root, minimal_import_path).model_dump(mode="json"),
        "pool_manifests": tuple(
            item.manifest_binding.model_dump(mode="json") for item in pool_indices
        ),
        "reference_theorem_collections": tuple(
            item.theorem_collection.model_dump(mode="json") for item in pool_indices
        ),
        "reference_representation_collections": tuple(
            item.representation_collection.model_dump(mode="json") for item in pool_indices
        ),
        "public_bundle_manifests": tuple(
            item.model_dump(mode="json") for item in manifest_bindings
        ),
        "private_linkage_id": linkage_id,
        "private_linkage": _binding(repo_root, linkage_path).model_dump(mode="json"),
        "item_count": EXACT_FRAME_ITEM_COUNT,
        "randomization_method": "independent_hmac_sha256_keys_not_serialized_v1",
        "private": True,
        "release_eligible": False,
    }
    private_manifest_id = "lf023_private_linkage_manifest_v1:" + hash_canonical(
        {"schema": "lf023_private_linkage_manifest_v1", **private_payload}
    )
    private_manifest = PrivateLinkageManifestV1.model_validate(
        {"manifest_id": private_manifest_id, **private_payload}
    )
    private_manifest_path = (
        output_root / "private" / "manifests" / f"{private_manifest_id.rsplit(':', 1)[-1]}.json"
    )
    _write_immutable(
        private_manifest_path,
        canonical_json_bytes(private_manifest.model_dump(mode="json")),
        private=True,
    )
    return AnnotationExportRun(
        bundles=(first, second),
        private_linkage_id=linkage_id,
        private_linkage_path=linkage_path,
        private_manifest=private_manifest,
        private_manifest_path=private_manifest_path,
    )


def hmac_compare(first: bytes, second: bytes) -> bool:
    """Constant-time equality for caller-supplied randomization material."""

    import hmac

    return hmac.compare_digest(first, second)
