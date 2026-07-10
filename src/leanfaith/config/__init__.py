"""Configuration loading, hashing, paths, and logging (PLAN.md LF-002)."""

from leanfaith.config.hashing import (
    CanonicalizationError,
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.config.loading import (
    ConfigError,
    DuplicateKeyError,
    LoadedConfig,
    load_config,
    load_yaml_mapping,
)
from leanfaith.config.models import MissingSecretError, SecretRef, StrictModel
from leanfaith.config.paths import RepoPaths, find_repo_root

__all__ = [
    "CanonicalizationError",
    "ConfigError",
    "DuplicateKeyError",
    "LoadedConfig",
    "MissingSecretError",
    "RepoPaths",
    "SecretRef",
    "StrictModel",
    "canonical_json_bytes",
    "find_repo_root",
    "hash_canonical",
    "hash_file",
    "load_config",
    "load_yaml_mapping",
    "sha256_hex",
]
