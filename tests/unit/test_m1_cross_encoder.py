from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.models import m0_dual_encoder as m0
from leanfaith.models import m1_cross_encoder as m1
from leanfaith.models.tokenizer_audit import FilePin, SnapshotBinding
from leanfaith.schemas.manifest import CodeState


class _CharacterTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        values = [ord(character) % 128 for character in text]
        return [1, *values, 2] if add_special_tokens else values

    def __call__(
        self,
        texts: list[str],
        *,
        padding: bool,
        truncation: bool,
        max_length: int,
        return_tensors: str,
    ) -> dict[str, Any]:
        torch = pytest.importorskip("torch")
        assert padding and truncation and return_tensors == "pt"
        rows = [self.encode(text, add_special_tokens=True)[:max_length] for text in texts]
        width = max(map(len, rows))
        return {
            "input_ids": torch.tensor(
                [row + [0] * (width - len(row)) for row in rows], dtype=torch.long
            ),
            "attention_mask": torch.tensor(
                [[1] * len(row) + [0] * (width - len(row)) for row in rows],
                dtype=torch.long,
            ),
        }


def _m0_protocol() -> m0.ExperimentalM0ProxyProtocolConfig:
    return m0.load_experimental_m0_proxy_config(
        Path("configs/models/experimental_m0_dual_encoder_proxy_v1.yaml")
    ).config


def _m1_protocol() -> m1.ExperimentalM1ProxyProtocolConfig:
    return m1.load_experimental_m1_proxy_config(
        Path("configs/models/experimental_m1_cross_encoder_proxy_v1.yaml")
    ).config


def _example(
    index: int,
    *,
    split: m0.ProxySplit,
    target: m0.ProxyTarget,
    private: bool = False,
    suffix: str = "",
) -> m0.M0ProxyExample:
    source = f"[HEADLESS]\n(n : Nat) : n = n{suffix}"
    candidate = f"[HEADLESS]\n(m : Nat) : m = m{suffix}"
    return m0.M0ProxyExample(
        record_id=f"experimental_mixed_pair:{index:064x}",
        split_component_id=f"split-component:{index:064x}",
        split=split,
        pseudo_target=target,
        source_text=source,
        candidate_text=candidate,
        source_text_sha256=hashlib.sha256(source.encode()).hexdigest(),
        candidate_text_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
        source_token_count=len(source) + 2,
        candidate_token_count=len(candidate) + 2,
        selected_length=512,
        long_input=False,
        proxy_training_eligible=split == "train",
        private_source_content=private,
        redistribution_allowed=not private,
        external_transmission_allowed=not private,
        release_eligible=not private,
    )


def _examples() -> tuple[m0.M0ProxyExample, ...]:
    return (
        *(
            _example(
                index,
                split="train",
                target="same_claim" if index <= 32 else "not_same_claim",
                private=index == 1,
                suffix="PRIVATE_SENTINEL" if index == 1 else "",
            )
            for index in range(1, 65)
        ),
        _example(65, split="validation", target="same_claim"),
        _example(66, split="validation", target="not_same_claim"),
        _example(67, split="test", target="same_claim"),
        _example(68, split="test", target="not_same_claim"),
    )


def _clean_code() -> CodeState:
    return CodeState(
        git_revision="1" * 40,
        git_dirty=False,
        base_git_commit="1" * 40,
        code_tree_hash="2" * 64,
        tracked_diff_hash="3" * 64,
        untracked_files=(),
    )


def _snapshot() -> SnapshotBinding:
    files = {
        "config.json": FilePin(sha256="1" * 64, byte_count=1),
        "special_tokens_map.json": FilePin(sha256="2" * 64, byte_count=1),
        "tokenizer.json": FilePin(sha256="3" * 64, byte_count=1),
        "tokenizer_config.json": FilePin(sha256="4" * 64, byte_count=1),
    }
    return SnapshotBinding.model_construct(
        model_id="answerdotai/ModernBERT-base",
        revision="8949b909ec900327062f0ebf497f51aef5e6f0c8",
        path="/tmp/tokenizer",
        use_fast=True,
        native_max_length=8192,
        files=files,
        snapshot_content_hash=hash_canonical(
            {name: pin.model_dump(mode="json") for name, pin in sorted(files.items())}
        ),
    )


def _receipt(snapshot: SnapshotBinding) -> m0.M0OfficialCheckpointReceipt:
    protocol = _m0_protocol()
    required = {
        name: m0.M0CheckpointFile(sha256=pin.sha256, byte_count=pin.byte_count)
        for name, pin in snapshot.files.items()
    }
    required["model.safetensors"] = m0.M0CheckpointFile(
        sha256=protocol.backbone.weight_sha256,
        byte_count=protocol.backbone.weight_byte_count,
    )
    data: dict[str, Any] = {
        "schema_version": 1,
        "model_id": protocol.backbone.model_id,
        "revision": protocol.backbone.revision,
        "hf_snapshot_api_url": protocol.backbone.hf_snapshot_api_url,
        "receipt_basis": protocol.backbone.receipt_basis,
        "required_files": {
            name: pin.model_dump(mode="json") for name, pin in sorted(required.items())
        },
        "tokenizer_snapshot_content_hash": snapshot.snapshot_content_hash,
        "local_files_only": True,
        "trust_remote_code": False,
    }
    return m0.M0OfficialCheckpointReceipt.model_validate(
        {**data, "receipt_id": "m0-official-checkpoint:" + hash_canonical(data)}
    )


def test_m1_protocol_reuses_exact_m0_backbone_tokenizer_and_schedule() -> None:
    zero = _m0_protocol()
    one = _m1_protocol()

    assert one.backbone == zero.backbone
    assert one.training == zero.training
    assert one.tokenizer_audit_profile_id == zero.tokenizer_audit_profile_id
    assert one.architecture.encoder_calls_per_pair == 1
    assert one.semantic_prediction is False
    assert one.model_selection_eligible is False


def test_packing_is_tagged_ordered_and_changes_under_swap() -> None:
    item = _example(1, split="train", target="same_claim")
    packed = m1.pack_m1_pair(item)
    swapped = m1.pack_m1_pair(
        item.model_copy(
            update={
                "source_text": item.candidate_text,
                "candidate_text": item.source_text + " changed",
            }
        )
    )

    assert packed.startswith("[REFERENCE]\n[HEADLESS]\n")
    assert "\n[CANDIDATE]\n[HEADLESS]\n" in packed
    assert packed != swapped


def test_packed_tokenization_excludes_only_packed_long_inputs() -> None:
    examples = (
        _example(1, split="train", target="same_claim"),
        _example(2, split="train", target="not_same_claim", suffix="x" * 300),
        _example(3, split="validation", target="same_claim"),
    )
    profile, lengths = m1._packed_tokenization(_CharacterTokenizer(), examples, selected_length=128)
    eligible = m1._m1_schedule_examples(examples, packed_lengths=lengths, selected_length=128)

    assert profile.long_input_count == 1
    assert profile.eligible_training_count == 1
    assert [item.record_id for item in eligible] == [examples[0].record_id]


def test_real_m1_module_encodes_each_pair_once() -> None:
    torch = pytest.importorskip("torch")

    class TinyEncoder(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embedding = torch.nn.Embedding(128, 4)
            self.calls = 0

        def forward(self, *, input_ids: Any, attention_mask: Any) -> object:
            del attention_mask
            self.calls += 1
            return SimpleNamespace(last_hidden_state=self.embedding(input_ids))

    encoder = TinyEncoder()
    model = m1.build_m1_cross_encoder_module(encoder=encoder, hidden_size=4)
    result = model(
        input_ids=torch.tensor([[1, 2, 3], [4, 5, 0]]),
        attention_mask=torch.tensor([[1, 1, 1], [1, 1, 0]]),
    )

    assert model.encoder is encoder
    assert encoder.calls == 1
    assert result["logits"].shape == (2,)
    assert result["pooled_pair_embeddings"].shape == (2, 4)


def test_tiny_m1_training_is_executable_replayable_and_private_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    examples = _examples()
    protocol = _m1_protocol()
    m0_protocol = _m0_protocol()
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    checkpoint_dir = tmp_path / m0_protocol.backbone.revision
    checkpoint_dir.mkdir()
    checkpoint = m0.M0LocalCheckpointBinding(snapshot_path=str(checkpoint_dir), receipt=receipt)
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "manifest.json").write_text("fixture")
    input_manifest = SimpleNamespace(
        artifact_id=f"experimental-m0-proxy-inputs:{'5' * 64}",
        dataset_id=f"experimental_mixed_supervision:{'6' * 64}",
        protocol=m0_protocol,
        tokenizer_decision=SimpleNamespace(
            audit_id=f"tokenizer_audit:{'7' * 64}",
            candidate_key="modernbert_base",
            selected_length=512,
            snapshot_content_hash=snapshot.snapshot_content_hash,
        ),
    )
    tokenizer = _CharacterTokenizer()

    class TinyEncoder(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embedding = torch.nn.Embedding(128, 4)

        def forward(self, *, input_ids: Any, attention_mask: Any) -> object:
            del attention_mask
            return SimpleNamespace(last_hidden_state=self.embedding(input_ids))

    def runtime() -> m1.LoadedM1ProxyRuntime:
        torch.manual_seed(101)
        model = m1.build_m1_cross_encoder_module(encoder=TinyEncoder(), hidden_size=4)
        packed, _ = m1._packed_tokenization(tokenizer, examples, selected_length=512)
        return m1.LoadedM1ProxyRuntime(
            model=model,
            tokenizer=tokenizer,
            selected_length=512,
            checkpoint=checkpoint,
            audited_tokenizer_snapshot=snapshot,
            protocol=protocol,
            initial_model_state_sha256=hashlib.sha256(
                m0._safetensors_state_bytes(model, module_importer=importlib.import_module)
            ).hexdigest(),
            packed_tokenization=packed,
        )

    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(m1, "verify_experimental_m0_proxy_inputs", lambda _: input_manifest)
    monkeypatch.setattr(m1, "_load_prepared_examples", lambda _: examples)
    monkeypatch.setattr(m1, "_load_audited_tokenizer", lambda _: tokenizer)
    monkeypatch.setattr(m1, "_verify_audited_tokenizer_snapshot", lambda _: tmp_path)
    monkeypatch.setattr(m1, "verify_local_modernbert_checkpoint", lambda _: checkpoint_dir)
    monkeypatch.setattr(m1, "collect_code_state", lambda _: _clean_code())
    monkeypatch.setattr(m1, "load_m1_proxy_runtime", lambda **_: runtime())

    first_dir = tmp_path / "train-a"
    first = m1.train_m1_proxy_one_epoch(
        repository_root=repository,
        prepared_input_dir=prepared,
        output_dir=first_dir,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=snapshot,
        protocol=protocol,
        allow_experimental_mixed_supervision=True,
    )
    second_dir = tmp_path / "train-b"
    second = m1.train_m1_proxy_one_epoch(
        repository_root=repository,
        prepared_input_dir=prepared,
        output_dir=second_dir,
        checkpoint=checkpoint,
        audited_tokenizer_snapshot=snapshot,
        protocol=protocol,
        allow_experimental_mixed_supervision=True,
    )

    assert first.artifact_id == second.artifact_id
    assert first.optimizer_steps == 2
    assert first.examples_exposed == 64
    manifest = m1.verify_m1_proxy_training(first_dir)
    assert manifest.training_executed is True
    assert manifest.contains_private_source_content is True
    assert manifest.redistribution_allowed is False
    for name in m1._OUTPUT_FILES:
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()
        assert b"PRIVATE_SENTINEL" not in (first_dir / name).read_bytes()

    metrics = json.loads((first_dir / "metrics.json").read_text())
    assert metrics["diagnostics"]["validation"]["record_count"] == 2
    changed = {**metrics, "loss_normalization_weight": 999.0}
    (first_dir / "metrics.json").write_bytes(canonical_json_bytes(changed) + b"\n")
    with pytest.raises(m1.ExperimentalM1ProxyError, match="hash differs"):
        m1.verify_m1_proxy_training(first_dir)
    (first_dir / "metrics.json").write_bytes((second_dir / "metrics.json").read_bytes())
    (first_dir / "predictions.jsonl").write_bytes(b"tampered\n")
    with pytest.raises(m1.ExperimentalM1ProxyError, match="hash differs"):
        m1.verify_m1_proxy_training(first_dir)


def test_m1_cli_is_stable_and_training_fails_closed_without_opt_in(tmp_path: Path) -> None:
    runner = CliRunner()
    for command in ("train-m1-proxy", "verify-m1-training"):
        help_result = runner.invoke(app, [command, "--help"])
        assert help_result.exit_code == 0, help_result.output

    result = runner.invoke(
        app,
        [
            "train-m1-proxy",
            "--prepared-input-dir",
            str(tmp_path / "prepared"),
            "--tokenizer-audit-dir",
            str(tmp_path / "audit"),
            "--checkpoint-dir",
            str(tmp_path / "checkpoint"),
            "--output-dir",
            str(tmp_path / "output"),
        ],
    )
    assert result.exit_code == 1
    assert "M1 proxy training rejected" in result.output


def test_m1_config_is_canonical_yaml() -> None:
    loaded = m1.load_experimental_m1_proxy_config(
        Path("configs/models/experimental_m1_cross_encoder_proxy_v1.yaml")
    )
    assert loaded.config.profile_id == "experimental_m1_cross_encoder_proxy_v1"
    assert loaded.config_hash == hash_canonical(loaded.config.model_dump(mode="json"))
