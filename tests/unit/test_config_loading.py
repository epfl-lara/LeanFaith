"""LF-002: strict config loading, unknown-key failure, secret references."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanfaith.config import (
    ConfigError,
    DuplicateKeyError,
    MissingSecretError,
    SecretRef,
    StrictModel,
    load_config,
    load_yaml_mapping,
)


class _Nested(StrictModel):
    retries: int = 3


class _Example(StrictModel):
    name: str
    timeout_seconds: float = 30.0
    nested: _Nested = _Nested()
    token: SecretRef | None = None


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_valid_config(tmp_path: Path) -> None:
    path = _write(tmp_path, "name: probe\ntimeout_seconds: 12.5\n")
    loaded = load_config(path, _Example)
    assert loaded.config.name == "probe"
    assert loaded.config.timeout_seconds == 12.5
    assert loaded.config.nested.retries == 3
    assert loaded.path == path
    assert len(loaded.config_hash) == 64


def test_unknown_top_level_key_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "name: probe\nunexpected: 1\n")
    with pytest.raises(ConfigError, match="unexpected"):
        load_config(path, _Example)


def test_unknown_nested_key_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "name: probe\nnested:\n  retries: 2\n  bogus: true\n")
    with pytest.raises(ConfigError, match="bogus"):
        load_config(path, _Example)


def test_duplicate_key_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "name: a\nname: b\n")
    with pytest.raises(DuplicateKeyError, match="duplicate key 'name'"):
        load_config(path, _Example)


def test_nested_duplicate_key_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "name: a\nnested:\n  retries: 1\n  retries: 2\n")
    with pytest.raises(DuplicateKeyError, match="duplicate key 'retries'"):
        load_config(path, _Example)


def test_non_mapping_root_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "- 1\n- 2\n")
    with pytest.raises(ConfigError, match="root must be a mapping"):
        load_yaml_mapping(path)


def test_invalid_yaml_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "name: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_yaml_mapping(path)


def test_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        load_yaml_mapping(tmp_path / "absent.yaml")


def test_error_message_names_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "name: 1\nname: 2\n")
    with pytest.raises(DuplicateKeyError, match=r"config\.yaml"):
        load_config(path, _Example)


def test_hash_stable_across_key_order(tmp_path: Path) -> None:
    first = load_config(_write(tmp_path, "name: x\ntimeout_seconds: 1.0\n"), _Example)
    reordered = tmp_path / "reordered.yaml"
    reordered.write_text("timeout_seconds: 1.0\nname: x\n", encoding="utf-8")
    second = load_config(reordered, _Example)
    assert first.config_hash == second.config_hash


def test_hash_changes_with_content(tmp_path: Path) -> None:
    first = load_config(_write(tmp_path, "name: x\n"), _Example)
    other = tmp_path / "other.yaml"
    other.write_text("name: y\n", encoding="utf-8")
    second = load_config(other, _Example)
    assert first.config_hash != second.config_hash


def test_secret_ref_resolves_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEANFAITH_TEST_TOKEN", "s3cret")
    path = _write(tmp_path, "name: x\ntoken:\n  env: LEANFAITH_TEST_TOKEN\n")
    loaded = load_config(path, _Example)
    assert loaded.config.token is not None
    assert loaded.config.token.resolve() == "s3cret"


def test_secret_value_never_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEANFAITH_TEST_TOKEN", "s3cret")
    path = _write(tmp_path, "name: x\ntoken:\n  env: LEANFAITH_TEST_TOKEN\n")
    loaded = load_config(path, _Example)
    dumped = loaded.config.model_dump_json()
    assert "s3cret" not in dumped
    assert "s3cret" not in repr(loaded.config)
    assert "s3cret" not in loaded.config_hash


def test_missing_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEANFAITH_TEST_TOKEN", raising=False)
    ref = SecretRef(env="LEANFAITH_TEST_TOKEN")
    with pytest.raises(MissingSecretError, match="LEANFAITH_TEST_TOKEN"):
        ref.resolve()


def test_empty_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEANFAITH_TEST_TOKEN", "")
    with pytest.raises(MissingSecretError):
        SecretRef(env="LEANFAITH_TEST_TOKEN").resolve()


def test_secret_env_name_pattern_enforced() -> None:
    with pytest.raises(ValueError, match="pattern"):
        SecretRef(env="lowercase")


def test_models_are_frozen() -> None:
    config = _Example(name="x")
    with pytest.raises(Exception, match="frozen"):
        config.name = "y"  # type: ignore[misc]
