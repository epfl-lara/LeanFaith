from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.cli.export_annotation import (
    AnnotationExportInputError,
    _read_randomization_keys,
)


def test_randomization_key_reader_requires_two_distinct_binary_keys(tmp_path: Path) -> None:
    first = tmp_path / "first.key"
    second = tmp_path / "second.key"
    first.write_bytes(b"a" * 32)
    second.write_bytes(b"b" * 32)

    assert _read_randomization_keys(()) is None
    assert _read_randomization_keys((first, second)) == (b"a" * 32, b"b" * 32)

    with pytest.raises(AnnotationExportInputError, match="exactly two"):
        _read_randomization_keys((first,))
    with pytest.raises(AnnotationExportInputError, match="distinct"):
        _read_randomization_keys((first, first))


def test_export_annotation_cli_is_registered() -> None:
    result = CliRunner().invoke(app, ["export-annotation", "--help"])
    assert result.exit_code == 0
    assert "two independently randomized" in result.stdout
    assert "--randomization-key" in result.stdout


def test_training_readiness_cli_is_registered() -> None:
    result = CliRunner().invoke(app, ["audit-training-readiness", "--help"])
    assert result.exit_code == 0
    assert "scientifically ready to train" in result.stdout
    assert "--reduced-data-ablation" in result.stdout
