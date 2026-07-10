"""Strict YAML config loading (PLAN.md LF-002).

Loading fails closed: duplicate keys, non-mapping roots, YAML syntax errors,
and unknown/invalid fields all raise ``ConfigError`` subclasses that name the
offending file. A successful load returns the validated model together with
the raw mapping and a canonical content hash for run manifests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.models import StrictModel


class ConfigError(ValueError):
    """Raised when a config file cannot be loaded or validated."""


class DuplicateKeyError(ConfigError):
    """Raised when a YAML mapping repeats a key."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys at any depth."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[object] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if isinstance(key, dict | list):
            raise ConfigError(f"unhashable mapping key at {key_node.start_mark}")
        if key in seen:
            raise DuplicateKeyError(f"duplicate key {key!r} at {key_node.start_mark}")
        seen.add(key)
    return dict(loader.construct_pairs(node, deep=True))


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _check_node_types(value: object, path: Path, location: str) -> None:
    """Recursively restrict loaded YAML to JSON-shaped nodes.

    SafeLoader can produce sets (``!!set``), dates/datetimes, and bytes
    (``!!binary``); all are rejected so configs stay deterministic and
    canonical-JSON hashable. Dates must be written as quoted strings.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_node_types(item, path, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigError(f"non-string key {key!r} at {location} in {path}")
            _check_node_types(item, path, f"{location}.{key}")
        return
    raise ConfigError(
        f"unsupported YAML node type {type(value).__name__} at {location} in {path}; "
        "only null/bool/int/float/string/list/mapping are allowed (quote dates)"
    )


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load a YAML file that must contain a single mapping document."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        document = yaml.load(text, Loader=_StrictLoader)
    except DuplicateKeyError as exc:
        raise DuplicateKeyError(f"{path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"config root must be a mapping in {path}, got {type(document).__name__}")
    _check_node_types(document, path, "$")
    return document


@dataclass(frozen=True, slots=True)
class LoadedConfig[ModelT: StrictModel]:
    """A validated config plus its provenance."""

    config: ModelT
    path: Path
    raw: dict[str, Any]
    config_hash: str


def load_config[ModelT: StrictModel](path: Path, model_type: type[ModelT]) -> LoadedConfig[ModelT]:
    """Load and validate ``path`` against ``model_type``; compute its content hash.

    The hash covers the validated model dump (defaults included), so two files
    that resolve to the same effective configuration hash identically and any
    semantic change — including a changed default — changes the hash.
    """
    raw = load_yaml_mapping(path)
    try:
        config = model_type.model_validate(raw)
    except ValidationError as exc:
        # Never echo raw input values: a mistyped secret literal must not
        # leak into logs or persisted doctor reports (§6.1).
        details = "; ".join(
            f"{'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"invalid config {path}: {details}") from exc
    config_hash = hash_canonical(config.model_dump(mode="json"))
    return LoadedConfig(config=config, path=path, raw=raw, config_hash=config_hash)
