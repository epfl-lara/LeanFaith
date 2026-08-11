"""Deterministic authoring for the public Qwen LF-022 weak-judge batch.

The generic weak-batch executor intentionally consumes a fully pinned spec but
does not guess one.  This module is the single supported authoring path for the
current Qwen schema-v3 inventory.  It validates and copies every direct input,
binds the exact reviewed Kimi/DeepSeek judge contracts, and performs no network
operation.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from leanfaith.config.hashing import canonical_json_bytes, hash_file, sha256_hex
from leanfaith.generation.lf022_production import (
    LF022ProductionFamilyMatrix,
    LF022ProviderCatalogSnapshot,
)
from leanfaith.generation.lf022_supervision_candidates import (
    LF022SupervisionCandidateManifest,
    LF022SupervisionCandidateRecord,
)
from leanfaith.generation.lf022_weak_batch import (
    BoundArtifact,
    JudgeEndpointPin,
    LF022WeakBatchError,
    LF022WeakBatchSpec,
    _load_canonical_jsonl,
    _load_canonical_model,
    _validate_candidate_inventory_records,
    _validate_family_pins,
    _validate_weak_config,
)
from leanfaith.generation.lf022_weak_live_smoke import (
    lf022_weak_judge_route_for_slot,
)
from leanfaith.generation.providers import DecodingValue

_QWEN_FAMILY = "qwen3"
_QWEN_MODEL = "Qwen/Qwen3.5-397B-A17B"
_HELDOUT_FAMILY = "openai_codex"
_HELDOUT_PROVIDER = "openai_codex_exec"
_HELDOUT_MODEL = "openai/gpt-5.6-terra"
_HELDOUT_DEPLOYMENT = "gpt-5.6-terra"
_HELDOUT_CATALOG_SHA256 = "7490a78c4834a3fd50751e849daf7bd050866d7dd6ca9813387279e745cd13e1"


@dataclass(frozen=True, slots=True)
class _QwenWeakBatchAuthoringProfile:
    """Immutable reviewed inputs admitted by the snapshot-specific command."""

    inventory_id: str
    candidate_manifest_sha256: str
    candidate_records_sha256: str
    record_count: int
    dispatch_pair_count: int
    required_judge_call_count: int
    weak_supervision_config_sha256: str
    production_matrix_id: str
    production_matrix_sha256: str
    production_catalog_sha256: str


_QWEN_SNAPSHOT_PROFILE = _QwenWeakBatchAuthoringProfile(
    inventory_id=(
        "lf022_supervision_inventory:"
        "09cd971c0158447ab7c1dd2ef77f56d75576568e2caddf36f7c67c4fbb48ec0a"
    ),
    candidate_manifest_sha256=("75fd6c5046a4f63abf8603337bea4c2c6e277c7f827a57f9ed6f04463531399f"),
    candidate_records_sha256=("2f9b1cd518bcd8769976eec8c4717ef21e16a3165966f30ef54df376faef2978"),
    record_count=718,
    dispatch_pair_count=718,
    required_judge_call_count=2_872,
    weak_supervision_config_sha256=(
        "54109288c1b8bb02e02e053e1ab9db887d828ce28296bf9a26d0dde0d403b9a8"
    ),
    production_matrix_id=(
        "lf022_family_matrix:931ec21d52f937e09140bcd2647382d19975e79bc26902ad6f0931bf72771506"
    ),
    production_matrix_sha256=("c5c3e80496034147c11c699e0608aa4f3802991e9226b807e22f70a27ffc1afb"),
    production_catalog_sha256=("d1cb87ed8042b9c4a2d20abc96bb52979280f8976e08b2113584af9128e0fe0f"),
)


@dataclass(frozen=True, slots=True)
class LF022WeakBatchSpecFreezeResult:
    """Paths and workload counts emitted by one offline spec freeze."""

    spec_path: Path
    spec_sha256: str
    production_catalog_path: Path
    production_catalog_sha256: str
    dispatch_pair_count: int
    required_judge_call_count: int


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _reject_symlink_components(
    path: Path,
    *,
    label: str,
    allow_missing: bool,
) -> Path:
    """Return a lexical absolute path after rejecting every symlink component."""

    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise LF022WeakBatchError(f"{label} is missing: {current}") from None
        except OSError as exc:
            raise LF022WeakBatchError(
                f"cannot inspect {label} path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise LF022WeakBatchError(f"{label} contains a symlink component: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise LF022WeakBatchError(f"{label} parent component is not a directory: {current}")
    return absolute


def _repo_relative(path: Path, *, repo_root: Path, label: str) -> str:
    safe_root = _reject_symlink_components(
        repo_root,
        label="repository root",
        allow_missing=False,
    )
    safe_path = _reject_symlink_components(path, label=label, allow_missing=True)
    try:
        return str(safe_path.resolve().relative_to(safe_root.resolve()))
    except ValueError as exc:
        raise LF022WeakBatchError(f"{label} must be inside the repository") from exc


def _immutable(path: Path, payload: bytes, *, label: str) -> str:
    path = _reject_symlink_components(
        path,
        label=f"immutable {label} path",
        allow_missing=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(
        path,
        label=f"immutable {label} path",
        allow_missing=True,
    )
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise LF022WeakBatchError(f"immutable {label} conflicts at {path}")
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
                raise LF022WeakBatchError(
                    f"concurrent immutable {label} conflict at {path}"
                ) from None
        return hash_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _regular_bytes(path: Path, *, label: str) -> bytes:
    path = _reject_symlink_components(path, label=label, allow_missing=False)
    if not path.is_file():
        raise LF022WeakBatchError(f"{label} is not a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LF022WeakBatchError(f"cannot read {label}: {path}: {exc}") from exc


def _provider_revision(matrix: LF022ProductionFamilyMatrix, family_id: str) -> str:
    pin = matrix.pins_by_id.get(family_id)
    if pin is None:
        raise LF022WeakBatchError(f"required family is absent: {family_id}")
    if pin.checkpoint_revision is not None:
        return pin.checkpoint_revision
    if pin.provider_catalog_artifact is None:
        raise LF022WeakBatchError(f"provider family lacks a catalog binding: {family_id}")
    return f"provider-deployment-snapshot:{pin.provider_catalog_artifact.sha256}"


def _validate_exact_matrix_and_catalog(
    *,
    repo_root: Path,
    matrix_path: Path,
    matrix: LF022ProductionFamilyMatrix,
) -> tuple[Path, LF022ProviderCatalogSnapshot]:
    if (
        matrix.matrix_id != _QWEN_SNAPSHOT_PROFILE.production_matrix_id
        or hash_file(matrix_path) != _QWEN_SNAPSHOT_PROFILE.production_matrix_sha256
    ):
        raise LF022WeakBatchError("production family matrix differs from reviewed snapshot")
    judge_a = lf022_weak_judge_route_for_slot("judge_A")
    judge_b = lf022_weak_judge_route_for_slot("judge_B")
    expected_rcp = {
        _QWEN_FAMILY: _QWEN_MODEL,
        judge_a.family_id: judge_a.model_id,
        judge_b.family_id: judge_b.model_id,
    }
    if _QWEN_FAMILY not in matrix.proposer_family_ids:
        raise LF022WeakBatchError("Qwen is not admitted for the proposer role")
    if judge_a.family_id not in matrix.judge_family_ids:
        raise LF022WeakBatchError("Kimi is not admitted for the judge_A role")
    if judge_b.family_id not in matrix.judge_family_ids:
        raise LF022WeakBatchError("DeepSeek is not admitted for the judge_B role")
    if (
        matrix.heldout_eval_family_id != _HELDOUT_FAMILY
        or matrix.heldout_eval_supervision_excluded is not True
    ):
        raise LF022WeakBatchError("held-out OpenAI/Codex evaluator boundary differs")

    catalog_bindings: set[tuple[str, str]] = set()
    for family_id, model_id in expected_rcp.items():
        pin = matrix.pins_by_id.get(family_id)
        if (
            pin is None
            or pin.provider_id != "epfl_rcp"
            or pin.model_id != model_id
            or pin.provider_deployment_id != model_id
            or pin.provider_catalog_artifact is None
        ):
            raise LF022WeakBatchError(f"exact EPFL RCP deployment differs for family {family_id}")
        catalog_bindings.add(
            (pin.provider_catalog_artifact.path, pin.provider_catalog_artifact.sha256)
        )
    if len(catalog_bindings) != 1:
        raise LF022WeakBatchError("Qwen, Kimi, and DeepSeek must bind one production catalog")

    heldout = matrix.pins_by_id.get(_HELDOUT_FAMILY)
    if (
        heldout is None
        or heldout.provider_id != _HELDOUT_PROVIDER
        or heldout.model_id != _HELDOUT_MODEL
        or heldout.provider_deployment_id != _HELDOUT_DEPLOYMENT
        or heldout.provider_catalog_artifact is None
        or heldout.provider_catalog_artifact.sha256 != _HELDOUT_CATALOG_SHA256
        or _HELDOUT_FAMILY in matrix.judge_family_ids
        or _HELDOUT_FAMILY in matrix.proposer_family_ids
        or _HELDOUT_FAMILY in matrix.sci_validator_family_ids
    ):
        raise LF022WeakBatchError("held-out OpenAI/Codex deployment or role differs")
    heldout_catalog_path = repo_root / heldout.provider_catalog_artifact.path
    _regular_bytes(heldout_catalog_path, label="held-out Codex catalog")
    if hash_file(heldout_catalog_path) != _HELDOUT_CATALOG_SHA256:
        raise LF022WeakBatchError("held-out OpenAI/Codex catalog differs")

    catalog_binding_path, catalog_sha = next(iter(catalog_bindings))
    catalog_path = repo_root / catalog_binding_path
    _regular_bytes(catalog_path, label="production catalog")
    if (
        catalog_sha != _QWEN_SNAPSHOT_PROFILE.production_catalog_sha256
        or hash_file(catalog_path) != catalog_sha
    ):
        raise LF022WeakBatchError("production catalog differs from family-matrix binding")
    catalog_model = _load_canonical_model(catalog_path, LF022ProviderCatalogSnapshot)
    assert isinstance(catalog_model, LF022ProviderCatalogSnapshot)
    if catalog_model.provider_id != "epfl_rcp":
        raise LF022WeakBatchError("production catalog is not the EPFL RCP catalog")
    catalog_deployments = {
        (item.model_id, item.deployment_id) for item in catalog_model.deployments
    }
    for model_id in expected_rcp.values():
        if (model_id, model_id) not in catalog_deployments:
            raise LF022WeakBatchError(
                f"exact deployment is absent from production catalog: {model_id}"
            )
    return catalog_path, catalog_model


def freeze_lf022_qwen_weak_batch_spec(
    *,
    repo_root: Path,
    candidate_manifest_path: Path,
    candidate_records_path: Path,
    weak_supervision_config_path: Path,
    production_family_matrix_path: Path,
    randomization_key: bytes,
    output_dir: Path,
    batch_name: str = "qwen_snapshot1019_weak_judging_v1",
) -> LF022WeakBatchSpecFreezeResult:
    """Freeze one exact Qwen/Kimi/DeepSeek weak-batch spec without network I/O."""

    repo_root = _reject_symlink_components(
        repo_root,
        label="repository root",
        allow_missing=False,
    ).resolve()
    if not repo_root.is_dir():
        raise LF022WeakBatchError(f"repository root is not a directory: {repo_root}")
    output_dir = output_dir if output_dir.is_absolute() else repo_root / output_dir
    _repo_relative(output_dir, repo_root=repo_root, label="weak-batch spec output")
    if len(randomization_key) < 32:
        raise LF022WeakBatchError("randomization key must contain at least 32 bytes")

    candidate_manifest_path = _reject_symlink_components(
        candidate_manifest_path,
        label="candidate manifest",
        allow_missing=False,
    )
    candidate_records_path = _reject_symlink_components(
        candidate_records_path,
        label="candidate records",
        allow_missing=False,
    )
    weak_supervision_config_path = _reject_symlink_components(
        weak_supervision_config_path,
        label="weak-supervision config",
        allow_missing=False,
    )
    production_family_matrix_path = _reject_symlink_components(
        production_family_matrix_path,
        label="production family matrix",
        allow_missing=False,
    )
    if hash_file(candidate_manifest_path) != _QWEN_SNAPSHOT_PROFILE.candidate_manifest_sha256:
        raise LF022WeakBatchError("candidate manifest differs from reviewed Qwen snapshot")
    if hash_file(candidate_records_path) != _QWEN_SNAPSHOT_PROFILE.candidate_records_sha256:
        raise LF022WeakBatchError("candidate records differ from reviewed Qwen snapshot")
    if (
        hash_file(weak_supervision_config_path)
        != _QWEN_SNAPSHOT_PROFILE.weak_supervision_config_sha256
    ):
        raise LF022WeakBatchError("weak-supervision config differs from reviewed snapshot")
    if hash_file(production_family_matrix_path) != _QWEN_SNAPSHOT_PROFILE.production_matrix_sha256:
        raise LF022WeakBatchError("production family matrix differs from reviewed snapshot")

    manifest_model = _load_canonical_model(
        candidate_manifest_path,
        LF022SupervisionCandidateManifest,
    )
    assert isinstance(manifest_model, LF022SupervisionCandidateManifest)
    manifest = manifest_model
    candidate_models = _load_canonical_jsonl(
        candidate_records_path,
        LF022SupervisionCandidateRecord,
    )
    candidates = tuple(
        item for item in candidate_models if isinstance(item, LF022SupervisionCandidateRecord)
    )
    if (
        manifest.schema_version != 3
        or manifest.proposer_family_id != _QWEN_FAMILY
        or manifest.proposer_model != _QWEN_MODEL
        or manifest.inventory_id != _QWEN_SNAPSHOT_PROFILE.inventory_id
        or manifest.record_count != _QWEN_SNAPSHOT_PROFILE.record_count
        or manifest.dispatch_eligible_count != _QWEN_SNAPSHOT_PROFILE.dispatch_pair_count
        or manifest.required_future_judge_call_count
        != _QWEN_SNAPSHOT_PROFILE.required_judge_call_count
    ):
        raise LF022WeakBatchError("authoring requires the exact Qwen schema-v3 inventory")
    if manifest.records_sha256 != hash_file(candidate_records_path):
        raise LF022WeakBatchError("candidate manifest records hash differs from input")
    _validate_candidate_inventory_records(manifest=manifest, candidates=candidates)
    if len(candidates) != _QWEN_SNAPSHOT_PROFILE.record_count:
        raise LF022WeakBatchError("candidate record count differs from reviewed Qwen snapshot")

    _validate_weak_config(weak_supervision_config_path)
    matrix_model = _load_canonical_model(
        production_family_matrix_path,
        LF022ProductionFamilyMatrix,
    )
    assert isinstance(matrix_model, LF022ProductionFamilyMatrix)
    matrix = matrix_model
    catalog_path, _ = _validate_exact_matrix_and_catalog(
        repo_root=repo_root,
        matrix_path=production_family_matrix_path,
        matrix=matrix,
    )

    judge_a_route = lf022_weak_judge_route_for_slot("judge_A")
    judge_b_route = lf022_weak_judge_route_for_slot("judge_B")
    if (
        manifest.judge_a_family_id != judge_a_route.family_id
        or manifest.judge_b_family_id != judge_b_route.family_id
        or manifest.primary_eval_judge_family_id != _HELDOUT_FAMILY
    ):
        raise LF022WeakBatchError("candidate inventory differs from reviewed judge roles")

    input_root = output_dir / "inputs"
    copies = {
        "candidate_manifest": (
            input_root / "candidate_manifest.json",
            _regular_bytes(candidate_manifest_path, label="candidate manifest"),
        ),
        "candidate_records": (
            input_root / "candidate_records.jsonl",
            _regular_bytes(candidate_records_path, label="candidate records"),
        ),
        "weak_supervision_config": (
            input_root / "weak_supervision.yaml",
            _regular_bytes(weak_supervision_config_path, label="weak-supervision config"),
        ),
        "production_family_matrix": (
            input_root / "production_family_matrix.json",
            _regular_bytes(production_family_matrix_path, label="production family matrix"),
        ),
        "production_catalog": (
            input_root / "production_catalog.json",
            _regular_bytes(catalog_path, label="production catalog"),
        ),
    }
    copy_hashes = {
        name: _immutable(path, payload, label=name.replace("_", " "))
        for name, (path, payload) in copies.items()
    }

    def binding(name: str) -> BoundArtifact:
        path = copies[name][0]
        return BoundArtifact(
            path=_repo_relative(path, repo_root=repo_root, label=name),
            sha256=copy_hashes[name],
        )

    spec = LF022WeakBatchSpec(
        batch_name=batch_name,
        candidate_manifest=binding("candidate_manifest"),
        candidate_records=binding("candidate_records"),
        weak_supervision_config=binding("weak_supervision_config"),
        production_family_matrix=binding("production_family_matrix"),
        randomization_key_sha256=sha256_hex(randomization_key),
        judge_a=JudgeEndpointPin(
            provider_slot="judge_A",
            provider="epfl_rcp",
            model=judge_a_route.model_id,
            family_id=judge_a_route.family_id,
            revision=_provider_revision(matrix, judge_a_route.family_id),
            decoding=cast(
                dict[str, DecodingValue],
                judge_a_route.decoding.provider_decoding(),
            ),
        ),
        judge_b=JudgeEndpointPin(
            provider_slot="judge_B",
            provider="epfl_rcp",
            model=judge_b_route.model_id,
            family_id=judge_b_route.family_id,
            revision=_provider_revision(matrix, judge_b_route.family_id),
            decoding=cast(
                dict[str, DecodingValue],
                judge_b_route.decoding.provider_decoding(),
            ),
        ),
        primary_eval_family_id=_HELDOUT_FAMILY,
    )
    _validate_family_pins(spec, copies["production_family_matrix"][0])
    spec_path = output_dir / "weak_batch_spec.json"
    spec_sha = _immutable(
        spec_path,
        canonical_json_bytes(spec.model_dump(mode="json")) + b"\n",
        label="Qwen weak-batch spec",
    )
    return LF022WeakBatchSpecFreezeResult(
        spec_path=spec_path,
        spec_sha256=spec_sha,
        production_catalog_path=copies["production_catalog"][0],
        production_catalog_sha256=copy_hashes["production_catalog"],
        dispatch_pair_count=manifest.dispatch_eligible_count,
        required_judge_call_count=manifest.required_future_judge_call_count,
    )


__all__ = [
    "LF022WeakBatchSpecFreezeResult",
    "freeze_lf022_qwen_weak_batch_spec",
]
