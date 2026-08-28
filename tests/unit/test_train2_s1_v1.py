"""CPU-only tests for the durable Queue-5 two-arm S1 runner."""

from __future__ import annotations

import fcntl
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import leanfaith.train2.s1_v1 as s1
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.train2.trainer import TrainerConfig, TrainerResult

_FIXTURE_REVISION = "f" * 40


def _write(path: Path, value: str) -> s1.FrozenFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return s1.FrozenFile(path=path, sha256=hash_file(path))


def _fixture_inputs(tmp_path: Path) -> s1.S1V1Inputs:
    corpus = tmp_path / "corpus"
    train = _write(corpus / "records_train_v1.jsonl", "fixture train\n")
    validation = _write(corpus / "records_validation_v1.jsonl", "fixture validation\n")
    tokenizer_dir = tmp_path / "tokenizer"
    tokenizer_files = tuple(
        _write(tokenizer_dir / name, f"fixture {name}\n")
        for name in (
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
        )
    )
    arms: list[s1.EncoderArm] = []
    for name in ("cpt_chunks", "cpt_mixed"):
        encoder = tmp_path / name
        arms.append(
            s1.EncoderArm(
                name=name,
                encoder_dir=encoder,
                config=_write(encoder / "config.json", f"{name} config\n"),
                model=_write(encoder / "model.safetensors", f"{name} model\n"),
                run_manifest=_write(encoder / "run_manifest.json", f"{name} manifest\n"),
            )
        )
    corpus_manifest_path = corpus / "corpus_v1_manifest.json"
    corpus_manifest_path.write_bytes(
        canonical_json_bytes(
            {
                "status": "completed",
                "method_version": "corpus_v1_track_d_merge_v1",
                "counts": {"split": {"test": 2488, "train": 64, "validation": 16}},
                "outputs": {
                    "records_train_v1.jsonl": {
                        "path": str(train.path),
                        "sha256": train.sha256,
                    },
                    "records_validation_v1.jsonl": {
                        "path": str(validation.path),
                        "sha256": validation.sha256,
                    },
                },
            }
        )
    )
    return s1.S1V1Inputs(
        corpus_root=corpus,
        corpus_manifest=s1.FrozenFile(corpus_manifest_path, hash_file(corpus_manifest_path)),
        train_records=train,
        validation_records=validation,
        tokenizer_dir=tokenizer_dir,
        tokenizer_files=tokenizer_files,
        arms=tuple(arms),
        train_count=64,
        validation_count=16,
    )


def _gpu_idle(_index: int, _max_idle_memory_mib: int) -> dict[str, object]:
    return {"schema_version": 1, "idle": True, "checked_at": "fixture"}


def _fake_trainer(
    calls: list[TrainerConfig],
    *,
    fail_encoder: str | None = None,
) -> Callable[[TrainerConfig], TrainerResult]:
    def run(config: TrainerConfig) -> TrainerResult:
        calls.append(config)
        if config.encoder_init_dir.name == fail_encoder:
            raise RuntimeError(f"fixture failure for {fail_encoder}")
        config.out_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
        ):
            (config.out_dir / name).write_text(f"output {name}\n", encoding="utf-8")
        best = config.out_dir / "best.safetensors"
        last = config.out_dir / "last.safetensors"
        best.write_text(f"best {config.encoder_init_dir.name}\n", encoding="utf-8")
        last.write_text(f"last {config.encoder_init_dir.name}\n", encoding="utf-8")
        assert config.val_records_jsonl is not None
        optimizer_steps = math.ceil(64 / (config.batch_size * config.grad_accum)) * config.epochs
        manifest_path = config.out_dir / "run_manifest.json"
        manifest_path.write_bytes(
            canonical_json_bytes(
                {
                    "kind": "m1_sft_run",
                    "config": config.model_dump(mode="json"),
                    "git_revision": _FIXTURE_REVISION,
                    "input_sha256": {
                        "records_jsonl": hash_file(config.records_jsonl),
                        "val_records_jsonl": hash_file(config.val_records_jsonl),
                    },
                    "record_counts": {"train": 64, "validation": 16, "holdout": 0},
                    "optimizer_steps": optimizer_steps,
                    "warmup_steps": round(optimizer_steps * config.warmup_ratio),
                    "checkpoint_sha256": {
                        "best": hash_file(best),
                        "last": hash_file(last),
                    },
                }
            )
        )
        return TrainerResult(
            out_dir=config.out_dir,
            manifest_path=manifest_path,
            best_checkpoint=best,
            last_checkpoint=last,
            best_epoch=2,
            best_metric=0.9,
            initial_train_loss=1.0,
            final_train_loss=0.1,
            history=(),
        )

    return run


@pytest.fixture(autouse=True)
def _fixed_git_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s1, "_git_revision", lambda: _FIXTURE_REVISION)


def test_frozen_fixture_inputs_replay(tmp_path: Path) -> None:
    s1.verify_frozen_inputs(_fixture_inputs(tmp_path))


def test_runs_exact_two_arms_sequentially_and_records_eval_block(tmp_path: Path) -> None:
    inputs = _fixture_inputs(tmp_path)
    calls: list[TrainerConfig] = []
    output = tmp_path / "output"

    manifest = s1.run_s1_v1(
        output,
        inputs=inputs,
        gpu_lock_path=tmp_path / "gpu.lock",
        trainer=_fake_trainer(calls),
        gpu_probe=_gpu_idle,
        enforce_storage_root=False,
    )

    assert manifest["status"] == "completed"
    assert [config.encoder_init_dir.name for config in calls] == ["cpt_chunks", "cpt_mixed"]
    for config in calls:
        assert config.seq_len == 1024
        assert config.epochs == 2
        assert config.batch_size == 8
        assert config.grad_accum == 4
        assert config.lr == pytest.approx(2e-5)
        assert config.warmup_ratio == pytest.approx(0.1)
        assert config.seed == 20260828
        assert config.swap_orientation == "augment"
        assert config.class_balance == "weighted"
        assert config.tokenizer_dir == inputs.tokenizer_dir
    blocker = manifest["golden_dev_evaluation"]
    assert blocker["status"] == "blocked"
    assert blocker["reason_code"] == "literal_seal_missing_trusted_dev_only_text_artifact"
    assert blocker["mixed_canonical_file_opened"] is False
    assert blocker["evaluation_attempted"] is False


def test_refuses_unowned_nonempty_output_root(tmp_path: Path) -> None:
    inputs = _fixture_inputs(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    (output / "foreign.txt").write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(s1.S1V1Error, match="refusing overwrite") as raised:
        s1.run_s1_v1(
            output,
            inputs=inputs,
            gpu_lock_path=tmp_path / "gpu.lock",
            trainer=_fake_trainer([]),
            gpu_probe=_gpu_idle,
            enforce_storage_root=False,
        )
    assert raised.value.reason_code == "unsafe_nonempty_output"


def test_completed_queue_is_reverified_and_skipped_without_gpu_probe(tmp_path: Path) -> None:
    inputs = _fixture_inputs(tmp_path)
    output = tmp_path / "output"
    first_calls: list[TrainerConfig] = []
    s1.run_s1_v1(
        output,
        inputs=inputs,
        gpu_lock_path=tmp_path / "gpu.lock",
        trainer=_fake_trainer(first_calls),
        gpu_probe=_gpu_idle,
        enforce_storage_root=False,
    )

    def forbidden_probe(_index: int, _limit: int) -> dict[str, object]:
        raise AssertionError("completed queue must not inspect the GPU")

    second_calls: list[TrainerConfig] = []
    manifest = s1.run_s1_v1(
        output,
        inputs=inputs,
        gpu_lock_path=tmp_path / "gpu.lock",
        trainer=_fake_trainer(second_calls),
        gpu_probe=forbidden_probe,
        enforce_storage_root=False,
    )
    assert manifest["status"] == "completed"
    assert len(first_calls) == 2
    assert second_calls == []


def test_failed_second_arm_resumes_with_new_attempt_only(tmp_path: Path) -> None:
    inputs = _fixture_inputs(tmp_path)
    output = tmp_path / "output"
    failed_calls: list[TrainerConfig] = []
    with pytest.raises(RuntimeError, match="fixture failure"):
        s1.run_s1_v1(
            output,
            inputs=inputs,
            gpu_lock_path=tmp_path / "gpu.lock",
            trainer=_fake_trainer(failed_calls, fail_encoder="cpt_mixed"),
            gpu_probe=_gpu_idle,
            enforce_storage_root=False,
        )
    failure = json.loads((output / "failure_manifest.json").read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["arm"] == "cpt_mixed"
    assert failure["stage"] == "train_arm"

    resumed_calls: list[TrainerConfig] = []
    manifest = s1.run_s1_v1(
        output,
        inputs=inputs,
        gpu_lock_path=tmp_path / "gpu.lock",
        trainer=_fake_trainer(resumed_calls),
        gpu_probe=_gpu_idle,
        enforce_storage_root=False,
    )
    assert manifest["status"] == "completed"
    assert [config.encoder_init_dir.name for config in resumed_calls] == ["cpt_mixed"]
    assert resumed_calls[0].out_dir.name == "attempt_002"


def test_tampered_completed_checkpoint_fails_closed(tmp_path: Path) -> None:
    inputs = _fixture_inputs(tmp_path)
    output = tmp_path / "output"
    s1.run_s1_v1(
        output,
        inputs=inputs,
        gpu_lock_path=tmp_path / "gpu.lock",
        trainer=_fake_trainer([]),
        gpu_probe=_gpu_idle,
        enforce_storage_root=False,
    )
    (output / "attempts/cpt_chunks/attempt_001/best.safetensors").write_text(
        "tampered\n", encoding="utf-8"
    )

    with pytest.raises(s1.S1V1Error) as raised:
        s1.run_s1_v1(
            output,
            inputs=inputs,
            gpu_lock_path=tmp_path / "gpu.lock",
            trainer=_fake_trainer([]),
            gpu_probe=_gpu_idle,
            enforce_storage_root=False,
        )
    assert raised.value.reason_code == "checkpoint_hash_mismatch"


def test_gpu_lock_contention_fails_before_probe_or_trainer(tmp_path: Path) -> None:
    inputs = _fixture_inputs(tmp_path)
    gpu_lock = tmp_path / "gpu.lock"
    held = gpu_lock.open("a+b")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    calls: list[TrainerConfig] = []
    try:
        with pytest.raises(s1.S1V1Error) as raised:
            s1.run_s1_v1(
                tmp_path / "output",
                inputs=inputs,
                gpu_lock_path=gpu_lock,
                trainer=_fake_trainer(calls),
                gpu_probe=lambda _index, _limit: pytest.fail("GPU probe must not run"),
                enforce_storage_root=False,
            )
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    assert raised.value.reason_code == "gpu_lock_busy"
    assert calls == []


def test_nonidle_gpu_fails_before_trainer(tmp_path: Path) -> None:
    inputs = _fixture_inputs(tmp_path)
    calls: list[TrainerConfig] = []

    with pytest.raises(s1.S1V1Error) as raised:
        s1.run_s1_v1(
            tmp_path / "output",
            inputs=inputs,
            gpu_lock_path=tmp_path / "gpu.lock",
            trainer=_fake_trainer(calls),
            gpu_probe=lambda _index, _limit: {"idle": False},
            enforce_storage_root=False,
        )
    assert raised.value.reason_code == "gpu_not_idle"
    assert calls == []


def test_completed_verifier_survives_new_head_and_requires_seal_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _fixture_inputs(tmp_path)
    output = tmp_path / "output"
    s1.run_s1_v1(
        output,
        inputs=inputs,
        gpu_lock_path=tmp_path / "gpu.lock",
        trainer=_fake_trainer([]),
        gpu_probe=_gpu_idle,
        enforce_storage_root=False,
    )
    monkeypatch.setattr(s1, "_git_revision", lambda: "e" * 40)
    assert (
        s1.verify_s1_v1(output, inputs=inputs, enforce_storage_root=False)["status"] == "completed"
    )

    manifest_path = output / "run_manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["golden_dev_evaluation"] = {"status": "completed"}
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(s1.S1V1Error) as raised:
        s1.verify_s1_v1(output, inputs=inputs, enforce_storage_root=False)
    assert raised.value.reason_code == "evaluation_block_manifest_mismatch"
