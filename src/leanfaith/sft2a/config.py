"""Load and verify the frozen SFT2A one-root contract."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from leanfaith.config.hashing import hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.paths import find_repo_root
from leanfaith.sft2a.models import (
    ArtifactBinding,
    ProviderPin,
    SFT2AConfig,
    SFT2AOpusConfig,
    SFT2AProductionConfig,
    SFT2AV5Config,
)

DEFAULT_CONFIG_PATH = Path("configs/sft2a/one_root_v1.yaml")
OPUS_CONFIG_PATH = Path("configs/sft2a/one_root_opus5_v1.yaml")
SFT2AAnyConfig = SFT2AConfig | SFT2AOpusConfig | SFT2AProductionConfig | SFT2AV5Config

_EXPECTED_LABELING_DEFAULTS_SHA256 = (
    "4554a071b06b1af9015b253b5e64b2a0a4d013630e5224ef7729bbf65757646f"
)

_EXPECTED_REPR = {
    "freeze_commit": "176a783842c5a73b84413dfa8347670608b615d9",
    "implementation_commit": "93cd9cf9d4848827f2bacad57a35c3d7f01500f7",
    "spec_hash": "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8",
    "renderer_semantic_hash": ("0bec5429cc0e539841208be53cd52189a7b80cbdb4649ee2d45b84bd8a5ef1fd"),
    "implementation_set_hash": ("9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff"),
    "lean_renderer_sha256": ("4471262f812746046570c51dde5958ee33db31a450a6974071efce584ba56bc3"),
    "injected_helper_sha256": ("a6650452eebe683db295df1dfe925d3db8b03fc24e55cbc6793e838b5fe2f272"),
    "python_renderer_sha256": ("496237e190c394e9bd3c3036e2bc01c635905116c5084787a42e6cb569f45517"),
    "frozen_config_sha256": ("a65d5b29760bbc5eb89405927f946f205eb99856c0538fdf5b57d3f9eceb0db7"),
    "universe_profile_hash": ("d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61"),
    "render_context_hash": ("5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62"),
}


class SFT2AConfigError(RuntimeError):
    """A frozen input, binary, schema, or repository pin differs."""


@dataclass(frozen=True, slots=True)
class LoadedSFT2AConfig:
    config: SFT2AAnyConfig
    path: Path
    config_hash: str
    repo_root: Path
    proposer_prompt: str
    judge_prompt: str
    proposer_schema: dict[str, object]
    judge_schema: dict[str, object]


def _repo_artifact(repo_root: Path, binding: ArtifactBinding) -> Path:
    pure = PurePosixPath(binding.path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise SFT2AConfigError(f"unsafe repository artifact path: {binding.path!r}")
    path = (repo_root / Path(*pure.parts)).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise SFT2AConfigError(f"artifact escapes repository: {binding.path!r}") from exc
    if path.is_symlink() or not path.is_file():
        raise SFT2AConfigError(f"artifact is missing or unsafe: {binding.path!r}")
    observed = hash_file(path)
    if observed != binding.sha256:
        raise SFT2AConfigError(
            f"artifact hash mismatch for {binding.path}: {observed} != {binding.sha256}"
        )
    return path


def _strict_json(path: Path) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def nonfinite(value: str) -> float:
        raise ValueError(f"non-finite JSON value {value!r}")

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=nonfinite,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SFT2AConfigError(f"invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SFT2AConfigError(f"JSON artifact root must be an object: {path}")
    return value


def _git_object(repo_root: Path, revision: str) -> str:
    completed = subprocess.run(
        ("git", "rev-parse", f"{revision}^{{commit}}"),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    observed = completed.stdout.strip()
    if completed.returncode != 0 or observed != revision:
        raise SFT2AConfigError(f"required Git commit is unavailable or differs: {revision}")
    return observed


def _git_file_hash(repo_root: Path, revision: str, relative_path: str) -> str:
    completed = subprocess.run(
        ("git", "show", f"{revision}:{relative_path}"),
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SFT2AConfigError(f"cannot read {relative_path} from frozen commit {revision}")
    return sha256_hex(completed.stdout)


def _verify_repr(config: SFT2AAnyConfig, repo_root: Path) -> None:
    repr_pin = config.repr
    observed = {
        "freeze_commit": repr_pin.freeze_commit,
        "implementation_commit": repr_pin.implementation_commit,
        "spec_hash": repr_pin.spec_hash,
        "renderer_semantic_hash": repr_pin.renderer_semantic_hash,
        "implementation_set_hash": repr_pin.implementation_set_hash,
        "lean_renderer_sha256": repr_pin.lean_renderer.sha256,
        "injected_helper_sha256": repr_pin.injected_helper_sha256,
        "python_renderer_sha256": repr_pin.python_renderer.sha256,
        "frozen_config_sha256": repr_pin.frozen_config.sha256,
        "universe_profile_hash": repr_pin.universe_profile_hash,
        "render_context_hash": repr_pin.render_context_hash,
    }
    if observed != _EXPECTED_REPR:
        raise SFT2AConfigError("SFT2A REPR pins differ from the fourth freeze")
    _git_object(repo_root, repr_pin.freeze_commit)
    _git_object(repo_root, repr_pin.implementation_commit)
    for binding in (
        repr_pin.lean_renderer,
        repr_pin.python_renderer,
        repr_pin.frozen_config,
    ):
        _repo_artifact(repo_root, binding)
        frozen_hash = _git_file_hash(repo_root, repr_pin.freeze_commit, binding.path)
        if frozen_hash != binding.sha256:
            raise SFT2AConfigError(
                f"freeze commit bytes differ for {binding.path}: {frozen_hash} != {binding.sha256}"
            )


def _verify_labeling_defaults(config: SFT2AProductionConfig, repo_root: Path) -> None:
    binding = config.labeling_defaults_policy
    if binding.sha256 != _EXPECTED_LABELING_DEFAULTS_SHA256:
        raise SFT2AConfigError("production config is not bound to the active SFT2 defaults")
    path = _repo_artifact(repo_root, binding)
    policy = load_yaml_mapping(path)
    if policy.get("status") != "active_default":
        raise SFT2AConfigError("bound SFT2 labeling policy is not active")
    providers = policy.get("providers")
    if not isinstance(providers, dict):
        raise SFT2AConfigError("bound SFT2 labeling policy lacks providers")
    expected = {
        "claude": config.claude_judge,
        "codex": config.proposer,
        "lemex": config.lemex_auditor,
    }
    for name, pin in expected.items():
        provider = providers.get(name)
        if not isinstance(provider, dict):
            raise SFT2AConfigError(f"bound SFT2 labeling policy lacks {name}")
        observed = {
            "cli": provider.get("cli"),
            "model": provider.get("model"),
            "effort": provider.get("effort"),
            "server_revision_status": provider.get("server_revision_status"),
        }
        frozen = {
            "cli": pin.cli,
            "model": pin.model,
            "effort": pin.effort,
            "server_revision_status": pin.server_revision_status,
        }
        if observed != frozen:
            raise SFT2AConfigError(f"production {name} pin differs from active defaults")


def _verify_v5_inputs(config: SFT2AV5Config, repo_root: Path) -> None:
    seal_path = _repo_artifact(repo_root, config.recovery_v4_seal)
    seal = _strict_json(seal_path)
    staging_value = seal.get("staging_root")
    roots_value = seal.get("sealed_roots")
    if (
        seal.get("receipt_id") != "leanfaith_sft2a_recovery_v4_combined_tree_seal_v5"
        or not isinstance(staging_value, str)
        or roots_value != ["runs/diverse_root_production_defaults_pilot_recovery_v4"]
    ):
        raise SFT2AConfigError("v5 recovery-v4 seal contract differs")
    staging = Path(staging_value)
    files = [
        path
        for relative in roots_value
        for path in (staging / str(relative)).rglob("*")
        if path.is_file() and not path.is_symlink()
    ]
    lines = b"".join(f"{hash_file(path)}  {path}\n".encode() for path in sorted(files, key=str))
    observed = hashlib.sha256(lines).hexdigest()
    if observed != seal.get("combined_tree_sha256"):
        raise SFT2AConfigError("recovery-v4 historical evidence differs from its v5 seal")
    _repo_artifact(repo_root, config.closure_canaries)
    census_input = Path(config.source_census.compiler_data_path)
    if (
        census_input.is_symlink()
        or not census_input.is_file()
        or hash_file(census_input) != config.source_census.compiler_data_sha256
    ):
        raise SFT2AConfigError("v5 compiler-data census input differs")
    if config.rehearsal.authorized:
        raise SFT2AConfigError("v5 base config must not authorize the 100-root rehearsal")


def verify_provider_binary(pin: ProviderPin) -> None:
    configured = Path(pin.binary_path).resolve(strict=True)
    discovered_text = shutil.which(pin.cli)
    if discovered_text is None or Path(discovered_text).resolve(strict=True) != configured:
        raise SFT2AConfigError(f"{pin.cli} executable path differs from its frozen pin")
    if hash_file(configured) != pin.binary_sha256:
        raise SFT2AConfigError(f"{pin.cli} executable bytes differ from the frozen pin")
    completed = subprocess.run(
        (str(configured), "--version"),
        check=False,
        capture_output=True,
        timeout=30,
    )
    version = (completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip()
    if completed.returncode != 0 or version != pin.cli_version:
        raise SFT2AConfigError(
            f"{pin.cli} version mismatch: observed={version!r}, expected={pin.cli_version!r}"
        )


def load_sft2a_config(
    path: Path | None = None,
    *,
    verify_binaries: bool = False,
) -> LoadedSFT2AConfig:
    repo_root = find_repo_root(Path.cwd())
    config_path = path or repo_root / DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = repo_root / config_path
    raw = load_yaml_mapping(config_path)
    config_id = raw.get("config_id")
    if config_id == "leanfaith_sft2a_one_root_v1":
        loaded: LoadedConfig[SFT2AAnyConfig] = load_config(config_path, SFT2AConfig)
    elif config_id == "leanfaith_sft2a_one_root_opus5_v1":
        loaded = load_config(config_path, SFT2AOpusConfig)
    elif config_id == "leanfaith_sft2a_production_pilot_v1":
        loaded = load_config(config_path, SFT2AProductionConfig)
    elif config_id == "leanfaith_sft2a_closure_aware_v5":
        loaded = load_config(config_path, SFT2AV5Config)
    else:
        raise SFT2AConfigError(f"unsupported SFT2A config_id: {config_id!r}")
    config = loaded.config
    _verify_repr(config, repo_root)
    if isinstance(config, SFT2AProductionConfig):
        _verify_labeling_defaults(config, repo_root)
    if isinstance(config, SFT2AV5Config):
        _verify_v5_inputs(config, repo_root)
    proposer_prompt_path = _repo_artifact(repo_root, config.prompts.codex_proposer)
    judge_prompt_path = _repo_artifact(repo_root, config.prompts.blinded_claude_judge)
    proposer_schema_path = _repo_artifact(repo_root, config.schemas.codex_proposer_output)
    judge_schema_path = _repo_artifact(repo_root, config.schemas.blinded_judge_output)
    if verify_binaries:
        for pin in (config.proposer, config.claude_judge, config.lemex_auditor):
            verify_provider_binary(pin)
    return LoadedSFT2AConfig(
        config=config,
        path=config_path,
        config_hash=loaded.config_hash,
        repo_root=repo_root,
        proposer_prompt=proposer_prompt_path.read_text(encoding="utf-8"),
        judge_prompt=judge_prompt_path.read_text(encoding="utf-8"),
        proposer_schema=_strict_json(proposer_schema_path),
        judge_schema=_strict_json(judge_schema_path),
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "OPUS_CONFIG_PATH",
    "LoadedSFT2AConfig",
    "SFT2AConfigError",
    "load_sft2a_config",
    "verify_provider_binary",
]
