"""Fast CPU-only tests for the Track T-S1/S2 trainer boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.train2.trainer import (
    TrainerConfig,
    TrainingRecord,
    compute_class_weights,
    load_records,
    orientation_for_record,
)

_PATHS = {
    "records_jsonl": Path("/tmp/records.jsonl"),
    "encoder_init_dir": Path("/tmp/encoder"),
    "out_dir": Path("/tmp/out"),
}


def _record(**overrides: object) -> TrainingRecord:
    return TrainingRecord.model_validate(
        {
            "record_id": "r0",
            "reference_headless": "theorem t (n : Nat) : n = n",
            "candidate_headless": "theorem u (m : Nat) : m = m",
            "label": True,
            "group_key": "g0",
            **overrides,
        }
    )


class TestTrainerConfig:
    def test_defaults_and_tokenizer_fallback(self) -> None:
        config = TrainerConfig.model_validate(_PATHS)
        assert config.tokenizer_dir == config.encoder_init_dir
        assert config.seq_len == 1024
        assert config.epochs == 2
        assert config.lr == pytest.approx(2e-5)
        assert config.swap_orientation == "augment"
        assert config.class_balance == "weighted"

    def test_explicit_tokenizer_and_s2_state_are_retained(self) -> None:
        config = TrainerConfig.model_validate(
            {
                **_PATHS,
                "tokenizer_dir": Path("/tmp/tokenizer"),
                "init_state_safetensors": Path("/tmp/s1.safetensors"),
                "holdout_families": ["negation"],
            }
        )
        assert config.tokenizer_dir == Path("/tmp/tokenizer")
        assert config.init_state_safetensors == Path("/tmp/s1.safetensors")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("seq_len", 0),
            ("epochs", 0),
            ("batch_size", 0),
            ("grad_accum", 0),
            ("lr", 0.0),
            ("warmup_ratio", 1.1),
            ("label_smoothing", 1.0),
        ],
    )
    def test_rejects_invalid_values(self, field: str, value: object) -> None:
        with pytest.raises(ValidationError):
            TrainerConfig.model_validate({**_PATHS, field: value})

    def test_rejects_extra_and_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            TrainerConfig.model_validate({**_PATHS, "resize_embeddings": True})
        config = TrainerConfig.model_validate(_PATHS)
        with pytest.raises(ValidationError):
            config.seq_len = 256


class TestRecordLoader:
    def test_loads_schema_and_ignores_extra_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "records.jsonl"
        row = {
            "record_id": "r1",
            "reference_headless": "theorem a : True",
            "candidate_headless": "theorem b : True",
            "label": True,
            "group_key": "g1",
            "family": None,
            "source": "synthetic",
            "weight": 2.5,
            "future_provenance": {"version": 2},
        }
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        assert load_records(path) == [TrainingRecord.model_validate(row)]

    @pytest.mark.parametrize(
        "update",
        [
            {"label": "yes"},
            {"record_id": " "},
            {"weight": 0.0},
            {"candidate_headless": None},
        ],
    )
    def test_rejects_bad_rows_with_line_context(
        self, tmp_path: Path, update: dict[str, object]
    ) -> None:
        path = tmp_path / "bad.jsonl"
        row = _record().model_dump(mode="json") | update
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"bad.jsonl:1: invalid training record"):
            load_records(path)

    def test_rejects_duplicate_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "duplicates.jsonl"
        raw = json.dumps(_record().model_dump(mode="json"))
        path.write_text(raw + "\n" + raw + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate record_id"):
            load_records(path)


def test_orientation_is_deterministic_per_record_epoch() -> None:
    records = [_record(record_id=f"r{index}") for index in range(32)]
    first = [orientation_for_record(row, seed=17, epoch=0) for row in records]
    assert first == [orientation_for_record(row, seed=17, epoch=0) for row in records]
    assert first != [orientation_for_record(row, seed=17, epoch=1) for row in records]
    assert {False, True}.issubset(first)


def test_inverse_frequency_class_weights() -> None:
    rows = [
        _record(record_id="p", label=True),
        _record(record_id="n0", label=False),
        _record(record_id="n1", label=False),
        _record(record_id="n2", label=False),
    ]
    weights = compute_class_weights(rows)
    assert weights[True] == pytest.approx(2.0)
    assert weights[False] == pytest.approx(2.0 / 3.0)
    assert sum(weights[row.label] for row in rows) / len(rows) == pytest.approx(1.0)


def test_class_weights_require_both_labels() -> None:
    with pytest.raises(ValueError, match="both labels"):
        compute_class_weights([_record()])
