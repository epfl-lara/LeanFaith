"""Fast, GPU-free tests for the Track T-S0 MLM runner: config + field detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.train2.mlm_cpt import (
    TEXT_FIELD_CANDIDATES,
    MlmCptConfig,
    detect_text_field,
    load_text_records,
)

_PATHS = {
    "input_jsonl": Path("/tmp/in.jsonl"),
    "snapshot_dir": Path("/tmp/snap"),
    "out_dir": Path("/tmp/out"),
}


def _config(**overrides: object) -> MlmCptConfig:
    return MlmCptConfig.model_validate({**_PATHS, "max_steps": 10, **overrides})


class TestDetectTextField:
    def test_detects_each_candidate(self) -> None:
        for name in TEXT_FIELD_CANDIDATES:
            assert detect_text_field({name: "theorem t : True := trivial"}) == name

    def test_prefers_text_over_later_candidates(self) -> None:
        record = {"source_text": "b", "content": "a", "text": "c"}
        assert detect_text_field(record) == "text"

    def test_skips_empty_or_non_string_values(self) -> None:
        record = {"text": "   ", "content": 7, "source_text": "theorem"}
        assert detect_text_field(record) == "source_text"

    def test_rejects_record_without_text(self) -> None:
        with pytest.raises(ValueError, match="no usable text field"):
            detect_text_field({"id": "x", "chars": 12})


class TestLoadTextRecords:
    def test_loads_and_reports_detected_field(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        rows = [{"content": "theorem a : True := trivial"}, {"content": "lemma b : 1 = 1 := rfl"}]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n\n", encoding="utf-8")
        texts, field = load_text_records(path)
        assert field == "content"
        assert texts == [row["content"] for row in rows]

    def test_rejects_record_missing_the_detected_field(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        path.write_text(
            json.dumps({"text": "ok"}) + "\n" + json.dumps({"content": "other"}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="'text' is missing or empty"):
            load_text_records(path)

    def test_rejects_non_object_line(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        path.write_text('["not", "an", "object"]\n', encoding="utf-8")
        with pytest.raises(ValueError, match="not an object"):
            load_text_records(path)

    def test_rejects_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        path.write_text("\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no records"):
            load_text_records(path)


class TestMlmCptConfig:
    def test_defaults(self) -> None:
        config = _config()
        assert config.seq_len == 1024
        assert config.mlm_probability == pytest.approx(0.30)
        assert config.batch_size == 8
        assert config.grad_accum == 1
        assert config.seed == 0
        assert config.device == "cuda"
        assert config.bf16 is True

    def test_epochs_alone_is_valid(self) -> None:
        config = MlmCptConfig.model_validate({**_PATHS, "epochs": 2})
        assert config.epochs == 2
        assert config.max_steps is None

    def test_rejects_both_max_steps_and_epochs(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of max_steps or epochs"):
            MlmCptConfig.model_validate({**_PATHS, "max_steps": 10, "epochs": 1})

    def test_rejects_neither_max_steps_nor_epochs(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of max_steps or epochs"):
            MlmCptConfig.model_validate(dict(_PATHS))

    def test_rejects_out_of_range_mlm_probability(self) -> None:
        for bad in (0.0, 1.0, -0.1):
            with pytest.raises(ValidationError):
                _config(mlm_probability=bad)

    def test_rejects_nonpositive_dimensions(self) -> None:
        for field in ("seq_len", "batch_size", "grad_accum", "lr", "max_steps"):
            with pytest.raises(ValidationError):
                _config(**{field: 0})

    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            _config(resize_embeddings=True)

    def test_is_frozen(self) -> None:
        config = _config()
        with pytest.raises(ValidationError):
            config.seq_len = 512
