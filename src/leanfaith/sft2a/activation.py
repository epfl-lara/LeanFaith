"""Fail-closed, additive authorization transition for the production SFT2A pilot."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import load_config
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.legacy import _atomic_exact
from leanfaith.sft2a.models import (
    AuthorizedProductionPilotReadinessConfig,
    PilotActivationPlan,
    ProductionPilotReadinessConfig,
    SFT2AProductionConfig,
)
from leanfaith.sft2a.readiness import (
    LoadedPilotReadiness,
    implementation_identity,
    load_pilot_readiness,
)

DEFAULT_ACTIVATION_PLAN_PATH = Path("configs/sft2a/pilot_activation_production_v2.yaml")
_HEX40 = re.compile(r"[0-9a-f]{40}")


class PilotActivationError(RuntimeError):
    """The historical state, exact sentence, or additive activation differs."""


@dataclass(frozen=True, slots=True)
class LoadedPilotActivation:
    plan: PilotActivationPlan
    path: Path
    plan_file_sha256: str
    plan_hash: str
    loaded: LoadedSFT2AConfig
    source_readiness: LoadedPilotReadiness
    source_sample_manifest: dict[str, object]
    expected_authorization_sentence: str


@dataclass(frozen=True, slots=True)
class AuthorizedActivationArtifacts:
    authorization_receipt: dict[str, object]
    authorization_receipt_bytes: bytes
    authorization_receipt_sha256: str
    readiness: AuthorizedProductionPilotReadinessConfig
    readiness_bytes: bytes
    readiness_file_sha256: str
    readiness_hash: str
    output_root: Path
    tmux_session: str


def _repo_path(repo_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise PilotActivationError(f"unsafe activation artifact path: {relative!r}")
    path = repo_root.joinpath(*pure.parts)
    if path.is_symlink():
        raise PilotActivationError(f"activation artifact path is a symlink: {relative}")
    return path


def _bound_file(repo_root: Path, relative: str, expected_sha256: str) -> Path:
    path = _repo_path(repo_root, relative)
    if not path.is_file() or hash_file(path) != expected_sha256:
        raise PilotActivationError(f"activation-bound artifact differs: {relative}")
    return path


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotActivationError(f"invalid activation JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PilotActivationError(f"activation JSON root is not an object: {path}")
    return value


def _identity(value: Mapping[str, str]) -> dict[str, str]:
    result = {
        "implementation_commit": value.get("implementation_commit", ""),
        "implementation_tree": value.get("implementation_tree", ""),
    }
    if any(_HEX40.fullmatch(item) is None for item in result.values()):
        raise PilotActivationError("activation implementation identity is malformed")
    return result


def load_pilot_activation(
    loaded: LoadedSFT2AConfig,
    path: Path | None = None,
) -> LoadedPilotActivation:
    """Verify the frozen unauthorized source state without authorizing anything."""

    if not isinstance(loaded.config, SFT2AProductionConfig):
        raise PilotActivationError("pilot activation requires the production-default config")
    repo_root = loaded.repo_root
    plan_path = path or repo_root / DEFAULT_ACTIVATION_PLAN_PATH
    if not plan_path.is_absolute():
        plan_path = repo_root / plan_path
    plan_loaded = load_config(plan_path, PilotActivationPlan)
    plan = plan_loaded.config
    production_path = _bound_file(
        repo_root, plan.production_config.path, plan.production_config.sha256
    )
    if (
        production_path.resolve() != loaded.path.resolve()
        or plan.production_config_hash != loaded.config_hash
    ):
        raise PilotActivationError("activation production config lineage differs")
    source_path = _bound_file(
        repo_root,
        plan.source_readiness_config.path,
        plan.source_readiness_config.sha256,
    )
    source = load_pilot_readiness(loaded, source_path)
    if not isinstance(source.config, ProductionPilotReadinessConfig):
        raise PilotActivationError("activation source is not production readiness")
    if source.config_hash != plan.source_readiness_config_hash:
        raise PilotActivationError("activation source readiness hash differs")
    if (
        source.config.status != "ready_not_authorized"
        or source.authorization.get("authorized") is not False
    ):
        raise PilotActivationError("activation source is no longer the frozen unauthorized state")
    _bound_file(
        repo_root,
        plan.source_authorization_receipt.path,
        plan.source_authorization_receipt.sha256,
    )
    if source.config.authorization_receipt != plan.source_authorization_receipt:
        raise PilotActivationError("activation source authorization receipt differs")
    for observed, expected, label in (
        (loaded.config.labeling_defaults_policy, plan.labeling_defaults_policy, "policy"),
        (source.config.catalog, plan.catalog, "catalog"),
        (
            source.config.exact_settings_smoke_receipt,
            plan.exact_settings_smoke_receipt,
            "exact-settings smoke",
        ),
    ):
        if observed != expected:
            raise PilotActivationError(f"activation {label} binding differs")
        _bound_file(repo_root, expected.path, expected.sha256)
    if (
        source.config.expected_sample_sha256 != plan.expected_sample_sha256
        or source.config.ceilings != plan.ceilings
        or hash_canonical(plan.ceilings.model_dump(mode="json")) != plan.ceilings_sha256
    ):
        raise PilotActivationError("activation sample or ceilings differ")
    source_output = Path(loaded.config.staging_root) / source.config.sample_output_subdir
    sample_path = source_output / "sample.jsonl"
    manifest_path = source_output / "sample_manifest.json"
    if hash_file(sample_path) != plan.expected_sample_sha256:
        raise PilotActivationError("historical unauthorized staged sample differs")
    if hash_file(manifest_path) != plan.source_staged_sample_manifest_sha256:
        raise PilotActivationError("historical unauthorized sample manifest differs")
    sample_manifest = _object(manifest_path)
    implementation = sample_manifest.get("implementation")
    expected_implementation = {
        "implementation_commit": plan.source_sample_implementation_commit,
        "implementation_tree": plan.source_sample_implementation_tree,
    }
    readiness = sample_manifest.get("readiness")
    if (
        sample_manifest.get("sample_sha256") != plan.expected_sample_sha256
        or sample_manifest.get("pilot_authorized") is not False
        or implementation != expected_implementation
        or not isinstance(readiness, dict)
        or readiness.get("config_hash") != source.config_hash
        or readiness.get("authorization_receipt_sha256") != plan.source_authorization_receipt.sha256
    ):
        raise PilotActivationError("historical unauthorized sample lineage differs")
    source_receipt = source.authorization
    sentence = source_receipt.get("required_authorization_sentence")
    if (
        not isinstance(sentence, str)
        or sha256_hex(sentence.encode()) != plan.authorization_sentence_sha256
    ):
        raise PilotActivationError("activation authorization sentence binding differs")
    if any(
        (
            plan.pilot_launch_currently_authorized,
            plan.legacy_rejudge_authorized,
            plan.publication_authorized,
            plan.scale_50k_authorized,
        )
    ):
        raise PilotActivationError("activation plan unexpectedly authorizes an execution scope")
    source_output_resolved = source_output.resolve()
    target_output = (Path(loaded.config.staging_root) / plan.fresh_sample_output_subdir).resolve()
    if source_output_resolved == target_output:
        raise PilotActivationError("activation must use a fresh pilot output root")
    return LoadedPilotActivation(
        plan=plan,
        path=plan_path,
        plan_file_sha256=hash_file(plan_path),
        plan_hash=plan_loaded.config_hash,
        loaded=loaded,
        source_readiness=source,
        source_sample_manifest=sample_manifest,
        expected_authorization_sentence=sentence,
    )


def build_authorized_activation(
    activation: LoadedPilotActivation,
    *,
    authorization_sentence: str,
    implementation: Mapping[str, str],
) -> AuthorizedActivationArtifacts:
    """Build deterministic v2 bytes only when the exact authorization sentence matches."""

    if not hmac.compare_digest(authorization_sentence, activation.expected_authorization_sentence):
        raise PilotActivationError("exact pilot authorization sentence was not supplied")
    identity = _identity(implementation)
    plan = activation.plan
    receipt: dict[str, object] = {
        "schema_version": 1,
        "receipt_id": "leanfaith_sft2a_pilot_authorization_receipt_production_v2",
        "pilot_config_id": plan.target_config_id,
        "source_readiness_config": plan.source_readiness_config.model_dump(mode="json"),
        "source_readiness_config_hash": plan.source_readiness_config_hash,
        "source_authorization_receipt": plan.source_authorization_receipt.model_dump(mode="json"),
        "activation_plan_sha256": activation.plan_file_sha256,
        "activation_plan_hash": activation.plan_hash,
        "activation_implementation": identity,
        "production_config_file_sha256": plan.production_config.sha256,
        "production_config_hash": plan.production_config_hash,
        "labeling_defaults_policy_sha256": plan.labeling_defaults_policy.sha256,
        "exact_settings_smoke_receipt_sha256": plan.exact_settings_smoke_receipt.sha256,
        "sample_sha256": plan.expected_sample_sha256,
        "ceilings_sha256": plan.ceilings_sha256,
        "authorization_scope": "12_root_production_default_pilot_only",
        "authorized": True,
        "exact_authorization_sentence": authorization_sentence,
        "exact_authorization_sentence_sha256": plan.authorization_sentence_sha256,
        "sample_output_subdir": plan.fresh_sample_output_subdir,
        "tmux_session": plan.authorized_detached_launch.session_name,
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "scale_50k_authorized": False,
    }
    receipt_bytes = canonical_json_bytes(receipt) + b"\n"
    receipt_sha256 = sha256_hex(receipt_bytes)
    source_config = activation.source_readiness.config.model_dump(mode="json")
    source_config.update(
        {
            "config_id": plan.target_config_id,
            "status": "authorized_pilot",
            "sample_output_subdir": plan.fresh_sample_output_subdir,
            "authorization_receipt": {
                "path": plan.target_authorization_receipt_path,
                "sha256": receipt_sha256,
            },
            "detached_launch": plan.authorized_detached_launch.model_dump(mode="json"),
            "activation_plan": {
                "path": str(activation.path.relative_to(activation.loaded.repo_root)),
                "sha256": activation.plan_file_sha256,
            },
            "source_readiness_config": plan.source_readiness_config.model_dump(mode="json"),
            "source_readiness_config_hash": plan.source_readiness_config_hash,
            "source_authorization_receipt": plan.source_authorization_receipt.model_dump(
                mode="json"
            ),
        }
    )
    readiness = AuthorizedProductionPilotReadinessConfig.model_validate(source_config)
    readiness_mapping = readiness.model_dump(mode="json")
    readiness_bytes = yaml.safe_dump(
        readiness_mapping,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).encode()
    return AuthorizedActivationArtifacts(
        authorization_receipt=receipt,
        authorization_receipt_bytes=receipt_bytes,
        authorization_receipt_sha256=receipt_sha256,
        readiness=readiness,
        readiness_bytes=readiness_bytes,
        readiness_file_sha256=sha256_hex(readiness_bytes),
        readiness_hash=hash_canonical(readiness_mapping),
        output_root=Path(activation.loaded.config.staging_root) / plan.fresh_sample_output_subdir,
        tmux_session=plan.authorized_detached_launch.session_name,
    )


def preview_authorized_activation(
    activation: LoadedPilotActivation,
    *,
    implementation: Mapping[str, str] | None = None,
) -> AuthorizedActivationArtifacts:
    """Compute prospective authorized hashes without writing or authorizing files."""

    identity = implementation or implementation_identity(activation.loaded.repo_root)
    return build_authorized_activation(
        activation,
        authorization_sentence=activation.expected_authorization_sentence,
        implementation=identity,
    )


def materialize_authorized_activation(
    activation: LoadedPilotActivation,
    *,
    authorization_sentence: str,
) -> AuthorizedActivationArtifacts:
    """Write new immutable v2 files after an exact user authorization message."""

    artifacts = build_authorized_activation(
        activation,
        authorization_sentence=authorization_sentence,
        implementation=implementation_identity(activation.loaded.repo_root),
    )
    receipt_path = _repo_path(
        activation.loaded.repo_root,
        activation.plan.target_authorization_receipt_path,
    )
    readiness_path = _repo_path(
        activation.loaded.repo_root,
        activation.plan.target_readiness_config_path,
    )
    _atomic_exact(receipt_path, artifacts.authorization_receipt_bytes)
    _atomic_exact(readiness_path, artifacts.readiness_bytes)
    return artifacts


def activation_summary(
    activation: LoadedPilotActivation,
    artifacts: AuthorizedActivationArtifacts,
) -> dict[str, object]:
    """Return a no-write preview or post-materialization handoff."""

    repo_root = activation.loaded.repo_root
    receipt_path = _repo_path(repo_root, activation.plan.target_authorization_receipt_path)
    readiness_path = _repo_path(repo_root, activation.plan.target_readiness_config_path)
    return {
        "activation_id": activation.plan.activation_id,
        "status": activation.plan.status,
        "current_launch_authorized": False,
        "source_readiness_sha256": activation.plan.source_readiness_config.sha256,
        "source_authorization_receipt_sha256": activation.plan.source_authorization_receipt.sha256,
        "source_sample_manifest_sha256": activation.plan.source_staged_sample_manifest_sha256,
        "sample_sha256": activation.plan.expected_sample_sha256,
        "prospective_authorization_receipt_sha256": artifacts.authorization_receipt_sha256,
        "prospective_readiness_file_sha256": artifacts.readiness_file_sha256,
        "prospective_readiness_hash": artifacts.readiness_hash,
        "target_authorization_receipt_path": str(receipt_path),
        "target_readiness_config_path": str(readiness_path),
        "target_files_materialized": receipt_path.is_file() or readiness_path.is_file(),
        "fresh_output_root": str(artifacts.output_root),
        "tmux_session": artifacts.tmux_session,
        "provider_calls_executed": 0,
        "lean_requests_executed": 0,
        "legacy_rejudge_authorized": False,
        "publication_authorized": False,
        "scale_50k_authorized": False,
    }


__all__ = [
    "DEFAULT_ACTIVATION_PLAN_PATH",
    "AuthorizedActivationArtifacts",
    "LoadedPilotActivation",
    "PilotActivationError",
    "activation_summary",
    "build_authorized_activation",
    "load_pilot_activation",
    "materialize_authorized_activation",
    "preview_authorized_activation",
]
