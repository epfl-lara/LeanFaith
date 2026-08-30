"""Additive SFT2A pilot-readiness, authorization, Git, and historical-seal checks."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.paths import find_repo_root
from leanfaith.sft2a.config import LoadedSFT2AConfig
from leanfaith.sft2a.models import PilotReadinessConfig

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
    config: PilotReadinessConfig
    path: Path
    config_hash: str
    repo_root: Path
    authorization: dict[str, object]
    historical_seal: dict[str, object]


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
    loaded: LoadedConfig[PilotReadinessConfig] = load_config(config_path, PilotReadinessConfig)
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
    return LoadedPilotReadiness(
        config=config,
        path=config_path,
        config_hash=loaded.config_hash,
        repo_root=repo_root,
        authorization=authorization,
        historical_seal=historical,
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
