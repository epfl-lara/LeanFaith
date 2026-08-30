"""Additive SFT2A pilot-readiness, authorization, Git, and historical-seal checks."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.models import (
    AuthorizedProductionPilotReadinessConfig,
    PilotReadinessConfig,
    ProductionPilotReadinessConfig,
    SFT2AProductionConfig,
)

DEFAULT_PILOT_READINESS_PATH = Path("configs/sft2a/pilot_readiness_v2.yaml")

_OWNED_REPOSITORY_PATHS = (
    "plans/40_sft2_llm_transforms.md",
    "src/leanfaith/sft2a",
    "configs/sft2a",
    "prompts/sft2a",
    "tests/unit/sft2a",
)


class PilotReadinessError(RuntimeError):
    """A readiness artifact, authorization, Git identity, or historical seal differs."""


@dataclass(frozen=True, slots=True)
class LoadedPilotReadiness:
    config: (
        PilotReadinessConfig
        | ProductionPilotReadinessConfig
        | AuthorizedProductionPilotReadinessConfig
    )
    path: Path
    config_hash: str
    repo_root: Path
    authorization: dict[str, object]
    historical_seal: dict[str, object]
    exact_settings_smoke: dict[str, object] | None


def _repo_file(repo_root: Path, relative: str, expected_sha256: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise PilotReadinessError(f"unsafe readiness artifact path: {relative!r}")
    path = repo_root.joinpath(*pure.parts)
    if path.is_symlink() or not path.is_file() or hash_file(path) != expected_sha256:
        raise PilotReadinessError(f"readiness artifact hash differs: {relative}")
    return path


def _object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotReadinessError(f"invalid readiness JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PilotReadinessError(f"readiness JSON root is not an object: {path}")
    return value


def verify_historical_fable_seal(document: dict[str, object]) -> str:
    staging_value = document.get("staging_root")
    roots_value = document.get("sealed_roots")
    if not isinstance(staging_value, str) or not isinstance(roots_value, list):
        raise PilotReadinessError("historical Fable seal lacks staging root or sealed roots")
    if any(not isinstance(value, str) or not value for value in roots_value):
        raise PilotReadinessError("historical Fable seal has malformed roots")
    staging = Path(staging_value)
    files: list[Path] = []
    for relative in roots_value:
        assert isinstance(relative, str)
        root = staging / relative
        if not root.is_dir() or root.is_symlink():
            raise PilotReadinessError(f"historical Fable sealed root is unavailable: {root}")
        files.extend(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    lines = b"".join(f"{hash_file(path)}  {path}\n".encode() for path in sorted(files, key=str))
    observed = hashlib.sha256(lines).hexdigest()
    if observed != document.get("combined_tree_sha256"):
        raise PilotReadinessError("historical Fable combined-tree seal differs")
    receipts = document.get("required_file_receipts")
    if not isinstance(receipts, dict):
        raise PilotReadinessError("historical Fable seal lacks required file receipts")
    for relative, expected in receipts.items():
        if (
            not isinstance(relative, str)
            or not isinstance(expected, str)
            or hash_file(staging / relative) != expected
        ):
            raise PilotReadinessError(f"historical Fable required receipt differs: {relative}")
    return observed


def load_pilot_readiness(
    base: LoadedSFT2AConfig,
    path: Path | None = None,
) -> LoadedPilotReadiness:
    repo_root = find_repo_root(Path.cwd())
    config_path = path or repo_root / DEFAULT_PILOT_READINESS_PATH
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    raw = load_yaml_mapping(config_path)
    if raw.get("config_id") == "leanfaith_sft2a_diverse_root_opus5_pilot_v2":
        loaded: LoadedConfig[
            PilotReadinessConfig
            | ProductionPilotReadinessConfig
            | AuthorizedProductionPilotReadinessConfig
        ] = load_config(config_path, PilotReadinessConfig)
    elif raw.get("config_id") == "leanfaith_sft2a_production_defaults_pilot_v1":
        loaded = load_config(config_path, ProductionPilotReadinessConfig)
    elif raw.get("config_id") == "leanfaith_sft2a_production_defaults_pilot_v2":
        loaded = load_config(config_path, AuthorizedProductionPilotReadinessConfig)
    else:
        raise PilotReadinessError("unsupported pilot readiness config ID")
    config = loaded.config
    base_path = _repo_file(
        repo_root,
        config.base_opus_smoke_config.path,
        config.base_opus_smoke_config.sha256,
    )
    if (
        base.path.resolve() != base_path.resolve()
        or base.config_hash != config.base_opus_smoke_config_hash
    ):
        raise PilotReadinessError("pilot readiness is not bound to the loaded Opus smoke config")
    _repo_file(repo_root, config.catalog.path, config.catalog.sha256)
    authorization = _object(
        _repo_file(
            repo_root,
            config.authorization_receipt.path,
            config.authorization_receipt.sha256,
        )
    )
    ceilings_hash = hash_canonical(config.ceilings.model_dump(mode="json"))
    if (
        authorization.get("pilot_config_id") != config.config_id
        or authorization.get("sample_sha256") != config.expected_sample_sha256
        or authorization.get("ceilings_sha256") != ceilings_hash
    ):
        raise PilotReadinessError("pilot authorization receipt bindings differ")
    historical = _object(
        _repo_file(
            repo_root,
            config.historical_fable_seal.path,
            config.historical_fable_seal.sha256,
        )
    )
    verify_historical_fable_seal(historical)
    exact_settings_smoke: dict[str, object] | None = None
    if isinstance(config, ProductionPilotReadinessConfig):
        if not isinstance(base.config, SFT2AProductionConfig):
            raise PilotReadinessError("production readiness requires the production config")
        policy_binding = config.labeling_defaults_policy
        smoke_binding = config.exact_settings_smoke_receipt
        _repo_file(repo_root, policy_binding.path, policy_binding.sha256)
        if policy_binding != base.config.labeling_defaults_policy:
            raise PilotReadinessError("readiness policy differs from the production config")
        exact_settings_smoke = _object(
            _repo_file(repo_root, smoke_binding.path, smoke_binding.sha256)
        )
        run_root = Path(base.config.staging_root) / base.config.run_layout.run_output_subdir
        expected_smoke = {
            "production_config_sha256": hash_file(base.path),
            "production_config_hash": base.config_hash,
            "labeling_defaults_policy_sha256": policy_binding.sha256,
            "one_root_manifest_sha256": hash_file(run_root / "manifest.json"),
            "one_root_replay_receipt_sha256": hash_file(run_root / "reproducibility_receipt.json"),
            "one_root_audit_manifest_sha256": hash_file(run_root / "audit_lemex_v1/manifest.json"),
            "successful": True,
        }
        if any(exact_settings_smoke.get(key) != value for key, value in expected_smoke.items()):
            raise PilotReadinessError("exact-settings smoke receipt or durable output differs")
        providers = exact_settings_smoke.get("providers")
        expected_providers = {
            "proposer": base.config.proposer.model_dump(mode="json"),
            "claude_judge": base.config.claude_judge.model_dump(mode="json"),
            "lemex_auditor": base.config.lemex_auditor.model_dump(mode="json"),
        }
        if providers != expected_providers:
            raise PilotReadinessError("exact-settings smoke provider pins differ")
    if isinstance(config, AuthorizedProductionPilotReadinessConfig):
        activation_plan = load_yaml_mapping(
            _repo_file(
                repo_root,
                config.activation_plan.path,
                config.activation_plan.sha256,
            )
        )
        _repo_file(
            repo_root,
            config.source_readiness_config.path,
            config.source_readiness_config.sha256,
        )
        _repo_file(
            repo_root,
            config.source_authorization_receipt.path,
            config.source_authorization_receipt.sha256,
        )
        if (
            activation_plan.get("activation_id") != "leanfaith_sft2a_production_pilot_activation_v2"
            or activation_plan.get("pilot_launch_currently_authorized") is not False
            or config.source_readiness_config_hash
            != activation_plan.get("source_readiness_config_hash")
            or authorization.get("source_readiness_config_hash")
            != config.source_readiness_config_hash
            or authorization.get("activation_plan_sha256") != config.activation_plan.sha256
            or authorization.get("sample_output_subdir") != config.sample_output_subdir
            or authorization.get("tmux_session") != config.detached_launch.session_name
            or authorization.get("legacy_rejudge_authorized") is not False
            or authorization.get("publication_authorized") is not False
            or authorization.get("scale_50k_authorized") is not False
        ):
            raise PilotReadinessError("authorized activation lineage or scope differs")
    return LoadedPilotReadiness(
        config=config,
        path=config_path,
        config_hash=loaded.config_hash,
        repo_root=repo_root,
        authorization=authorization,
        historical_seal=historical,
        exact_settings_smoke=exact_settings_smoke,
    )


def require_pilot_authorization(readiness: LoadedPilotReadiness) -> None:
    if readiness.authorization.get("authorized") is not True:
        raise PilotReadinessError("hash-bound pilot receipt does not authorize execution")
    if readiness.config.status != "authorized_pilot":
        raise PilotReadinessError("pilot config itself is not authorized")


def implementation_identity(repo_root: Path, *, require_clean: bool = True) -> dict[str, str]:
    if require_clean:
        status = subprocess.run(
            ("git", "status", "--porcelain", "--", *_OWNED_REPOSITORY_PATHS),
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if status.strip():
            raise PilotReadinessError("SFT2A-owned repository paths are not fully committed")
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD^{commit}"),
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ("git", "rev-parse", "HEAD^{tree}"),
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"implementation_commit": commit, "implementation_tree": tree}


__all__ = [
    "DEFAULT_PILOT_READINESS_PATH",
    "LoadedPilotReadiness",
    "PilotReadinessError",
    "implementation_identity",
    "load_pilot_readiness",
    "require_pilot_authorization",
    "verify_historical_fable_seal",
]
